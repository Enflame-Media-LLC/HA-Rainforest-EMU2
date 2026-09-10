"""Tests for privacy-safe Rainforest diagnostics."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from custom_components.rainforest_emu2.communication import (
    FailureReason,
    FailureStage,
    MeterRecord,
    MeterSnapshot,
    RavenSnapshot,
    ValidationResult,
    snapshot_field_key,
)

DEVICE_MAC = "0013500000000001"
METER_MAC = "0013500102030405"
USB_SERIAL = "USB-SERIAL-PRIVATE"
FULL_BY_ID_PATH = "/dev/serial/by-id/usb-Rainforest_EMU-2_USB-SERIAL-PRIVATE-if00"


def _runtime() -> SimpleNamespace:
    validation = ValidationResult(
        path=FULL_BY_ID_PATH,
        device_mac=DEVICE_MAC,
        manufacturer="Rainforest Automation",
        model="EMU-2",
        firmware="1.2.3",
        meters=(
            MeterRecord(bytes.fromhex(METER_MAC), METER_MAC, "Main meter", "electric"),
        ),
    )
    snapshot = RavenSnapshot(
        meters={
            METER_MAC: MeterSnapshot(12.0, 1.0, 0.0, 0.15, "USD"),
        },
        signal_strength=80,
        present_fields=frozenset(
            {
                snapshot_field_key("demand", METER_MAC),
                snapshot_field_key("signal_strength"),
            }
        ),
    )
    client = SimpleNamespace(
        path=FULL_BY_ID_PATH,
        timeout_count=2,
        reconnect_count=3,
        last_success=datetime(2026, 9, 6, tzinfo=UTC),
        last_error_stage=FailureStage.POLL,
        last_error_reason=FailureReason.TIMEOUT,
    )
    coordinator = SimpleNamespace(data=snapshot, last_update_success=True)
    return SimpleNamespace(
        client=client, validation=validation, coordinator=coordinator
    )


@pytest.mark.asyncio
async def test_diagnostics_redact_private_identifiers() -> None:
    """Diagnostics expose useful state without device or transport secrets."""
    from custom_components.rainforest_emu2.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    payload = await async_get_config_entry_diagnostics(
        None, SimpleNamespace(runtime_data=_runtime())
    )
    rendered = json.dumps(payload)
    for secret in (DEVICE_MAC, METER_MAC, USB_SERIAL, FULL_BY_ID_PATH):
        assert secret not in rendered

    assert payload["connection"]["path_strategy"] == "by_id"
    assert payload["connection"]["path_basename"] is None
    assert payload["connection"]["timeout_count"] == 2
    assert payload["connection"]["reconnect_count"] == 3
    assert payload["connection"]["last_error"] == {
        "stage": "poll",
        "reason": "timeout",
    }
    assert payload["coordinator"]["present_fields"] == [
        "device:signal_strength",
        "meter:0:demand",
    ]
    # Meter nicknames are user-provided and may contain account or household
    # identifiers, so diagnostics expose only an indexed, allowlisted type.
    assert payload["meters"] == [{"index": 0, "type": "electric"}]


@pytest.mark.asyncio
async def test_diagnostics_for_unloaded_entry_is_safe() -> None:
    """Diagnostics remain serializable while an entry is unloaded."""
    from custom_components.rainforest_emu2.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    payload = await async_get_config_entry_diagnostics(
        None, SimpleNamespace(runtime_data=None)
    )

    assert payload["status"] == "not_loaded"
    assert payload["integration"]["domain"] == "rainforest_emu2"


@pytest.mark.parametrize(
    "secret",
    [
        DEVICE_MAC,
        METER_MAC,
        USB_SERIAL,
        "account-123456",
        "install-abcdef",
        "link-token-secret",
        "private household",
    ],
)
async def test_diagnostics_omit_embedded_untrusted_text(secret):
    """Protocol text and field names cannot smuggle private identifiers."""
    from dataclasses import replace

    from custom_components.rainforest_emu2.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    runtime = _runtime()
    text = f"EMU-2 {secret}"
    runtime.validation = replace(
        runtime.validation,
        model=text,
        firmware=text,
        manufacturer=text,
        meters=(replace(runtime.validation.meters[0], name=text, meter_type=text),),
    )
    runtime.coordinator.data = replace(
        runtime.coordinator.data,
        present_fields=frozenset({f"device:{secret}", f"meter:{METER_MAC}:{secret}"}),
    )
    payload = await async_get_config_entry_diagnostics(
        None, SimpleNamespace(runtime_data=runtime)
    )
    assert secret not in json.dumps(payload)
    assert "name" not in payload["meters"][0]
