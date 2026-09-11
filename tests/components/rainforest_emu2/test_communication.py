from __future__ import annotations

import asyncio
import errno
import os
from collections import deque

import pytest
from aioraven.data import (
    Currency,
    CurrentSummationDelivered,
    DeviceInfo,
    InstantaneousDemand,
    MeterInfo,
    MeterList,
    MeterType,
    NetworkInfo,
    PriceCluster,
)

from custom_components.rainforest_emu2 import communication
from custom_components.rainforest_emu2.communication import (
    FailureReason,
    FailureStage,
    MeterRecord,
    MeterSnapshot,
    PortCandidate,
    PortLockRegistry,
    RainforestCommunicationError,
    RavenClient,
    RavenSnapshot,
    ValidationResult,
    safe_path_details,
    snapshot_field_key,
)

from .conftest import FakeRavenDevice, FakeRavenFactory

DEVICE_MAC = bytes.fromhex("0013500000000001")
METER_MAC = bytes.fromhex("0013500102030405")


def _device_info(*, mac: bytes | None = DEVICE_MAC) -> DeviceInfo:
    return DeviceInfo(
        device_mac_id=mac,
        install_code=None,
        link_key=None,
        fw_version="1.2.3",
        hw_version=None,
        image_type=None,
        manufacturer="Rainforest Automation",
        model_id="EMU-2",
        date_code=None,
    )


def _meter_info(
    *,
    mac: bytes | None = METER_MAC,
    meter_type: MeterType | None = MeterType.ELECTRIC,
) -> MeterInfo:
    return MeterInfo(
        device_mac_id=DEVICE_MAC,
        meter_mac_id=mac,
        meter_type=meter_type,
        nick_name="Main meter",
        account=None,
        auth=None,
        host=None,
        enabled=True,
    )


def _successful_fake(fake: FakeRavenDevice) -> None:
    fake.meter_list_results.append(MeterList(DEVICE_MAC, [METER_MAC]))
    fake.device_info = _device_info()
    fake.meter_infos[METER_MAC] = _meter_info()


async def test_canonical_symlinks_share_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = PortLockRegistry()
    monkeypatch.setattr(os.path, "realpath", lambda path: "/dev/ttyACM0")

    assert registry.lock_for("/dev/serial/by-id/device") is registry.lock_for(
        "/dev/ttyACM0"
    )


async def test_default_clients_contend_before_open(
    fast_lifecycle_timeouts, monkeypatch
):
    """Separate production clients must not independently open one gateway."""
    first = RavenClient("/dev/ttyACM0")
    second = RavenClient("/dev/ttyACM0")
    fake = FakeRavenDevice()
    _successful_fake(fake)
    monkeypatch.setattr(communication, "RAVEnSerialDevice", FakeRavenFactory(fake))
    async with first.registry.acquire(first.path):
        with pytest.raises(RainforestCommunicationError) as raised:
            await second.async_validate()
        assert raised.value.reason == FailureReason.BUSY
        assert fake.calls == []


async def test_collector_units_reach_entities(fast_lifecycle_timeouts, monkeypatch):
    """Dependency demand is kW and link strength is percent, not W/dB."""
    from types import SimpleNamespace
    from unittest.mock import Mock

    from custom_components.rainforest_emu2.coordinator import RainforestRuntimeData
    from custom_components.rainforest_emu2.sensor import async_setup_entry

    fake = PollingRavenDevice()
    _prepare_polling_connection(fake)
    _add_polling_cycle(fake, demand="2.5", signal=80)
    monkeypatch.setattr(communication, "RAVEnSerialDevice", FakeRavenFactory(fake))
    client = RavenClient("/dev/ttyACM0")
    try:
        snapshot = await client.async_refresh((METER_MAC,))
        coordinator = Mock(data=snapshot, last_update_success=True)
        coordinator.config_entry = SimpleNamespace(unique_id=DEVICE_MAC.hex())
        validation = ValidationResult(
            client.path,
            DEVICE_MAC.hex(),
            "Rainforest",
            "EMU-2",
            "1.2.3",
            (MeterRecord(METER_MAC, METER_MAC.hex(), "Main", "electric"),),
        )
        entry = SimpleNamespace(
            runtime_data=RainforestRuntimeData(client, coordinator, validation)
        )
        entities = []
        await async_setup_entry(None, entry, entities.extend)
        values = {entity.entity_description.key: entity for entity in entities}
        assert values["demand"].native_value == 2500.0
        assert values["demand"].native_unit_of_measurement == "W"
        assert values["signal_strength"].native_value == 80
        assert values["signal_strength"].native_unit_of_measurement == "%"
        assert values["signal_strength"].device_class is None
    finally:
        await client.async_shutdown()


def test_by_id_path_is_redacted() -> None:
    assert safe_path_details("/dev/serial/by-id/usb-Rainforest_SECRET-if00") == {
        "strategy": "by_id",
        "basename": None,
    }


@pytest.mark.parametrize(
    ("path", "details"),
    [
        ("/dev/ttyACM0", {"strategy": "raw", "basename": "ttyACM0"}),
        ("/dev/ttyUSB12", {"strategy": "raw", "basename": "ttyUSB12"}),
        ("/dev/serial/by-path/pci-SECRET", {"strategy": "by_path", "basename": None}),
        ("/tmp/ttyACM0", {"strategy": "unknown", "basename": None}),
        ("/dev/ttyS0", {"strategy": "unknown", "basename": None}),
    ],
)
def test_safe_path_details_only_exposes_supported_path_names(
    path: str, details: dict[str, str | None]
) -> None:
    assert safe_path_details(path) == details


def test_structured_error_does_not_render_cause() -> None:
    err = RainforestCommunicationError(
        FailureStage.OPEN,
        FailureReason.PERMISSION,
        retryable=False,
        cause=PermissionError("/dev/serial/by-id/SECRET"),
    )

    assert str(err) == "open:permission"
    assert "SECRET" not in str(err)
    assert err.stage is FailureStage.OPEN
    assert err.reason is FailureReason.PERMISSION
    assert err.retryable is False
    assert isinstance(err.cause, PermissionError)


def test_snapshot_field_key_uses_device_and_meter_namespaces() -> None:
    assert snapshot_field_key("signal_strength") == "device:signal_strength"
    assert snapshot_field_key("demand", "0013500102030405") == (
        "meter:0013500102030405:demand"
    )


def test_public_records_have_immutable_shapes() -> None:
    candidate = PortCandidate(
        token="opaque",
        path="/dev/ttyACM0",
        label="Rainforest EMU-2",
        vid=0x04B4,
        pid=0x0003,
        serial_number=None,
        manufacturer="Rainforest",
        description=None,
        location=None,
    )
    record = MeterRecord(b"12345678", "0013500102030405", "Meter", "electric")
    validation = ValidationResult(
        "/dev/ttyACM0", "0013500102030405", None, None, None, (record,)
    )
    meter = MeterSnapshot(1.0, 2.0, 3.0, 4.0, "USD")
    snapshot = RavenSnapshot(
        {record.mac_hex: meter}, 90, frozenset({"device:signal_strength"})
    )

    assert candidate.path == "/dev/ttyACM0"
    assert validation.meters == (record,)
    assert snapshot.meters[record.mac_hex].demand == 1.0
    with pytest.raises((AttributeError, TypeError)):
        candidate.path = "/dev/ttyUSB0"  # type: ignore[misc]


async def test_alias_held_distinct_group_fails_before_mutating_mapping() -> None:
    registry = PortLockRegistry()
    first = "/dev/ttyACM0"
    second = "/dev/ttyUSB0"
    mac = "0013500102030405"

    registry.alias(first, mac)
    first_lock = registry.lock_for(first)
    second_lock = registry.lock_for(second)
    assert first_lock is not second_lock

    await second_lock.acquire()
    try:
        with pytest.raises(RainforestCommunicationError) as raised:
            registry.alias(second, mac)
        assert raised.value.stage is FailureStage.LOCK
        assert raised.value.reason is FailureReason.BUSY
        assert registry.lock_for(second) is second_lock
        assert registry.lock_for(first) is first_lock
    finally:
        second_lock.release()

    registry.alias(second, mac)
    assert registry.lock_for(second) is first_lock


async def test_alias_owner_merges_to_held_source_lock() -> None:
    registry = PortLockRegistry()
    first = "/dev/ttyACM0"
    second = "/dev/ttyUSB0"
    mac = DEVICE_MAC.hex()
    registry.alias(first, mac)

    async with registry.acquire(second) as owner:
        registry.alias(second, mac, owner=owner)

        assert registry.lock_for(first) is registry.lock_for(second)
        with pytest.raises(RainforestCommunicationError) as raised:
            async with registry.acquire(first):
                raise AssertionError("merged path must remain mutually exclusive")
        assert raised.value.stage is FailureStage.LOCK
        assert raised.value.reason is FailureReason.BUSY


async def test_alias_owner_rejects_held_target_before_mutating() -> None:
    registry = PortLockRegistry()
    first = "/dev/ttyACM0"
    second = "/dev/ttyUSB0"
    mac = DEVICE_MAC.hex()
    registry.alias(first, mac)
    first_lock = registry.lock_for(first)
    second_lock = registry.lock_for(second)
    await first_lock.acquire()

    try:
        async with registry.acquire(second) as owner:
            with pytest.raises(RainforestCommunicationError) as raised:
                registry.alias(second, mac, owner=owner)
            assert raised.value.stage is FailureStage.LOCK
            assert raised.value.reason is FailureReason.BUSY
            assert registry.lock_for(first) is first_lock
            assert registry.lock_for(second) is second_lock
    finally:
        first_lock.release()


async def test_alias_rejects_foreign_held_source_before_mutating() -> None:
    registry = PortLockRegistry()
    first = "/dev/ttyACM0"
    second = "/dev/ttyUSB0"
    mac = DEVICE_MAC.hex()
    registry.alias(first, mac)
    first_lock = registry.lock_for(first)
    ready = asyncio.Event()
    release = asyncio.Event()

    async def hold_source() -> None:
        async with registry.acquire(second):
            ready.set()
            await release.wait()

    holder = asyncio.create_task(hold_source())
    await ready.wait()
    second_lock = registry.lock_for(second)
    try:
        with pytest.raises(RainforestCommunicationError) as raised:
            registry.alias(second, mac)
        assert raised.value.stage is FailureStage.LOCK
        assert raised.value.reason is FailureReason.BUSY
        assert registry.lock_for(first) is first_lock
        assert registry.lock_for(second) is second_lock
    finally:
        release.set()
        await holder


async def test_lock_contention_is_bounded_and_structured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = PortLockRegistry()
    lock = registry.lock_for("/dev/ttyACM0")
    await lock.acquire()
    monkeypatch.setattr(
        "custom_components.rainforest_emu2.communication.LOCK_TIMEOUT", 0.01
    )

    try:
        with pytest.raises(RainforestCommunicationError) as raised:
            async with registry.acquire("/dev/ttyACM0"):
                raise AssertionError("the held lock must not be entered")
        assert raised.value.stage is FailureStage.LOCK
        assert raised.value.reason is FailureReason.BUSY
    finally:
        lock.release()


async def test_validate_third_sync_attempt_succeeds_and_closes(
    fake_raven_device: FakeRavenDevice,
    fast_lifecycle_timeouts: None,
) -> None:
    fake_raven_device.meter_list_results.extend(
        [TimeoutError(), TimeoutError(), MeterList(DEVICE_MAC, [METER_MAC])]
    )
    fake_raven_device.device_info = _device_info()
    fake_raven_device.meter_infos[METER_MAC] = _meter_info()
    registry = PortLockRegistry()
    client = RavenClient("/dev/ttyACM0", registry=registry)

    result = await client.async_validate()

    if os.name == "posix":
        assert fake_raven_device.kwargs.get("exclusive") is True
    assert fake_raven_device.calls.count("meter_list") == 3
    assert result.device_mac == DEVICE_MAC.hex()
    assert result.meters[0].mac_hex == "0013500102030405"
    assert result.meters[0].meter_type == "electric"
    assert fake_raven_device.calls[-1] == "close"

    registry.alias("/dev/ttyUSB0", result.device_mac)
    assert registry.lock_for("/dev/ttyUSB0") is registry.lock_for("/dev/ttyACM0")


async def test_validate_same_hardware_sequentially_on_two_paths(
    fast_lifecycle_timeouts: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = FakeRavenDevice()
    second = FakeRavenDevice()
    _successful_fake(first)
    _successful_fake(second)
    monkeypatch.setattr(
        "custom_components.rainforest_emu2.communication.RAVEnSerialDevice",
        FakeRavenFactory(first, second),
    )
    registry = PortLockRegistry()

    first_result = await RavenClient("/dev/ttyACM0", registry=registry).async_validate()
    second_result = await RavenClient(
        "/dev/ttyUSB0", registry=registry
    ).async_validate()

    assert first_result.device_mac == second_result.device_mac == DEVICE_MAC.hex()
    assert registry.lock_for("/dev/ttyACM0") is registry.lock_for("/dev/ttyUSB0")
    assert first.calls[-1] == second.calls[-1] == "close"


async def test_validate_all_sync_attempts_timeout_and_abort(
    fake_raven_device: FakeRavenDevice,
    fast_lifecycle_timeouts: None,
) -> None:
    fake_raven_device.meter_list_results.extend(
        [TimeoutError(), TimeoutError(), TimeoutError()]
    )

    with pytest.raises(RainforestCommunicationError) as raised:
        await RavenClient("/dev/ttyACM0").async_validate()

    assert raised.value.stage is FailureStage.SYNC
    assert raised.value.reason is FailureReason.TIMEOUT
    assert raised.value.retryable is True
    assert fake_raven_device.calls[-1] == "abort"


async def test_validate_empty_meter_list_returns_no_paired_meters(
    fake_raven_device: FakeRavenDevice,
    fast_lifecycle_timeouts: None,
) -> None:
    fake_raven_device.meter_list_results.append(MeterList(DEVICE_MAC, []))
    fake_raven_device.device_info = _device_info()

    result = await RavenClient("/dev/ttyACM0").async_validate()

    assert result.meters == ()
    assert fake_raven_device.calls[-1] == "close"


async def test_validate_missing_device_info_fails_and_aborts(
    fake_raven_device: FakeRavenDevice,
    fast_lifecycle_timeouts: None,
) -> None:
    fake_raven_device.meter_list_results.append(MeterList(DEVICE_MAC, []))
    fake_raven_device.device_info = None

    with pytest.raises(RainforestCommunicationError) as raised:
        await RavenClient("/dev/ttyACM0").async_validate()

    assert raised.value.stage is FailureStage.DEVICE_INFO
    assert raised.value.reason is FailureReason.NO_RESPONSE
    assert fake_raven_device.calls[-1] == "abort"


async def test_validate_invalid_device_identity_fails_as_malformed(
    fake_raven_device: FakeRavenDevice,
    fast_lifecycle_timeouts: None,
) -> None:
    fake_raven_device.meter_list_results.append(MeterList(None, []))
    fake_raven_device.device_info = _device_info(mac=b"too-short")

    with pytest.raises(RainforestCommunicationError) as raised:
        await RavenClient("/dev/ttyACM0").async_validate()

    assert raised.value.stage is FailureStage.DEVICE_INFO
    assert raised.value.reason is FailureReason.MALFORMED


async def test_validate_close_timeout_aborts_then_returns_success(
    fake_raven_device: FakeRavenDevice,
    fast_lifecycle_timeouts: None,
) -> None:
    _successful_fake(fake_raven_device)
    fake_raven_device.close_result = asyncio.Future()

    result = await RavenClient("/dev/ttyACM0").async_validate()

    assert result.device_mac == DEVICE_MAC.hex()
    assert fake_raven_device.calls[-2:] == ["close", "abort"]


async def test_validate_success_with_close_and_abort_failure_maps_cleanup(
    fake_raven_device: FakeRavenDevice,
    fast_lifecycle_timeouts: None,
) -> None:
    _successful_fake(fake_raven_device)
    fake_raven_device.close_result = OSError(errno.EIO, "SECRET")
    fake_raven_device.abort_result = OSError(errno.EIO, "SECRET")

    with pytest.raises(RainforestCommunicationError) as raised:
        await RavenClient("/dev/ttyACM0").async_validate()

    assert raised.value.stage is FailureStage.CLEANUP
    assert raised.value.reason is FailureReason.IO
    assert "SECRET" not in str(raised.value)


async def test_validate_original_error_survives_cleanup_failure(
    fake_raven_device: FakeRavenDevice,
    fast_lifecycle_timeouts: None,
) -> None:
    fake_raven_device.meter_list_results.extend(
        [TimeoutError(), TimeoutError(), TimeoutError()]
    )
    fake_raven_device.abort_result = OSError(errno.EIO, "cleanup SECRET")

    with pytest.raises(RainforestCommunicationError) as raised:
        await RavenClient("/dev/ttyACM0").async_validate()

    assert raised.value.stage is FailureStage.SYNC
    assert raised.value.reason is FailureReason.TIMEOUT


async def test_validate_cancellation_propagates_after_abort_and_lock_release(
    fake_raven_device: FakeRavenDevice,
    fast_lifecycle_timeouts: None,
) -> None:
    fake_raven_device.meter_list_results.append(asyncio.CancelledError())
    registry = PortLockRegistry()

    with pytest.raises(asyncio.CancelledError):
        await RavenClient("/dev/ttyACM0", registry=registry).async_validate()

    assert fake_raven_device.calls[-1] == "abort"
    async with registry.acquire("/dev/ttyACM0"):
        pass


async def test_validate_open_cancellation_aborts_before_propagating(
    fake_raven_device: FakeRavenDevice,
    fast_lifecycle_timeouts: None,
) -> None:
    fake_raven_device.open_result = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await RavenClient("/dev/ttyACM0").async_validate()

    assert fake_raven_device.calls == ["open", "abort"]


async def test_validate_held_lock_fails_boundedly_before_open(
    fake_raven_device: FakeRavenDevice,
    fast_lifecycle_timeouts: None,
) -> None:
    registry = PortLockRegistry()
    lock = registry.lock_for("/dev/ttyACM0")
    await lock.acquire()

    try:
        with pytest.raises(RainforestCommunicationError) as raised:
            await asyncio.wait_for(
                RavenClient("/dev/ttyACM0", registry=registry).async_validate(),
                timeout=0.1,
            )
    finally:
        lock.release()

    assert raised.value.stage is FailureStage.LOCK
    assert raised.value.reason is FailureReason.BUSY
    assert fake_raven_device.calls == []


async def test_validate_preserves_exact_unknown_meter_type_defect(
    fake_raven_device: FakeRavenDevice,
    fast_lifecycle_timeouts: None,
) -> None:
    fake_raven_device.meter_list_results.append(MeterList(DEVICE_MAC, [METER_MAC]))
    fake_raven_device.device_info = _device_info()
    fake_raven_device.meter_infos[METER_MAC] = ValueError(
        "'0x0000' is not a valid MeterType"
    )

    result = await RavenClient("/dev/ttyACM0").async_validate()

    assert result.meters == (
        MeterRecord(METER_MAC, METER_MAC.hex(), "Meter 0405", None),
    )


async def test_validate_preserves_valid_meter_info_without_type(
    fake_raven_device: FakeRavenDevice,
    fast_lifecycle_timeouts: None,
) -> None:
    fake_raven_device.meter_list_results.append(MeterList(DEVICE_MAC, [METER_MAC]))
    fake_raven_device.device_info = _device_info()
    fake_raven_device.meter_infos[METER_MAC] = _meter_info(meter_type=None)

    result = await RavenClient("/dev/ttyACM0").async_validate()

    assert result.meters == (
        MeterRecord(METER_MAC, METER_MAC.hex(), "Main meter", None),
    )


async def test_validate_skips_unrelated_malformed_meter_info(
    fake_raven_device: FakeRavenDevice,
    fast_lifecycle_timeouts: None,
) -> None:
    fake_raven_device.meter_list_results.append(MeterList(DEVICE_MAC, [METER_MAC]))
    fake_raven_device.device_info = _device_info()
    fake_raven_device.meter_infos[METER_MAC] = ValueError(
        "'garbage' is not a valid MeterType"
    )

    result = await RavenClient("/dev/ttyACM0").async_validate()

    assert result.meters == ()


async def test_validate_permission_error_never_downgrades_exclusive_open(
    fake_raven_device: FakeRavenDevice,
    fast_lifecycle_timeouts: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name != "posix":
        pytest.skip("exclusive serial open applies on POSIX")
    fake_raven_device.open_result = PermissionError(errno.EACCES, "SECRET")
    factory = FakeRavenFactory(fake_raven_device)
    monkeypatch.setattr(
        "custom_components.rainforest_emu2.communication.RAVEnSerialDevice", factory
    )

    with pytest.raises(RainforestCommunicationError) as raised:
        await RavenClient("/dev/ttyACM0").async_validate()

    assert raised.value.stage is FailureStage.OPEN
    assert raised.value.reason is FailureReason.PERMISSION
    assert factory.calls == [("/dev/ttyACM0", {"exclusive": True})]


async def test_validate_retries_without_exclusive_only_when_keyword_unsupported(
    fast_lifecycle_timeouts: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name != "posix":
        pytest.skip("exclusive serial open applies on POSIX")
    unsupported = FakeRavenDevice()
    unsupported.open_result = TypeError(
        "open_serial_connection() got an unexpected keyword argument 'exclusive'"
    )
    fallback = FakeRavenDevice()
    _successful_fake(fallback)
    factory = FakeRavenFactory(unsupported, fallback)
    monkeypatch.setattr(
        "custom_components.rainforest_emu2.communication.RAVEnSerialDevice", factory
    )

    result = await RavenClient("/dev/ttyACM0").async_validate()

    assert result.device_mac == DEVICE_MAC.hex()
    assert factory.calls == [
        ("/dev/ttyACM0", {"exclusive": True}),
        ("/dev/ttyACM0", {}),
    ]
    assert unsupported.calls[-1] == "abort"
    assert fallback.calls[-1] == "close"


async def test_validate_constructor_retries_only_unsupported_exclusive_keyword(
    fast_lifecycle_timeouts: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name != "posix":
        pytest.skip("exclusive serial open applies on POSIX")
    fallback = FakeRavenDevice()
    _successful_fake(fallback)
    unsupported = TypeError(
        "RAVEnSerialDevice() got an unexpected keyword argument 'exclusive'"
    )
    factory = FakeRavenFactory(unsupported, fallback)
    monkeypatch.setattr(
        "custom_components.rainforest_emu2.communication.RAVEnSerialDevice", factory
    )

    result = await RavenClient("/dev/ttyACM0").async_validate()

    assert result.device_mac == DEVICE_MAC.hex()
    assert factory.calls == [
        ("/dev/ttyACM0", {"exclusive": True}),
        ("/dev/ttyACM0", {}),
    ]


async def test_validate_fallback_constructor_error_is_structured(
    fast_lifecycle_timeouts: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name != "posix":
        pytest.skip("exclusive serial open applies on POSIX")
    unsupported = FakeRavenDevice()
    unsupported.open_result = TypeError(
        "open_serial_connection() got an unexpected keyword argument 'exclusive'"
    )
    factory = FakeRavenFactory(
        unsupported,
        PermissionError(errno.EACCES, "SECRET"),
    )
    monkeypatch.setattr(
        "custom_components.rainforest_emu2.communication.RAVEnSerialDevice", factory
    )

    with pytest.raises(RainforestCommunicationError) as raised:
        await RavenClient("/dev/ttyACM0").async_validate()

    assert raised.value.stage is FailureStage.OPEN
    assert raised.value.reason is FailureReason.PERMISSION
    assert "SECRET" not in str(raised.value)


async def test_shutdown_closes_then_aborts_a_stuck_device(
    fake_raven_device: FakeRavenDevice,
    fast_lifecycle_timeouts: None,
) -> None:
    client = RavenClient("/dev/ttyACM0")
    client._device = fake_raven_device
    fake_raven_device.close_result = asyncio.Future()

    await client.async_shutdown()

    assert fake_raven_device.calls == ["close", "abort"]


class PollingRavenDevice(FakeRavenDevice):
    """Fake with controllable polling responses and overlap detection."""

    def __init__(self) -> None:
        super().__init__()
        self.summation_results: deque[object] = deque()
        self.demand_results: deque[object] = deque()
        self.price_results: deque[object] = deque()
        self.network_results: deque[object] = deque()
        self.active_commands = 0
        self.max_active_commands = 0

    async def _poll_command(self, name: str, result: object) -> object:
        self.calls.append(name)
        self.active_commands += 1
        self.max_active_commands = max(self.max_active_commands, self.active_commands)
        try:
            await asyncio.sleep(0)
            return await self._resolve(result)
        finally:
            self.active_commands -= 1

    async def get_current_summation_delivered(
        self, *, meter: bytes | None = None, refresh: bool | None = None
    ) -> object:
        assert meter == METER_MAC
        assert refresh is True
        return await self._poll_command(
            f"summation:{meter.hex()}", self.summation_results.popleft()
        )

    async def get_instantaneous_demand(
        self, *, meter: bytes | None = None, refresh: bool | None = None
    ) -> object:
        assert meter == METER_MAC
        assert refresh is True
        return await self._poll_command(
            f"demand:{meter.hex()}", self.demand_results.popleft()
        )

    async def get_current_price(
        self, *, meter: bytes | None = None, refresh: bool | None = None
    ) -> object:
        assert meter == METER_MAC
        assert refresh is True
        return await self._poll_command(
            f"price:{meter.hex()}", self.price_results.popleft()
        )

    async def get_network_info(self) -> object:
        return await self._poll_command("network_info", self.network_results.popleft())


def _prepare_polling_connection(fake: PollingRavenDevice) -> None:
    fake.meter_list_results.append(MeterList(DEVICE_MAC, [METER_MAC]))
    fake.device_info = _device_info()


def _add_polling_cycle(
    fake: PollingRavenDevice,
    *,
    delivered: str | None = "12.0",
    received: str | None = "1.0",
    demand: str | None = "2.5",
    price: str | None = "0.15",
    currency: Currency | None = Currency.USD,
    signal: int | None = 87,
) -> None:
    fake.summation_results.append(
        CurrentSummationDelivered(DEVICE_MAC, METER_MAC, None, delivered, received)
    )
    fake.demand_results.append(InstantaneousDemand(DEVICE_MAC, METER_MAC, None, demand))
    fake.price_results.append(
        PriceCluster(
            DEVICE_MAC,
            METER_MAC,
            None,
            price,
            currency,
            None,
            None,
            None,
        )
    )
    fake.network_results.append(
        NetworkInfo(DEVICE_MAC, None, None, None, None, None, None, None, signal)
    )


async def test_refresh_retries_whole_cycle_and_discards_partial_values(
    fast_lifecycle_timeouts: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = PollingRavenDevice()
    _prepare_polling_connection(first)
    _add_polling_cycle(first, delivered="99.0")
    first.demand_results.clear()
    first.demand_results.append(TimeoutError())
    second = PollingRavenDevice()
    _prepare_polling_connection(second)
    _add_polling_cycle(second, delivered="12.0", demand="2.5")
    monkeypatch.setattr(
        communication, "RAVEnSerialDevice", FakeRavenFactory(first, second)
    )
    client = RavenClient("/dev/ttyACM0")

    snapshot = await client.async_refresh((METER_MAC,))

    assert first.calls[-1] == "abort"
    assert second.calls[:3] == ["open", "meter_list", "device_info"]
    assert snapshot.meters[METER_MAC.hex()].demand == 2500
    assert snapshot.meters[METER_MAC.hex()].delivered == 12.0
    assert snapshot.meters[METER_MAC.hex()].delivered != 99.0
    assert snapshot.meters[METER_MAC.hex()].price == 0.15
    assert snapshot.meters[METER_MAC.hex()].currency == "USD"
    assert snapshot_field_key("price", METER_MAC.hex()) in snapshot.present_fields
    await client.async_shutdown()


async def test_refresh_stops_after_two_failed_attempts(
    fast_lifecycle_timeouts: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = PollingRavenDevice()
    second = PollingRavenDevice()
    for fake, failure in ((first, TimeoutError()), (second, OSError(errno.EIO, "x"))):
        _prepare_polling_connection(fake)
        _add_polling_cycle(fake)
        fake.demand_results.clear()
        fake.demand_results.append(failure)
    factory = FakeRavenFactory(first, second)
    monkeypatch.setattr(communication, "RAVEnSerialDevice", factory)
    client = RavenClient("/dev/ttyACM0")

    with pytest.raises(RainforestCommunicationError) as raised:
        await client.async_refresh((METER_MAC,))

    assert raised.value.stage is FailureStage.POLL
    assert raised.value.reason is FailureReason.IO
    assert len(factory.calls) == 2
    assert first.calls[-1] == second.calls[-1] == "abort"
    await client.async_shutdown()


async def test_refresh_omits_malformed_optional_price_without_retry(
    fast_lifecycle_timeouts: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = PollingRavenDevice()
    _prepare_polling_connection(fake)
    _add_polling_cycle(fake)
    fake.price_results.clear()
    fake.price_results.append(ValueError("malformed optional price"))
    factory = FakeRavenFactory(fake)
    monkeypatch.setattr(communication, "RAVEnSerialDevice", factory)
    client = RavenClient("/dev/ttyACM0")

    snapshot = await client.async_refresh((METER_MAC,))

    meter = snapshot.meters[METER_MAC.hex()]
    assert meter.price is None
    assert meter.currency is None
    assert snapshot_field_key("price", METER_MAC.hex()) not in snapshot.present_fields
    assert len(factory.calls) == 1
    await client.async_shutdown()


async def test_refresh_omits_malformed_optional_network_without_retry(
    fast_lifecycle_timeouts: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = PollingRavenDevice()
    _prepare_polling_connection(fake)
    _add_polling_cycle(fake)
    fake.network_results.clear()
    fake.network_results.append(ValueError("malformed optional network"))
    factory = FakeRavenFactory(fake)
    monkeypatch.setattr(communication, "RAVEnSerialDevice", factory)
    client = RavenClient("/dev/ttyACM0")

    snapshot = await client.async_refresh((METER_MAC,))

    assert snapshot.signal_strength is None
    assert snapshot_field_key("signal_strength") not in snapshot.present_fields
    assert len(factory.calls) == 1
    await client.async_shutdown()


async def test_refresh_records_exact_field_presence_and_requires_price_currency(
    fast_lifecycle_timeouts: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = PollingRavenDevice()
    _prepare_polling_connection(fake)
    _add_polling_cycle(fake, received=None, currency=None)
    monkeypatch.setattr(communication, "RAVEnSerialDevice", FakeRavenFactory(fake))
    client = RavenClient("/dev/ttyACM0")

    snapshot = await client.async_refresh((METER_MAC,))

    meter_key = METER_MAC.hex()
    assert snapshot.present_fields == frozenset(
        {
            f"meter:{meter_key}:delivered",
            f"meter:{meter_key}:demand",
            "device:signal_strength",
        }
    )
    assert snapshot.meters[meter_key] == MeterSnapshot(2500, 12.0, None, None, None)
    await client.async_shutdown()


def test_cycle_budget_scales_with_command_count_and_open_work() -> None:
    assert communication.cycle_budget(1, needs_open=True) == 49.25
    assert communication.cycle_budget(2, needs_open=True) == 64.25
    assert communication.cycle_budget(2, needs_open=False) == 38.0


def test_protocol_timeouts_cover_emu_response_window() -> None:
    """EMU-2 responses can take four seconds, including during setup."""

    assert communication.METER_LIST_TIMEOUT >= 5.0
    assert communication.QUERY_TIMEOUT >= 5.0


async def test_refresh_outer_watchdog_bounds_each_attempt(
    fast_lifecycle_timeouts: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = PollingRavenDevice()
    second = PollingRavenDevice()
    for fake in (first, second):
        _prepare_polling_connection(fake)
        _add_polling_cycle(fake)
        fake.summation_results.clear()
        fake.summation_results.append(asyncio.Future())
    monkeypatch.setattr(communication, "QUERY_TIMEOUT", 1.0)
    monkeypatch.setattr(communication, "cycle_budget", lambda *_args, **_kw: 0.01)
    monkeypatch.setattr(
        communication, "RAVEnSerialDevice", FakeRavenFactory(first, second)
    )
    client = RavenClient("/dev/ttyACM0")

    with pytest.raises(RainforestCommunicationError) as raised:
        await asyncio.wait_for(client.async_refresh((METER_MAC,)), timeout=0.2)

    assert raised.value.stage is FailureStage.POLL
    assert raised.value.reason is FailureReason.TIMEOUT
    assert first.calls[-1] == second.calls[-1] == "abort"
    await client.async_shutdown()


async def test_concurrent_refreshes_never_overlap_protocol_commands(
    fast_lifecycle_timeouts: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = PollingRavenDevice()
    _prepare_polling_connection(fake)
    _add_polling_cycle(fake)
    _add_polling_cycle(fake, demand="3.5")
    monkeypatch.setattr(communication, "RAVEnSerialDevice", FakeRavenFactory(fake))
    client = RavenClient("/dev/ttyACM0")

    first, second = await asyncio.gather(
        client.async_refresh((METER_MAC,)), client.async_refresh((METER_MAC,))
    )

    assert first.meters[METER_MAC.hex()].demand == 2500
    assert second.meters[METER_MAC.hex()].demand == 3500
    assert fake.max_active_commands == 1
    assert fake.calls.count("open") == 1
    await client.async_shutdown()


async def test_refresh_holds_connection_and_port_lease_until_shutdown(
    fast_lifecycle_timeouts: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persistent = PollingRavenDevice()
    _prepare_polling_connection(persistent)
    _add_polling_cycle(persistent)
    after_shutdown = FakeRavenDevice()
    _successful_fake(after_shutdown)
    factory = FakeRavenFactory(persistent, after_shutdown)
    monkeypatch.setattr(communication, "RAVEnSerialDevice", factory)
    registry = PortLockRegistry()
    client = RavenClient("/dev/ttyACM0", registry=registry)

    await client.async_refresh((METER_MAC,))

    assert "close" not in persistent.calls
    with pytest.raises(RainforestCommunicationError) as raised:
        await RavenClient("/dev/ttyACM0", registry=registry).async_validate()
    assert raised.value.stage is FailureStage.LOCK
    assert raised.value.reason is FailureReason.BUSY
    assert len(factory.calls) == 1

    await client.async_shutdown()
    assert persistent.calls[-1] == "close"
    result = await RavenClient("/dev/ttyACM0", registry=registry).async_validate()
    assert result.device_mac == DEVICE_MAC.hex()


async def test_set_path_closes_old_connection_before_next_refresh(
    fast_lifecycle_timeouts: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = PollingRavenDevice()
    second = PollingRavenDevice()
    for fake in (first, second):
        _prepare_polling_connection(fake)
        _add_polling_cycle(fake)
    factory = FakeRavenFactory(first, second)
    monkeypatch.setattr(communication, "RAVEnSerialDevice", factory)
    client = RavenClient("/dev/ttyACM0")
    await client.async_refresh((METER_MAC,))

    await client.async_set_path("/dev/ttyUSB0")
    snapshot = await client.async_refresh((METER_MAC,))

    assert first.calls[-1] == "close"
    assert factory.calls[-1][0] == "/dev/ttyUSB0"
    assert snapshot.meters[METER_MAC.hex()].demand == 2500
    await client.async_shutdown()


async def test_reconfigure_success_closes_current_and_leaves_candidate_closed(
    fast_lifecycle_timeouts: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = PollingRavenDevice()
    _prepare_polling_connection(current)
    _add_polling_cycle(current)
    candidate = FakeRavenDevice()
    _successful_fake(candidate)
    factory = FakeRavenFactory(current, candidate)
    monkeypatch.setattr(communication, "RAVEnSerialDevice", factory)
    client = RavenClient("/dev/ttyACM0")
    await client.async_refresh((METER_MAC,))

    result = await client.async_reconfigure_path("/dev/ttyUSB0")

    assert current.calls[-1] == "close"
    assert candidate.calls[0] == "open"
    assert candidate.calls[-1] == "close"
    assert result.path == "/dev/ttyUSB0"
    assert client.path == "/dev/ttyUSB0"
    assert client._device is None


async def test_restore_path_reopens_original_transport_after_reconfigure(
    fast_lifecycle_timeouts: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected otherwise-valid candidate can return to the original transport."""
    current = PollingRavenDevice()
    _prepare_polling_connection(current)
    _add_polling_cycle(current)
    candidate = FakeRavenDevice()
    _successful_fake(candidate)
    restored = PollingRavenDevice()
    _prepare_polling_connection(restored)
    factory = FakeRavenFactory(current, candidate, restored)
    monkeypatch.setattr(communication, "RAVEnSerialDevice", factory)
    client = RavenClient("/dev/ttyACM0")
    await client.async_refresh((METER_MAC,))

    await client.async_reconfigure_path("/dev/ttyUSB0")
    await client.async_restore_path("/dev/ttyACM0")

    assert client.path == "/dev/ttyACM0"
    assert client._device is restored
    assert factory.calls[-1][0] == "/dev/ttyACM0"
    await client.async_shutdown()


async def test_reconfigure_failure_restores_original_path_connection_and_metadata(
    fast_lifecycle_timeouts: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = PollingRavenDevice()
    _prepare_polling_connection(current)
    _add_polling_cycle(current)
    candidate = FakeRavenDevice()
    candidate.meter_list_results.append(
        MeterList(bytes.fromhex("0013500000000099"), [METER_MAC])
    )
    candidate.device_info = _device_info(mac=bytes.fromhex("0013500000000099"))
    candidate.meter_infos[METER_MAC] = OSError(errno.EIO, "candidate")
    restored = PollingRavenDevice()
    _prepare_polling_connection(restored)
    factory = FakeRavenFactory(current, candidate, restored)
    monkeypatch.setattr(communication, "RAVEnSerialDevice", factory)
    client = RavenClient("/dev/ttyACM0")
    await client.async_refresh((METER_MAC,))
    original_info = client.device_info

    with pytest.raises(RainforestCommunicationError) as raised:
        await client.async_reconfigure_path("/dev/ttyUSB0")

    assert raised.value.stage is FailureStage.METER_INFO
    assert client.path == "/dev/ttyACM0"
    assert client._device is restored
    assert restored.calls[:3] == ["open", "meter_list", "device_info"]
    assert client.device_info is original_info
    await client.async_shutdown()


async def test_reconfigure_restoration_failure_does_not_mask_candidate_error(
    fast_lifecycle_timeouts: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = PollingRavenDevice()
    _prepare_polling_connection(current)
    _add_polling_cycle(current)
    candidate = FakeRavenDevice()
    candidate.meter_list_results.extend([TimeoutError()] * 3)
    restore_failure = PermissionError(errno.EACCES, "original path secret")
    factory = FakeRavenFactory(current, candidate, restore_failure)
    monkeypatch.setattr(communication, "RAVEnSerialDevice", factory)
    client = RavenClient("/dev/ttyACM0")
    await client.async_refresh((METER_MAC,))

    with pytest.raises(RainforestCommunicationError) as raised:
        await client.async_reconfigure_path("/dev/ttyUSB0")

    assert raised.value.stage is FailureStage.SYNC
    assert raised.value.reason is FailureReason.TIMEOUT
    assert client.path == "/dev/ttyACM0"
    assert client._device is None
    await client.async_shutdown()


async def _wait_for_call(fake: FakeRavenDevice, call: str) -> None:
    """Yield until a fake reaches one deterministic protocol boundary."""

    for _ in range(20):
        if call in fake.calls:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"Timed out waiting for {call}")


@pytest.mark.parametrize("operation", ["shutdown", "set_path", "reconfigure"])
async def test_cancellation_during_close_discards_transport_before_reuse(
    operation: str,
    fast_lifecycle_timeouts: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = PollingRavenDevice()
    _prepare_polling_connection(first)
    _add_polling_cycle(first)
    second = PollingRavenDevice()
    _prepare_polling_connection(second)
    _add_polling_cycle(second, demand="3.5")
    factory = FakeRavenFactory(first, second)
    monkeypatch.setattr(communication, "RAVEnSerialDevice", factory)
    client = RavenClient("/dev/ttyACM0")
    await client.async_refresh((METER_MAC,))
    first.close_result = asyncio.Future()

    if operation == "shutdown":
        closing = asyncio.create_task(client.async_shutdown())
    elif operation == "set_path":
        closing = asyncio.create_task(client.async_set_path("/dev/ttyUSB0"))
    else:
        closing = asyncio.create_task(client.async_reconfigure_path("/dev/ttyUSB0"))
    await _wait_for_call(first, "close")
    closing.cancel()

    with pytest.raises(asyncio.CancelledError):
        await closing

    assert first.calls[-1] == "abort"
    assert client._device is None
    assert client.path == "/dev/ttyACM0"

    snapshot = await client.async_refresh((METER_MAC,))

    assert snapshot.meters[METER_MAC.hex()].demand == 3500
    assert factory.calls[-1][0] == "/dev/ttyACM0"
    await client.async_shutdown()


async def test_refresh_waits_for_inflight_validation_transport(
    fast_lifecycle_timeouts: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validating = FakeRavenDevice()
    meter_list_result: asyncio.Future[MeterList] = asyncio.Future()
    validating.meter_list_results.append(meter_list_result)
    validating.device_info = _device_info()
    validating.meter_infos[METER_MAC] = _meter_info()
    polling = PollingRavenDevice()
    _prepare_polling_connection(polling)
    _add_polling_cycle(polling)
    monkeypatch.setattr(
        communication, "RAVEnSerialDevice", FakeRavenFactory(validating, polling)
    )
    client = RavenClient("/dev/ttyACM0")

    validating_task = asyncio.create_task(client.async_validate())
    await _wait_for_call(validating, "meter_list")
    refresh_task = asyncio.create_task(client.async_refresh((METER_MAC,)))
    await asyncio.sleep(0)

    assert not refresh_task.done()
    assert validating.calls == ["open", "meter_list"]

    meter_list_result.set_result(MeterList(DEVICE_MAC, [METER_MAC]))
    await validating_task
    snapshot = await refresh_task

    assert snapshot.meters[METER_MAC.hex()].demand == 2500
    await client.async_shutdown()


async def test_reconfigure_never_publishes_failed_candidate_metadata(
    fast_lifecycle_timeouts: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = PollingRavenDevice()
    _prepare_polling_connection(current)
    _add_polling_cycle(current)
    candidate = FakeRavenDevice()
    candidate_mac = bytes.fromhex("0013500000000099")
    candidate.meter_list_results.append(MeterList(candidate_mac, [METER_MAC]))
    candidate.device_info = _device_info(mac=candidate_mac)
    candidate.meter_infos[METER_MAC] = asyncio.Future()
    restored = PollingRavenDevice()
    _prepare_polling_connection(restored)
    monkeypatch.setattr(
        communication,
        "RAVEnSerialDevice",
        FakeRavenFactory(current, candidate, restored),
    )
    client = RavenClient("/dev/ttyACM0")
    await client.async_refresh((METER_MAC,))
    original_info = client.device_info

    reconfigure = asyncio.create_task(client.async_reconfigure_path("/dev/ttyUSB0"))
    await _wait_for_call(candidate, f"meter_info:{METER_MAC.hex()}")

    assert client.device_info is original_info

    candidate.meter_infos[METER_MAC].set_exception(OSError(errno.EIO, "candidate"))
    with pytest.raises(RainforestCommunicationError) as raised:
        await reconfigure

    assert raised.value.stage is FailureStage.METER_INFO
    assert client.device_info is original_info
    await client.async_shutdown()
