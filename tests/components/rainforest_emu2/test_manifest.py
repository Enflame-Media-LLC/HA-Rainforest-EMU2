"""Manifest and distribution metadata contract tests."""

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
