"""Safe, typed boundary for Rainforest serial communication."""

from __future__ import annotations

import asyncio
import errno
import os
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from aioraven.serial import RAVEnSerialDevice

from .const import (
    ABORT_TIMEOUT,
    CLOSE_TIMEOUT,
    LOCK_TIMEOUT,
    METER_LIST_BACKOFFS,
    METER_LIST_TIMEOUT,
    OPEN_TIMEOUT,
    QUERY_TIMEOUT,
    SETTLE_DELAY,
)


class FailureStage(StrEnum):
    """Operation stage at which communication failed."""

    SCAN = "scan"
    LOCK = "lock"
    OPEN = "open"
    SYNC = "sync"
    DEVICE_INFO = "device_info"
    METER_INFO = "meter_info"
    POLL = "poll"
    CLEANUP = "cleanup"


class FailureReason(StrEnum):
    """Safe, stable reason values suitable for UI and diagnostics."""

    MISSING = "missing"
    PERMISSION = "permission"
    BUSY = "busy"
    TIMEOUT = "timeout"
    NO_RESPONSE = "no_response"
    MALFORMED = "malformed"
    UNSUPPORTED = "unsupported"
    IO = "io"


class RainforestCommunicationError(Exception):
    """Communication failure with a privacy-safe string representation."""

    def __init__(
        self,
        stage: FailureStage,
        reason: FailureReason,
        *,
        retryable: bool,
        cause: BaseException | None = None,
    ) -> None:
        self.stage = stage
        self.reason = reason
        self.retryable = retryable
        # Retain the cause for exception chaining and internal diagnostics, but
        # deliberately omit it from args/str/repr to avoid leaking paths.
        self.cause = cause
        super().__init__(f"{stage.value}:{reason.value}")


@dataclass(frozen=True, slots=True)
class PortCandidate:
    """A discovered or manually supplied serial-port candidate."""

    token: str
    path: str
    label: str
    vid: int | None
    pid: int | None
    serial_number: str | None
    manufacturer: str | None
    description: str | None
    location: str | None


@dataclass(frozen=True, slots=True)
class MeterRecord:
    """A meter returned by the device meter list."""

    mac: bytes
    mac_hex: str
    name: str
    meter_type: str | None


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Validated device metadata and usable meters."""

    path: str
    device_mac: str
    manufacturer: str | None
    model: str | None
    firmware: str | None
    meters: tuple[MeterRecord, ...]


@dataclass(frozen=True, slots=True)
class MeterSnapshot:
    """Values returned for one meter during a refresh."""

    demand: float | None
    delivered: float | None
    received: float | None
    price: float | None
    currency: str | None


@dataclass(frozen=True, slots=True)
class RavenSnapshot:
    """Immutable refresh result with explicit field presence."""

    meters: Mapping[str, MeterSnapshot]
    signal_strength: int | None
    present_fields: frozenset[str]


@dataclass(frozen=True, slots=True)
class _PortLockLease:
    """Proof that one acquire context owns a specific lock group."""

    group: str
    lock: asyncio.Lock
    token: object


def snapshot_field_key(field: str, meter_mac_hex: str | None = None) -> str:
    """Return the canonical key for a device or meter snapshot field."""

    return f"meter:{meter_mac_hex}:{field}" if meter_mac_hex else f"device:{field}"


def canonical_port_key(path: os.PathLike[str] | str) -> str:
    """Return the filesystem identity used for in-process port locking."""

    return os.path.realpath(os.fspath(path))


_RAW_DEVICE_NAME = re.compile(r"tty(?:ACM|USB)\d+\Z")


def safe_path_details(path: os.PathLike[str] | str) -> dict[str, str | None]:
    """Describe a serial path without exposing serial-bearing identifiers."""

    path_text = os.fspath(path)
    if path_text.startswith("/dev/serial/by-id/"):
        return {"strategy": "by_id", "basename": None}
    if path_text.startswith("/dev/serial/by-path/"):
        return {"strategy": "by_path", "basename": None}

    basename = os.path.basename(path_text)
    if path_text.startswith("/dev/") and _RAW_DEVICE_NAME.fullmatch(basename):
        return {"strategy": "raw", "basename": basename}
    return {"strategy": "unknown", "basename": None}


def _device_mac_key(device_mac: bytes | bytearray | str) -> str:
    """Normalize a hardware MAC to an internal, non-rendered identity key."""

    if isinstance(device_mac, (bytes, bytearray)):
        return bytes(device_mac).hex()
    return re.sub(r"[:-]", "", device_mac).lower()


class PortLockRegistry:
    """Share locks between equivalent paths and validated hardware identities."""

    def __init__(self) -> None:
        self._path_groups: dict[str, str] = {}
        self._group_paths: dict[str, set[str]] = {}
        self._group_macs: dict[str, set[str]] = {}
        self._mac_groups: dict[str, str] = {}
        self._group_locks: dict[str, asyncio.Lock] = {}
        self._group_owners: dict[str, object | None] = {}

    def _ensure_group(self, path_key: str) -> str:
        group = self._path_groups.get(path_key)
        if group is not None:
            return group
        group = path_key
        self._path_groups[path_key] = group
        self._group_paths[group] = {path_key}
        self._group_macs[group] = set()
        self._group_locks[group] = asyncio.Lock()
        self._group_owners[group] = None
        return group

    def lock_for(self, path: os.PathLike[str] | str) -> asyncio.Lock:
        """Return the lock for a canonical path or an already-known MAC group."""

        path_key = canonical_port_key(path)
        return self._group_locks[self._ensure_group(path_key)]

    @staticmethod
    def _busy_error() -> RainforestCommunicationError:
        return RainforestCommunicationError(
            FailureStage.LOCK,
            FailureReason.BUSY,
            retryable=True,
        )

    def alias(
        self,
        path: os.PathLike[str] | str,
        device_mac: bytes | bytearray | str,
        *,
        owner: _PortLockLease | None = None,
    ) -> None:
        """Associate a validated path with its hardware identity transactionally.

        A caller holding an ownership lease may merge an idle hardware group
        into its actively held path group. Without that lease, both groups must
        be idle. All contention checks happen before changing any mapping.
        """

        path_key = canonical_port_key(path)
        mac_key = _device_mac_key(device_mac)
        current_group = self._path_groups.get(path_key)
        current_lock = self._group_locks.get(current_group or path_key)
        target_group = self._mac_groups.get(mac_key)
        owns_current = (
            owner is not None
            and current_group is not None
            and owner.group == current_group
            and owner.lock is current_lock
            and self._group_owners.get(current_group) is owner.token
        )

        if owner is not None and not owns_current:
            raise self._busy_error()
        if current_lock is not None and current_lock.locked() and not owns_current:
            raise self._busy_error()

        if current_group is not None:
            current_macs = self._group_macs[current_group]
            if current_macs and mac_key not in current_macs:
                raise self._busy_error()

        if target_group is not None and target_group != current_group:
            # Do not call _ensure_group (or otherwise mutate mappings) before
            # this contention check.  A held group remains wholly independent.
            target_lock = self._group_locks[target_group]
            if target_lock.locked():
                raise self._busy_error()
            source_macs = self._group_macs.get(current_group or "", set())
            if source_macs - {mac_key}:
                raise self._busy_error()

        if current_group is None:
            current_group = self._ensure_group(path_key)

        if target_group is None:
            self._mac_groups[mac_key] = current_group
            self._group_macs[current_group].add(mac_key)
            return

        if target_group == current_group:
            self._group_macs[current_group].add(mac_key)
            return

        # When validation owns the source, preserve that held lock by merging
        # the idle identity group into it.  Otherwise preserve the established
        # target group.  Checks and mapping changes are synchronous, making the
        # convergence atomic from the event loop's perspective.
        destination = current_group if owns_current else target_group
        source = target_group if owns_current else current_group
        for source_path in self._group_paths[source]:
            self._path_groups[source_path] = destination
            self._group_paths[destination].add(source_path)
        for source_mac in self._group_macs[source]:
            self._mac_groups[source_mac] = destination
            self._group_macs[destination].add(source_mac)
        self._mac_groups[mac_key] = destination
        self._group_macs[destination].add(mac_key)
        del self._group_paths[source]
        del self._group_macs[source]
        del self._group_locks[source]
        del self._group_owners[source]

    @asynccontextmanager
    async def acquire(
        self, path: os.PathLike[str] | str
    ) -> AsyncIterator[_PortLockLease]:
        """Acquire a path lock, with contention bounded by ``LOCK_TIMEOUT``."""

        path_key = canonical_port_key(path)
        group = self._ensure_group(path_key)
        lock = self._group_locks[group]
        try:
            async with asyncio.timeout(LOCK_TIMEOUT):
                await lock.acquire()
        except TimeoutError as err:
            raise RainforestCommunicationError(
                FailureStage.LOCK,
                FailureReason.BUSY,
                retryable=True,
                cause=err,
            ) from err
        token = object()
        lease = _PortLockLease(group, lock, token)
        self._group_owners[group] = token
        try:
            yield lease
        finally:
            if self._group_owners.get(group) is token:
                self._group_owners[group] = None
            lock.release()


class RavenClient:
    """Own one bounded, exclusive Rainforest serial lifecycle."""

    def __init__(
        self,
        path: str,
        *,
        registry: PortLockRegistry | None = None,
        **_: Any,
    ) -> None:
        self.path = path
        self.registry = registry or PortLockRegistry()
        self.device_info: Any = None
        self._device: Any = None

    @staticmethod
    def _error(
        stage: FailureStage,
        reason: FailureReason,
        cause: BaseException | None = None,
    ) -> RainforestCommunicationError:
        """Create a structured error with consistent retry semantics."""

        return RainforestCommunicationError(
            stage,
            reason,
            retryable=reason
            in {
                FailureReason.MISSING,
                FailureReason.BUSY,
                FailureReason.TIMEOUT,
                FailureReason.NO_RESPONSE,
                FailureReason.IO,
            },
            cause=cause,
        )

    @staticmethod
    def _underlying_errno(err: BaseException) -> int | None:
        """Find an errno without rendering a possibly sensitive exception."""

        current: BaseException | None = err
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            number = getattr(current, "errno", None)
            if isinstance(number, int):
                return number
            current = current.__cause__ or current.__context__
        return None

    @classmethod
    def _translate_error(
        cls,
        stage: FailureStage,
        err: BaseException,
    ) -> RainforestCommunicationError:
        """Translate transport exceptions into the privacy-safe error model."""

        if isinstance(err, RainforestCommunicationError):
            return err
        if isinstance(err, TimeoutError):
            return cls._error(stage, FailureReason.TIMEOUT, err)

        number = cls._underlying_errno(err)
        if number == errno.ENOENT:
            return cls._error(stage, FailureReason.MISSING, err)
        if number in {errno.EACCES, errno.EPERM}:
            return cls._error(stage, FailureReason.PERMISSION, err)
        if number == errno.EBUSY:
            return cls._error(stage, FailureReason.BUSY, err)
        if isinstance(err, (TypeError, ValueError)):
            return cls._error(stage, FailureReason.UNSUPPORTED, err)
        return cls._error(stage, FailureReason.IO, err)

    @staticmethod
    def _exclusive_keyword_unsupported(err: BaseException) -> bool:
        """Return whether a TypeError proves ``exclusive`` is unsupported."""

        if not isinstance(err, TypeError):
            return False
        message = str(err)
        return "unexpected keyword argument" in message and "exclusive" in message

    async def _async_abort_device(
        self, device: Any
    ) -> RainforestCommunicationError | None:
        """Force-close a device within its independent deadline."""

        try:
            async with asyncio.timeout(ABORT_TIMEOUT):
                await device.abort()
        except TimeoutError as err:
            return self._error(FailureStage.CLEANUP, FailureReason.TIMEOUT, err)
        except Exception as err:  # Dependency and serial backends vary by platform.
            return self._error(FailureStage.CLEANUP, FailureReason.IO, err)
        return None

    async def _async_cleanup_device(
        self,
        device: Any,
        *,
        graceful: bool,
    ) -> RainforestCommunicationError | None:
        """Close or abort a device, returning rather than raising cleanup errors."""

        if graceful:
            try:
                async with asyncio.timeout(CLOSE_TIMEOUT):
                    await device.close()
            except asyncio.CancelledError:
                await self._async_abort_device(device)
                raise
            except Exception:
                # A failed graceful close is recovered only by a successful,
                # independently bounded abort below.
                pass
            else:
                if self._device is device:
                    self._device = None
                return None

        abort_error = await self._async_abort_device(device)
        if self._device is device:
            self._device = None
        return abort_error

    async def _async_open(self) -> Any:
        """Construct and open a device with safe POSIX exclusivity fallback."""

        exclusive = os.name == "posix"
        kwargs = {"exclusive": True} if exclusive else {}
        try:
            device = RAVEnSerialDevice(self.path, **kwargs)
        except Exception as err:
            if not (exclusive and self._exclusive_keyword_unsupported(err)):
                raise self._translate_error(FailureStage.OPEN, err) from err
            exclusive = False
            try:
                device = RAVEnSerialDevice(self.path)
            except Exception as fallback_err:
                raise self._translate_error(
                    FailureStage.OPEN, fallback_err
                ) from fallback_err
        self._device = device
        try:
            async with asyncio.timeout(OPEN_TIMEOUT):
                await device.open()
        except asyncio.CancelledError:
            raise
        except Exception as err:
            if exclusive and self._exclusive_keyword_unsupported(err):
                cleanup_error = await self._async_abort_device(device)
                self._device = None
                if cleanup_error is not None:
                    raise cleanup_error from err

                try:
                    device = RAVEnSerialDevice(self.path)
                except Exception as fallback_err:
                    raise self._translate_error(
                        FailureStage.OPEN, fallback_err
                    ) from fallback_err
                self._device = device
                try:
                    async with asyncio.timeout(OPEN_TIMEOUT):
                        await device.open()
                except asyncio.CancelledError:
                    raise
                except Exception as fallback_err:
                    raise self._translate_error(
                        FailureStage.OPEN, fallback_err
                    ) from fallback_err
                return device
            raise self._translate_error(FailureStage.OPEN, err) from err
        return device

    async def _async_synchronize(self, device: Any) -> Any:
        """Synchronize from the one successful meter-list response."""

        await asyncio.sleep(SETTLE_DELAY)
        for attempt, backoff in enumerate(METER_LIST_BACKOFFS, start=1):
            if backoff:
                await asyncio.sleep(backoff)
            try:
                async with asyncio.timeout(METER_LIST_TIMEOUT):
                    meter_list = await device.get_meter_list()
            except asyncio.CancelledError:
                raise
            except TimeoutError as err:
                if attempt == len(METER_LIST_BACKOFFS):
                    raise self._error(
                        FailureStage.SYNC, FailureReason.TIMEOUT, err
                    ) from err
                continue
            except Exception as err:
                raise self._translate_error(FailureStage.SYNC, err) from err
            if meter_list is not None:
                return meter_list
        raise self._error(FailureStage.SYNC, FailureReason.NO_RESPONSE)

    async def _async_device_info(self, device: Any) -> Any:
        """Read and validate the device's permanent hardware identity."""

        try:
            async with asyncio.timeout(QUERY_TIMEOUT):
                info = await device.get_device_info()
        except asyncio.CancelledError:
            raise
        except Exception as err:
            raise self._translate_error(FailureStage.DEVICE_INFO, err) from err

        if info is None:
            raise self._error(FailureStage.DEVICE_INFO, FailureReason.NO_RESPONSE)
        mac = getattr(info, "device_mac_id", None)
        if not isinstance(mac, bytes) or len(mac) != 8:
            raise self._error(FailureStage.DEVICE_INFO, FailureReason.MALFORMED)
        return info

    @staticmethod
    def _meter_label(mac: bytes) -> str:
        """Return a generic meter label exposing only the approved suffix."""

        return f"Meter {mac.hex()[-4:].upper()}"

    async def _async_meter_records(
        self,
        device: Any,
        meter_macs: Any,
    ) -> tuple[MeterRecord, ...]:
        """Read usable electric meters, isolating malformed records."""

        if meter_macs is None:
            return ()
        if not isinstance(meter_macs, (list, tuple)):
            raise self._error(FailureStage.SYNC, FailureReason.MALFORMED)

        records: list[MeterRecord] = []
        for meter_mac in meter_macs:
            if not isinstance(meter_mac, bytes) or len(meter_mac) != 8:
                continue
            try:
                async with asyncio.timeout(QUERY_TIMEOUT):
                    info = await device.get_meter_info(meter=meter_mac)
            except asyncio.CancelledError:
                raise
            except ValueError as err:
                if err.args == ("'0x0000' is not a valid MeterType",):
                    records.append(
                        MeterRecord(
                            meter_mac,
                            meter_mac.hex(),
                            self._meter_label(meter_mac),
                            None,
                        )
                    )
                continue
            except Exception as err:
                raise self._translate_error(FailureStage.METER_INFO, err) from err

            if info is None or getattr(info, "enabled", True) is False:
                continue
            if getattr(info, "meter_mac_id", None) != meter_mac:
                continue
            meter_type = getattr(info, "meter_type", None)
            meter_type_value = getattr(meter_type, "value", meter_type)
            if meter_type_value not in {None, "electric"}:
                continue
            name = getattr(info, "nick_name", None) or self._meter_label(meter_mac)
            records.append(
                MeterRecord(
                    meter_mac,
                    meter_mac.hex(),
                    name,
                    meter_type_value,
                )
            )
        return tuple(records)

    async def async_validate(self) -> ValidationResult:
        """Validate a device and always release its transport and port lock."""

        async with self.registry.acquire(self.path) as owner:
            device: Any = None
            try:
                device = await self._async_open()
                meter_list = await self._async_synchronize(device)
                meter_macs = getattr(meter_list, "meter_mac_ids", None)
                if not hasattr(meter_list, "meter_mac_ids"):
                    raise self._error(FailureStage.SYNC, FailureReason.MALFORMED)

                info = await self._async_device_info(device)
                self.device_info = info
                device_mac = info.device_mac_id.hex()
                self.registry.alias(self.path, device_mac, owner=owner)
                meters = await self._async_meter_records(device, meter_macs)
                result = ValidationResult(
                    path=self.path,
                    device_mac=device_mac,
                    manufacturer=getattr(info, "manufacturer", None),
                    model=getattr(info, "model_id", None),
                    firmware=getattr(info, "fw_version", None),
                    meters=meters,
                )
            except BaseException as err:
                cleanup_target = device or self._device
                if cleanup_target is not None:
                    cleanup_error = await self._async_cleanup_device(
                        cleanup_target, graceful=False
                    )
                    if cleanup_error is not None:
                        err.add_note(f"secondary failure: {cleanup_error}")
                raise

            cleanup_error = await self._async_cleanup_device(device, graceful=True)
            if cleanup_error is not None:
                raise cleanup_error
            return result

    async def async_shutdown(self) -> None:
        """Bound shutdown and force-abort when graceful close cannot finish."""

        if self._device is None:
            return
        cleanup_error = await self._async_cleanup_device(
            self._device,
            graceful=True,
        )
        if cleanup_error is not None:
            raise cleanup_error
