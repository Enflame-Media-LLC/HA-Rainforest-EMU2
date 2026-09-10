"""Sensor entities for Rainforest EMU-2 and RAVEn Enhanced devices."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .communication import MeterRecord, MeterSnapshot, snapshot_field_key
from .const import DOMAIN
from .coordinator import RainforestCoordinator, RainforestRuntimeData


@dataclass(frozen=True, kw_only=True)
class RainforestSensorEntityDescription(SensorEntityDescription):
    """Describe an immutable Rainforest sensor value."""

    value_fn: Callable[[MeterSnapshot], StateType]


METER_SENSORS = (
    RainforestSensorEntityDescription(
        key="demand",
        translation_key="power_demand",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda snapshot: snapshot.demand,
    ),
    RainforestSensorEntityDescription(
        key="delivered",
        translation_key="energy_delivered",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda snapshot: snapshot.delivered,
    ),
    RainforestSensorEntityDescription(
        key="received",
        translation_key="energy_received",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda snapshot: snapshot.received,
    ),
    RainforestSensorEntityDescription(
        key="price",
        translation_key="energy_price",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda snapshot: snapshot.price,
    ),
)

SIGNAL_SENSOR = SensorEntityDescription(
    key="signal_strength",
    translation_key="signal_strength",
    native_unit_of_measurement=PERCENTAGE,
    state_class=SensorStateClass.MEASUREMENT,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create sensors from the validated runtime owned by this entry."""
    runtime: RainforestRuntimeData = entry.runtime_data
    entities: list[RainforestSensor] = [RainforestSignalSensor(runtime)]
    for meter in runtime.validation.meters:
        entities.extend(
            RainforestMeterSensor(runtime.coordinator, meter, description)
            for description in METER_SENSORS
        )
    async_add_entities(entities)


class RainforestSensor(CoordinatorEntity[RainforestCoordinator], SensorEntity):
    """Base for a field-aware coordinator sensor."""

    _attr_has_entity_name = True
    _meter_mac_hex: str | None = None

    @property
    def available(self) -> bool:
        """Require both a current cycle and explicit field presence."""
        field = snapshot_field_key(self.entity_description.key, self._meter_mac_hex)
        return super().available and field in self.coordinator.data.present_fields


class RainforestMeterSensor(RainforestSensor):
    """A value associated with one validated smart meter."""

    entity_description: RainforestSensorEntityDescription
    _meter_mac_hex: str

    def __init__(
        self,
        coordinator: RainforestCoordinator,
        meter: MeterRecord,
        description: RainforestSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._meter_mac_hex = meter.mac_hex
        self._attr_unique_id = f"{meter.mac_hex}_{description.key}"
        entry = coordinator.config_entry
        if entry is None or entry.unique_id is None:
            raise ValueError("Rainforest meter requires a validated gateway identity")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, meter.mac_hex)},
            name=meter.name,
            via_device=(DOMAIN, entry.unique_id),
        )

    @property
    def native_value(self) -> StateType:
        """Return the current native meter value."""
        snapshot = self.coordinator.data.meters.get(self._meter_mac_hex)
        if snapshot is None:
            return None
        return self.entity_description.value_fn(snapshot)

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the validated currency rate unit for the price field."""
        if self.entity_description.key == "price":
            snapshot = self.coordinator.data.meters.get(self._meter_mac_hex)
            currency = snapshot.currency if snapshot is not None else None
            return currency
        return self.entity_description.native_unit_of_measurement

    @property
    def available(self) -> bool:
        """Require currency as part of a usable monetary value."""
        if self.entity_description.key == "price":
            snapshot = self.coordinator.data.meters.get(self._meter_mac_hex)
            if snapshot is None or not snapshot.currency:
                return False
        return super().available


class RainforestSignalSensor(RainforestSensor):
    """Signal strength associated with the Rainforest USB gateway."""

    def __init__(self, runtime: RainforestRuntimeData) -> None:
        super().__init__(runtime.coordinator)
        self.entity_description = SIGNAL_SENSOR
        validation = runtime.validation
        self._attr_unique_id = f"{validation.device_mac}_signal_strength"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, validation.device_mac)},
            name="Rainforest",
            manufacturer=validation.manufacturer,
            model=validation.model,
            sw_version=validation.firmware,
        )

    @property
    def native_value(self) -> int | None:
        """Return gateway link strength as a percentage."""
        return self.coordinator.data.signal_strength
