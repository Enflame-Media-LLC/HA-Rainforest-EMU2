"""Tests for Rainforest polling and config-entry lifecycle."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import (
    ConfigEntryError,
    ConfigEntryNotReady,
    ConfigEntryState,
)
from homeassistant.const import CONF_DEVICE
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rainforest_emu2.communication import (
    FailureReason,
    FailureStage,
    MeterRecord,
    MeterSnapshot,
    RainforestCommunicationError,
    RavenSnapshot,
    ValidationResult,
)
from custom_components.rainforest_emu2.const import (
    CONF_METERS,
    CONF_USB_PID,
    CONF_USB_SERIAL,
    CONF_USB_VID,
    DOMAIN,
    PLATFORMS,
    UPDATE_INTERVAL,
)

METER_MAC = bytes.fromhex("0013500102030405")
METER_HEX = METER_MAC.hex()
DEVICE_MAC = "0013500000000001"
VALIDATION = ValidationResult(
    path="/dev/ttyACM0",
    device_mac=DEVICE_MAC,
    manufacturer="Rainforest",
    model="EMU-2",
    firmware="1.2.3",
    meters=(MeterRecord(METER_MAC, METER_HEX, "Main", "electric"),),
)
SNAPSHOT = RavenSnapshot(
    meters={
        METER_HEX: MeterSnapshot(
            demand=42.0,
            delivered=1.0,
            received=0.0,
            price=None,
            currency=None,
        )
    },
    signal_strength=80,
    present_fields=frozenset({f"meter:{METER_HEX}:demand"}),
)


class RuntimeClient:
    """A small client double at the integration's communication boundary."""

    def __init__(
        self,
        *,
        validation: ValidationResult = VALIDATION,
        refresh: object = SNAPSHOT,
        validation_error: BaseException | None = None,
        shutdown_error: BaseException | None = None,
    ) -> None:
        self.validation = validation
        self.refresh = refresh
        self.validation_error = validation_error
        self.shutdown_error = shutdown_error
        self.validate_calls = 0
        self.refresh_calls: list[tuple[bytes, ...]] = []
        self.shutdown_calls = 0

    async def async_validate(self) -> ValidationResult:
        self.validate_calls += 1
        if self.validation_error is not None:
            raise self.validation_error
        return self.validation

    async def async_refresh(self, meter_macs: tuple[bytes, ...]) -> RavenSnapshot:
        self.refresh_calls.append(meter_macs)
        if isinstance(self.refresh, BaseException):
            raise self.refresh
        assert isinstance(self.refresh, RavenSnapshot)
        return self.refresh

    async def async_shutdown(self) -> None:
        self.shutdown_calls += 1
        if self.shutdown_error is not None:
            raise self.shutdown_error


def _entry(**data_overrides: object) -> MockConfigEntry:
    """Create one persisted entry with valid, explicitly selected metadata."""

    data = {
        CONF_DEVICE: "/dev/ttyACM0",
        CONF_METERS: [METER_HEX],
        CONF_USB_SERIAL: "USB-SERIAL",
        CONF_USB_VID: 0x04B4,
        CONF_USB_PID: 0x0003,
    }
    data.update(data_overrides)
    return MockConfigEntry(domain=DOMAIN, data=data, unique_id=DEVICE_MAC)


async def test_coordinator_refreshes_only_selected_meter_bytes(hass) -> None:
    """Changing selected-meter decoding would poll the wrong meter."""
    from custom_components.rainforest_emu2.coordinator import RainforestCoordinator

    client = RuntimeClient()
    coordinator = RainforestCoordinator(hass, client, (METER_MAC,))

    await coordinator.async_refresh()

    assert coordinator.data is SNAPSHOT
    assert coordinator.last_update_success
    assert client.refresh_calls == [(METER_MAC,)]
    assert coordinator.update_interval == UPDATE_INTERVAL


async def test_coordinator_does_not_publish_stale_data_after_refresh_failure(
    hass,
) -> None:
    """Removing UpdateFailed translation would leave stale data current."""
    from custom_components.rainforest_emu2.coordinator import RainforestCoordinator

    client = RuntimeClient(
        refresh=RainforestCommunicationError(
            FailureStage.POLL, FailureReason.TIMEOUT, retryable=True
        )
    )
    coordinator = RainforestCoordinator(hass, client, (METER_MAC,))
    coordinator.data = SNAPSHOT

    with pytest.raises(UpdateFailed, match="poll:timeout"):
        await coordinator._async_update_data()

    assert coordinator.data is SNAPSHOT


@pytest.mark.parametrize(
    "error",
    [
        RainforestCommunicationError(
            FailureStage.OPEN, FailureReason.MISSING, retryable=True
        ),
        RainforestCommunicationError(
            FailureStage.LOCK, FailureReason.BUSY, retryable=True
        ),
    ],
)
async def test_setup_maps_retryable_validation_failure_to_not_ready(
    hass, monkeypatch, error
) -> None:
    """Changing retryable setup failures to permanent errors breaks HA backoff."""
    import custom_components.rainforest_emu2 as integration

    entry = _entry()
    entry.add_to_hass(hass)
    entry._async_set_state(hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
    client = RuntimeClient(validation_error=error)
    monkeypatch.setattr(integration, "RavenClient", lambda path: client)

    with pytest.raises(ConfigEntryNotReady) as raised:
        await integration.async_setup_entry(hass, entry)

    assert (
        str(raised.value) == "open:missing"
        if error.stage is FailureStage.OPEN
        else "lock:busy"
    )
    assert client.shutdown_calls == 1
    assert entry.runtime_data is None


async def test_setup_maps_identity_mismatch_to_reconfigure_error(
    hass, monkeypatch
) -> None:
    """Accepting a different hardware MAC would attach an entry to another device."""
    import custom_components.rainforest_emu2 as integration

    entry = _entry()
    entry.add_to_hass(hass)
    entry._async_set_state(hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
    client = RuntimeClient(
        validation=ValidationResult(
            path=VALIDATION.path,
            device_mac="0013500000000002",
            manufacturer=VALIDATION.manufacturer,
            model=VALIDATION.model,
            firmware=VALIDATION.firmware,
            meters=VALIDATION.meters,
        )
    )
    monkeypatch.setattr(integration, "RavenClient", lambda path: client)

    with pytest.raises(ConfigEntryError, match="reconfigure"):
        await integration.async_setup_entry(hass, entry)

    assert client.shutdown_calls == 1
    assert entry.runtime_data is None


async def test_setup_recovers_only_one_exact_usb_serial_match_before_client_creation(
    hass, monkeypatch
) -> None:
    """Matching on VID/PID alone would silently attach the wrong replugged device."""
    import custom_components.rainforest_emu2 as integration

    entry = _entry(**{CONF_DEVICE: "/dev/ttyACM9"})
    entry.add_to_hass(hass)
    entry._async_set_state(hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
    client = RuntimeClient()
    monkeypatch.setattr(integration.os.path, "exists", lambda path: False)
    monkeypatch.setattr(
        integration,
        "async_scan_supported_ports",
        AsyncMock(
            return_value=(
                SimpleNamespace(
                    path="/dev/ttyACM0",
                    vid=0x04B4,
                    pid=0x0003,
                    serial_number="USB-SERIAL",
                ),
            )
        ),
    )
    constructed_paths: list[str] = []

    def make_client(path: str) -> RuntimeClient:
        constructed_paths.append(path)
        return client

    monkeypatch.setattr(integration, "RavenClient", make_client)
    monkeypatch.setattr(
        integration.RainforestCoordinator,
        "async_config_entry_first_refresh",
        AsyncMock(),
    )
    forward = AsyncMock()
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)

    assert await integration.async_setup_entry(hass, entry)

    assert constructed_paths == ["/dev/ttyACM0"]
    assert entry.data[CONF_DEVICE] == "/dev/ttyACM0"
    forward.assert_awaited_once_with(entry, PLATFORMS)


@pytest.mark.parametrize(
    "candidates",
    [
        (
            SimpleNamespace(
                path="/dev/ttyACM0",
                vid=0x04B4,
                pid=0x0003,
                serial_number=None,
            ),
        ),
        (
            SimpleNamespace(
                path="/dev/ttyACM0",
                vid=0x04B4,
                pid=0x0003,
                serial_number="USB-SERIAL",
            ),
            SimpleNamespace(
                path="/dev/ttyACM1",
                vid=0x04B4,
                pid=0x0003,
                serial_number="USB-SERIAL",
            ),
        ),
    ],
)
async def test_setup_never_guesses_a_serialless_or_ambiguous_path(
    hass, monkeypatch, candidates
) -> None:
    """Guessing a replacement path can bind an entry to another physical unit."""
    import custom_components.rainforest_emu2 as integration

    entry = _entry(**{CONF_DEVICE: "/dev/ttyACM9"})
    entry.add_to_hass(hass)
    entry._async_set_state(hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
    client = RuntimeClient(
        validation_error=RainforestCommunicationError(
            FailureStage.OPEN, FailureReason.MISSING, retryable=True
        )
    )
    monkeypatch.setattr(integration.os.path, "exists", lambda path: False)
    monkeypatch.setattr(
        integration, "async_scan_supported_ports", AsyncMock(return_value=candidates)
    )
    monkeypatch.setattr(integration, "RavenClient", lambda path: client)

    with pytest.raises(ConfigEntryNotReady):
        await integration.async_setup_entry(hass, entry)

    assert entry.data[CONF_DEVICE] == "/dev/ttyACM9"
    assert client.shutdown_calls == 1


async def test_setup_cleans_up_when_platform_forwarding_fails(
    hass, monkeypatch
) -> None:
    """Removing setup cleanup leaks a client after a platform setup failure."""
    import custom_components.rainforest_emu2 as integration

    entry = _entry()
    entry.add_to_hass(hass)
    entry._async_set_state(hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
    client = RuntimeClient()
    monkeypatch.setattr(integration, "RavenClient", lambda path: client)
    monkeypatch.setattr(
        integration.RainforestCoordinator,
        "async_config_entry_first_refresh",
        AsyncMock(),
    )
    monkeypatch.setattr(
        hass.config_entries,
        "async_forward_entry_setups",
        AsyncMock(side_effect=RuntimeError("platform failure")),
    )

    with pytest.raises(RuntimeError, match="platform failure"):
        await integration.async_setup_entry(hass, entry)

    assert client.shutdown_calls == 1
    assert entry.runtime_data is None


async def test_unload_unloads_platforms_before_shutting_down_owning_client(
    hass, monkeypatch
) -> None:
    """Closing before platform unload risks entities reading a dead runtime."""
    import custom_components.rainforest_emu2 as integration
    from custom_components.rainforest_emu2.coordinator import (
        RainforestCoordinator,
        RainforestRuntimeData,
    )

    entry = _entry()
    entry.add_to_hass(hass)
    entry._async_set_state(hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
    client = RuntimeClient()
    coordinator = RainforestCoordinator(hass, client, (METER_MAC,))
    entry.runtime_data = RainforestRuntimeData(client, coordinator, VALIDATION)
    events: list[str] = []

    async def unload_platforms(*_args: object) -> bool:
        events.append("platforms")
        return True

    async def shutdown() -> None:
        events.append("shutdown")
        await RuntimeClient.async_shutdown(client)

    client.async_shutdown = shutdown  # type: ignore[method-assign]
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", unload_platforms)

    assert await integration.async_unload_entry(hass, entry)

    assert events == ["platforms", "shutdown"]
    assert entry.runtime_data is None


async def test_setup_cancellation_waits_for_client_cleanup(hass, monkeypatch) -> None:
    """Letting cancellation skip cleanup leaks the serial lease."""
    import custom_components.rainforest_emu2 as integration

    entry = _entry()
    entry.add_to_hass(hass)
    entry._async_set_state(hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
    cleanup_complete = asyncio.Event()

    class CancellingClient(RuntimeClient):
        async def async_validate(self) -> ValidationResult:
            raise asyncio.CancelledError

        async def async_shutdown(self) -> None:
            self.shutdown_calls += 1
            await asyncio.sleep(0)
            cleanup_complete.set()

    client = CancellingClient()
    monkeypatch.setattr(integration, "RavenClient", lambda path: client)

    with pytest.raises(asyncio.CancelledError):
        await integration.async_setup_entry(hass, entry)

    assert cleanup_complete.is_set()
    assert client.shutdown_calls == 1
    assert entry.runtime_data is None


@pytest.mark.parametrize("phase", ["validate", "refresh", "runtime"])
@pytest.mark.parametrize("retryable", [True, False])
async def test_ha_exception_tracebacks_hide_dependency_secrets(
    hass, monkeypatch, phase, retryable
):
    """HA debug tracebacks must never render the dependency cause."""
    import traceback

    import custom_components.rainforest_emu2 as integration
    from custom_components.rainforest_emu2.coordinator import RainforestCoordinator

    error = RainforestCommunicationError(
        FailureStage.POLL, FailureReason.TIMEOUT, retryable=retryable
    )
    error.__cause__ = OSError("SECRET-USB-PATH")
    client = RuntimeClient(
        validation_error=error if phase == "validate" else None, refresh=error
    )
    if phase == "runtime":
        coordinator = RainforestCoordinator(hass, client, (METER_MAC,))
        await coordinator.async_refresh()
        assert not coordinator.last_update_success
        caught = coordinator.last_exception
    else:
        entry = _entry()
        entry.add_to_hass(hass)
        entry._async_set_state(hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
        monkeypatch.setattr(integration, "RavenClient", lambda path: client)
        expected = ConfigEntryNotReady if retryable else ConfigEntryError
        with pytest.raises(expected) as raised:
            await integration.async_setup_entry(hass, entry)
        caught = raised.value
        if not retryable:
            assert not isinstance(caught, ConfigEntryNotReady)
    assert "SECRET-USB-PATH" not in "".join(traceback.format_exception(caught))


async def test_stop_closes_client_and_unload_removes_stop_listener(hass, monkeypatch):
    """A stopped or unloaded entry must not retain an open client callback."""
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

    import custom_components.rainforest_emu2 as integration

    clients = [RuntimeClient(), RuntimeClient()]
    monkeypatch.setattr(integration, "RavenClient", lambda path: clients.pop(0))
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())
    monkeypatch.setattr(
        hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
    )
    entry = _entry()
    entry.add_to_hass(hass)
    entry._async_set_state(hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
    assert await integration.async_setup_entry(hass, entry)
    old_client = entry.runtime_data.client
    assert await integration.async_unload_entry(hass, entry)
    await entry._async_process_on_unload(hass)
    entry._async_set_state(hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
    assert await integration.async_setup_entry(hass, entry)
    new_client = entry.runtime_data.client
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert old_client.shutdown_calls == 1
    assert new_client.shutdown_calls == 1


@pytest.mark.parametrize("meters", [["00135001020304  "], ["                "]])
async def test_setup_rejects_non_hex_meter_identity(hass, monkeypatch, meters):
    import custom_components.rainforest_emu2 as integration

    entry = _entry(**{CONF_METERS: meters})
    entry.add_to_hass(hass)
    entry._async_set_state(hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
    monkeypatch.setattr(
        integration,
        "RavenClient",
        lambda path: pytest.fail("invalid meter opened client"),
    )
    with pytest.raises(ConfigEntryError):
        await integration.async_setup_entry(hass, entry)


async def test_setup_rejects_two_invalid_hardware_identities(hass, monkeypatch):
    from dataclasses import replace

    import custom_components.rainforest_emu2 as integration

    entry = _entry()
    entry.add_to_hass(hass)
    entry._async_set_state(hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
    hass.config_entries.async_update_entry(entry, unique_id="invalid")
    client = RuntimeClient(validation=replace(VALIDATION, device_mac="invalid"))
    monkeypatch.setattr(integration, "RavenClient", lambda path: client)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())
    with pytest.raises(ConfigEntryError):
        await integration.async_setup_entry(hass, entry)


@pytest.mark.parametrize("shutdown_fails", [False, True])
async def test_unload_repeated_cancellation_finishes_cleanup(
    hass, monkeypatch, shutdown_fails
):
    """Cancellation must wait for cleanup and remain cancellation on close failure."""
    import custom_components.rainforest_emu2 as integration
    from custom_components.rainforest_emu2.coordinator import (
        RainforestCoordinator,
        RainforestRuntimeData,
    )

    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    class DelayedClient(RuntimeClient):
        async def async_shutdown(self):
            started.set()
            await release.wait()
            finished.set()
            if shutdown_fails:
                raise OSError("SECRET-CLOSE-PATH")

    client = DelayedClient()
    entry = _entry()
    entry.add_to_hass(hass)
    entry.runtime_data = RainforestRuntimeData(
        client, RainforestCoordinator(hass, client, (METER_MAC,)), VALIDATION
    )
    monkeypatch.setattr(
        hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
    )
    task = asyncio.create_task(integration.async_unload_entry(hass, entry))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
