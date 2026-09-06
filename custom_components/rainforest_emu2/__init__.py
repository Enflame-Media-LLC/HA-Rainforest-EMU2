"""Rainforest EMU-2 and RAVEn Enhanced config-entry lifecycle."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import replace

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady

from .communication import (
    RainforestCommunicationError,
    RavenClient,
    safe_path_details,
)
from .config_flow import async_scan_supported_ports
from .const import (
    CONF_METERS,
    CONF_USB_PID,
    CONF_USB_SERIAL,
    CONF_USB_VID,
    PLATFORMS,
)
from .coordinator import (
    RainforestCoordinator,
    RainforestRuntimeData,
    RainforestUpdateFailed,
)

_LOGGER = logging.getLogger(__name__)


def _normalized_device_id(value: object) -> str | None:
    """Normalize an EUI-64 without rendering it outside internal comparisons."""
    if not isinstance(value, str):
        return None
    compact = re.sub(r"[:-]", "", value).lower()
    if len(compact) != 16 or any(
        character not in "0123456789abcdef" for character in compact
    ):
        return None
    return compact


def _selected_meter_macs(entry: ConfigEntry) -> tuple[bytes, ...]:
    """Decode the persisted selected meters or require reconfiguration."""
    selected = entry.data.get(CONF_METERS)
    if not isinstance(selected, list) or not selected:
        raise ConfigEntryError("Rainforest configuration requires reconfigure")

    meter_macs: list[bytes] = []
    for meter in selected:
        if (
            not isinstance(meter, str)
            or re.fullmatch(r"[0-9a-fA-F]{16}", meter) is None
        ):
            raise ConfigEntryError("Rainforest configuration requires reconfigure")
        meter_mac = bytes.fromhex(meter)
        if meter_mac in meter_macs:
            raise ConfigEntryError("Rainforest configuration requires reconfigure")
        meter_macs.append(meter_mac)
    return tuple(meter_macs)


def _path_repair_metadata(entry: ConfigEntry) -> tuple[int, int, str] | None:
    """Return exact persisted USB identity only when it is safe to use."""
    vid = entry.data.get(CONF_USB_VID)
    pid = entry.data.get(CONF_USB_PID)
    serial = entry.data.get(CONF_USB_SERIAL)
    if (
        isinstance(vid, bool)
        or not isinstance(vid, int)
        or isinstance(pid, bool)
        or not isinstance(pid, int)
        or not isinstance(serial, str)
        or not serial.strip()
    ):
        return None
    return vid, pid, serial


async def _async_repair_missing_raw_path(
    hass: HomeAssistant, entry: ConfigEntry, path: str
) -> str:
    """Repair only a missing raw path with one exact USB identity match."""
    if safe_path_details(path)[
        "strategy"
    ] != "raw" or await hass.async_add_executor_job(os.path.exists, path):
        return path
    metadata = _path_repair_metadata(entry)
    if metadata is None:
        return path

    vid, pid, serial = metadata
    candidates = await async_scan_supported_ports(hass)
    matches = [
        candidate
        for candidate in candidates
        if candidate.vid == vid
        and candidate.pid == pid
        and candidate.serial_number is not None
        and candidate.serial_number == serial
        and candidate.path
    ]
    if len(matches) != 1:
        return path

    repaired_path = matches[0].path
    updated_data = dict(entry.data)
    updated_data["device"] = repaired_path
    hass.config_entries.async_update_entry(entry, data=updated_data)
    return repaired_path


def _config_entry_failure(error: RainforestCommunicationError) -> Exception:
    """Map structured startup errors without exposing their original cause."""
    if error.retryable:
        return ConfigEntryNotReady(str(error))
    return ConfigEntryError("Rainforest configuration requires reconfigure")


def _is_permanent_refresh_error(error: ConfigEntryNotReady) -> bool:
    """Identify a permanent structured error wrapped by first-refresh handling."""
    update_error = error.__cause__
    return (
        isinstance(update_error, RainforestUpdateFailed) and not update_error.retryable
    )


async def _async_finish_shutdown(client: RavenClient) -> BaseException | None:
    """Wait for owned bounded shutdown even if the caller is cancelled."""

    async def _shutdown_result() -> BaseException | None:
        # Shielded futures can report exceptions to the event loop after caller
        # cancellation. Return failures as data so raw causes never reach HA logs.
        try:
            await client.async_shutdown()
        except BaseException as err:
            return err
        return None

    task = asyncio.create_task(_shutdown_result())
    cancellation_received = False
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise asyncio.CancelledError from None
            cancellation_received = True
            continue
        if cancellation_received:
            raise asyncio.CancelledError from None
        return result


def _log_shutdown_error(error: BaseException) -> None:
    """Log only stable structured fields from a shutdown failure."""
    if isinstance(error, RainforestCommunicationError):
        _LOGGER.warning(
            "Could not close Rainforest client (stage=%s, reason=%s)",
            error.stage.value,
            error.reason.value,
        )
    else:
        _LOGGER.warning("Could not close Rainforest client")


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload an entry after a user reconfiguration update."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one fully validated client before forwarding any platform."""
    client: RavenClient | None = None
    try:
        path = entry.data.get("device")
        if not isinstance(path, str) or not path:
            raise ConfigEntryError("Rainforest configuration requires reconfigure")
        path = await _async_repair_missing_raw_path(hass, entry, path)
        meter_macs = _selected_meter_macs(entry)

        client = RavenClient(path)
        validation = await client.async_validate()
        reported_id = _normalized_device_id(validation.device_mac)
        if reported_id is None or reported_id != _normalized_device_id(entry.unique_id):
            raise ConfigEntryError("Rainforest configuration requires reconfigure")
        available_meters = {meter.mac: meter for meter in validation.meters}
        if any(mac not in available_meters for mac in meter_macs):
            raise ConfigEntryError("Rainforest configuration requires reconfigure")
        validation = replace(
            validation, meters=tuple(available_meters[mac] for mac in meter_macs)
        )

        coordinator = RainforestCoordinator(
            hass, client, meter_macs, config_entry=entry
        )
        entry.runtime_data = RainforestRuntimeData(client, coordinator, validation)
        try:
            await coordinator.async_config_entry_first_refresh()
        except ConfigEntryNotReady as err:
            if _is_permanent_refresh_error(err):
                raise ConfigEntryError(
                    "Rainforest configuration requires reconfigure"
                ) from None
            raise
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException as err:
        entry.runtime_data = None
        if client is not None:
            shutdown_error = await _async_finish_shutdown(client)
            if shutdown_error is not None and not isinstance(
                shutdown_error, asyncio.CancelledError
            ):
                _log_shutdown_error(shutdown_error)
        if isinstance(err, RainforestCommunicationError):
            raise _config_entry_failure(err) from None
        raise

    async def _async_stop(_event: Event) -> None:
        """Release the owning client when Home Assistant stops."""
        shutdown_error = await _async_finish_shutdown(client)
        if shutdown_error is not None:
            if isinstance(shutdown_error, asyncio.CancelledError):
                raise shutdown_error
            _log_shutdown_error(shutdown_error)

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop)
    )
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload platforms before releasing this entry's serial transport."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    runtime_data = getattr(entry, "runtime_data", None)
    if runtime_data is None:
        return True
    shutdown_error = await _async_finish_shutdown(runtime_data.client)
    if shutdown_error is not None:
        if not isinstance(shutdown_error, asyncio.CancelledError):
            _log_shutdown_error(shutdown_error)
            return False
        raise shutdown_error
    entry.runtime_data = None
    return True
