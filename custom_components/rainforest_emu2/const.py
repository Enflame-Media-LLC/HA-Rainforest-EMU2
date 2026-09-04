"""Constants for the Rainforest EMU-2 integration."""

from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "rainforest_emu2"
PLATFORMS = [Platform.SENSOR]

CONF_METERS = "meters"
CONF_USB_SERIAL = "usb_serial"
CONF_USB_VID = "usb_vid"
CONF_USB_PID = "usb_pid"

SUPPORTED_USB_IDS = frozenset({(0x04B4, 0x0003), (0x0403, 0x8A28)})

UPDATE_INTERVAL = timedelta(seconds=30)
OPEN_TIMEOUT = 5.0
SETTLE_DELAY = 0.5
QUERY_TIMEOUT = 3.0
METER_LIST_TIMEOUT = 2.0
METER_LIST_BACKOFFS = (0.0, 0.25, 0.5)
CLOSE_TIMEOUT = 2.0
ABORT_TIMEOUT = 2.0
LOCK_TIMEOUT = 1.0
WATCHDOG_MARGIN = 1.0
