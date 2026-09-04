# Rainforest EMU-2 and RAVEn Enhanced Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a production-ready HACS custom integration that reliably discovers, configures, polls, recovers, and diagnoses Rainforest EMU-2 and legacy RAVEn USB devices without the built-in integration's shared-timeout and serial-lifecycle failures.

**Architecture:** A distinct `rainforest_emu2` integration wraps upstream `aioraven==0.7.1` behind one communication boundary. Config flow owns discovery and validation, the coordinator owns persistent polling and full-cycle recovery, and entity/diagnostic layers consume immutable results without touching the serial transport.

**Tech Stack:** Python 3.13+, Home Assistant 2026.3.0+, `aioraven==0.7.1`, PySerial/`serial_asyncio_fast`, pytest, `pytest-homeassistant-custom-component`, Ruff, mypy, Hassfest, HACS Action, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-04-rainforest-emu2-integration-design.md`

## Global Constraints

- Domain is exactly `rainforest_emu2`; never shadow or replace `rainforest_raven`.
- Support EMU-2 `04B4:0003` and legacy RAVEn `0403:8A28` only in normal discovery.
- Minimum Home Assistant version is `2026.3.0`.
- Runtime dependency remains upstream `aioraven==0.7.1`; do not fork or vendor it.
- Never call `RAVEnSerialDevice` outside `communication.py`.
- Never call `aioraven.synchronize()`; synchronize through the single successful `get_meter_list()` result.
- Every serial wait, retry, close, abort, setup, refresh, reload, and shutdown path is bounded and cancellation-safe.
- Automatic USB discovery requires user confirmation before opening a port.
- Permanent config-entry identity is the normalized device-reported hardware MAC.
- Do not modify the built-in integration's config entry or entity registry.
- Use the standard HACS `custom_components/rainforest_emu2` layout without `zip_release`.
- Never log raw XML, complete paths, USB serials, hardware/meter MACs, utility accounts, install codes, or link keys.

---

## File map

| File | Responsibility |
|---|---|
| `custom_components/rainforest_emu2/const.py` | Domain, USB IDs, configuration keys, polling interval, timeout/backoff constants |
| `custom_components/rainforest_emu2/communication.py` | Port candidates, stable paths, locks, structured errors, lifecycle, validation, refresh, cleanup |
| `custom_components/rainforest_emu2/config_flow.py` | USB confirmation, resilient manual scan, advanced path, meter selection, import, reconfigure |
| `custom_components/rainforest_emu2/coordinator.py` | `DataUpdateCoordinator`, startup/retry mapping, immutable runtime snapshots |
| `custom_components/rainforest_emu2/sensor.py` | Five sensor descriptions, stable unique IDs, field-level availability |
| `custom_components/rainforest_emu2/diagnostics.py` | Redacted config-entry and runtime diagnostics |
| `custom_components/rainforest_emu2/__init__.py` | Entry setup/unload, typed runtime storage, update listener |
| `custom_components/rainforest_emu2/manifest.json` | Home Assistant metadata, dependency and USB matchers |
| `custom_components/rainforest_emu2/strings.json` | English source strings for flow, errors, aborts, entity names |
| `custom_components/rainforest_emu2/translations/en.json` | Generated/verified English translation copy |
| `tests/components/rainforest_emu2/conftest.py` | Shared fake device, port, entry, and fast-timeout fixtures |
| `tests/components/rainforest_emu2/test_communication.py` | Lifecycle, deadlines, locks, synchronization, parsing, recovery |
| `tests/components/rainforest_emu2/test_config_flow.py` | Discovery, manual setup, validation, import, reconfigure, identity |
| `tests/components/rainforest_emu2/test_coordinator.py` | Startup, polling, full-cycle retry, partial data, availability |
| `tests/components/rainforest_emu2/test_sensor.py` | Device registry, entity IDs, units, classes, state/availability |
| `tests/components/rainforest_emu2/test_diagnostics.py` | Redaction and safe operational diagnostics |
| `hacs.json`, `README.md`, `CHANGELOG.md`, `LICENSE` | Distribution, installation, migration, support, attribution |
| `.github/workflows/*.yml` | Test matrix, lint/type checks, Hassfest/HACS validation, guarded release |

---

### Task 1: Repository and manifest foundation

**Files:**
- Create: `custom_components/rainforest_emu2/__init__.py`
- Create: `custom_components/rainforest_emu2/const.py`
- Create: `custom_components/rainforest_emu2/manifest.json`
- Create: `custom_components/rainforest_emu2/strings.json`
- Create: `custom_components/rainforest_emu2/translations/en.json`
- Create: `hacs.json`
- Create: `pyproject.toml`
- Create: `tests/conftest.py`
- Create: `tests/components/rainforest_emu2/__init__.py`
- Create: `tests/components/rainforest_emu2/test_manifest.py`

**Interfaces:**
- Consumes: Approved domain, version floor, dependency, USB IDs, and code owner.
- Produces: `DOMAIN`, `PLATFORMS`, timeout constants, supported USB tuples, and a loadable custom-integration package.

- [ ] **Step 1: Write the failing manifest tests**

```python
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parents[3]
COMPONENT = ROOT / "custom_components" / "rainforest_emu2"


def test_manifest_contract() -> None:
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    assert manifest["domain"] == "rainforest_emu2"
    assert manifest["version"] == "0.1.0"
    assert manifest["requirements"] == ["aioraven==0.7.1"]
    assert manifest["dependencies"] == ["usb"]
    assert manifest["codeowners"] == ["@ENFM-RyanJ"]
    assert {(item["vid"], item["pid"]) for item in manifest["usb"]} == {
        ("04B4", "0003"),
        ("0403", "8A28"),
    }


def test_hacs_contract() -> None:
    hacs = json.loads((ROOT / "hacs.json").read_text())
    assert hacs == {
        "name": "Rainforest EMU-2 and RAVEn Enhanced",
        "homeassistant": "2026.3.0",
    }
```

- [ ] **Step 2: Run the tests and confirm the missing-file failure**

Run: `pytest tests/components/rainforest_emu2/test_manifest.py -q`

Expected: FAIL because `manifest.json` and `hacs.json` do not exist.

- [ ] **Step 3: Create the component metadata and constants**

Use this exact manifest payload:

```json
{
  "domain": "rainforest_emu2",
  "name": "Rainforest EMU-2 and RAVEn Enhanced",
  "version": "0.1.0",
  "codeowners": ["@ENFM-RyanJ"],
  "config_flow": true,
  "dependencies": ["usb"],
  "documentation": "https://github.com/Enflame-Media-LLC/HA-Rainforest-EMU2",
  "issue_tracker": "https://github.com/Enflame-Media-LLC/HA-Rainforest-EMU2/issues",
  "integration_type": "hub",
  "iot_class": "local_polling",
  "requirements": ["aioraven==0.7.1"],
  "usb": [
    {"vid": "04B4", "pid": "0003", "manufacturer": "*rainforest*", "description": "*emu-2*"},
    {"vid": "0403", "pid": "8A28", "manufacturer": "*rainforest*", "description": "*raven*"}
  ]
}
```

Create `const.py` with these public names:

```python
from datetime import timedelta

DOMAIN = "rainforest_emu2"
from homeassistant.const import Platform

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
```

Keep `__init__.py` import-safe until Task 7. Configure Ruff for Python 3.13 and 88-character formatting; enable `E`, `F`, `I`, `UP`, `B`, `ASYNC`, and `RUF` rules. Add the standard autouse `enable_custom_integrations` fixture in root `tests/conftest.py`.

- [ ] **Step 4: Run the foundation checks**

Run: `pytest tests/components/rainforest_emu2/test_manifest.py -q && ruff check custom_components tests`

Expected: PASS.

- [ ] **Step 5: Commit the foundation**

```bash
git add custom_components tests hacs.json pyproject.toml
git commit -m "chore: scaffold Rainforest EMU-2 integration"
```

---

### Task 2: Communication types, safe paths, and structured errors

**Files:**
- Create: `custom_components/rainforest_emu2/communication.py`
- Create: `tests/components/rainforest_emu2/test_communication.py`

**Interfaces:**
- Consumes: Timeout and USB constants from `const.py`.
- Produces: `FailureStage`, `FailureReason`, `RainforestCommunicationError`, `PortCandidate`, `MeterRecord`, `ValidationResult`, `MeterSnapshot`, `RavenSnapshot`, `snapshot_field_key()`, `canonical_port_key()`, `safe_path_details()`, `PortLockRegistry`, and `RavenClient`.

- [ ] **Step 1: Write failing tests for error shape, path privacy, and lock identity**

```python
async def test_canonical_symlinks_share_lock(tmp_path, monkeypatch):
    registry = PortLockRegistry()
    monkeypatch.setattr(os.path, "realpath", lambda path: "/dev/ttyACM0")
    assert registry.lock_for("/dev/serial/by-id/device") is registry.lock_for("/dev/ttyACM0")


def test_by_id_path_is_redacted() -> None:
    assert safe_path_details("/dev/serial/by-id/usb-Rainforest_SECRET-if00") == {
        "strategy": "by_id",
        "basename": None,
    }


def test_structured_error_does_not_render_cause() -> None:
    err = RainforestCommunicationError(
        FailureStage.OPEN,
        FailureReason.PERMISSION,
        retryable=False,
        cause=PermissionError("/dev/serial/by-id/SECRET"),
    )
    assert str(err) == "open:permission"
    assert "SECRET" not in str(err)
```

- [ ] **Step 2: Run the focused tests and confirm import failures**

Run: `pytest tests/components/rainforest_emu2/test_communication.py -q`

Expected: FAIL because the communication types do not exist.

- [ ] **Step 3: Implement the exact public data contract**

```python
class FailureStage(StrEnum):
    SCAN = "scan"
    LOCK = "lock"
    OPEN = "open"
    SYNC = "sync"
    DEVICE_INFO = "device_info"
    METER_INFO = "meter_info"
    POLL = "poll"
    CLEANUP = "cleanup"


class FailureReason(StrEnum):
    MISSING = "missing"
    PERMISSION = "permission"
    BUSY = "busy"
    TIMEOUT = "timeout"
    NO_RESPONSE = "no_response"
    MALFORMED = "malformed"
    UNSUPPORTED = "unsupported"
    IO = "io"


class RainforestCommunicationError(Exception):
    def __init__(self, stage, reason, *, retryable, cause=None):
        self.stage = stage
        self.reason = reason
        self.retryable = retryable
        self.cause = cause
        super().__init__(f"{stage.value}:{reason.value}")
```

Define frozen/slotted dataclasses with these fields:

```python
@dataclass(frozen=True, slots=True)
class PortCandidate:
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
    mac: bytes
    mac_hex: str
    name: str
    meter_type: str | None

@dataclass(frozen=True, slots=True)
class ValidationResult:
    path: str
    device_mac: str
    manufacturer: str | None
    model: str | None
    firmware: str | None
    meters: tuple[MeterRecord, ...]

@dataclass(frozen=True, slots=True)
class MeterSnapshot:
    demand: float | None
    delivered: float | None
    received: float | None
    price: float | None
    currency: str | None

@dataclass(frozen=True, slots=True)
class RavenSnapshot:
    meters: Mapping[str, MeterSnapshot]
    signal_strength: int | None
    present_fields: frozenset[str]
```

Use one field-key convention everywhere:

```python
def snapshot_field_key(field: str, meter_mac_hex: str | None = None) -> str:
    return f"meter:{meter_mac_hex}:{field}" if meter_mac_hex else f"device:{field}"
```

Meter keys are `demand`, `delivered`, `received`, and `price`; the device key is `signal_strength`. `PortLockRegistry.lock_for(path)` keys locks by `os.path.realpath(path)` and never logs the key. `PortLockRegistry.acquire(path)` is bounded by `LOCK_TIMEOUT` and maps contention to `LOCK:BUSY`; it never waits indefinitely. `PortLockRegistry.alias(path, device_mac)` records the validated identity against the same lock group so later paths can converge safely. `safe_path_details()` returns `{strategy, basename}` with no basename for `by_id`, and only `ttyACM*`/`ttyUSB*` basenames for raw devices.

- [ ] **Step 4: Run communication type tests**

Run: `pytest tests/components/rainforest_emu2/test_communication.py -q`

Expected: PASS for the new type/path/lock tests.

- [ ] **Step 5: Commit the communication contract**

```bash
git add custom_components/rainforest_emu2/communication.py tests/components/rainforest_emu2/test_communication.py
git commit -m "feat: define safe Rainforest communication contract"
```

---

### Task 3: Bounded open, synchronization, validation, and cleanup

**Files:**
- Modify: `custom_components/rainforest_emu2/communication.py`
- Modify: `tests/components/rainforest_emu2/conftest.py`
- Modify: `tests/components/rainforest_emu2/test_communication.py`

**Interfaces:**
- Consumes: Types from Task 2 and `RAVEnSerialDevice` from `aioraven.serial`.
- Produces: `RavenClient.async_validate() -> ValidationResult`, `RavenClient.async_shutdown() -> None`, and bounded private open/sync/cleanup methods.

- [ ] **Step 1: Add a controllable fake device fixture**

```python
class FakeRavenDevice:
    def __init__(self, path: str, **kwargs):
        self.path = path
        self.kwargs = kwargs
        self.calls: list[str] = []
        self.meter_list_results = deque()
        self.device_info = None
        self.meter_infos = {}

    async def open(self): self.calls.append("open")
    async def get_meter_list(self):
        self.calls.append("meter_list")
        result = self.meter_list_results.popleft()
        if isinstance(result, BaseException): raise result
        return result
    async def get_device_info(self): self.calls.append("device_info"); return self.device_info
    async def get_meter_info(self, *, meter=None):
        self.calls.append(f"meter_info:{meter.hex()}")
        result = self.meter_infos[meter]
        if isinstance(result, BaseException): raise result
        return result
    async def close(self): self.calls.append("close")
    async def abort(self): self.calls.append("abort")
```

- [ ] **Step 2: Write failing validation/lifecycle tests**

Cover these exact assertions in separately named tests:

```python
assert fake.kwargs.get("exclusive") is True
assert fake.calls.count("meter_list") == 3
assert result.meters[0].mac_hex == "0013500102030405"
assert fake.calls[-1] == "close"
```

Add cases where the third meter-list attempt succeeds, all attempts time out, close times out and abort succeeds, cancellation propagates after abort, missing device info fails, empty list maps to no paired meters, and successful work plus failed close/abort maps to `CLEANUP`. Add a held-lock case that completes within `LOCK_TIMEOUT` and raises `RainforestCommunicationError(FailureStage.LOCK, FailureReason.BUSY)`. After identity is read, assert `registry.alias(path, device_mac)` maps path and MAC to the same lock group.

- [ ] **Step 3: Run lifecycle tests and confirm failures**

Run: `pytest tests/components/rainforest_emu2/test_communication.py -q -k 'validate or cleanup or cancel or exclusive'`

Expected: FAIL because `RavenClient.async_validate()` is absent.

- [ ] **Step 4: Implement bounded lifecycle helpers**

Use `asyncio.timeout()` independently around open, each query, close, and abort. Construct the dependency as:

```python
kwargs = {"exclusive": True} if os.name == "posix" else {}
self._device = RAVEnSerialDevice(self._path, **kwargs)
```

Only retry constructor/open without `exclusive` when the failure proves the keyword is unsupported; never downgrade after `EACCES` or `EBUSY`. Translate `ENOENT`, `EACCES`/`EPERM`, and `EBUSY` into `MISSING`, `PERMISSION`, and `BUSY`.

Implement synchronization as:

```python
await asyncio.sleep(SETTLE_DELAY)
for attempt, backoff in enumerate(METER_LIST_BACKOFFS, start=1):
    if backoff:
        await asyncio.sleep(backoff)
    try:
        async with asyncio.timeout(METER_LIST_TIMEOUT):
            meter_list = await device.get_meter_list()
    except TimeoutError as err:
        if attempt == len(METER_LIST_BACKOFFS):
            raise RainforestCommunicationError(
                FailureStage.SYNC,
                FailureReason.TIMEOUT,
                retryable=True,
                cause=err,
            ) from err
        continue
    if meter_list is not None:
        return meter_list
raise RainforestCommunicationError(
    FailureStage.SYNC,
    FailureReason.NO_RESPONSE,
    retryable=True,
)
```

Do not call `device.synchronize()` anywhere. Recognize only the dependency's specific `"0x0000" is not a valid MeterType` `ValueError` as an unknown usable meter and label it with the final four hexadecimal characters; skip other malformed meter info. Always close/abort and release the port lock in `finally`. A persistent coordinator connection retains ownership until `async_shutdown()`; a competing flow receives the bounded busy error rather than waiting behind a refresh.

- [ ] **Step 5: Run lifecycle tests**

Run: `pytest tests/components/rainforest_emu2/test_communication.py -q`

Expected: PASS.

- [ ] **Step 6: Commit validation and cleanup**

```bash
git add custom_components/rainforest_emu2/communication.py tests/components/rainforest_emu2
git commit -m "feat: add bounded Raven validation lifecycle"
```

---

### Task 4: Persistent polling and complete-cycle recovery

**Files:**
- Modify: `custom_components/rainforest_emu2/communication.py`
- Modify: `tests/components/rainforest_emu2/test_communication.py`

**Interfaces:**
- Consumes: `RavenClient` lifecycle and snapshot dataclasses.
- Produces: `RavenClient.async_refresh(meter_macs: tuple[bytes, ...]) -> RavenSnapshot`, `RavenClient.async_set_path(path: str) -> None`, `RavenClient.async_reconfigure_path(path: str) -> ValidationResult`, and `RavenClient.device_info`.

- [ ] **Step 1: Write failing refresh/recovery tests**

Create two fake devices and verify:

```python
snapshot = await client.async_refresh((METER_A,))
assert first.calls[-1] == "abort"
assert second.calls[:3] == ["open", "meter_list", "device_info"]
assert snapshot.meters[METER_A.hex()].demand == 2.5
assert snapshot.meters[METER_A.hex()].delivered != 99.0
```

The first device returns valid summation then times out on demand; the second returns a complete cycle. Add tests for two failed attempts, optional price parse failure, optional network failure, per-field presence, path replacement, command-count-scaled watchdog calculation, and no command overlap. Add transactional reconfiguration tests: the current persistent connection closes before candidate validation, success leaves it closed for entry reload, and candidate failure restores the original path/connection without publishing candidate data.

- [ ] **Step 2: Run refresh tests and confirm failure**

Run: `pytest tests/components/rainforest_emu2/test_communication.py -q -k 'refresh or watchdog or partial'`

Expected: FAIL because refresh behavior is absent.

- [ ] **Step 3: Implement one-cycle collection and one full retry**

Implement `_async_collect_cycle()` with serialized 3-second command deadlines for summation, demand, price, then network information. Required transport timeouts/errors abort the attempt. Optional parse/empty results produce `None` and omit their `snapshot_field_key()` value from `present_fields`. Store the price response's ISO 4217 currency code in `MeterSnapshot.currency`; absence of currency makes the price field unavailable rather than inventing a unit.

Calculate the watchdog as:

```python
def cycle_budget(meter_count: int, *, needs_open: bool) -> float:
    commands = meter_count * 3 + 1
    open_budget = OPEN_TIMEOUT + SETTLE_DELAY + sum(METER_LIST_BACKOFFS) + (
        len(METER_LIST_BACKOFFS) * METER_LIST_TIMEOUT
    ) + QUERY_TIMEOUT if needs_open else 0.0
    return open_budget + commands * QUERY_TIMEOUT + CLOSE_TIMEOUT + WATCHDOG_MARGIN
```

`async_refresh()` makes at most two complete attempts. On the first transport failure it aborts, discards the partial local dictionary, reconnects/synchronizes, and reruns every command. Only a completed attempt is converted into `RavenSnapshot`.

`async_reconfigure_path()` runs under the same client's ownership. It saves the old path, closes the current connection, validates the candidate through the bounded pipeline, and returns the validation result on success. On candidate failure it restores the old path and performs one bounded reconnect attempt; restoration failure is logged safely without masking the candidate error. This permits reconfigure without creating a second client that competes for the exclusive port.

- [ ] **Step 4: Run communication tests**

Run: `pytest tests/components/rainforest_emu2/test_communication.py -q`

Expected: PASS with no leaked tasks reported by pytest.

- [ ] **Step 5: Commit polling recovery**

```bash
git add custom_components/rainforest_emu2/communication.py tests/components/rainforest_emu2/test_communication.py
git commit -m "feat: recover Raven polling with full-cycle retry"
```

---

### Task 5: Discovery, manual scanning, validation, and meter selection

**Files:**
- Create: `custom_components/rainforest_emu2/config_flow.py`
- Modify: `custom_components/rainforest_emu2/strings.json`
- Modify: `custom_components/rainforest_emu2/translations/en.json`
- Create: `tests/components/rainforest_emu2/test_config_flow.py`

**Interfaces:**
- Consumes: `PortCandidate`, `RavenClient.async_validate()`, structured errors, Home Assistant USB models/helpers.
- Produces: `RainforestEmu2ConfigFlow`, `async_scan_supported_ports(hass) -> tuple[PortCandidate, ...]`, and config data `{device, meters, usb_serial, usb_vid, usb_pid}`.

- [ ] **Step 1: Write failing USB-confirmation and scanner tests**

```python
result = await hass.config_entries.flow.async_init(
    DOMAIN,
    context={"source": SOURCE_USB},
    data=usb_service_info,
)
assert result["type"] is FlowResultType.FORM
assert result["step_id"] == "confirm"
mock_validate.assert_not_awaited()
```

Add tests proving normal manual scan uses Home Assistant results, falls back to executor PySerial scan only on exception/zero supported matches, deduplicates by canonical path, filters unrelated VID/PID pairs, and uses opaque tokens even when two labels are identical.

- [ ] **Step 2: Run config-flow tests and confirm import failure**

Run: `pytest tests/components/rainforest_emu2/test_config_flow.py -q -k 'usb or scan'`

Expected: FAIL because `config_flow.py` does not exist.

- [ ] **Step 3: Implement discovery and resilient scan**

`async_step_usb()` stores `UsbServiceInfo`, sets title placeholders, applies a genuine USB-serial discovery ID when present or `_async_handle_discovery_without_unique_id()` otherwise, deduplicates matching flows, and returns `async_show_form(step_id="confirm")`. Only submitted confirmation calls validation.

`async_scan_supported_ports()` calls `usb.async_scan_serial_ports()` first. Call `serial.tools.list_ports.comports()` through `hass.async_add_executor_job()` only after scanner failure or zero supported matches. Normalize numeric/string VID/PID values, create tokens from a per-scan index plus a hash of canonical path, and keep token-to-record mapping private to the flow.

- [ ] **Step 4: Write failing validation and meter-selection tests**

Verify every safe reason maps to a field/base error and remains on the form. Verify third-attempt success advances to `meters`, imported/default meters are selected, at least one meter is required, unique ID is `format_mac(device_mac)`, duplicate hardware aborts, and the entry contains no complete labels or raw dependency objects.

- [ ] **Step 5: Implement validation and translated forms**

Use these form steps: `user`, `advanced`, `confirm`, and `meters`. A no-device result returns the `user` form with description placeholders and actions for rescan/advanced path; it does not abort. Reject `://` in advanced paths. Preserve selected token/path after retryable failure.

Use translation keys `device_missing`, `device_permission`, `device_busy`, `open_timeout`, `response_timeout`, `no_response`, `no_paired_meters`, `no_supported_meters`, `identity_invalid`, `cleanup_failed`, and `unknown`. Do not interpolate paths, MACs, or serials into error copy.

- [ ] **Step 6: Run config-flow tests**

Run: `pytest tests/components/rainforest_emu2/test_config_flow.py -q`

Expected: PASS.

- [ ] **Step 7: Commit setup flow**

```bash
git add custom_components/rainforest_emu2 tests/components/rainforest_emu2/test_config_flow.py
git commit -m "feat: add resilient Rainforest setup flow"
```

---

### Task 6: Built-in migration assistance and reconfigure flow

**Files:**
- Modify: `custom_components/rainforest_emu2/communication.py`
- Modify: `custom_components/rainforest_emu2/config_flow.py`
- Modify: `custom_components/rainforest_emu2/strings.json`
- Modify: `custom_components/rainforest_emu2/translations/en.json`
- Modify: `tests/components/rainforest_emu2/test_communication.py`
- Modify: `tests/components/rainforest_emu2/test_config_flow.py`

**Interfaces:**
- Consumes: Setup-flow validation/config data from Task 5 and `RavenClient.async_reconfigure_path()` from Task 4.
- Produces: `async_step_import_builtin()`, `async_step_import_confirm()`, `async_step_reconfigure()`, and `async_step_reconfigure_confirm()`.

- [ ] **Step 1: Write failing import tests**

Create a fake `rainforest_raven` entry using its actual `CONF_DEVICE` and `CONF_MAC` data keys and assert the flow:

```python
assert result["step_id"] == "import_confirm"
assert old_entry.disabled_by is not None
assert old_entry.data == old_data
assert imported_defaults == {"0013500102030405"}
```

Also prove an enabled built-in entry produces `builtin_still_enabled` without opening the port, removed/disabled entries may proceed, missing old paths stay editable, only still-present meter MACs are preselected, and the old entry/entity registry is never updated or removed.

- [ ] **Step 2: Write failing reconfigure tests**

Verify reconfigure uses the loaded entry's existing `RavenClient`, validates the selected path transactionally, aborts on a hardware-MAC mismatch, restores the old connection after candidate failure, updates the same entry through `async_update_reload_and_abort`, changes meter selection/path metadata, and never creates a second config entry.

- [ ] **Step 3: Run import/reconfigure tests and confirm failure**

Run: `pytest tests/components/rainforest_emu2/test_config_flow.py -q -k 'import or reconfigure'`

Expected: FAIL because those steps are absent.

- [ ] **Step 4: Implement guarded import and reconfigure**

Offer import from the user flow when built-in entries exist. Treat both user-disabled and removed built-in entries as safe ownership states; do not accept an integration-enabled entry. Copy only path and requested meter MACs into transient flow state. Run normal validation, then intersect imported MACs with validated meter results.

In reconfigure, retrieve typed runtime data for the target entry and call `runtime.client.async_reconfigure_path(path)`. Then call `await self.async_set_unique_id(result.device_mac)` followed by `self._abort_if_unique_id_mismatch()`, and update only validated data with `async_update_reload_and_abort(entry, data_updates=...)`.

- [ ] **Step 5: Run all config-flow tests**

Run: `pytest tests/components/rainforest_emu2/test_config_flow.py -q`

Expected: PASS.

- [ ] **Step 6: Commit migration and repair flows**

```bash
git add custom_components/rainforest_emu2 tests/components/rainforest_emu2/test_communication.py tests/components/rainforest_emu2/test_config_flow.py
git commit -m "feat: add safe migration and reconfigure flows"
```

---

### Task 7: Coordinator and config-entry lifecycle

**Files:**
- Create: `custom_components/rainforest_emu2/coordinator.py`
- Modify: `custom_components/rainforest_emu2/__init__.py`
- Create: `tests/components/rainforest_emu2/test_coordinator.py`

**Interfaces:**
- Consumes: `RavenClient`, `RavenSnapshot`, `UPDATE_INTERVAL`, and entry data from Task 5.
- Produces: `RainforestCoordinator`, `RainforestRuntimeData`, `async_setup_entry()`, and `async_unload_entry()`.

- [ ] **Step 1: Write failing coordinator tests**

```python
await coordinator.async_config_entry_first_refresh()
assert coordinator.data == expected_snapshot
assert coordinator.last_update_success
client.async_refresh.assert_awaited_once_with((METER_A,))
```

Add tests mapping retryable initial errors to `ConfigEntryNotReady`, permanent identity/schema errors to `ConfigEntryError`, exhausted runtime errors to `UpdateFailed`, reload/shutdown to bounded `async_shutdown()`, and an update listener to unload/reload. Add startup path-resolution cases: an absent raw path with matching stored VID/PID/USB serial updates to the new scan path, while a serial-less ambiguous match does not guess and remains retryable.

- [ ] **Step 2: Run coordinator tests and confirm failure**

Run: `pytest tests/components/rainforest_emu2/test_coordinator.py -q`

Expected: FAIL because the coordinator does not exist.

- [ ] **Step 3: Implement typed runtime and coordinator**

```python
@dataclass(slots=True)
class RainforestRuntimeData:
    client: RavenClient
    coordinator: RainforestCoordinator
    validation: ValidationResult


class RainforestCoordinator(DataUpdateCoordinator[RavenSnapshot]):
    async def _async_update_data(self) -> RavenSnapshot:
        try:
            return await self.client.async_refresh(self.meter_macs)
        except RainforestCommunicationError as err:
            raise UpdateFailed(str(err)) from err
```

Store runtime data on the typed config entry, call first refresh before platform forwarding, register an update listener, and unload sensor platforms before bounded client shutdown. Before constructing the client, startup checks the saved path; when missing, it scans once and may update the entry only on an exact stored VID/PID/USB-serial match. It never guesses among serial-less devices. Startup then invokes validation/connect so it can select `ConfigEntryNotReady` versus `ConfigEntryError` before coordinator polling.

- [ ] **Step 4: Run coordinator and setup tests**

Run: `pytest tests/components/rainforest_emu2/test_coordinator.py -q`

Expected: PASS.

- [ ] **Step 5: Commit runtime lifecycle**

```bash
git add custom_components/rainforest_emu2/__init__.py custom_components/rainforest_emu2/coordinator.py tests/components/rainforest_emu2/test_coordinator.py
git commit -m "feat: add Rainforest coordinator lifecycle"
```

---

### Task 8: Sensors and device registry

**Files:**
- Create: `custom_components/rainforest_emu2/sensor.py`
- Modify: `custom_components/rainforest_emu2/strings.json`
- Modify: `custom_components/rainforest_emu2/translations/en.json`
- Create: `tests/components/rainforest_emu2/test_sensor.py`

**Interfaces:**
- Consumes: `RainforestRuntimeData`, `RavenSnapshot`, and validated meters.
- Produces: one device for the USB gateway, one device per meter, and sensor entities for demand, delivered, received, price, and signal strength.

- [ ] **Step 1: Write failing entity tests**

Assert exact native units and device/state classes:

```python
assert states["sensor.main_meter_power_demand"].attributes["unit_of_measurement"] == "W"
assert states["sensor.main_meter_energy_delivered"].attributes["unit_of_measurement"] == "kWh"
assert states["sensor.main_meter_energy_received"].attributes["unit_of_measurement"] == "kWh"
assert states["sensor.main_meter_energy_price"].attributes["device_class"] == "monetary"
assert states["sensor.rainforest_signal_strength"].attributes["unit_of_measurement"] == "dB"
```

Verify unique IDs use full unlogged hardware/meter MAC plus a fixed field suffix, names use validated nickname/generic suffix, all devices belong only to the custom config entry, partial missing fields mark only their entity unavailable, and restored/stale values are not reported as available.

- [ ] **Step 2: Run sensor tests and confirm failure**

Run: `pytest tests/components/rainforest_emu2/test_sensor.py -q`

Expected: FAIL because `sensor.py` does not exist.

- [ ] **Step 3: Implement coordinator entities**

Use frozen `SensorEntityDescription` records and `CoordinatorEntity`. For each entity:

```python
@property
def available(self) -> bool:
    field = snapshot_field_key(self.entity_description.key, self._meter_mac_hex)
    return super().available and field in self.coordinator.data.present_fields
```

The device-level signal entity passes `None` as the meter MAC so its availability key is `device:signal_strength`.

Demand uses `SensorDeviceClass.POWER`, `UnitOfPower.WATT`, and `SensorStateClass.MEASUREMENT`. Delivered/received use `SensorDeviceClass.ENERGY`, `UnitOfEnergy.KILO_WATT_HOUR`, and `SensorStateClass.TOTAL_INCREASING`. Price uses the device currency when present and signal uses dB with measurement state class. Preserve the built-in integration's value semantics where they are valid.

- [ ] **Step 4: Run entity tests**

Run: `pytest tests/components/rainforest_emu2/test_sensor.py -q`

Expected: PASS.

- [ ] **Step 5: Commit entities**

```bash
git add custom_components/rainforest_emu2/sensor.py custom_components/rainforest_emu2/strings.json custom_components/rainforest_emu2/translations/en.json tests/components/rainforest_emu2/test_sensor.py
git commit -m "feat: expose Rainforest energy sensors"
```

---

### Task 9: Privacy-safe diagnostics and observability

**Files:**
- Create: `custom_components/rainforest_emu2/diagnostics.py`
- Modify: `custom_components/rainforest_emu2/communication.py`
- Modify: `custom_components/rainforest_emu2/coordinator.py`
- Create: `tests/components/rainforest_emu2/test_diagnostics.py`

**Interfaces:**
- Consumes: Runtime data, communication counters, safe path details, Home Assistant redaction helpers.
- Produces: `async_get_config_entry_diagnostics()` and safe debug log events.

- [ ] **Step 1: Write failing redaction tests**

```python
payload = await async_get_config_entry_diagnostics(hass, entry)
rendered = json.dumps(payload)
for secret in (DEVICE_MAC, METER_MAC, USB_SERIAL, FULL_BY_ID_PATH, ACCOUNT, INSTALL_CODE, LINK_KEY):
    assert secret not in rendered
assert payload["connection"]["path_strategy"] == "by_id"
assert payload["connection"]["timeout_count"] == 2
```

Capture logs and prove the same secrets never appear during open, retry, malformed-meter handling, path update, close timeout, or abort timeout.

- [ ] **Step 2: Run diagnostics tests and confirm failure**

Run: `pytest tests/components/rainforest_emu2/test_diagnostics.py -q`

Expected: FAIL because diagnostics are absent.

- [ ] **Step 3: Implement diagnostics and counters**

Return safe config-entry metadata, integration version, path strategy/safe basename, model/firmware, selected meter count, last success timestamp, reconnect/timeout counters, latest safe stage/reason, and coordinator field-presence flags. Redact config-entry data before serialization and build meter records by index rather than MAC suffix.

Use structured log arguments for stage/reason/attempt/duration. Never pass dependency objects, raw exceptions with paths, raw response objects, or config-entry data directly to `_LOGGER`.

- [ ] **Step 4: Run diagnostics and full unit tests**

Run: `pytest tests/components/rainforest_emu2/test_diagnostics.py -q && pytest tests/components/rainforest_emu2 -q`

Expected: PASS.

- [ ] **Step 5: Commit diagnostics**

```bash
git add custom_components/rainforest_emu2 tests/components/rainforest_emu2/test_diagnostics.py
git commit -m "feat: add redacted Rainforest diagnostics"
```

---

### Task 10: Documentation, branding, CI, and release safeguards

**Files:**
- Create: `README.md`
- Create: `CHANGELOG.md`
- Create: `LICENSE`
- Create: `brand/icon.png`
- Copy: `brand/icon.png` to `custom_components/rainforest_emu2/brand/icon.png`
- Create: `.gitignore`
- Create: `.github/dependabot.yml`
- Create: `.github/workflows/test.yml`
- Create: `.github/workflows/lint.yml`
- Create: `.github/workflows/hacs.yml`
- Create: `.github/workflows/hassfest.yml`
- Create: `.github/workflows/release.yml`
- Modify: `tests/components/rainforest_emu2/test_manifest.py`

**Interfaces:**
- Consumes: Completed component and version `0.1.0`.
- Produces: User installation/migration/recovery documentation, valid brand assets, protected CI, and a tag/version-checked GitHub Release path.

- [ ] **Step 1: Add failing repository-completeness assertions**

```python
def test_distribution_files() -> None:
    for relative in ("README.md", "CHANGELOG.md", "LICENSE", "brand/icon.png"):
        assert (ROOT / relative).is_file()
    assert (ROOT / "brand/icon.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_single_hacs_integration() -> None:
    integrations = [p for p in (ROOT / "custom_components").iterdir() if p.is_dir()]
    assert [p.name for p in integrations] == ["rainforest_emu2"]
```

- [ ] **Step 2: Run completeness test and confirm failure**

Run: `pytest tests/components/rainforest_emu2/test_manifest.py -q`

Expected: FAIL because documentation, license, and brand files are missing.

- [ ] **Step 3: Write actionable user documentation**

README sections must be: Requirements, HACS installation, Manual installation, Add device, USB permissions/passthrough, Error guide, Reconfigure, Built-in migration, Entity-ID migration checklist, Energy Dashboard update, Rollback, Diagnostics/privacy, Supported devices, Support, and License/attribution.

The HACS instructions specify repository URL, category `Integration`, download, Home Assistant restart, then Settings → Devices & services → Add Integration. Explicitly state the repository cannot be installed as a Home Assistant App repository. The migration checklist tells users to record entity IDs, disable the built-in entry, add/validate the custom entry, update consumers, observe one full polling interval, and only then remove the old entry if desired.

- [ ] **Step 4: Add valid branding and Apache-2.0 attribution**

Create a square PNG icon at least 256×256 with transparent background and ensure the root and integration-local copies are byte-identical. `LICENSE` contains Apache License 2.0. `CHANGELOG.md` includes an `0.1.0` section naming both supported products and the corrected timeout/lifecycle behavior.

- [ ] **Step 5: Add CI workflows**

`test.yml` runs pytest on Python 3.13 with a matrix containing Home Assistant `2026.3.0` and latest stable, installing dependencies with pip's resolver. `lint.yml` runs `ruff format --check`, `ruff check`, and mypy. `hacs.yml` uses `hacs/action@main`; `hassfest.yml` uses `home-assistant/actions/hassfest@master`.

`release.yml` runs only on `v*` tags, reads `manifest.json`, exits unless tag `v${manifest.version}` matches, reruns tests/validation, and creates a GitHub Release using the repository source archive. It does not create or configure `zip_release`.

- [ ] **Step 6: Run local verification**

Run:

```bash
ruff format --check custom_components tests
ruff check custom_components tests
mypy custom_components/rainforest_emu2
pytest tests/components/rainforest_emu2 -q
```

Expected: every command exits 0.

- [ ] **Step 7: Commit distribution and CI**

```bash
git add README.md CHANGELOG.md LICENSE brand custom_components/rainforest_emu2/brand .gitignore .github tests/components/rainforest_emu2/test_manifest.py
git commit -m "docs: prepare Rainforest integration for HACS"
```

---

### Task 11: End-to-end validation and repository metadata

**Files:**
- Modify only files required by verified failures.
- Update GitHub repository description and topics through the GitHub API.

**Interfaces:**
- Consumes: Tasks 1–10.
- Produces: A review-ready branch that meets every acceptance criterion without publishing `v0.1.0` prematurely.

- [ ] **Step 1: Run the complete local suite**

```bash
ruff format --check custom_components tests
ruff check custom_components tests
mypy custom_components/rainforest_emu2
pytest -q
```

Expected: all checks pass with zero warnings, leaked tasks, or skipped integration tests unless a skip documents an unavailable external validator.

- [ ] **Step 2: Validate manifests and translations**

Run Hassfest in its documented container/action-compatible mode and run HACS Action validation against the branch. Confirm `strings.json` and `translations/en.json` contain the same flow/entity keys and valid JSON.

- [ ] **Step 3: Verify install layout in a clean Home Assistant config directory**

Copy only `custom_components/rainforest_emu2` into a temporary clean config, start Home Assistant 2026.3.0, and assert logs contain neither import/setup errors nor an override warning for `rainforest_raven`. Repeat with current stable Home Assistant.

- [ ] **Step 4: Recheck privacy and timeout invariants**

Run:

```bash
rg -n "\.synchronize\(" custom_components tests
rg -n "async with.*RAVEnSerialDevice|RAVEnSerialDevice" custom_components/rainforest_emu2
rg -n "raw_xml|install_code|link_key|account" custom_components/rainforest_emu2
```

Expected: no `.synchronize()` call; the only production `RAVEnSerialDevice` import/use is in `communication.py`; sensitive fields appear only in explicit redaction logic and never in log statements.

- [ ] **Step 5: Set required GitHub metadata**

Set description to `Reliable Home Assistant integration for Rainforest EMU-2 and legacy RAVEn energy monitors.` Set topics to `home-assistant`, `homeassistant`, `hacs`, `rainforest-automation`, `emu2`, `raven`, and `energy-monitor`.

- [ ] **Step 6: Review the branch diff and commit verified corrections**

Inspect `git diff main...HEAD`, ensure no secrets/generated caches are present, and commit only changes caused by concrete validation failures:

```bash
git add custom_components/rainforest_emu2 tests/components/rainforest_emu2 README.md CHANGELOG.md hacs.json pyproject.toml .github
git commit -m "fix: resolve integration validation findings"
```

If validation required no changes, do not create an empty commit.

- [ ] **Step 7: Request code review before release**

Use `superpowers:requesting-code-review`, address findings through `superpowers:receiving-code-review`, rerun the complete suite, and prepare a pull request. Do not tag or publish `v0.1.0` until branch protection checks pass and the user approves release.
