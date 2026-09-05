"""Safe, typed boundary for Rainforest serial communication."""

from __future__ import annotations

import asyncio
import errno
import logging
import math
import os
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from aioraven.serial import RAVEnSerialDevice
from iso4217 import Currency

from .const import (
    ABORT_TIMEOUT,
    CLOSE_TIMEOUT,
    LOCK_TIMEOUT,
    METER_LIST_BACKOFFS,
    METER_LIST_TIMEOUT,
    OPEN_TIMEOUT,
    QUERY_TIMEOUT,
    SETTLE_DELAY,
    WATCHDOG_MARGIN,
)

_LOGGER = logging.getLogger(__name__)


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


def cycle_budget(meter_count: int, *, needs_open: bool) -> float:
    """Return a watchdog budget that cannot preempt per-command deadlines."""

    commands = meter_count * 3 + 1
    open_budget = (
        OPEN_TIMEOUT
        + SETTLE_DELAY
        + sum(METER_LIST_BACKOFFS)
        + len(METER_LIST_BACKOFFS) * METER_LIST_TIMEOUT
        + QUERY_TIMEOUT
        if needs_open
        else 0.0
    )
    return open_budget + commands * QUERY_TIMEOUT + CLOSE_TIMEOUT + WATCHDOG_MARGIN


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
        self._command_lock = asyncio.Lock()
        self._lease_context: Any = None
        self._lease_owner: _PortLockLease | None = None

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
                try:
                    await self._async_abort_device(device)
                finally:
                    if self._device is device:
                        self._device = None
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

    async def _async_acquire_persistent_lease(self) -> None:
        """Acquire and retain this client's port lease."""

        if self._lease_context is not None:
            return
        context = self.registry.acquire(self.path)
        owner = await context.__aenter__()
        self._lease_context = context
        self._lease_owner = owner

    async def _async_release_persistent_lease(self) -> None:
        """Release a retained port lease exactly once."""

        context = self._lease_context
        if context is None:
            return
        self._lease_context = None
        self._lease_owner = None
        await context.__aexit__(None, None, None)

    async def _async_connect(self) -> None:
        """Open, synchronize, and identify a persistent transport."""

        if self._device is not None:
            return
        await self._async_acquire_persistent_lease()
        device: Any = None
        try:
            device = await self._async_open()
            meter_list = await self._async_synchronize(device)
            if not hasattr(meter_list, "meter_mac_ids"):
                raise self._error(FailureStage.SYNC, FailureReason.MALFORMED)
            info = await self._async_device_info(device)
            self.registry.alias(
                self.path,
                info.device_mac_id.hex(),
                owner=self._lease_owner,
            )
            self.device_info = info
        except BaseException as err:
            cleanup_target = device or self._device
            if cleanup_target is not None:
                cleanup_error = await self._async_cleanup_device(
                    cleanup_target, graceful=False
                )
                if cleanup_error is not None:
                    err.add_note(f"secondary failure: {cleanup_error}")
            raise

    async def _async_close_persistent(
        self,
        *,
        graceful: bool,
        release_lease: bool,
    ) -> RainforestCommunicationError | None:
        """Close the current transport and optionally relinquish ownership."""

        cleanup_error: RainforestCommunicationError | None = None
        try:
            if self._device is not None:
                cleanup_error = await self._async_cleanup_device(
                    self._device,
                    graceful=graceful,
                )
        finally:
            if release_lease:
                await self._async_release_persistent_lease()
        return cleanup_error

    @staticmethod
    def _parse_number(value: object) -> float | None:
        """Parse one finite protocol number without inventing a value."""

        if value is None or isinstance(value, bool):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) else None

    async def _async_required_poll(self, call: Any) -> Any:
        """Run one required poll command inside its own deadline."""

        try:
            async with asyncio.timeout(QUERY_TIMEOUT):
                response = await call()
        except asyncio.CancelledError:
            raise
        except Exception as err:
            raise self._translate_error(FailureStage.POLL, err) from err
        if response is None:
            raise self._error(FailureStage.POLL, FailureReason.NO_RESPONSE)
        return response

    async def _async_optional_poll(self, call: Any) -> Any:
        """Run an optional command, isolating only parsing/schema failures."""

        try:
            async with asyncio.timeout(QUERY_TIMEOUT):
                return await call()
        except asyncio.CancelledError:
            raise
        except (TypeError, ValueError):
            return None
        except Exception as err:
            raise self._translate_error(FailureStage.POLL, err) from err

    async def _async_collect_cycle(
        self,
        meter_macs: tuple[bytes, ...],
    ) -> RavenSnapshot:
        """Collect one complete cycle into a new, unpublished result."""

        device = self._device
        if device is None:
            raise self._error(FailureStage.POLL, FailureReason.NO_RESPONSE)

        meters: dict[str, MeterSnapshot] = {}
        present_fields: set[str] = set()
        for meter_mac in meter_macs:
            meter_key = meter_mac.hex()
            summation = await self._async_required_poll(
                lambda meter_mac=meter_mac: device.get_current_summation_delivered(
                    meter=meter_mac,
                    refresh=True,
                )
            )
            if getattr(summation, "meter_mac_id", meter_mac) != meter_mac:
                raise self._error(FailureStage.POLL, FailureReason.MALFORMED)
            delivered = self._parse_number(
                getattr(summation, "summation_delivered", None)
            )
            received = self._parse_number(
                getattr(summation, "summation_received", None)
            )
            if delivered is None and received is None:
                raise self._error(FailureStage.POLL, FailureReason.MALFORMED)

            demand_response = await self._async_required_poll(
                lambda meter_mac=meter_mac: device.get_instantaneous_demand(
                    meter=meter_mac,
                    refresh=True,
                )
            )
            if getattr(demand_response, "meter_mac_id", meter_mac) != meter_mac:
                raise self._error(FailureStage.POLL, FailureReason.MALFORMED)
            demand = self._parse_number(getattr(demand_response, "demand", None))
            if demand is None:
                raise self._error(FailureStage.POLL, FailureReason.MALFORMED)

            price_response = await self._async_optional_poll(
                lambda meter_mac=meter_mac: device.get_current_price(
                    meter=meter_mac,
                    refresh=True,
                )
            )
            price: float | None = None
            currency: str | None = None
            if (
                price_response is not None
                and getattr(price_response, "meter_mac_id", meter_mac) == meter_mac
            ):
                parsed_price = self._parse_number(
                    getattr(price_response, "price", None)
                )
                raw_currency = getattr(price_response, "currency", None)
                if parsed_price is not None and isinstance(raw_currency, Currency):
                    price = parsed_price
                    currency = raw_currency.value

            if demand is not None:
                present_fields.add(snapshot_field_key("demand", meter_key))
            if delivered is not None:
                present_fields.add(snapshot_field_key("delivered", meter_key))
            if received is not None:
                present_fields.add(snapshot_field_key("received", meter_key))
            if price is not None:
                present_fields.add(snapshot_field_key("price", meter_key))
            meters[meter_key] = MeterSnapshot(
                demand=demand,
                delivered=delivered,
                received=received,
                price=price,
                currency=currency,
            )

        network = await self._async_optional_poll(device.get_network_info)
        signal_strength: int | None = None
        if network is not None:
            raw_signal = getattr(network, "link_strength", None)
            if (
                isinstance(raw_signal, int)
                and not isinstance(raw_signal, bool)
                and 0 <= raw_signal <= 255
            ):
                signal_strength = raw_signal
                present_fields.add(snapshot_field_key("signal_strength"))

        return RavenSnapshot(
            MappingProxyType(meters),
            signal_strength,
            frozenset(present_fields),
        )

    async def async_refresh(
        self,
        meter_macs: tuple[bytes, ...],
    ) -> RavenSnapshot:
        """Poll every selected meter, retrying one whole failed cycle."""

        async with self._command_lock:
            last_error: RainforestCommunicationError | None = None
            for attempt in range(2):
                needs_open = self._device is None
                try:
                    async with asyncio.timeout(
                        cycle_budget(len(meter_macs), needs_open=needs_open)
                    ):
                        if needs_open:
                            await self._async_connect()
                        return await self._async_collect_cycle(meter_macs)
                except asyncio.CancelledError as err:
                    cleanup_error = await self._async_close_persistent(
                        graceful=False,
                        release_lease=False,
                    )
                    if cleanup_error is not None:
                        err.add_note(f"secondary failure: {cleanup_error}")
                    raise
                except BaseException as err:
                    last_error = self._translate_error(FailureStage.POLL, err)
                    cleanup_error = await self._async_close_persistent(
                        graceful=False,
                        release_lease=False,
                    )
                    if cleanup_error is not None:
                        last_error.add_note(f"secondary failure: {cleanup_error}")
                    if attempt == 1:
                        raise last_error from err
            raise last_error or self._error(
                FailureStage.POLL, FailureReason.NO_RESPONSE
            )

    async def async_set_path(self, path: str) -> None:
        """Replace the path after safely relinquishing the old transport."""

        async with self._command_lock:
            if path == self.path:
                return
            cleanup_error = await self._async_close_persistent(
                graceful=True,
                release_lease=True,
            )
            if cleanup_error is not None:
                raise cleanup_error
            self.path = path
            self.device_info = None

    async def async_reconfigure_path(self, path: str) -> ValidationResult:
        """Validate a replacement path and restore the original on failure."""

        async with self._command_lock:
            original_path = self.path
            original_info = self.device_info
            cleanup_error = await self._async_close_persistent(
                graceful=True,
                release_lease=True,
            )
            if cleanup_error is not None:
                raise cleanup_error

            self.path = path
            try:
                return await self._async_validate()
            except BaseException as candidate_error:
                self.path = original_path
                self.device_info = original_info
                try:
                    await self._async_connect()
                except BaseException as restore_error:
                    restored_error = self._translate_error(
                        FailureStage.OPEN, restore_error
                    )
                    candidate_error.add_note(
                        f"secondary restoration failure: {restored_error}"
                    )
                    _LOGGER.warning(
                        "Could not restore Rainforest transport after reconfigure "
                        "failure (stage=%s, reason=%s)",
                        restored_error.stage.value,
                        restored_error.reason.value,
                    )
                finally:
                    self.device_info = original_info
                raise

    async def async_restore_path(self, path: str) -> None:
        """Return to a previously configured path after rejecting a candidate."""

        async with self._command_lock:
            cleanup_error = await self._async_close_persistent(
                graceful=True,
                release_lease=True,
            )
            if cleanup_error is not None:
                raise cleanup_error
            self.path = path
            self.device_info = None
            await self._async_connect()

    async def async_validate(self) -> ValidationResult:
        """Validate a device and always release its transport and port lock."""

        async with self._command_lock:
            return await self._async_validate()

    async def _async_validate(self) -> ValidationResult:
        """Validate a device while this client's lifecycle lock is already held."""

        async with self.registry.acquire(self.path) as owner:
            device: Any = None
            try:
                device = await self._async_open()
                meter_list = await self._async_synchronize(device)
                meter_macs = getattr(meter_list, "meter_mac_ids", None)
                if not hasattr(meter_list, "meter_mac_ids"):
                    raise self._error(FailureStage.SYNC, FailureReason.MALFORMED)

                info = await self._async_device_info(device)
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
            self.device_info = info
            return result

    async def async_shutdown(self) -> None:
        """Bound shutdown and force-abort when graceful close cannot finish."""

        async with self._command_lock:
            cleanup_error = await self._async_close_persistent(
                graceful=True,
                release_lease=True,
            )
            if cleanup_error is not None:
                raise cleanup_error
