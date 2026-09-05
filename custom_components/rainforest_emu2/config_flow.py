"""Config flow for the Rainforest EMU-2 integration."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections.abc import Iterable, Mapping
from typing import Any, Protocol, cast

import serial.tools.list_ports
import voluptuous as vol
from homeassistant.components import usb
from homeassistant.config_entries import ConfigEntry, ConfigFlow
from homeassistant.const import CONF_DEVICE, CONF_MAC
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.service_info.usb import UsbServiceInfo

from .communication import (
    FailureReason,
    FailureStage,
    PortCandidate,
    RainforestCommunicationError,
    RavenClient,
    ValidationResult,
)
from .const import (
    CONF_METERS,
    CONF_USB_PID,
    CONF_USB_SERIAL,
    CONF_USB_VID,
    DOMAIN,
    SUPPORTED_USB_IDS,
)

CONF_ACTION = "action"
CONF_ADVANCED_PATH = "path"
ACTION_RESCAN = "rescan"
ACTION_ADVANCED = "advanced"
ACTION_SELECT = "select"
ACTION_IMPORT_BUILTIN = "import_builtin"
BUILTIN_DOMAIN = "rainforest_raven"
CONF_ENTRY_ID = "entry_id"

_LOGGER = logging.getLogger(__name__)


class _ReconfigureClient(Protocol):
    """Subset of the runtime client needed before Task 7 defines runtime data."""

    async def async_reconfigure_path(self, path: str) -> ValidationResult:
        """Validate a candidate path while preserving transaction ownership."""

    async def async_restore_path(self, path: str) -> None:
        """Restore the original persistent transport after candidate rejection."""


class _RuntimeData(Protocol):
    """Structural stand-in for Task 7's config-entry runtime dataclass."""

    client: _ReconfigureClient


def _usb_number(value: object) -> int | None:
    """Normalize the string or numeric vendor/product IDs used by USB APIs."""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return None
    try:
        return int(text[2:] if text.startswith("0x") else text, 16)
    except ValueError:
        return None


def _safe_text(value: object) -> str | None:
    """Accept only non-empty text values from USB discovery records."""

    if not isinstance(value, str):
        return None
    return value.strip() or None


def _candidate_from_port(port: object, index: int) -> PortCandidate | None:
    """Convert an HA or PySerial port record into a supported candidate."""

    path = getattr(port, "device", None)
    if not isinstance(path, str) or not path:
        return None
    vid = _usb_number(getattr(port, "vid", None))
    pid = _usb_number(getattr(port, "pid", None))
    if (vid, pid) not in SUPPORTED_USB_IDS:
        return None
    canonical_path = os.path.realpath(path)
    path_hash = hashlib.sha256(canonical_path.encode()).hexdigest()[:12]
    description = _safe_text(getattr(port, "description", None))
    manufacturer = _safe_text(getattr(port, "manufacturer", None))
    label = (
        description
        if isinstance(description, str) and description
        else manufacturer
        if isinstance(manufacturer, str) and manufacturer
        else "Rainforest serial device"
    )
    return PortCandidate(
        token=f"port-{index}-{path_hash}",
        path=path,
        label=label,
        vid=vid,
        pid=pid,
        serial_number=_safe_text(getattr(port, "serial_number", None)),
        manufacturer=manufacturer,
        description=description,
        location=_safe_text(getattr(port, "location", None)),
    )


def _supported_candidates(ports: Iterable[object]) -> tuple[PortCandidate, ...]:
    """Filter records and deduplicate alias paths by their canonical identity."""

    candidates: list[PortCandidate] = []
    seen_paths: set[str] = set()
    for index, port in enumerate(ports):
        candidate = _candidate_from_port(port, index)
        if candidate is None:
            continue
        canonical_path = os.path.realpath(candidate.path)
        if canonical_path in seen_paths:
            continue
        seen_paths.add(canonical_path)
        candidates.append(candidate)
    return tuple(candidates)


def _provisional_identity(candidate: PortCandidate) -> str:
    """Return a privacy-safe stable discovery identity before hardware validation."""

    if candidate.serial_number is not None:
        return f"usb:{candidate.serial_number}"
    return f"path:{_canonical_path_hash(candidate.path)}"


def _canonical_path_hash(path: str) -> str:
    """Return the private, stable matching key for a local serial path."""

    canonical_path = os.path.realpath(path)
    return hashlib.sha256(canonical_path.encode()).hexdigest()[:16]


async def async_scan_supported_ports(hass: Any) -> tuple[PortCandidate, ...]:
    """Return supported serial ports without ever opening one.

    Home Assistant's scanner is authoritative.  PySerial is used only when it
    fails or has no supported result, which keeps the compatibility fallback
    out of the normal setup path.
    """

    try:
        candidates = _supported_candidates(
            await hass.async_add_executor_job(usb.scan_serial_ports)
        )
    except Exception:  # The fallback is deliberately limited to scanner failure.
        candidates = ()
    if candidates:
        return candidates
    ports = await hass.async_add_executor_job(serial.tools.list_ports.comports)
    return _supported_candidates(ports)


def _error_key(error: RainforestCommunicationError) -> str:
    """Map a structured transport failure to a safe translated error key."""

    if error.stage is FailureStage.CLEANUP:
        return "cleanup_failed"
    if error.reason is FailureReason.MISSING:
        return "device_missing"
    if error.reason is FailureReason.PERMISSION:
        return "device_permission"
    if error.reason is FailureReason.BUSY:
        return "device_busy"
    if error.reason is FailureReason.TIMEOUT:
        return (
            "open_timeout" if error.stage is FailureStage.OPEN else "response_timeout"
        )
    if error.reason is FailureReason.NO_RESPONSE:
        return "no_response"
    if error.reason is FailureReason.UNSUPPORTED:
        return "no_supported_meters"
    if error.reason is FailureReason.MALFORMED:
        return "identity_invalid"
    return "unknown"


class RainforestEmu2ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle setup without exposing serial identifiers in UI errors or entry data."""

    VERSION = 1

    def __init__(self) -> None:
        self._ports: dict[str, PortCandidate] = {}
        self._selected_port: PortCandidate | None = None
        self._validation: ValidationResult | None = None
        self._usb_info: UsbServiceInfo | None = None
        self._selected_meters: set[str] = set()
        self._matching_path_hash: str | None = None
        self._matching_usb_serial: str | None = None
        self._imported_meters: set[str] | None = None
        self._import_source_entry_id: str | None = None
        self._reconfigure_entry: ConfigEntry | None = None

    async def _async_refresh_ports(self) -> tuple[PortCandidate, ...]:
        ports = await async_scan_supported_ports(self.hass)
        self._ports = {port.token: port for port in ports}
        return ports

    def _show_user_form(
        self,
        *,
        error: str | None = None,
        no_devices: bool = False,
    ) -> dict[str, Any]:
        options = {token: candidate.label for token, candidate in self._ports.items()}
        actions = [ACTION_SELECT, ACTION_RESCAN, ACTION_ADVANCED]
        if self._builtin_entries():
            actions.append(ACTION_IMPORT_BUILTIN)
        schema: dict[Any, Any] = {
            vol.Required(CONF_ACTION, default=ACTION_SELECT): vol.In(actions)
        }
        if options:
            schema[vol.Optional(CONF_DEVICE)] = vol.In(options)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(schema),
            errors={CONF_DEVICE: error} if error else None,
            description_placeholders={"status": "no_devices" if no_devices else ""},
        )

    def _builtin_entries(self) -> list[ConfigEntry]:
        """Return built-in entries without treating their data as ours to mutate."""

        return self.hass.config_entries.async_entries(
            BUILTIN_DOMAIN,
            include_disabled=True,
        )

    async def async_step_user(
        self, user_input: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Show scanned ports and offer a safe advanced-path option."""

        if user_input is not None:
            action = user_input.get(CONF_ACTION)
            if action == ACTION_ADVANCED:
                return await self.async_step_advanced()
            if action == ACTION_RESCAN:
                ports = await self._async_refresh_ports()
                return self._show_user_form(no_devices=not ports)
            if action == ACTION_IMPORT_BUILTIN:
                return await self.async_step_import_builtin()
            token = user_input.get(CONF_DEVICE)
            if isinstance(token, str) and token in self._ports:
                return await self._async_select_port(self._ports[token])

        ports = await self._async_refresh_ports()
        return self._show_user_form(no_devices=not ports)

    async def async_step_advanced(
        self, user_input: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Accept a local serial path only after explicit user submission."""

        errors: dict[str, str] = {}
        if user_input is not None:
            path = user_input.get(CONF_ADVANCED_PATH)
            if not isinstance(path, str) or not path.strip() or "://" in path:
                errors[CONF_ADVANCED_PATH] = "unknown"
            else:
                return await self._async_select_port(
                    PortCandidate(
                        token="advanced",
                        path=path.strip(),
                        label="Manual serial device",
                        vid=None,
                        pid=None,
                        serial_number=None,
                        manufacturer=None,
                        description=None,
                        location=None,
                    )
                )
        return self.async_show_form(
            step_id="advanced",
            data_schema=vol.Schema({vol.Required(CONF_ADVANCED_PATH): str}),
            errors=errors or None,
        )

    async def async_step_import_builtin(
        self, user_input: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Offer migration defaults only from a built-in entry no longer enabled."""

        entries = {entry.entry_id: entry for entry in self._builtin_entries()}
        if user_input is not None:
            entry_id = user_input.get(CONF_ENTRY_ID)
            entry = entries.get(entry_id) if isinstance(entry_id, str) else None
            if entry is None:
                return self.async_abort(reason="builtin_not_found")
            if entry.disabled_by is None:
                return self.async_abort(reason="builtin_still_enabled")

            path = entry.data.get(CONF_DEVICE)
            self._selected_port = PortCandidate(
                token="builtin-import",
                path=path if isinstance(path, str) else "",
                label="Imported Rainforest device",
                vid=None,
                pid=None,
                serial_number=None,
                manufacturer=None,
                description=None,
                location=None,
            )
            imported_meters = entry.data.get(CONF_MAC, ())
            self._imported_meters = {
                meter.lower()
                for meter in imported_meters
                if isinstance(meter, str)
                and len(meter) == 16
                and all(character in "0123456789abcdefABCDEF" for character in meter)
            }
            self._import_source_entry_id = entry.entry_id
            return await self.async_step_import_confirm()

        if not entries:
            return self.async_abort(reason="builtin_not_found")
        return self.async_show_form(
            step_id="import_builtin",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ENTRY_ID): vol.In(
                        {entry_id: "Rainforest RAVEn entry" for entry_id in entries}
                    )
                }
            ),
        )

    def _import_path_has_enabled_builtin_owner(self, path: str) -> bool:
        """Return whether an enabled built-in entry currently owns a path."""

        source_entry_id = self._import_source_entry_id
        if source_entry_id is not None:
            source = self.hass.config_entries.async_get_entry(source_entry_id)
            if source is not None and source.disabled_by is None:
                return True
        canonical_path = os.path.realpath(path)
        return any(
            entry.disabled_by is None
            and isinstance(entry.data.get(CONF_DEVICE), str)
            and os.path.realpath(entry.data[CONF_DEVICE]) == canonical_path
            for entry in self._builtin_entries()
        )

    def _show_import_confirm_form(
        self, errors: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Show the editable imported path while preserving validation errors."""

        candidate = self._selected_port
        assert candidate is not None
        return self.async_show_form(
            step_id="import_confirm",
            data_schema=vol.Schema(
                {vol.Required(CONF_ADVANCED_PATH, default=candidate.path): str}
            ),
            errors=errors,
        )

    async def async_step_import_confirm(
        self, user_input: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Allow an imported path to be corrected before serial validation."""

        candidate = self._selected_port
        assert candidate is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            path = user_input.get(CONF_ADVANCED_PATH, candidate.path)
            if not isinstance(path, str) or not path.strip() or "://" in path:
                errors[CONF_ADVANCED_PATH] = "unknown"
            else:
                if self._import_path_has_enabled_builtin_owner(path.strip()):
                    return self.async_abort(reason="builtin_still_enabled")
                self._selected_port = PortCandidate(
                    token="builtin-import",
                    path=path.strip(),
                    label="Imported Rainforest device",
                    vid=None,
                    pid=None,
                    serial_number=None,
                    manufacturer=None,
                    description=None,
                    location=None,
                )
                return await self._async_confirm_validation("import_confirm")
        return self._show_import_confirm_form(errors or None)

    async def async_step_usb(self, discovery_info: UsbServiceInfo) -> dict[str, Any]:
        """Stage a USB discovery; validation starts only from confirmation."""

        self._usb_info = discovery_info
        candidate = _candidate_from_port(discovery_info, 0)
        if candidate is None:
            return await self.async_step_user()
        self.context["title_placeholders"] = {"name": "Rainforest EMU-2"}
        return await self._async_select_port(candidate)

    def _candidate_matches_existing_entry(self, candidate: PortCandidate) -> bool:
        """Check persisted safe USB metadata before opening a candidate port."""

        for entry in self._async_current_entries():
            data = entry.data
            if (
                candidate.serial_number is not None
                and candidate.serial_number == _safe_text(data.get(CONF_USB_SERIAL))
            ):
                return True
            stored_path = data.get(CONF_DEVICE)
            if isinstance(stored_path, str) and (
                os.path.realpath(stored_path) == os.path.realpath(candidate.path)
            ):
                return True
        return False

    def is_matching(self, other_flow: RainforestEmu2ConfigFlow) -> bool:
        """Match discovery flows by private port identity or USB serial."""

        return (
            self._matching_path_hash is not None
            and self._matching_path_hash == other_flow._matching_path_hash
        ) or (
            self._matching_usb_serial is not None
            and self._matching_usb_serial == other_flow._matching_usb_serial
        )

    async def _async_select_port(self, candidate: PortCandidate) -> dict[str, Any]:
        """Deduplicate a selected candidate before any serial validation occurs."""

        if self._candidate_matches_existing_entry(candidate):
            return self.async_abort(reason="already_configured")
        self._selected_port = candidate
        self._matching_path_hash = _canonical_path_hash(candidate.path)
        self._matching_usb_serial = candidate.serial_number
        if self.hass.config_entries.flow.async_has_matching_flow(self):
            return self.async_abort(reason="already_in_progress")
        if self._usb_info is not None and candidate.serial_number is None:
            await self._async_handle_discovery_without_unique_id()
            return await self.async_step_confirm()
        await self.async_set_unique_id(_provisional_identity(candidate))
        return await self.async_step_confirm()

    async def _async_validate_selected(self) -> RainforestCommunicationError | None:
        """Validate the staged port and retain only the safe result."""

        if self._selected_port is None:
            return RainforestCommunicationError(
                FailureStage.SCAN, FailureReason.MISSING, retryable=True
            )
        try:
            self._validation = await RavenClient(
                self._selected_port.path
            ).async_validate()
        except RainforestCommunicationError as err:
            return err
        return None

    async def _async_finish_validation(self, step_id: str) -> dict[str, Any]:
        """Retain valid meter defaults without persisting unvalidated data."""

        assert self._validation is not None
        if not self._validation.meters:
            if step_id == "import_confirm":
                return self._show_import_confirm_form({"base": "no_paired_meters"})
            return self.async_show_form(
                step_id=step_id, errors={"base": "no_paired_meters"}
            )
        validated_meters = {meter.mac_hex for meter in self._validation.meters}
        if self._imported_meters is not None:
            self._selected_meters = self._imported_meters & validated_meters
        else:
            self._selected_meters = (
                self._selected_meters & validated_meters
                if self._selected_meters
                else validated_meters
            )
        return await self.async_step_meters()

    async def _async_confirm_validation(self, step_id: str) -> dict[str, Any]:
        """Run normal validation after a flow-specific explicit confirmation."""

        error = await self._async_validate_selected()
        if error is not None:
            if step_id == "import_confirm":
                return self._show_import_confirm_form({"base": _error_key(error)})
            return self.async_show_form(
                step_id=step_id, errors={"base": _error_key(error)}
            )
        return await self._async_finish_validation(step_id)

    async def async_step_confirm(
        self, user_input: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Validate only after the user expressly confirms the selected port."""

        if user_input is None:
            return self.async_show_form(step_id="confirm")
        return await self._async_confirm_validation("confirm")

    async def async_step_reconfigure(
        self, user_input: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Stage a current or replacement path before touching the runtime client."""

        entry = self._get_reconfigure_entry()
        self._reconfigure_entry = entry
        if user_input is not None:
            path = user_input.get(CONF_DEVICE)
            if not isinstance(path, str) or not path.strip() or "://" in path:
                return self.async_show_form(
                    step_id="reconfigure",
                    data_schema=vol.Schema(
                        {
                            vol.Required(
                                CONF_DEVICE,
                                default=entry.data.get(CONF_DEVICE, ""),
                            ): str
                        }
                    ),
                    errors={CONF_DEVICE: "unknown"},
                )
            self._selected_port = PortCandidate(
                token="reconfigure",
                path=path.strip(),
                label="Reconfigured Rainforest device",
                vid=entry.data.get(CONF_USB_VID),
                pid=entry.data.get(CONF_USB_PID),
                serial_number=entry.data.get(CONF_USB_SERIAL),
                manufacturer=None,
                description=None,
                location=None,
            )
            self._selected_meters = {
                meter
                for meter in entry.data.get(CONF_METERS, ())
                if isinstance(meter, str)
            }
            return await self.async_step_reconfigure_confirm()

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DEVICE,
                        default=entry.data.get(CONF_DEVICE, ""),
                    ): str
                }
            ),
        )

    async def _async_reconfigure_client(
        self, entry: ConfigEntry
    ) -> tuple[_ReconfigureClient, bool]:
        """Use a loaded runtime client, or a bounded temporary client when unloaded."""

        runtime = getattr(entry, "runtime_data", None)
        if runtime is not None and hasattr(runtime, "client"):
            return cast(_RuntimeData, runtime).client, False
        path = entry.data.get(CONF_DEVICE)
        if not isinstance(path, str) or not path:
            raise RainforestCommunicationError(
                FailureStage.SCAN,
                FailureReason.MISSING,
                retryable=False,
            )
        return cast(_ReconfigureClient, RavenClient(path)), True

    async def _async_restore_reconfigure_client(
        self,
        client: _ReconfigureClient,
        original_path: str,
        original_error: BaseException,
    ) -> None:
        """Restore a loaded client without allowing rollback to mask the failure."""

        rollback_error = await self._async_wait_for_cleanup(
            client.async_restore_path(original_path)
        )
        if rollback_error is not None:
            original_error.add_note("secondary reconfigure rollback failure")
            if isinstance(rollback_error, RainforestCommunicationError):
                _LOGGER.warning(
                    "Could not restore Rainforest transport after reconfigure "
                    "failure (stage=%s, reason=%s)",
                    rollback_error.stage.value,
                    rollback_error.reason.value,
                )
            else:
                _LOGGER.warning(
                    "Could not restore Rainforest transport after reconfigure failure"
                )

    async def _async_wait_for_cleanup(self, operation: Any) -> BaseException | None:
        """Finish a bounded cleanup task before allowing cancellation to escape."""

        task = asyncio.create_task(operation)
        cancellation_received = False
        while True:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done() and task.cancelled():
                    raise
                cancellation_received = True
                continue
            except BaseException as err:
                if cancellation_received:
                    raise asyncio.CancelledError from None
                return err
            if cancellation_received:
                raise asyncio.CancelledError
            return None

    async def _async_shutdown_temporary_client(
        self, client: _ReconfigureClient
    ) -> None:
        """Bound temporary-client cleanup without hiding an earlier flow error."""

        cleanup_error = await self._async_wait_for_cleanup(
            cast(RavenClient, client).async_shutdown()
        )
        if isinstance(cleanup_error, RainforestCommunicationError):
            _LOGGER.warning(
                "Could not close temporary Rainforest validation client "
                "(stage=%s, reason=%s)",
                cleanup_error.stage.value,
                cleanup_error.reason.value,
            )
        elif cleanup_error is not None:
            _LOGGER.warning("Could not close temporary Rainforest validation client")

    async def async_step_reconfigure_confirm(
        self, user_input: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Validate transactionally, reject another device, and preserve this entry."""

        if user_input is None:
            return self.async_show_form(step_id="reconfigure_confirm")
        entry = self._reconfigure_entry or self._get_reconfigure_entry()
        candidate = self._selected_port
        assert candidate is not None
        original_path = entry.data.get(CONF_DEVICE)
        assert isinstance(original_path, str)
        client: _ReconfigureClient | None = None
        temporary_client = False
        reconfigured = False
        try:
            try:
                client, temporary_client = await self._async_reconfigure_client(entry)
                result = await client.async_reconfigure_path(candidate.path)
                reconfigured = True
                await self.async_set_unique_id(format_mac(result.device_mac))
                self._abort_if_unique_id_mismatch()
                self._validation = result
            except BaseException as err:
                if reconfigured and client is not None:
                    await self._async_restore_reconfigure_client(
                        client,
                        original_path,
                        err,
                    )
                raise
            finally:
                if temporary_client and client is not None:
                    await self._async_shutdown_temporary_client(client)
        except RainforestCommunicationError as err:
            return self.async_show_form(
                step_id="reconfigure_confirm", errors={"base": _error_key(err)}
            )
        return await self._async_finish_validation("reconfigure_confirm")

    async def async_step_meters(
        self, user_input: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Select at least one validated electric meter and create the entry."""

        assert self._validation is not None
        valid_meters = {meter.mac_hex: meter.name for meter in self._validation.meters}
        if user_input is not None:
            selected = user_input.get(CONF_METERS, [])
            selected_set = set(selected) if isinstance(selected, list) else set()
            selected_set &= valid_meters.keys()
            if selected_set:
                self._selected_meters = selected_set
                if self._reconfigure_entry is not None:
                    candidate = self._selected_port
                    assert candidate is not None
                    return self.async_update_reload_and_abort(
                        self._reconfigure_entry,
                        data_updates={
                            CONF_DEVICE: candidate.path,
                            CONF_METERS: sorted(selected_set),
                            CONF_USB_SERIAL: candidate.serial_number,
                            CONF_USB_VID: candidate.vid,
                            CONF_USB_PID: candidate.pid,
                        },
                    )
                await self.async_set_unique_id(format_mac(self._validation.device_mac))
                self._abort_if_unique_id_configured()
                candidate = self._selected_port
                assert candidate is not None
                return self.async_create_entry(
                    title="Rainforest EMU-2",
                    data={
                        CONF_DEVICE: candidate.path,
                        CONF_METERS: sorted(selected_set),
                        CONF_USB_SERIAL: candidate.serial_number,
                        CONF_USB_VID: candidate.vid,
                        CONF_USB_PID: candidate.pid,
                    },
                )
            errors = {CONF_METERS: "no_supported_meters"}
        else:
            errors = None
        return self.async_show_form(
            step_id="meters",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_METERS, default=sorted(self._selected_meters)
                    ): cv.multi_select(valid_meters)
                }
            ),
            errors=errors,
        )
