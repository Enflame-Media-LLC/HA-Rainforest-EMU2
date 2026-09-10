"""Tests for Rainforest EMU-2 sensors."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components.rainforest_emu2.communication import (
    MeterRecord,
    MeterSnapshot,
    RavenSnapshot,
    ValidationResult,
    snapshot_field_key,
)
from custom_components.rainforest_emu2.coordinator import RainforestRuntimeData

DEVICE_MAC = "0011223344556677"
METER_MAC = "8899aabbccddeeff"


def _runtime(*, present: frozenset[str], success: bool = True):
    snapshot = RavenSnapshot(
        meters={
            METER_MAC: MeterSnapshot(
                demand=123.0,
                delivered=456.5,
                received=7.25,
                price=0.135,
                currency="USD",
            )
        },
        signal_strength=71,
        present_fields=present,
    )
    coordinator = Mock()
    coordinator.data = snapshot
    coordinator.last_update_success = success
    coordinator.config_entry = SimpleNamespace(unique_id=DEVICE_MAC)
    coordinator.async_add_listener.return_value = lambda: None
    validation = ValidationResult(
        path="/dev/ttyUSB0",
        device_mac=DEVICE_MAC,
        manufacturer="Rainforest Automation",
        model="EMU-2",
        firmware="1.2.3",
        meters=(
            MeterRecord(
                mac=bytes.fromhex(METER_MAC),
                mac_hex=METER_MAC,
                name="Main Meter",
                meter_type="electric",
            ),
        ),
    )
    return RainforestRuntimeData(Mock(), coordinator, validation)


@pytest.mark.asyncio
async def test_sensor_descriptions_values_devices_and_unique_ids() -> None:
    """Entities expose exact HA semantics and full private hardware identities."""
    from custom_components.rainforest_emu2.sensor import async_setup_entry

    fields = frozenset(
        {
            snapshot_field_key(key, METER_MAC)
            for key in ("demand", "delivered", "received", "price")
        }
        | {snapshot_field_key("signal_strength")}
    )
    entry = SimpleNamespace(runtime_data=_runtime(present=fields), entry_id="custom")
    entities = []
    await async_setup_entry(None, entry, entities.extend)

    assert len(entities) == 5
    by_key = {entity.entity_description.key: entity for entity in entities}
    demand = by_key["demand"]
    assert demand.native_value == 123.0
    assert demand.native_unit_of_measurement == "W"
    assert demand.device_class == "power"
    assert demand.state_class == "measurement"
    assert demand.unique_id == f"{METER_MAC}_demand"
    assert demand.device_info["name"] == "Main Meter"
    assert demand.device_info["identifiers"] == {("rainforest_emu2", METER_MAC)}
    assert demand.device_info["via_device"] == ("rainforest_emu2", DEVICE_MAC)

    for key, value in (("delivered", 456.5), ("received", 7.25)):
        entity = by_key[key]
        assert entity.native_value == value
        assert entity.native_unit_of_measurement == "kWh"
        assert entity.device_class == "energy"
        assert entity.state_class == "total_increasing"

    price = by_key["price"]
    assert price.native_unit_of_measurement == "USD"
    assert price.device_class == "monetary"
    assert price.unique_id == f"{METER_MAC}_price"

    signal = by_key["signal_strength"]
    assert signal.native_value == 71
    assert signal.native_unit_of_measurement == "%"
    assert signal.state_class == "measurement"
    assert signal.unique_id == f"{DEVICE_MAC}_signal_strength"
    assert signal.device_info["identifiers"] == {("rainforest_emu2", DEVICE_MAC)}


@pytest.mark.asyncio
async def test_field_and_coordinator_availability() -> None:
    """Missing, stale, and failed values are never reported as available."""
    from custom_components.rainforest_emu2.sensor import async_setup_entry

    runtime = _runtime(present=frozenset({snapshot_field_key("demand", METER_MAC)}))
    entry = SimpleNamespace(runtime_data=runtime, entry_id="custom")
    entities = []
    await async_setup_entry(None, entry, entities.extend)
    by_key = {entity.entity_description.key: entity for entity in entities}

    assert by_key["demand"].available
    assert not by_key["delivered"].available
    assert not by_key["received"].available
    assert not by_key["price"].available
    assert not by_key["signal_strength"].available

    runtime.coordinator.last_update_success = False
    assert all(not entity.available for entity in entities)


@pytest.mark.asyncio
async def test_missing_meter_snapshot_is_safe_and_unavailable() -> None:
    """A partial cycle cannot crash an entity while it is unavailable."""
    from custom_components.rainforest_emu2.sensor import async_setup_entry

    runtime = _runtime(present=frozenset())
    runtime.coordinator.data = RavenSnapshot(
        meters={}, signal_strength=None, present_fields=frozenset()
    )
    entry = SimpleNamespace(runtime_data=runtime, entry_id="custom")
    entities = []
    await async_setup_entry(None, entry, entities.extend)

    for entity in entities:
        assert entity.native_value is None
        assert not entity.available
