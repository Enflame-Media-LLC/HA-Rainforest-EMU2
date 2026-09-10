"""Polling coordinator and runtime data for Rainforest devices."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .communication import (
    RainforestCommunicationError,
    RavenClient,
    RavenSnapshot,
    ValidationResult,
)
from .const import DOMAIN, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)


class RainforestUpdateFailed(UpdateFailed):
    """Privacy-safe refresh failure retaining only retryability."""

    def __init__(self, error: RainforestCommunicationError) -> None:
        super().__init__(str(error))
        self.retryable = error.retryable


class RainforestCoordinator(DataUpdateCoordinator[RavenSnapshot]):
    """Fetch one complete, selected-meter Rainforest snapshot at a time."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: RavenClient,
        meter_macs: tuple[bytes, ...],
        *,
        config_entry: ConfigEntry | None = None,
    ) -> None:
        """Initialize a coordinator tied to its owning communication client."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client
        self.meter_macs = meter_macs

    async def _async_update_data(self) -> RavenSnapshot:
        """Return only a fully successful refresh cycle."""
        try:
            return await self.client.async_refresh(self.meter_macs)
        except RainforestCommunicationError as err:
            raise RainforestUpdateFailed(err) from None


@dataclass(slots=True)
class RainforestRuntimeData:
    """Objects owned by one loaded Rainforest config entry."""

    client: RavenClient
    coordinator: RainforestCoordinator
    validation: ValidationResult
