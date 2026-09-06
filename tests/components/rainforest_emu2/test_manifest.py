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


def test_distribution_files() -> None:
    """The repository contains the files HACS and users need to install it."""
    for relative in ("README.md", "CHANGELOG.md", "LICENSE", "brand/icon.png"):
        assert (ROOT / relative).is_file()
    assert (ROOT / "brand/icon.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_single_hacs_integration() -> None:
    """A HACS repository must expose one unambiguous integration directory."""
    integrations = [
        path for path in (ROOT / "custom_components").iterdir() if path.is_dir()
    ]
    assert [path.name for path in integrations] == ["rainforest_emu2"]
