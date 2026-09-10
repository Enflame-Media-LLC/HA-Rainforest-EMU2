"""Controllable Rainforest device doubles for communication tests."""

from __future__ import annotations

import inspect
from collections import deque
from typing import Any

import pytest


class FakeRavenDevice:
    """A deterministic stand-in for the external serial device."""

    def __init__(self, path: str = "", **kwargs: Any) -> None:
        self.path = path
        self.kwargs = kwargs
        self.calls: list[str] = []
        self.meter_list_results: deque[Any] = deque()
        self.device_info: Any = None
        self.meter_infos: dict[bytes, Any] = {}
        self.open_result: Any = None
        self.close_result: Any = None
        self.abort_result: Any = None

    @staticmethod
    async def _resolve(result: Any) -> Any:
        if inspect.isawaitable(result):
            return await result
        if isinstance(result, BaseException):
            raise result
        return result

    async def open(self) -> None:
        self.calls.append("open")
        await self._resolve(self.open_result)

    async def get_meter_list(self) -> Any:
        self.calls.append("meter_list")
        return await self._resolve(self.meter_list_results.popleft())

    async def get_device_info(self) -> Any:
        self.calls.append("device_info")
        return await self._resolve(self.device_info)

    async def get_meter_info(self, *, meter: bytes | None = None) -> Any:
        assert meter is not None
        self.calls.append(f"meter_info:{meter.hex()}")
        return await self._resolve(self.meter_infos[meter])

    async def close(self) -> None:
        self.calls.append("close")
        await self._resolve(self.close_result)

    async def abort(self) -> None:
        self.calls.append("abort")
        await self._resolve(self.abort_result)


class FakeRavenFactory:
    """Record construction and return configured fake devices in order."""

    def __init__(self, *devices: FakeRavenDevice | BaseException) -> None:
        self._devices = deque(devices)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, path: str, **kwargs: Any) -> FakeRavenDevice:
        self.calls.append((path, kwargs))
        device = self._devices.popleft()
        if isinstance(device, BaseException):
            raise device
        device.path = path
        device.kwargs = kwargs
        return device


@pytest.fixture
def fake_raven_device(
    monkeypatch: pytest.MonkeyPatch,
) -> FakeRavenDevice:
    """Install and return one controllable fake serial device."""

    from custom_components.rainforest_emu2 import communication

    fake = FakeRavenDevice()
    monkeypatch.setattr(
        communication, "RAVEnSerialDevice", FakeRavenFactory(fake), raising=False
    )
    return fake


@pytest.fixture
def fast_lifecycle_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove protocol delays while keeping real timeout boundaries."""

    from custom_components.rainforest_emu2 import communication

    monkeypatch.setattr(communication, "SETTLE_DELAY", 0.0, raising=False)
    monkeypatch.setattr(
        communication, "METER_LIST_BACKOFFS", (0.0, 0.0, 0.0), raising=False
    )
    monkeypatch.setattr(communication, "OPEN_TIMEOUT", 0.01, raising=False)
    monkeypatch.setattr(communication, "METER_LIST_TIMEOUT", 0.01, raising=False)
    monkeypatch.setattr(communication, "QUERY_TIMEOUT", 0.01, raising=False)
    monkeypatch.setattr(communication, "CLOSE_TIMEOUT", 0.01, raising=False)
    monkeypatch.setattr(communication, "ABORT_TIMEOUT", 0.01, raising=False)
    monkeypatch.setattr(communication, "LOCK_TIMEOUT", 0.01)
