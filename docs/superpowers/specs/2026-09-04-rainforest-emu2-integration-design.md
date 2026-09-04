# Rainforest EMU-2 and RAVEn Enhanced Integration Design

**Status:** Revised after usability and setup review; awaiting re-approval  
**Date:** 2026-09-04  
**Repository:** https://github.com/Enflame-Media-LLC/HA-Rainforest-EMU2  
**Integration domain:** `rainforest_emu2`  
**Minimum Home Assistant version:** 2026.3.0

## Purpose

Build a HACS-distributable Home Assistant custom integration for Rainforest Automation EMU-2 and legacy RAVEn USB devices. The integration retains the useful entities and local-polling behavior of Home Assistant's built-in `rainforest_raven` integration while correcting its timeout, serial lifecycle, discovery, identity, recovery, diagnostics, migration, and setup problems.

## Decisions

- The integration uses the distinct `rainforest_emu2` domain and does not override Home Assistant's built-in integration.
- It supports EMU-2 USB VID/PID `04B4:0003` and legacy RAVEn USB VID/PID `0403:8A28`.
- It supports Home Assistant 2026.3.0 and newer and is tested at both the version floor and the current supported release.
- HACS is the installation and update mechanism. The repository is not a Home Assistant App repository because Apps are Supervisor-managed containers, while this integration must run inside Home Assistant and use its config-entry and entity APIs.
- The integration uses upstream `aioraven==0.7.1` without maintaining a fork.
- A local communication layer isolates Home Assistant code from `aioraven` lifecycle, parsing, and timeout behavior.
- Releases use semantic versioning beginning with `v0.1.0`.
- Source adapted from Home Assistant and `aioraven` retains Apache-2.0 attribution.

## Corrections from the post-approval review

- Replace the impossible plan to reuse the return value of `aioraven.synchronize()`: version 0.7.1 returns `None`. The integration performs its own bounded `get_meter_list()` synchronization loop and returns the one successful response.
- Do not open a USB device merely because discovery fired. Automatic discovery first shows a confirmation form; communication starts only after user confirmation.
- Add a resilient manual scan. Home Assistant's USB scanner remains primary, with a direct PySerial scan fallback only when it fails or finds no supported device.
- Add an advanced local-device-path form for container or virtual-machine passthrough cases where VID/PID metadata is unavailable.
- Use an OS-level exclusive serial open on Linux/POSIX, plus an in-process shared port lock, to turn competing readers into a specific busy error instead of response splitting and misleading timeouts.
- Retry a complete refresh cycle on a new transport, never one command in the middle of a partially collected cycle.
- Add a derived cycle watchdog whose budget scales with the number of selected meters and commands. It is a safety backstop and cannot expire before the sum of the documented per-operation limits.
- Keep meters affected by `aioraven`'s known `MeterType("0x0000")` parse defect as unknown electric-capable meters with a generic label; unrelated malformed records are skipped independently.
- Replace a large exception-class hierarchy with one structured integration exception carrying stage, reason, retryability, and original cause.
- Add a reconfigure flow so users can rescan meters or repair a path without deleting and recreating the entry.
- Make cleanup precedence explicit: cleanup never masks an earlier error, but cleanup failure after otherwise successful validation fails setup because the port state is uncertain.
- Make migration limitations explicit. The custom domain cannot safely preserve entity IDs while disabled built-in entities still reserve them; the integration does not mutate the entity registry automatically.
- Use normal HACS integration layout rather than a custom release ZIP. `hacs.json` contains only supported keys, and releases are published as GitHub Releases rather than tags alone.
- Add required GitHub repository description and topics to the release checklist. Use `@ENFM-RyanJ`, the repository creator, as the initial manifest code owner.

## Non-goals

- Network-attached or serial-over-IP devices.
- Automatically deleting, disabling, or mutating a built-in `rainforest_raven` config entry or its entity-registry records.
- Modifying meter pairing, utility enrollment, Zigbee network configuration, pricing, schedules, or device firmware.
- Reimplementing the RAVEn XML protocol.
- Installing through a privileged Home Assistant App container.
- Supporting arbitrary PySerial URL handlers.

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

Owns every `RAVEnSerialDevice` interaction and provides:

- Supported-device checks and stable-path selection.
- Explicit open, synchronize-by-meter-list, query, close, and abort operations.
- Per-operation deadlines, derived cycle budgets, retry, and backoff.
- A successful synchronization result containing the already-received meter list.
- Structured error translation with safe stage and reason values.
- Safe cleanup after normal completion, timeout, cancellation, parsing failure, and transport failure.
- An integration-wide port-lock registry keyed by canonical path and, after validation, hardware MAC.

Config-flow and coordinator code never instantiate, close, or abort `RAVEnSerialDevice` directly.

### `config_flow.py`

Owns automatic USB confirmation, manual discovery, advanced path entry, built-in-entry migration assistance, validation, meter selection, reconfiguration, user-facing errors, permanent identity, and config-entry creation.

### `coordinator.py`

Owns the persistent connection, scheduled refreshes, full-cycle reconnect behavior, device information, partial-field availability, and integration availability.

### `sensor.py`

Exposes power demand, total energy delivered, total energy received, energy price, and signal strength. The entity model follows the built-in integration unless a change is required to correct an identified defect.

### `diagnostics.py`

Reports safe stage/reason values, stable-path strategy, device model and firmware, last success time, reconnect and timeout counters, configured meter count, and coordinator data. Device and meter MAC addresses and utility/account identifiers are redacted. Diagnostics may show a safe basename such as `ttyACM0`; a `/dev/serial/by-id` value is reduced to its path strategy and never exposes its serial-bearing filename.

## USB discovery and path handling

Automatic discovery uses manifest matchers for the two supported VID/PID pairs. Invoking `async_step_usb` stores the discovery record, prevents duplicate flows with `is_matching`/matching-flow checks, and displays a confirmation form. It never creates an entry or opens the port before confirmation.

Manual setup performs these steps:

1. Call Home Assistant's `usb.async_scan_serial_ports()`.
2. If that scan raises an error or yields no supported match, call PySerial's `serial.tools.list_ports.comports()` in Home Assistant's executor.
3. Normalize and merge records by canonical device path.
4. Show only the two supported VID/PID combinations in the normal selector.
5. Offer an explicitly labeled advanced local-path form when metadata is missing because of container or VM passthrough.

The advanced form accepts a nonempty local serial-device path, rejects URL schemes, and performs the same exclusive-open and hardware validation as discovered devices. It explains that the Home Assistant host/container must already have permission to access the path.

Selector values are collision-safe opaque tokens mapped internally to scan records. Human-readable labels are presentation only and include model, current path, physical USB location when available, and USB serial number when available.

Path preference is:

1. `/dev/serial/by-id/...`
2. A validated discovery-provided persistent link.
3. The current `/dev/ttyACM*` or `/dev/ttyUSB*` path.

The config entry stores the best path plus normalized USB identity metadata. After a confirmed discovery reads the hardware MAC, `async_set_unique_id()` followed by `_abort_if_unique_id_configured(updates={CONF_DEVICE: ...})` updates the saved path of an existing entry and aborts the duplicate setup flow.

USB discovery does not derive permanent identity from VID, PID, manufacturer, description, a transient path, or the string `None`. If a real USB serial number is available, it may identify and deduplicate the discovery flow only; the device-reported hardware MAC still replaces it before entry creation. Without a USB serial number, the flow uses `_async_handle_discovery_without_unique_id()` and matching-flow checks. Home Assistant intentionally limits that helper once an entry exists, so an additional serial-less unit is added through the manual selector/reconfigure path rather than unsafe automatic identity guessing. After `DeviceInfo.device_mac_id` is read and normalized with Home Assistant's MAC formatter, it becomes the permanent config-entry unique ID. Two identical serial-less devices remain independently selectable manually because their paths are separate during selection and their hardware MACs differ after validation.

## Setup and reconfigure flow

The user-visible sequence is:

1. Choose a detected device, choose advanced local path, or choose import from a disabled built-in entry.
2. Confirm the device and see concise permission/contention guidance before communication begins.
3. Validate the device with stage-specific progress/error text and a retry action that keeps entered data.
4. Select one or more discovered meters; imported meter MACs are preselected when still present.
5. Create the entry using the hardware MAC as unique ID.

If no supported device is found, the flow stays actionable instead of aborting: it offers rescan and advanced-path actions. Errors distinguish missing device, permission denied, port busy, open timeout, no protocol response, no paired meters, unsupported meters, malformed identity, and unknown failure.

A reconfigure entry action rescans/revalidates the current or replacement local path, verifies that the hardware MAC still matches the entry, refreshes the meter list, permits meter reselection, and updates/reloads the existing entry. It never creates a second entry.

## Validation state machine

Validation follows these stages:

1. Confirm the selected path still exists; for normal discovery, also confirm the supported VID/PID.
2. Acquire the shared port lock and open the serial transport exclusively where supported.
3. Allow a 500 millisecond cold-start settling interval.
4. Run a local synchronization loop by calling `get_meter_list()` with the retry policy below.
5. Return and reuse that one successful `MeterList`; do not call `aioraven.synchronize()` and do not request the list again.
6. Request device information and require a valid hardware MAC.
7. Request each listed meter's information independently.
8. Retain electric meters and meters whose type is absent.
9. If the dependency raises its known `0x0000` meter-type `ValueError`, retain that meter as unknown with a generic, redacted label. Skip and log other malformed or explicitly unsupported records without invalidating valid meters.
10. Close validation safely and release the port lock.
11. Present usable meters for selection.

An actual empty `MeterList` produces `no_paired_meters` with pairing guidance. A nonempty list containing no usable meters produces `no_supported_meters`. A `None` result or deadline expiry is a response failure, not an empty paired-meter list.

## Timeouts, retries, and cancellation

Each operation has its own deadline:

- Serial open: 5 seconds.
- Cold-start settling delay: 500 milliseconds.
- Meter-list attempt: 2 seconds.
- Meter-list attempts: 3.
- Delay between meter-list attempts: 250 milliseconds, then 500 milliseconds.
- Device-information query: 3 seconds.
- Meter-information query: 3 seconds per meter.
- Polling query: 3 seconds per command.
- Graceful close: 2 seconds.
- Forced-abort completion: 2 seconds.

Timeout and delay values are constants so tests replace them without sleeping. Retries use monotonic time and remain cancellation-safe. `CancelledError` always propagates after bounded cleanup.

The meter-list retry loop reuses the same newly opened transport because every attempt asks for the same idempotent response. Any timeout during later heterogeneous polling makes that transport untrustworthy: the integration aborts it, opens a fresh transport, resynchronizes, and reruns the entire refresh cycle exactly once. Data from the failed partial cycle is discarded and never mixed with retry data.

A computed watchdog wraps each full attempt only as a deadlock backstop. Its budget is at least the sum of open/settle/synchronization allowances, the number of scheduled commands multiplied by their individual limits, retry backoffs, cleanup allowance, and a small scheduling margin. A refresh with more meters therefore receives a proportionally larger budget. The two-attempt recovery path receives two independent attempt budgets.

## Lifecycle, ownership, and cleanup

Lifecycle is explicit rather than delegated to `async with RAVEnSerialDevice(...)`.

- On Linux/POSIX, construction requests `exclusive=True`; unsupported-platform keyword failures fall back only where OS exclusivity is unavailable.
- A shared in-process registry prevents config flow, reconfigure, setup, polling, reload, and shutdown from opening the same canonical port concurrently. Hardware MAC aliases are added after validation so renamed paths converge on the same lock.
- A connection is not published to the coordinator until open, meter-list synchronization, and device-information retrieval succeed.
- Successful validation attempts graceful close within two seconds.
- Timeout, cancellation, write/parse/transport failure, or graceful-close timeout uses forced abort.
- Abort is independently bounded. If it also times out, the device object is discarded and the error is logged safely.
- When an earlier error exists, cleanup errors are attached for diagnostics and never replace it.
- When work otherwise succeeded but cleanup failed, validation fails with `cleanup_failed` because the next open cannot be assumed safe.
- Reload and Home Assistant shutdown use bounded graceful close followed by abort and always release the registry lock in `finally`.

## Polling and partial-data behavior

The coordinator polls every 30 seconds. For each selected meter it requests summation, instantaneous demand, and price, then requests network information. Commands are serialized on the protocol connection.

Transport errors, timeouts, or an unusable core response fail the attempt, abort the connection, and trigger the one complete-cycle retry described above. If the retry fails, Home Assistant receives `UpdateFailed`; all coordinator entities become unavailable and stale values are not presented as current.

An isolated malformed or missing optional response does not discard other valid results. The returned coordinator data records field-level presence, and each entity is available only when both the coordinator succeeded and its own field is present. Price or signal-strength parsing therefore cannot silently preserve an old value or make otherwise valid energy data unavailable.

During initial config-entry setup, retryable missing, busy, open, and response failures raise `ConfigEntryNotReady` so Home Assistant backs off and retries without user intervention. A permanent schema or identity mismatch raises `ConfigEntryError` with reconfigure guidance. Normal runtime refreshes use `UpdateFailed` only after the bounded recovery attempt is exhausted.

## Error model and privacy-safe logging

The communication layer raises one `RainforestCommunicationError` containing:

- `stage`: an enum such as `SCAN`, `LOCK`, `OPEN`, `SYNC`, `DEVICE_INFO`, `METER_INFO`, `POLL`, or `CLEANUP`.
- `reason`: an enum such as `MISSING`, `PERMISSION`, `BUSY`, `TIMEOUT`, `NO_RESPONSE`, `MALFORMED`, `UNSUPPORTED`, or `IO`.
- `retryable`: whether retry is useful without changing configuration.
- `cause`: the original exception, retained for exception chaining but not exposed to UI or diagnostics.

Config-flow translations map safe `(stage, reason)` combinations to concise corrective messages. Unknown combinations use a generic error and retain traceback logging.

Debug logs include stage, reason, attempt number, elapsed duration, path strategy, response stanza type, and retry/reconnect/cleanup outcomes. They exclude raw XML, complete paths, device and meter MACs, USB serial numbers, utility accounts, and link/install keys.

## Built-in integration migration

When `rainforest_raven` entries exist, the user flow offers import assistance. It:

1. Reads the old device path and selected meter MACs without changing the old entry.
2. Requires the old entry to be disabled or removed before the new integration opens the port.
3. Revalidates the physical device and obtains its current hardware MAC.
4. Preselects imported meters that are still present and asks the user to confirm.
5. Creates the new `rainforest_emu2` entry only after validation.

The two integrations must never own the port simultaneously. Import does not preserve or rewrite entity IDs automatically: disabled built-in entities can still reserve those IDs, and the custom integration uses a different domain/unique-ID scheme. The README provides a safe sequence for recording old entity IDs, disabling the built-in entry, installing and validating the new entry, updating dashboards/automations/Energy Dashboard, and rolling back. It does not promise zero-touch migration.

## HACS and release packaging

The component manifest includes `domain`, `name`, `version`, `documentation`, `issue_tracker`, `codeowners`, `config_flow`, `dependencies`, `integration_type`, `iot_class`, `requirements`, and USB matchers for both products. Initial `codeowners` is `["@ENFM-RyanJ"]`.

The root `hacs.json` uses only supported keys:

```json
{
  "name": "Rainforest EMU-2 and RAVEn Enhanced",
  "homeassistant": "2026.3.0"
}
```

The normal repository layout places the single integration under `custom_components/rainforest_emu2`; `content_in_root`, `zip_release`, and `filename` are therefore omitted. HACS downloads the integration from the selected Git ref. The release workflow verifies that the tag and manifest versions match and publishes an actual GitHub Release; it does not build a custom ZIP.

Before release, the public repository receives a concise description, topics including `home-assistant`, `hacs`, `rainforest-automation`, `emu2`, and `raven`, a complete README, root `hacs.json`, and root `brand/icon.png`. The integration-local brand copy supports Home Assistant versions that load local custom-integration assets.

## Test strategy

Production behavior is developed test-first with pytest and Home Assistant's custom-component testing support.

Config-flow and discovery coverage includes:

- EMU-2 and legacy RAVEn automatic discovery show confirmation before opening.
- Automatic discovery never finishes without user confirmation.
- Home Assistant scanner success, scanner exception, zero-match PySerial fallback, and merged deduplication.
- Unsupported ports remain hidden from the normal selector.
- Advanced path rejects URL schemes and validates permission, busy, missing, wrong-device, and timeout cases.
- Rescan remains on an actionable form when no device is found.
- Opaque selector values survive duplicate human-readable labels.
- Delayed cold-start and the third meter-list attempt can succeed.
- One successful meter-list result is reused; no second list query occurs.
- Empty, missing, unsupported, mixed, malformed, and known `0x0000` meter-type cases.
- Multiple identical devices without USB serial numbers.
- Serial-less automatic discovery follows Home Assistant's one-entry safety limit and directs additional-device setup to the manual flow.
- Duplicate/concurrent flows and hardware-MAC unique identity.
- Replug/path change updates an existing entry after confirmed hardware identity.
- Retry forms preserve the selected path and user input.
- Built-in import requires disabled/removed ownership and preselects still-present meters.
- Reconfigure updates one existing entry, rejects hardware-MAC mismatch, and never creates another.

Communication and coordinator coverage includes:

- Exclusive-open support and unsupported-platform fallback.
- Shared locking across config flow, coordinator, reload, and shutdown.
- Open, synchronization, query, close, and abort deadlines.
- Cancellation propagates after cleanup.
- Original failures survive cleanup failures; successful work plus cleanup failure fails safely.
- Successful first refresh and per-field availability.
- Derived cycle budget grows with command count and cannot preempt valid per-command budgets.
- Poll timeout discards partial data, aborts, reconnects, resynchronizes, and retries the entire cycle once.
- Failed retry raises `UpdateFailed`; a successful retry publishes only retry-cycle data.
- Reconnect uses an updated stable path.
- Reload and shutdown remain bounded and release locks.
- Device registry and all sensor states remain correct on the minimum and current Home Assistant versions.

Packaging checks include HACS Action validation, Hassfest, Ruff format/lint, type checking, tests at Home Assistant 2026.3.0, tests at the current supported release, manifest/tag version consistency, and verification that exactly one integration exists under `custom_components`.

## Acceptance criteria

- The repository can be added to HACS as a custom Integration repository and installs to `custom_components/rainforest_emu2`.
- Home Assistant 2026.3.0 or newer loads it without overriding `rainforest_raven`.
- Both specified USB products are discoverable and manually selectable.
- Discovery always requires user confirmation before the port is opened.
- Manual setup remains possible when USB metadata is missing.
- A successful meter-list response is requested exactly once during validation.
- No shared fixed five-second deadline covers multiple commands.
- Every wait, including cleanup and cycle recovery, is bounded and cancellation-safe.
- Competing port ownership produces a specific busy error rather than split responses/timeouts.
- Devices without USB serial numbers do not collide, and saved paths recover after renumbering.
- The known `0x0000` meter-type defect does not hide an otherwise usable meter.
- Partial retry data is never published or mixed with fresh-cycle data.
- Setup/reconfigure errors retain user input and give a corrective next action.
- Migration never mutates the built-in entry or entity registry and clearly discloses entity-ID work.
- Repository metadata, manifests, assets, HACS validation, Hassfest, lint, typing, and the full test matrix pass before `v0.1.0` is published as a GitHub Release.

## References

- Home Assistant built-in Rainforest RAVEn integration: https://github.com/home-assistant/core/tree/dev/homeassistant/components/rainforest_raven
- `aioraven` 0.7.1 streams implementation: https://github.com/cottsay/aioraven/blob/0.7.1/aioraven/streams.py
- `aioraven` 0.7.1 meter parsing: https://github.com/cottsay/aioraven/blob/0.7.1/aioraven/device.py
- Home Assistant custom integration structure: https://developers.home-assistant.io/docs/creating_integration_file_structure/
- Home Assistant config-flow identity and confirmation requirements: https://developers.home-assistant.io/docs/core/integration/config_flow/
- HACS general publishing requirements: https://www.hacs.xyz/docs/publish/start/
- HACS integration publishing requirements: https://www.hacs.xyz/docs/publish/integration/
- Home Assistant App repositories: https://developers.home-assistant.io/docs/apps/repository/
