"""Tests for the Rainforest EMU-2 setup flow."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import (
    SOURCE_USB,
    ConfigEntryDisabler,
    ConfigEntryState,
)
from homeassistant.const import CONF_DEVICE, CONF_MAC
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service_info.usb import UsbServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry
from voluptuous_serialize import convert

from custom_components.rainforest_emu2.communication import (
    FailureReason,
    FailureStage,
    MeterRecord,
    RainforestCommunicationError,
    ValidationResult,
)
from custom_components.rainforest_emu2.const import CONF_METERS, DOMAIN

METER_MAC = "0013500102030405"
VALIDATION = ValidationResult(
    path="/dev/ttyACM0",
    device_mac="0013500000000001",
    manufacturer="Rainforest",
    model="EMU-2",
    firmware="1.2.3",
    meters=(MeterRecord(bytes.fromhex(METER_MAC), METER_MAC, "Main", "electric"),),
)


class ReconfigureClient:
    """Small runtime client double that exposes the flow's transport boundary."""

    def __init__(self, result: ValidationResult) -> None:
        self.result = result
        self.paths: list[str] = []
        self.connected_path: str | None = None

    async def async_reconfigure_path(self, path: str) -> ValidationResult:
        """Record the candidate path and return a controlled validation result."""

        self.paths.append(path)
        self.connected_path = path
        return self.result

    async def async_restore_path(self, path: str) -> None:
        """Record recovery of the prior persistent transport."""

        self.paths.append(path)
        self.connected_path = path


class Runtime:
    """Task 7-compatible runtime shape for reconfiguration tests."""

    def __init__(self, client: ReconfigureClient) -> None:
        self.client = client


async def _start_user_flow(hass, monkeypatch, validate: AsyncMock):
    """Start a manually selected flow with a deterministic supported port."""
    from custom_components.rainforest_emu2 import config_flow

    def ha_scan():
        return (
            UsbServiceInfo(
                device="/dev/ttyACM0",
                vid="04B4",
                pid="0003",
                serial_number="USB-SERIAL",
                manufacturer="Rainforest",
                description="EMU-2",
            ),
        )

    monkeypatch.setattr(
        config_flow,
        "RavenClient",
        lambda path: type("Client", (), {"async_validate": validate})(),
    )
    monkeypatch.setattr(config_flow.usb, "scan_serial_ports", ha_scan)
    hass.config.components.add("usb")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    device_options = next(
        value
        for key, value in result["data_schema"].schema.items()
        if key.schema == CONF_DEVICE
    )
    token = next(iter(device_options.container))
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"action": "select", CONF_DEVICE: token}
    )
    assert result["step_id"] == "confirm"
    return result


async def _start_selected_manual_flow(hass) -> dict[str, object]:
    """Start a user flow and select the first scanned serial candidate."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    device_options = next(
        value
        for key, value in result["data_schema"].schema.items()
        if key.schema == CONF_DEVICE
    )
    token = next(iter(device_options.container))
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"action": "select", CONF_DEVICE: token}
    )


async def _start_advanced_flow(hass, path: str) -> dict[str, object]:
    """Start an advanced flow and submit a local serial path."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"action": "advanced"}
    )
    assert result["step_id"] == "advanced"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"path": path}
    )


async def _start_builtin_import(hass, entry) -> dict[str, object]:
    """Advance a user flow through built-in entry selection without validation."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"action": "import_builtin"}
    )
    assert result["step_id"] == "import_builtin"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entry_id": entry.entry_id}
    )


async def test_serial_less_usb_discovery_uses_no_unique_id_handler(
    hass, monkeypatch
) -> None:
    """USB discovery without a genuine serial uses HA's no-ID flow policy."""
    from custom_components.rainforest_emu2 import config_flow

    called = False
    original = (
        config_flow.RainforestEmu2ConfigFlow._async_handle_discovery_without_unique_id
    )

    async def handle_without_unique_id(self) -> None:
        nonlocal called
        called = True
        await original(self)

    monkeypatch.setattr(
        config_flow.RainforestEmu2ConfigFlow,
        "_async_handle_discovery_without_unique_id",
        handle_without_unique_id,
    )
    hass.config.components.add("usb")

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USB},
        data=UsbServiceInfo(
            device="/dev/ttyACM0",
            vid="04B4",
            pid="0003",
            serial_number=None,
            manufacturer="Rainforest",
            description="EMU-2",
        ),
    )

    assert result["step_id"] == "confirm"
    assert called


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("usb_without_serial", "manual_with_serial"),
        ("manual_with_serial", "usb_without_serial"),
        ("advanced_without_serial", "usb_with_serial"),
        ("usb_with_serial", "advanced_without_serial"),
    ],
)
async def test_mixed_usb_metadata_deduplicates_by_canonical_path_before_open(
    hass, monkeypatch, first, second
) -> None:
    """A serial appearing or disappearing cannot create a second port flow."""
    from custom_components.rainforest_emu2 import config_flow

    canonical_path = "/dev/ttyACM0"
    alias_path = "/dev/serial/by-id/rainforest"
    with_serial = UsbServiceInfo(
        device=canonical_path,
        vid="04B4",
        pid="0003",
        serial_number="USB-SERIAL",
        manufacturer="Rainforest",
        description="EMU-2",
    )
    without_serial = UsbServiceInfo(
        device=alias_path,
        vid="04B4",
        pid="0003",
        serial_number=None,
        manufacturer="Rainforest",
        description="EMU-2",
    )
    monkeypatch.setattr(config_flow.usb, "scan_serial_ports", lambda: (with_serial,))
    monkeypatch.setattr(
        config_flow.os.path,
        "realpath",
        lambda path: canonical_path if path in {canonical_path, alias_path} else path,
    )
    validate = AsyncMock()
    monkeypatch.setattr(
        config_flow,
        "RavenClient",
        lambda path: type("Client", (), {"async_validate": validate})(),
    )
    hass.config.components.add("usb")

    async def start(kind: str) -> dict[str, object]:
        if kind == "usb_without_serial":
            return await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_USB}, data=without_serial
            )
        if kind == "usb_with_serial":
            return await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_USB}, data=with_serial
            )
        if kind == "manual_with_serial":
            return await _start_selected_manual_flow(hass)
        return await _start_advanced_flow(hass, alias_path)

    initial = await start(first)
    assert initial["step_id"] == "confirm"

    duplicate = await start(second)

    assert duplicate["type"] is FlowResultType.ABORT
    assert duplicate["reason"] == "already_in_progress"
    validate.assert_not_awaited()


async def test_usb_discovery_waits_for_confirmation_before_validation(
    hass, monkeypatch
) -> None:
    """USB discovery must never open a serial port until the user confirms it."""
    from custom_components.rainforest_emu2 import config_flow

    validate = AsyncMock()
    monkeypatch.setattr(
        config_flow,
        "RavenClient",
        lambda path: type("Client", (), {"async_validate": validate})(),
    )
    hass.config.components.add("usb")

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USB},
        data=UsbServiceInfo(
            device="/dev/ttyACM0",
            vid="04B4",
            pid="0003",
            serial_number="USB-SERIAL",
            manufacturer="Rainforest",
            description="EMU-2",
        ),
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    validate.assert_not_awaited()


async def test_usb_rediscovery_aborts_against_persisted_usb_serial_before_open(
    hass, monkeypatch
) -> None:
    """A rediscovered serial never opens when an existing entry owns it."""
    from custom_components.rainforest_emu2 import config_flow

    validate = AsyncMock()
    monkeypatch.setattr(
        config_flow,
        "RavenClient",
        lambda path: type("Client", (), {"async_validate": validate})(),
    )
    MockConfigEntry(
        domain=DOMAIN,
        unique_id="0013500000000001",
        data={
            CONF_DEVICE: "/dev/ttyACM0",
            CONF_METERS: [METER_MAC],
            "usb_serial": "USB-SERIAL",
            "usb_vid": 0x04B4,
            "usb_pid": 0x0003,
        },
    ).add_to_hass(hass)
    hass.config.components.add("usb")

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USB},
        data=UsbServiceInfo(
            device="/dev/ttyACM9",
            vid="04B4",
            pid="0003",
            serial_number="USB-SERIAL",
            manufacturer="Rainforest",
            description="EMU-2",
        ),
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    validate.assert_not_awaited()


async def test_cross_source_same_serial_aborts_second_flow_before_open(
    hass, monkeypatch
) -> None:
    """Manual and USB setup flows share a provisional serial identity."""
    from custom_components.rainforest_emu2 import config_flow

    def ha_scan():
        return (
            UsbServiceInfo(
                device="/dev/ttyACM0",
                vid="04B4",
                pid="0003",
                serial_number="USB-SERIAL",
                manufacturer="Rainforest",
                description="EMU-2",
            ),
        )

    validate = AsyncMock()
    monkeypatch.setattr(
        config_flow,
        "RavenClient",
        lambda path: type("Client", (), {"async_validate": validate})(),
    )
    monkeypatch.setattr(config_flow.usb, "scan_serial_ports", ha_scan)
    hass.config.components.add("usb")

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USB},
        data=ha_scan()[0],
    )
    assert result["step_id"] == "confirm"

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    device_options = next(
        value
        for key, value in result["data_schema"].schema.items()
        if key.schema == CONF_DEVICE
    )
    token = next(iter(device_options.container))
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"action": "select", CONF_DEVICE: token}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_in_progress"
    validate.assert_not_awaited()


async def test_cross_source_canonical_path_aborts_when_usb_serial_is_missing(
    hass, monkeypatch
) -> None:
    """Serial-less discovery deduplicates manual setup using canonical path identity."""
    from custom_components.rainforest_emu2 import config_flow

    def ha_scan():
        return (
            UsbServiceInfo(
                device="/dev/ttyACM0",
                vid="04B4",
                pid="0003",
                serial_number=None,
                manufacturer="Rainforest",
                description="EMU-2",
            ),
        )

    validate = AsyncMock()
    monkeypatch.setattr(
        config_flow,
        "RavenClient",
        lambda path: type("Client", (), {"async_validate": validate})(),
    )
    monkeypatch.setattr(config_flow.usb, "scan_serial_ports", ha_scan)
    hass.config.components.add("usb")

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USB},
        data=ha_scan()[0],
    )
    assert result["step_id"] == "confirm"

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    device_options = next(
        value
        for key, value in result["data_schema"].schema.items()
        if key.schema == CONF_DEVICE
    )
    token = next(iter(device_options.container))
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"action": "select", CONF_DEVICE: token}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_in_progress"
    validate.assert_not_awaited()


async def test_scan_prefers_home_assistant_results_and_deduplicates_paths(
    hass, monkeypatch
) -> None:
    """A scan keeps supported USB devices once per canonical serial path."""
    from custom_components.rainforest_emu2 import config_flow

    def ha_scan():
        return (
            UsbServiceInfo(
                device="/dev/serial/by-id/rainforest",
                vid="04b4",
                pid="0003",
                serial_number=None,
                manufacturer="Rainforest",
                description="EMU-2",
            ),
            UsbServiceInfo(
                device="/dev/ttyACM0",
                vid="0x04B4",
                pid="0x0003",
                serial_number=None,
                manufacturer="Rainforest",
                description="EMU-2",
            ),
            UsbServiceInfo(
                device="/dev/ttyUSB0",
                vid="1234",
                pid="5678",
                serial_number=None,
                manufacturer=None,
                description=None,
            ),
        )

    executor = AsyncMock(side_effect=lambda function: function())
    monkeypatch.setattr(config_flow.usb, "scan_serial_ports", ha_scan)
    monkeypatch.setattr(hass, "async_add_executor_job", executor)
    monkeypatch.setattr(config_flow.os.path, "realpath", lambda _path: "/dev/ttyACM0")

    candidates = await config_flow.async_scan_supported_ports(hass)

    assert len(candidates) == 1
    assert candidates[0].path == "/dev/serial/by-id/rainforest"
    assert candidates[0].token != candidates[0].label
    executor.assert_awaited_once_with(config_flow.usb.scan_serial_ports)


async def test_scan_assigns_distinct_opaque_tokens_to_colliding_labels(
    hass, monkeypatch
) -> None:
    """Identical human labels cannot select the wrong detected serial port."""
    from custom_components.rainforest_emu2 import config_flow

    def ha_scan():
        return (
            UsbServiceInfo(
                device="/dev/ttyACM0",
                vid="04B4",
                pid="0003",
                serial_number="first",
                manufacturer="Rainforest",
                description="EMU-2",
            ),
            UsbServiceInfo(
                device="/dev/ttyACM1",
                vid="04B4",
                pid="0003",
                serial_number="second",
                manufacturer="Rainforest",
                description="EMU-2",
            ),
        )

    monkeypatch.setattr(config_flow.usb, "scan_serial_ports", ha_scan)

    candidates = await config_flow.async_scan_supported_ports(hass)

    assert [candidate.label for candidate in candidates] == ["EMU-2", "EMU-2"]
    assert len({candidate.token for candidate in candidates}) == 2


async def test_duplicate_hardware_aborts_when_final_mac_is_known(
    hass, monkeypatch
) -> None:
    """A new path cannot create another entry for validated hardware."""
    from custom_components.rainforest_emu2 import config_flow

    def ha_scan():
        return (
            UsbServiceInfo(
                device="/dev/ttyACM0",
                vid="04B4",
                pid="0003",
                serial_number="new-usb-serial",
                manufacturer="Rainforest",
                description="EMU-2",
            ),
        )

    MockConfigEntry(
        domain=DOMAIN,
        unique_id="0013500000000001",
        data={
            CONF_DEVICE: "/dev/ttyACM9",
            CONF_METERS: [METER_MAC],
            "usb_serial": "old-usb-serial",
            "usb_vid": 0x04B4,
            "usb_pid": 0x0003,
        },
    ).add_to_hass(hass)
    monkeypatch.setattr(config_flow.usb, "scan_serial_ports", ha_scan)
    monkeypatch.setattr(
        config_flow,
        "RavenClient",
        lambda path: type(
            "Client", (), {"async_validate": AsyncMock(return_value=VALIDATION)}
        )(),
    )
    hass.config.components.add("usb")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    device_options = next(
        value
        for key, value in result["data_schema"].schema.items()
        if key.schema == CONF_DEVICE
    )
    token = next(iter(device_options.container))
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"action": "select", CONF_DEVICE: token}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "meters"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_METERS: [METER_MAC]}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.parametrize("scanner_fails", (False, True))
async def test_scan_uses_pyserial_only_after_empty_or_failed_ha_scan(
    hass, monkeypatch, scanner_fails
) -> None:
    """The compatibility scan runs only after HA cannot find a supported port."""
    from custom_components.rainforest_emu2 import config_flow

    def ha_scan():
        if scanner_fails:
            raise RuntimeError("scanner unavailable")
        return (
            UsbServiceInfo(
                device="/dev/ttyUSB9",
                vid="1234",
                pid="5678",
                serial_number=None,
                manufacturer=None,
                description=None,
            ),
        )

    fallback_port = UsbServiceInfo(
        device="/dev/ttyACM0",
        vid="0403",
        pid="8A28",
        serial_number=None,
        manufacturer="Rainforest",
        description="RAVEn",
    )
    executor = AsyncMock(side_effect=lambda function: function())
    monkeypatch.setattr(config_flow.usb, "scan_serial_ports", ha_scan)
    monkeypatch.setattr(
        config_flow.serial.tools.list_ports, "comports", lambda: (fallback_port,)
    )
    monkeypatch.setattr(hass, "async_add_executor_job", executor)

    candidates = await config_flow.async_scan_supported_ports(hass)

    assert [candidate.path for candidate in candidates] == ["/dev/ttyACM0"]
    assert executor.await_count == 2


async def test_retryable_validation_errors_preserve_selection_until_success(
    hass, monkeypatch
) -> None:
    """Retrying a failed confirmation keeps the same staged port selection."""
    missing = RainforestCommunicationError(
        FailureStage.OPEN, FailureReason.MISSING, retryable=True
    )
    validate = AsyncMock(side_effect=(missing, missing, VALIDATION))
    result = await _start_user_flow(hass, monkeypatch, validate)

    for attempt in range(2):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "confirm"
        assert result["errors"] == {"base": "device_missing"}
        assert "/dev/tty" not in str(result)
        assert attempt < 2

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "meters"


async def test_meter_selection_creates_private_entry_data(hass, monkeypatch) -> None:
    """Only selected meter IDs and USB metadata enter persistent entry data."""
    result = await _start_user_flow(
        hass, monkeypatch, AsyncMock(return_value=VALIDATION)
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "meters"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_METERS: [METER_MAC]}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_DEVICE: "/dev/ttyACM0",
        CONF_METERS: [METER_MAC],
        "usb_serial": "USB-SERIAL",
        "usb_vid": 0x04B4,
        "usb_pid": 0x0003,
    }
    assert "Main" not in str(result["data"])
    assert result["result"].unique_id == "0013500000000001"


async def test_meter_schema_serializes_and_requires_a_selected_meter(
    hass, monkeypatch
) -> None:
    """The meter control is frontend-serializable and rejects an empty choice."""
    result = await _start_user_flow(
        hass, monkeypatch, AsyncMock(return_value=VALIDATION)
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["step_id"] == "meters"
    assert convert(result["data_schema"], custom_serializer=cv.custom_serializer)
    meter_key = next(iter(result["data_schema"].schema))
    assert meter_key.default() == [METER_MAC]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_METERS: []}
    )
    assert result["step_id"] == "meters"
    assert result["errors"] == {CONF_METERS: "no_supported_meters"}


async def test_advanced_path_rejects_uri(hass, monkeypatch) -> None:
    """Advanced setup accepts local paths, never URI-like transport strings."""
    hass.config.components.add("usb")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"action": "advanced"}
    )
    assert result["step_id"] == "advanced"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"path": "socket://private-device"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"path": "unknown"}


@pytest.mark.parametrize("action", ("rescan", "advanced"))
async def test_explicit_user_action_overrides_a_stale_port_token(
    hass, monkeypatch, action
) -> None:
    """Action controls cannot accidentally confirm a stale detected device."""
    result = await _start_user_flow(hass, monkeypatch, AsyncMock())
    flow_id = result["flow_id"]
    hass.config_entries.flow.async_abort(flow_id)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    device_options = next(
        value
        for key, value in result["data_schema"].schema.items()
        if key.schema == CONF_DEVICE
    )
    token = next(iter(device_options.container))

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"action": action, CONF_DEVICE: token}
    )

    expected_step = "advanced" if action == "advanced" else "user"
    assert result["step_id"] == expected_step


def test_candidate_sanitizes_non_string_usb_metadata() -> None:
    """Only JSON-safe string USB metadata crosses the discovery boundary."""
    from custom_components.rainforest_emu2.config_flow import _candidate_from_port

    candidate = _candidate_from_port(
        SimpleNamespace(
            device="/dev/ttyACM0",
            vid=0x04B4,
            pid=0x0003,
            serial_number=object(),
            manufacturer=object(),
            description=object(),
            location=object(),
        ),
        0,
    )

    assert candidate is not None
    assert candidate.serial_number is None
    assert candidate.manufacturer is None
    assert candidate.description is None
    assert candidate.location is None


def test_provisional_identity_uses_serial_or_canonical_path_without_labels() -> None:
    """Rediscovery identity is stable without exposing labels or raw paths."""
    from custom_components.rainforest_emu2.config_flow import _provisional_identity

    with_serial = SimpleNamespace(
        path="/dev/ttyACM0", serial_number="USB-SERIAL", label="same label"
    )
    without_serial = SimpleNamespace(
        path="/dev/ttyACM0", serial_number=None, label="same label"
    )

    assert _provisional_identity(with_serial) == "usb:USB-SERIAL"
    assert _provisional_identity(without_serial).startswith("path:")
    assert "ttyACM0" not in _provisional_identity(without_serial)


@pytest.mark.parametrize(
    ("stage", "reason", "expected"),
    [
        (FailureStage.OPEN, FailureReason.MISSING, "device_missing"),
        (FailureStage.OPEN, FailureReason.PERMISSION, "device_permission"),
        (FailureStage.OPEN, FailureReason.BUSY, "device_busy"),
        (FailureStage.OPEN, FailureReason.TIMEOUT, "open_timeout"),
        (FailureStage.SYNC, FailureReason.TIMEOUT, "response_timeout"),
        (FailureStage.SYNC, FailureReason.NO_RESPONSE, "no_response"),
        (FailureStage.DEVICE_INFO, FailureReason.MALFORMED, "identity_invalid"),
        (FailureStage.METER_INFO, FailureReason.UNSUPPORTED, "no_supported_meters"),
        (FailureStage.POLL, FailureReason.IO, "unknown"),
        (FailureStage.CLEANUP, FailureReason.IO, "cleanup_failed"),
    ],
)
def test_structured_validation_error_maps_to_safe_translation_key(
    stage, reason, expected
) -> None:
    """Every communication failure has UI-safe translated copy without IDs."""
    from custom_components.rainforest_emu2.config_flow import _error_key

    error = RainforestCommunicationError(
        stage, reason, retryable=True, cause=OSError("/dev/private-device")
    )

    assert _error_key(error) == expected
    assert "private" not in _error_key(error)


async def test_import_preselects_only_present_meters_without_mutating_builtin_entry(
    hass, monkeypatch
) -> None:
    """Migration copies only transient defaults from a disabled built-in entry."""
    from custom_components.rainforest_emu2 import config_flow

    old_data = {
        CONF_DEVICE: "/dev/ttyACM0",
        CONF_MAC: [METER_MAC, "0013500102030499"],
    }
    old_entry = MockConfigEntry(
        domain="rainforest_raven",
        data=old_data,
        disabled_by=ConfigEntryDisabler.USER,
    )
    old_entry.add_to_hass(hass)
    validate = AsyncMock(return_value=VALIDATION)
    monkeypatch.setattr(
        config_flow,
        "RavenClient",
        lambda path: type("Client", (), {"async_validate": validate})(),
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"action": "import_builtin"}
    )
    assert result["step_id"] == "import_builtin"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entry_id": old_entry.entry_id}
    )

    assert result["step_id"] == "import_confirm"
    assert old_entry.disabled_by is ConfigEntryDisabler.USER
    assert dict(old_entry.data) == old_data
    validate.assert_not_awaited()

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "meters"
    meter_key = next(iter(result["data_schema"].schema))
    assert meter_key.default() == [METER_MAC]
    assert old_entry.entry_id in hass.config_entries.async_entry_ids()
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_import_enabled_builtin_aborts_without_opening_serial(
    hass, monkeypatch
) -> None:
    """An enabled built-in entry retains ownership and cannot be imported."""
    from custom_components.rainforest_emu2 import config_flow

    old_entry = MockConfigEntry(
        domain="rainforest_raven",
        data={CONF_DEVICE: "/dev/ttyACM0", CONF_MAC: [METER_MAC]},
    )
    old_entry.add_to_hass(hass)
    validate = AsyncMock()
    monkeypatch.setattr(
        config_flow,
        "RavenClient",
        lambda path: type("Client", (), {"async_validate": validate})(),
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"action": "import_builtin"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entry_id": old_entry.entry_id}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "builtin_still_enabled"
    validate.assert_not_awaited()
    assert dict(old_entry.data) == {CONF_DEVICE: "/dev/ttyACM0", CONF_MAC: [METER_MAC]}


async def test_import_keeps_missing_builtin_path_editable(hass, monkeypatch) -> None:
    """An unavailable old path is editable before the new flow opens anything."""
    from custom_components.rainforest_emu2 import config_flow

    old_entry = MockConfigEntry(
        domain="rainforest_raven",
        data={CONF_DEVICE: "/dev/missing-rainforest", CONF_MAC: [METER_MAC]},
        disabled_by=ConfigEntryDisabler.USER,
    )
    old_entry.add_to_hass(hass)
    validate = AsyncMock(return_value=VALIDATION)
    monkeypatch.setattr(
        config_flow,
        "RavenClient",
        lambda path: type("Client", (), {"async_validate": validate})(),
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"action": "import_builtin"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entry_id": old_entry.entry_id}
    )

    assert result["step_id"] == "import_confirm"
    path_key = next(iter(result["data_schema"].schema))
    assert path_key.default() == "/dev/missing-rainforest"
    validate.assert_not_awaited()


async def test_import_empty_meter_result_returns_to_its_confirmation_step(
    hass, monkeypatch
) -> None:
    """An imported flow keeps its editable path step after an empty meter response."""
    from custom_components.rainforest_emu2 import config_flow

    old_entry = MockConfigEntry(
        domain="rainforest_raven",
        data={CONF_DEVICE: "/dev/ttyACM0", CONF_MAC: [METER_MAC]},
        disabled_by=ConfigEntryDisabler.USER,
    )
    old_entry.add_to_hass(hass)
    empty_result = ValidationResult(
        path="/dev/ttyACM0",
        device_mac=VALIDATION.device_mac,
        manufacturer="Rainforest",
        model="EMU-2",
        firmware="1.2.3",
        meters=(),
    )
    monkeypatch.setattr(
        config_flow,
        "RavenClient",
        lambda path: type(
            "Client", (), {"async_validate": AsyncMock(return_value=empty_result)}
        )(),
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"action": "import_builtin"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entry_id": old_entry.entry_id}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["step_id"] == "import_confirm"
    assert result["errors"] == {"base": "no_paired_meters"}


async def test_reconfigure_loaded_entry_uses_existing_client_and_updates_same_entry(
    hass,
) -> None:
    """Repair validates with the loaded client and reloads the same entry."""
    client = ReconfigureClient(VALIDATION)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=VALIDATION.device_mac,
        state=ConfigEntryState.LOADED,
        data={
            CONF_DEVICE: "/dev/ttyACM9",
            CONF_METERS: [METER_MAC],
            "usb_serial": "old-usb",
            "usb_vid": 0x04B4,
            "usb_pid": 0x0003,
        },
    )
    entry.runtime_data = Runtime(client)
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": entry.entry_id},
    )
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICE: "/dev/ttyACM0"}
    )
    assert result["step_id"] == "reconfigure_confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "meters"
    assert client.paths == ["/dev/ttyACM0"]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_METERS: [METER_MAC]}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.entry_id in hass.config_entries.async_entry_ids()
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    assert entry.data[CONF_DEVICE] == "/dev/ttyACM0"
    assert entry.data[CONF_METERS] == [METER_MAC]


async def test_reconfigure_identity_mismatch_restores_original_path_and_aborts(
    hass,
) -> None:
    """A candidate device with another hardware identity never replaces an entry."""
    wrong_identity = ValidationResult(
        path="/dev/ttyACM0",
        device_mac="0013500000000099",
        manufacturer="Rainforest",
        model="EMU-2",
        firmware="1.2.3",
        meters=VALIDATION.meters,
    )
    client = ReconfigureClient(wrong_identity)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=VALIDATION.device_mac,
        state=ConfigEntryState.LOADED,
        data={CONF_DEVICE: "/dev/ttyACM9", CONF_METERS: [METER_MAC]},
    )
    entry.runtime_data = Runtime(client)
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": entry.entry_id},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICE: "/dev/ttyACM0"}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"
    assert client.paths == ["/dev/ttyACM0", "/dev/ttyACM9"]
    assert entry.data[CONF_DEVICE] == "/dev/ttyACM9"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_reconfigure_unloaded_entry_uses_and_closes_bounded_temporary_client(
    hass, monkeypatch
) -> None:
    """An unloaded entry uses a temporary client that is closed after validation."""
    from custom_components.rainforest_emu2 import config_flow

    class TemporaryClient(ReconfigureClient):
        def __init__(self) -> None:
            super().__init__(VALIDATION)
            self.shutdowns = 0

        async def async_shutdown(self) -> None:
            self.shutdowns += 1

    client = TemporaryClient()
    monkeypatch.setattr(config_flow, "RavenClient", lambda path: client)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=VALIDATION.device_mac,
        state=ConfigEntryState.NOT_LOADED,
        data={CONF_DEVICE: "/dev/ttyACM9", CONF_METERS: [METER_MAC]},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": entry.entry_id},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICE: "/dev/ttyACM0"}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["step_id"] == "meters"
    assert client.paths == ["/dev/ttyACM0"]
    assert client.shutdowns == 1


async def test_import_rechecks_source_that_becomes_enabled_before_opening(
    hass, monkeypatch
) -> None:
    """A source re-enabled after selection retains ownership at validation time."""
    from custom_components.rainforest_emu2 import config_flow

    source = MockConfigEntry(
        domain="rainforest_raven",
        data={CONF_DEVICE: "/dev/ttyACM0", CONF_MAC: [METER_MAC]},
        disabled_by=ConfigEntryDisabler.USER,
    )
    source.add_to_hass(hass)
    validate = AsyncMock(return_value=VALIDATION)
    monkeypatch.setattr(
        config_flow,
        "RavenClient",
        lambda path: type("Client", (), {"async_validate": validate})(),
    )

    result = await _start_builtin_import(hass, source)
    source.disabled_by = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "builtin_still_enabled"
    validate.assert_not_awaited()


async def test_import_allows_source_removed_after_selection(hass, monkeypatch) -> None:
    """Removing a previously disabled source does not block the migration flow."""
    from custom_components.rainforest_emu2 import config_flow

    source = MockConfigEntry(
        domain="rainforest_raven",
        data={CONF_DEVICE: "/dev/ttyACM0", CONF_MAC: [METER_MAC]},
        disabled_by=ConfigEntryDisabler.USER,
    )
    source.add_to_hass(hass)
    validate = AsyncMock(return_value=VALIDATION)
    monkeypatch.setattr(
        config_flow,
        "RavenClient",
        lambda path: type("Client", (), {"async_validate": validate})(),
    )

    result = await _start_builtin_import(hass, source)
    del hass.config_entries._entries[source.entry_id]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["step_id"] == "meters"
    validate.assert_awaited_once()


async def test_import_rejects_edited_path_owned_by_enabled_builtin(
    hass, monkeypatch
) -> None:
    """Editing a migration path cannot open a port an enabled built-in owns."""
    from custom_components.rainforest_emu2 import config_flow

    source = MockConfigEntry(
        domain="rainforest_raven",
        data={CONF_DEVICE: "/dev/ttyACM0", CONF_MAC: [METER_MAC]},
        disabled_by=ConfigEntryDisabler.USER,
    )
    owner = MockConfigEntry(
        domain="rainforest_raven",
        data={CONF_DEVICE: "/dev/ttyACM1", CONF_MAC: [METER_MAC]},
    )
    source.add_to_hass(hass)
    owner.add_to_hass(hass)
    validate = AsyncMock(return_value=VALIDATION)
    monkeypatch.setattr(
        config_flow,
        "RavenClient",
        lambda path: type("Client", (), {"async_validate": validate})(),
    )

    result = await _start_builtin_import(hass, source)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"path": "/dev/ttyACM1"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "builtin_still_enabled"
    validate.assert_not_awaited()


async def test_import_validation_error_preserves_editable_path_and_retries(
    hass, monkeypatch
) -> None:
    """A validation error leaves the import path form intact for correction."""
    from custom_components.rainforest_emu2 import config_flow

    source = MockConfigEntry(
        domain="rainforest_raven",
        data={CONF_DEVICE: "/dev/ttyACM0", CONF_MAC: [METER_MAC]},
        disabled_by=ConfigEntryDisabler.USER,
    )
    source.add_to_hass(hass)
    paths: list[str] = []
    validate = AsyncMock(
        side_effect=(
            RainforestCommunicationError(
                FailureStage.OPEN, FailureReason.MISSING, retryable=True
            ),
            VALIDATION,
        )
    )
    monkeypatch.setattr(
        config_flow,
        "RavenClient",
        lambda path: (
            paths.append(path) or type("Client", (), {"async_validate": validate})()
        ),
    )

    result = await _start_builtin_import(hass, source)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["step_id"] == "import_confirm"
    path_key = next(iter(result["data_schema"].schema))
    assert path_key.default() == "/dev/ttyACM0"
    assert result["errors"] == {"base": "device_missing"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"path": "/dev/ttyACM2"}
    )

    assert result["step_id"] == "meters"
    assert paths == ["/dev/ttyACM0", "/dev/ttyACM2"]


async def test_import_no_meter_error_preserves_editable_path_and_retries(
    hass, monkeypatch
) -> None:
    """An empty meter result also leaves the import path available for correction."""
    from custom_components.rainforest_emu2 import config_flow

    source = MockConfigEntry(
        domain="rainforest_raven",
        data={CONF_DEVICE: "/dev/ttyACM0", CONF_MAC: [METER_MAC]},
        disabled_by=ConfigEntryDisabler.USER,
    )
    source.add_to_hass(hass)
    empty_result = ValidationResult(
        path="/dev/ttyACM0",
        device_mac=VALIDATION.device_mac,
        manufacturer="Rainforest",
        model="EMU-2",
        firmware="1.2.3",
        meters=(),
    )
    paths: list[str] = []
    validate = AsyncMock(side_effect=(empty_result, VALIDATION))
    monkeypatch.setattr(
        config_flow,
        "RavenClient",
        lambda path: (
            paths.append(path) or type("Client", (), {"async_validate": validate})()
        ),
    )

    result = await _start_builtin_import(hass, source)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["step_id"] == "import_confirm"
    path_key = next(iter(result["data_schema"].schema))
    assert path_key.default() == "/dev/ttyACM0"
    assert result["errors"] == {"base": "no_paired_meters"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"path": "/dev/ttyACM2"}
    )

    assert result["step_id"] == "meters"
    assert paths == ["/dev/ttyACM0", "/dev/ttyACM2"]


async def test_reconfigure_flow_conflict_restores_loaded_client_before_abort(
    hass,
) -> None:
    """An actual HA unique-ID progress abort rolls the live client back first."""
    client = ReconfigureClient(VALIDATION)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=VALIDATION.device_mac,
        state=ConfigEntryState.LOADED,
        data={CONF_DEVICE: "/dev/ttyACM9", CONF_METERS: [METER_MAC]},
    )
    entry.runtime_data = Runtime(client)
    entry.add_to_hass(hass)

    competing = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    competing_flow = hass.config_entries.flow._progress[competing["flow_id"]]
    await competing_flow.async_set_unique_id(VALIDATION.device_mac)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": entry.entry_id},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICE: "/dev/ttyACM0"}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_in_progress"
    assert client.paths == ["/dev/ttyACM0", "/dev/ttyACM9"]
    assert client.connected_path == "/dev/ttyACM9"
    hass.config_entries.flow.async_abort(competing["flow_id"])


async def test_reconfigure_cancellation_restores_loaded_client(
    hass, monkeypatch
) -> None:
    """Cancellation after candidate validation restores the old client before escape."""
    from custom_components.rainforest_emu2 import config_flow

    client = ReconfigureClient(VALIDATION)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=VALIDATION.device_mac,
        state=ConfigEntryState.LOADED,
        data={CONF_DEVICE: "/dev/ttyACM9", CONF_METERS: [METER_MAC]},
    )
    entry.runtime_data = Runtime(client)
    entry.add_to_hass(hass)

    async def cancel_unique_id(self, unique_id: str) -> None:
        raise asyncio.CancelledError

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": entry.entry_id},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICE: "/dev/ttyACM0"}
    )
    monkeypatch.setattr(
        config_flow.RainforestEmu2ConfigFlow,
        "async_set_unique_id",
        cancel_unique_id,
    )

    with pytest.raises(asyncio.CancelledError):
        await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert client.paths == ["/dev/ttyACM0", "/dev/ttyACM9"]
    assert client.connected_path == "/dev/ttyACM9"


async def test_cancellation_during_reconfigure_restore_waits_then_propagates(
    hass,
) -> None:
    """A new cancellation waits for rollback instead of detaching it."""

    class BlockingRestoreClient(ReconfigureClient):
        def __init__(self) -> None:
            super().__init__(VALIDATION)
            self.restore_started = asyncio.Event()
            self.restore_release = asyncio.Event()
            self.restore_completed = False

        async def async_restore_path(self, path: str) -> None:
            self.paths.append(path)
            self.restore_started.set()
            await self.restore_release.wait()
            self.connected_path = path
            self.restore_completed = True

    client = BlockingRestoreClient()
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=VALIDATION.device_mac,
        state=ConfigEntryState.LOADED,
        data={CONF_DEVICE: "/dev/ttyACM9", CONF_METERS: [METER_MAC]},
    )
    entry.runtime_data = Runtime(client)
    entry.add_to_hass(hass)
    competing = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    competing_flow = hass.config_entries.flow._progress[competing["flow_id"]]
    await competing_flow.async_set_unique_id(VALIDATION.device_mac)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": entry.entry_id},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICE: "/dev/ttyACM0"}
    )
    configure = asyncio.create_task(
        hass.config_entries.flow.async_configure(result["flow_id"], {})
    )
    await client.restore_started.wait()
    configure.cancel()
    await asyncio.sleep(0)

    assert not configure.done()
    client.restore_release.set()
    with pytest.raises(asyncio.CancelledError):
        await configure

    assert client.restore_completed
    assert client.connected_path == "/dev/ttyACM9"
    hass.config_entries.flow.async_abort(competing["flow_id"])


async def test_cancellation_during_temporary_shutdown_waits_then_propagates(
    hass, monkeypatch
) -> None:
    """Cancellation cannot be swallowed while an unloaded entry's client shuts down."""
    from custom_components.rainforest_emu2 import config_flow

    class BlockingTemporaryClient(ReconfigureClient):
        def __init__(self) -> None:
            super().__init__(VALIDATION)
            self.shutdown_started = asyncio.Event()
            self.shutdown_release = asyncio.Event()
            self.shutdown_completed = False

        async def async_shutdown(self) -> None:
            self.shutdown_started.set()
            await self.shutdown_release.wait()
            self.shutdown_completed = True

    client = BlockingTemporaryClient()
    monkeypatch.setattr(config_flow, "RavenClient", lambda path: client)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=VALIDATION.device_mac,
        state=ConfigEntryState.NOT_LOADED,
        data={CONF_DEVICE: "/dev/ttyACM9", CONF_METERS: [METER_MAC]},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": entry.entry_id},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICE: "/dev/ttyACM0"}
    )
    configure = asyncio.create_task(
        hass.config_entries.flow.async_configure(result["flow_id"], {})
    )
    await client.shutdown_started.wait()
    configure.cancel()
    await asyncio.sleep(0)

    assert not configure.done()
    client.shutdown_release.set()
    with pytest.raises(asyncio.CancelledError):
        await configure

    assert client.shutdown_completed


async def test_inner_cleanup_cancellation_propagates_without_spinning() -> None:
    """An internally cancelled cleanup task exits once instead of busy-looping."""
    from custom_components.rainforest_emu2.config_flow import RainforestEmu2ConfigFlow

    async def cancelled_cleanup() -> None:
        raise asyncio.CancelledError

    flow = RainforestEmu2ConfigFlow()
    with pytest.raises(asyncio.CancelledError):
        await flow._async_wait_for_cleanup(cancelled_cleanup())
