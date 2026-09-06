"""Privacy-safe diagnostics for the Rainforest integration."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .communication import FailureReason, FailureStage, RavenSnapshot, safe_path_details
from .const import DOMAIN, VERSION


def _safe_timestamp(value: object) -> str | None:
    """Serialize only an actual timestamp, never arbitrary object text."""

    if not isinstance(value, datetime):
        return None
    return value.isoformat()


def _allowlisted_text(value: object, allowed: frozenset[str]) -> str | None:
    """Export only known public literals, never arbitrary protocol text."""

    return value if isinstance(value, str) and value in allowed else None


def _safe_present_fields(
    snapshot: RavenSnapshot, meter_macs: Iterable[str]
) -> list[str]:
    """Replace private meter identities in field keys with stable indexes."""

    indexes = {mac: index for index, mac in enumerate(meter_macs)}
    result: list[str] = []
    for field in snapshot.present_fields:
        parts = field.split(":", 2)
        if parts == ["device", "signal_strength"]:
            result.append(f"device:{parts[1]}")
        elif len(parts) == 3 and parts[0] == "meter":
            index = indexes.get(parts[1])
            if index is not None and parts[2] in {
                "demand",
                "delivered",
                "received",
                "price",
            }:
                result.append(f"meter:{index}:{parts[2]}")
    return sorted(result)


def _safe_error(client: Any) -> dict[str, str] | None:
    """Return stable failure categories without exposing exception causes."""

    stage = getattr(client, "last_error_stage", None)
    reason = getattr(client, "last_error_reason", None)
    if not isinstance(stage, FailureStage) or not isinstance(reason, FailureReason):
        return None
    stage_value = getattr(stage, "value", None)
    reason_value = getattr(reason, "value", None)
    if not isinstance(stage_value, str) or not isinstance(reason_value, str):
        return None
    return {"stage": stage_value, "reason": reason_value}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics with all device and account identifiers removed."""

    del hass
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return {
            "integration": {"domain": DOMAIN, "version": VERSION},
            "status": "not_loaded",
        }

    client = runtime.client
    validation = runtime.validation
    path = getattr(client, "path", validation.path)
    path_details = safe_path_details(path)
    meter_macs = [meter.mac_hex for meter in validation.meters]
    snapshot = getattr(runtime.coordinator, "data", None)
    fields = (
        _safe_present_fields(snapshot, meter_macs)
        if isinstance(snapshot, RavenSnapshot)
        else []
    )
    coordinator = runtime.coordinator
    return {
        "integration": {"domain": DOMAIN, "version": VERSION},
        "connection": {
            "path_strategy": path_details["strategy"],
            "path_basename": path_details["basename"],
            "model": _allowlisted_text(
                validation.model, frozenset({"EMU-2", "RAVEn", "RAVEn Enhanced"})
            ),
            # Arbitrary firmware strings can be account/link tokens. Omit until
            # a verified set of public firmware identifiers is available.
            "firmware": None,
            "manufacturer": _allowlisted_text(
                validation.manufacturer,
                frozenset({"Rainforest", "Rainforest Automation"}),
            ),
            "selected_meter_count": len(validation.meters),
            "last_success": _safe_timestamp(getattr(client, "last_success", None)),
            "reconnect_count": _safe_int(getattr(client, "reconnect_count", 0)),
            "timeout_count": _safe_int(getattr(client, "timeout_count", 0)),
            "last_error": _safe_error(client),
        },
        "coordinator": {
            "last_update_success": bool(
                getattr(coordinator, "last_update_success", False)
            ),
            "present_fields": fields,
        },
        "meters": [
            {
                "index": index,
                "type": _allowlisted_text(meter.meter_type, frozenset({"electric"})),
            }
            for index, meter in enumerate(validation.meters)
        ],
    }


def _safe_int(value: object) -> int:
    """Keep counters numeric and non-negative in diagnostics."""

    return value if isinstance(value, int) and value >= 0 else 0
