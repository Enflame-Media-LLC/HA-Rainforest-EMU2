# Rainforest EMU-2 and RAVEn Enhanced Integration Design

**Status:** Approved  
**Date:** 2026-09-04  
**Repository:** https://github.com/Enflame-Media-LLC/HA-Rainforest-EMU2  
**Integration domain:** `rainforest_emu2`  
**Minimum Home Assistant version:** 2026.3.0

## Purpose

Build a HACS-distributable Home Assistant custom integration for Rainforest Automation EMU-2 and legacy RAVEn USB devices. The integration retains the entities and local-polling behavior of Home Assistant's built-in `rainforest_raven` integration while correcting its timeout, serial-device lifecycle, discovery, identity, recovery, diagnostics, and usability problems.

## Decisions

- The integration uses the distinct `rainforest_emu2` domain and does not override Home Assistant's built-in integration.
- It supports EMU-2 USB VID/PID `04B4:0003` and legacy RAVEn USB VID/PID `0403:8A28`.
- It supports Home Assistant 2026.3.0 and newer.
- HACS is the installation and update mechanism.
- The repository is not a Home Assistant App repository. Apps are containerized Supervisor services and are inappropriate for an in-process Python integration.
- The integration uses the upstream `aioraven==0.7.1` package without maintaining a fork.
- A local communication layer isolates Home Assistant code from `aioraven` lifecycle and timeout behavior.
- Releases use semantic versioning beginning with `v0.1.0`.
- Source adapted from Home Assistant and `aioraven` retains Apache-2.0 attribution.

## Non-goals

- Network-attached or serial-over-IP RAVEn devices.
- Automatically deleting, disabling, or mutating a built-in `rainforest_raven` config entry.
- Modifying meter pairing, utility enrollment, Zigbee network configuration, pricing, schedules, or device firmware.
- Reimplementing the RAVEn XML protocol.
- Installing the integration through a privileged Home Assistant App container.

## Repository layout

```text
.
├── .github/
│   ├── dependabot.yml
│   └── workflows/
│       ├── hacs.yml
│       ├── hassfest.yml
│       ├── lint.yml
│       ├── release.yml
│       └── test.yml
├── brand/
│   └── icon.png
├── custom_components/
│   └── rainforest_emu2/
│       ├── brand/
│       │   └── icon.png
│       ├── translations/
│       │   └── en.json
│       ├── __init__.py
│       ├── communication.py
│       ├── config_flow.py
│       ├── const.py
│       ├── coordinator.py
│       ├── diagnostics.py
│       ├── manifest.json
│       ├── sensor.py
│       └── strings.json
├── docs/
│   └── superpowers/
│       ├── plans/
│       └── specs/
├── tests/
│   ├── conftest.py
│   └── components/
│       └── rainforest_emu2/
├── .gitignore
├── CHANGELOG.md
├── LICENSE
├── README.md
├── hacs.json
└── pyproject.toml
```

## Component boundaries

### `communication.py`

Owns all interaction with `RAVEnSerialDevice`.

It provides:

- Supported-device filtering.
- Stable-path selection.
- Explicit open, synchronize, query, close, and abort operations.
- Per-operation deadlines.
- Retry and backoff policy.
- Meter-list discovery without a redundant second request.
- Conversion of dependency exceptions into stage-specific integration exceptions.
- Safe cleanup after normal completion, timeout, cancellation, and transport failure.
- A single connection lock so setup, polling, reload, and shutdown cannot use the same serial port concurrently.

Home Assistant config-flow and coordinator code do not instantiate or close `RAVEnSerialDevice` directly.

### `config_flow.py`

Owns USB discovery, manual supported-device selection, migration assistance, validation, user-facing errors, permanent identity, and config-entry creation.

### `coordinator.py`

Owns the persistent connection, scheduled data refreshes, reconnect behavior, device information, and integration availability.

### `sensor.py`

Exposes:

- Power demand.
- Total energy delivered.
- Total energy received.
- Energy price.
- Signal strength.

The entity model follows the built-in integration unless a change is required to correct an identified defect.

### `diagnostics.py`

Reports connection stage, selected stable path type, device model and firmware, last success time, reconnect count, timeout count, configured meters, and coordinator data. Device and meter MAC addresses, filesystem paths, and other identifiers are redacted.

## USB discovery and path handling

Manual setup lists only the two supported VID/PID combinations.

Selector values use collision-safe opaque values or device paths. Human-readable labels are presentation only and are never dictionary keys. Labels include model description, current device path, physical USB location when available, and USB serial number when available.

Path preference is:

1. `/dev/serial/by-id/...`
2. A validated discovery-provided persistent link.
3. The current `/dev/ttyACM*` or `/dev/ttyUSB*` path.

The config entry stores the best known path plus USB identity metadata. On later USB discovery, a matching hardware MAC updates the stored path.

USB discovery does not create a permanent identity from VID, PID, manufacturer, description, or the string `None`. Before communication establishes a hardware identity, the flow uses Home Assistant's discovery-without-unique-ID mechanism to avoid false collisions. After reading `DeviceInfo.device_mac_id`, the normalized hardware MAC becomes the permanent config-entry unique ID.

Two identical devices without USB serial numbers remain independently selectable and receive different permanent identities.

## Validation state machine

Validation follows these stages:

1. Confirm that the selected device still exists and still matches a supported VID/PID.
2. Open the serial transport.
3. Allow a short cold-start settling interval.
4. Request `MeterList` using the synchronization policy.
5. Reuse the successful `MeterList` response.
6. Request each meter's information independently.
7. Retain electric meters and meters whose type is absent.
8. Skip and log malformed or unsupported meter records without invalidating valid meters.
9. Request device information and obtain the hardware MAC.
10. Close the validation connection safely.
11. Present the discovered electric meters for selection.

An empty valid meter list produces a dedicated `no_paired_meters` error with pairing guidance. A non-empty list containing no usable electric meters produces `no_supported_meters`.

Automatic USB discovery presents a retryable confirmation form after transient communication failure. It does not permanently abort the flow merely because the first attempt timed out.

## Timeouts and retry policy

There is no single timeout covering multiple protocol operations.

Initial defaults:

- Serial open: 5 seconds.
- Cold-start settling delay: 250 milliseconds.
- Meter-list attempt: 2 seconds.
- Meter-list attempts: 3.
- Delay between attempts: 250 milliseconds, then 500 milliseconds.
- Device-information query: 3 seconds.
- Meter-information query: 3 seconds per meter.
- Polling query: 3 seconds per command.
- Graceful close: 2 seconds.
- Forced abort completion: 2 seconds.

Timeout values are constants so tests can replace them without sleeping.

A polling command receives one retry after the integration aborts the bad transport, reopens it, and re-synchronizes. The coordinator does not combine every meter query into one global deadline.

## Lifecycle and cleanup

The communication layer uses explicit lifecycle management rather than `async with RAVEnSerialDevice(...)`.

- Successful validation attempts a graceful close within two seconds.
- Any timeout, cancellation, parsing failure, write failure, or connection failure uses forced abort.
- If graceful close times out, the integration falls back to forced abort.
- If abort completion also times out, the integration logs the failure, discards the device object, and permits a later clean reconnect.
- Cleanup never masks the original exception.
- Reload and Home Assistant shutdown use bounded graceful close followed by abort.
- A connection is not published to the coordinator until open, synchronization, and device-information retrieval succeed.

## Polling and recovery

The coordinator polls every 30 seconds.

For each selected meter it independently requests summation, instantaneous demand, and price. It then requests network information. One malformed optional response does not discard valid data returned by other commands during the same cycle.

Transport errors and timeouts mark the refresh unsuccessful, force-abort the connection, and reconnect on the next attempt. The coordinator never continues using a transport that experienced a timeout.

Home Assistant controls entity availability through the coordinator's update state. The integration does not silently report stale values as current.

Connection retries are serialized with a lock. Setup, polling, reload, and shutdown cannot open competing handles for the same integration entry.

## Error model and logging

The communication layer raises typed errors containing a safe stage identifier:

- `DeviceBusyError`
- `DevicePermissionError`
- `DeviceMissingError`
- `DeviceOpenTimeoutError`
- `DeviceResponseTimeoutError`
- `MeterListUnavailableError`
- `NoPairedMetersError`
- `NoSupportedMetersError`
- `MalformedMeterError`
- `DeviceCleanupError`
- `DeviceCommunicationError`

Config-flow translations provide a specific corrective message for every error category.

Debug logging records:

- Connection stage.
- Attempt number.
- Elapsed duration.
- Path strategy, without the complete path.
- Response stanza type.
- Retry, reconnect, close, and abort outcomes.

Logs do not include full meter MAC addresses, device MAC addresses, utility account values, or raw XML payloads.

## Built-in integration migration

If one or more `rainforest_raven` entries exist, the new user flow offers an import option.

The import:

1. Reads the old device path and selected meter MAC addresses.
2. Requires the built-in entry to be disabled or removed before opening the port.
3. Rediscovers and revalidates the physical device.
4. Uses the hardware MAC as the new unique ID.
5. Creates a new `rainforest_emu2` entry with validated current data.

The integration never disables or deletes the built-in entry automatically.

The README explains that entity IDs belong to a different integration domain and may need dashboard, automation, and Energy Dashboard updates. It includes a rollback procedure.

## HACS and release packaging

The custom integration manifest includes:

- `domain`
- `name`
- `version`
- `documentation`
- `issue_tracker`
- `codeowners`
- `config_flow`
- `dependencies`
- `integration_type`
- `iot_class`
- `requirements`
- USB matchers for both supported products.

`hacs.json` declares an integration repository and Home Assistant 2026.3.0 as the minimum supported version.

The repository contains the brand asset required by HACS and an integration-local asset for Home Assistant 2026.3 and newer.

GitHub releases use tags such as `v0.1.0`. The release workflow produces a ZIP whose root contains `custom_components/rainforest_emu2`. Users may alternatively install the default branch through HACS as a custom integration repository.

## Test strategy

Tests use pytest and Home Assistant's custom-component test tooling. Production behavior is developed test-first.

Required config-flow tests:

- EMU-2 discovery.
- Legacy RAVEn discovery.
- Manual setup filters unrelated serial devices.
- Delayed cold-start succeeds.
- Each connection stage maps to the correct error.
- Timeout cleanup uses abort and returns within its own bound.
- Successful cleanup falls back from close to abort.
- Empty meter list.
- No supported electric meters.
- Mixed electric, absent-type, unsupported, and malformed meter records.
- Multiple identical devices without USB serial numbers.
- Display-label collisions cannot remove devices.
- Device path changes between scans.
- Concurrent and duplicate flows.
- Hardware-MAC unique ID.
- Retryable automatic discovery.
- Safe built-in-entry import.

Required coordinator tests:

- Successful first refresh.
- Multiple meters do not share one global deadline.
- One optional malformed response preserves other valid data.
- Timeout aborts the connection.
- Retry reconnects and re-synchronizes once.
- Failed retry reports an unsuccessful refresh.
- Reconnect uses an updated path.
- Reload and shutdown cleanup are bounded.
- Concurrent refresh and shutdown are serialized.
- Device registry and all sensor states remain correct.

Required packaging checks:

- HACS validation.
- Hassfest.
- Ruff format and lint.
- Type checking.
- Tests at the Home Assistant 2026.3.0 floor.
- Tests on the current supported Home Assistant release.
- Release ZIP layout verification.

## Acceptance criteria

- The repository can be added to HACS as a custom Integration repository.
- HACS installs files into `custom_components/rainforest_emu2`.
- Home Assistant 2026.3.0 or newer loads the integration without overriding `rainforest_raven`.
- Both specified USB products are discoverable and manually selectable.
- A successful meter-list response is not requested twice during validation.
- No multi-command operation is governed by one shared five-second timeout.
- A blocked close cannot keep setup, reload, or shutdown pending indefinitely.
- Devices without USB serial numbers do not collide.
- Saved paths recover after USB device renumbering.
- All defined error categories have user-facing translations and diagnostic logging.
- The complete automated test and repository-validation suite passes before `v0.1.0` is released.

## References

- Home Assistant built-in Rainforest RAVEn integration: https://github.com/home-assistant/core/tree/dev/homeassistant/components/rainforest_raven
- aioraven 0.7.1: https://github.com/cottsay/aioraven/tree/0.7.1
- Home Assistant custom integration structure: https://developers.home-assistant.io/docs/creating_integration_file_structure/
- Home Assistant config-flow unique IDs: https://developers.home-assistant.io/docs/core/integration/config_flow/
- HACS integration publishing requirements: https://www.hacs.xyz/docs/publish/integration/
- Home Assistant App repositories: https://developers.home-assistant.io/docs/apps/repository/
