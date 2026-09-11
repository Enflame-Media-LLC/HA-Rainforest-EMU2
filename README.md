# Rainforest EMU-2 and legacy RAVEn

A reliable, local polling Home Assistant custom integration for Rainforest
Automation EMU-2 and legacy RAVEn energy monitors. It supports USB passthrough,
containers, and virtual machines where serial paths can move or disappear.

## Requirements

- Home Assistant 2026.3.1 or newer
- Python 3.14.2 or newer when Home Assistant installs the integration
- Rainforest EMU-2 (`04B4:0003`) or legacy RAVEn (`0403:8A28`) USB gateway
- The gateway serial device exposed to Home Assistant

Communication is local. A Rainforest cloud account, install code, link key, or
utility credentials are not required.

## HACS installation

1. Open HACS in Home Assistant.
2. Open the menu, choose **Custom repositories**, and add
   `https://github.com/Enflame-Media-LLC/HA-Rainforest-EMU2` as category
   **Integration**.
3. Download **Rainforest EMU-2 and RAVEn Enhanced**.
4. Restart Home Assistant.
5. Go to **Settings → Devices & services → Add integration** and choose
   **Rainforest EMU-2 and RAVEn Enhanced**.

This repository is a Home Assistant custom integration. It cannot be installed
as a Home Assistant App repository.

## Manual installation

Download the repository and copy `custom_components/rainforest_emu2` into the
`custom_components` directory in your Home Assistant configuration directory.
Restart Home Assistant, then add the integration from **Settings → Devices &
services**.

## Add device

Connect the gateway before opening setup. Select the USB device reported by
Home Assistant, confirm it, validate the gateway, and choose the meters to
publish. The serial port is checked before it is opened.

If the device is not listed, choose **Rescan**. For containers and VMs, choose
**Advanced path** and enter the path exposed inside Home Assistant, such as
`/dev/ttyACM0`. The path is checked and the gateway is validated before an
entry is created.

## USB permissions and passthrough

The Home Assistant process needs read/write access to the serial device. In a
container, pass through the device and its group permissions, for example with
Docker's `--device` option. In a VM, attach the gateway to the Home Assistant
guest and confirm that the guest sees the device path. Avoid sharing one
gateway between two integrations at the same time.

When a raw `/dev/ttyACM*` or `/dev/ttyUSB*` path changes, the integration can
repair it only when the saved VID, PID, and non-empty USB serial identify one
device. Serial-less or ambiguous devices require manual selection.

## Error guide

- **No compatible devices found:** confirm USB passthrough, permissions, and
  that the gateway is connected, then rescan.
- **Device is busy:** stop another serial client or integration and retry.
- **Open or response timeout:** the EMU-2 serial protocol can take up to four
  seconds to answer. Check the cable, USB power, VM passthrough, and host load;
  Home Assistant retries transient failures automatically. If the host log
  contains `can't set config #1, error -32`, the Linux USB device has not
  completed enumeration yet, so changing the integration timeout will not
  help. Use a known-good data cable, a direct USB 2 port, and no hub/KVM, then
  verify that `/dev/serial/by-id` or `/dev/ttyACM*` exists before retrying.
- **Configuration requires reconfigure:** the gateway identity or saved meter
  list changed. Open **Reconfigure** and validate the current path.
- **Some sensors unavailable:** the latest cycle did not include that optional
  field. Required energy values remain independent of price or signal parsing.

## Reconfigure

Open the integration entry and choose **Reconfigure** to change the serial path
or meter selection. The existing client validates the candidate while holding
its lifecycle ownership. A mismatched gateway is rejected and the original
connection is restored. The same config entry is updated.

## Built-in migration

The built-in `rainforest_raven` integration and this integration have different
domains. To migrate safely:

1. Record existing entity IDs and automations, dashboards, helpers, scripts,
   and Energy Dashboard references that use them.
2. Disable the built-in Rainforest RAVEn entry.
3. Add this integration and use **Import built-in entry** when offered.
4. Confirm the imported path, validate the gateway, and select meters that are
   still present.
5. Update consumers to the new entity IDs.
6. Observe at least one complete polling interval and confirm readings.
7. Remove the old entry only after the new entities are working.

An enabled built-in entry remains the owner of its port and cannot be imported.
The old entry and its entity registry records are not modified by this flow.

## Entity-ID migration checklist

Entity IDs may differ from the built-in integration. Search automations,
scripts, scenes, dashboards, helpers, and notification templates for each
recorded old entity ID. Replace every reference, reload automations, and test
one normal polling cycle before removing the old entry.

## Energy Dashboard update

After migration, remove old consumed-energy entities from the Energy Dashboard
and add the new delivered-energy entity. Confirm the unit is `kWh`, keep the
same statistic type, and verify that the first statistics increase from the
expected baseline. Retain the old entity until the new statistics are checked.

## Rollback

Disable this integration and re-enable the built-in `rainforest_raven` entry if
you need to return to the previous integration. If the built-in entry was
removed, add it again using its documented setup flow. Restore recorded entity
IDs in automations and dashboards as needed.

## Diagnostics and privacy

Diagnostics contain safe connection strategy, a raw-device basename when
applicable, model and firmware, counters, field presence, and meter indexes.
Hardware and meter MAC addresses, USB serials, serial-bearing by-id paths,
account data, install codes, link keys, and raw XML are excluded. Logs use
stable stage and reason values rather than dependency exception text.

## Supported devices

- Rainforest Automation EMU-2 USB gateway (`04B4:0003`)
- Rainforest Automation legacy RAVEn USB gateway (`0403:8A28`)

Both gateways use the same local protocol boundary. Device identity comes from
the gateway-reported hardware MAC after validation.

## Support

Include Home Assistant version, integration version, device family, connection
strategy, and redacted diagnostics when opening an issue. Do not include USB
serials, hardware or meter MAC addresses, account numbers, install codes, link
keys, or raw protocol captures.

Report issues at
<https://github.com/Enflame-Media-LLC/HA-Rainforest-EMU2/issues>.

## License and attribution

This project is licensed under the Apache License 2.0. It uses the `aioraven`
package for the Rainforest serial protocol boundary. See `LICENSE` for the
complete license text.
