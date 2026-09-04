from __future__ import annotations

import asyncio
import errno
import os

import pytest
from aioraven.data import DeviceInfo, MeterInfo, MeterList, MeterType

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
