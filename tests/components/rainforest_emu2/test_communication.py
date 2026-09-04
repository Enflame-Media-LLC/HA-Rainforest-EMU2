from __future__ import annotations

import os

import pytest

from custom_components.rainforest_emu2.communication import (
    FailureReason,
    FailureStage,
    MeterRecord,
    MeterSnapshot,
    PortCandidate,
    PortLockRegistry,
    RainforestCommunicationError,
    RavenSnapshot,
    ValidationResult,
    safe_path_details,
    snapshot_field_key,
)


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
