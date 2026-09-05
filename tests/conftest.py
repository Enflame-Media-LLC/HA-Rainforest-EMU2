"""Shared Home Assistant test configuration."""

import sys
from collections.abc import Generator
from types import ModuleType

import pytest

# The repository's local HA 2025.1 test dependency predates this public 2026.3
# import location. Production deliberately imports the modern type; tests only
# provide the legacy equivalent so they can exercise the integration boundary.
try:
    from homeassistant.helpers.service_info.usb import UsbServiceInfo
except ModuleNotFoundError:
    from homeassistant.components import usb as legacy_usb
    from homeassistant.components.usb import UsbServiceInfo

    service_info_module = ModuleType("homeassistant.helpers.service_info")
    usb_module = ModuleType("homeassistant.helpers.service_info.usb")
    usb_module.UsbServiceInfo = UsbServiceInfo
    sys.modules[service_info_module.__name__] = service_info_module
    sys.modules[usb_module.__name__] = usb_module
    # Keep the test dependency compatible with the official 2026.3 scanner
    # contract. Individual tests patch this existing attribute strictly.
    legacy_usb.scan_serial_ports = lambda: ()


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> Generator[None]:
    """Enable loading custom integrations for every test."""
    yield
