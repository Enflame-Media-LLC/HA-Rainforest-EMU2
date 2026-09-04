"""Safe, typed boundary for Rainforest serial communication.

The serial lifecycle is implemented in later integration layers.  This module
owns the values and identity primitives those layers share, so paths and
errors can be handled without exposing device identifiers.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .const import LOCK_TIMEOUT


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

    def _ensure_group(self, path_key: str) -> str:
        group = self._path_groups.get(path_key)
        if group is not None:
            return group
        group = path_key
        self._path_groups[path_key] = group
        self._group_paths[group] = {path_key}
        self._group_macs[group] = set()
        self._group_locks[group] = asyncio.Lock()
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
    ) -> None:
        """Associate a validated path with its hardware identity transactionally.

        If a path's current group needs to merge into the group already known
        for this MAC, the current group must be free.  The check happens before
        changing any mapping, preventing a held group from being split across
        two hardware identities.
        """

        path_key = canonical_port_key(path)
        mac_key = _device_mac_key(device_mac)
        current_group = self._path_groups.get(path_key)
        current_lock = self._group_locks.get(current_group or path_key)
        target_group = self._mac_groups.get(mac_key)

        if current_group is not None:
            current_macs = self._group_macs[current_group]
            if current_macs and mac_key not in current_macs:
                raise self._busy_error()

        if target_group is not None and target_group != current_group:
            # Do not call _ensure_group (or otherwise mutate mappings) before
            # this contention check.  A held group remains wholly independent.
            if current_lock is not None and current_lock.locked():
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

        # Merge the unheld source group into the existing MAC group.  All
        # updates happen after the lock check above, so this operation is
        # atomic from the registry's perspective.
        for source_path in self._group_paths[current_group]:
            self._path_groups[source_path] = target_group
            self._group_paths[target_group].add(source_path)
        for source_mac in self._group_macs[current_group]:
            self._mac_groups[source_mac] = target_group
            self._group_macs[target_group].add(source_mac)
        self._group_macs[target_group].add(mac_key)
        del self._group_paths[current_group]
        del self._group_macs[current_group]
        del self._group_locks[current_group]

    @asynccontextmanager
    async def acquire(
        self, path: os.PathLike[str] | str
    ) -> AsyncIterator[asyncio.Lock]:
        """Acquire a path lock, with contention bounded by ``LOCK_TIMEOUT``."""

        lock = self.lock_for(path)
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
        try:
            yield lock
        finally:
            lock.release()


class RavenClient:
    """Placeholder communication client for the lifecycle implementation.

    Task 2 establishes the importable boundary and constructor shape.  The
    bounded serial lifecycle and polling methods are added by later tasks.
    """

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
