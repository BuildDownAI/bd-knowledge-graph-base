"""Load the human-editable ingest manifest (sources.yml).

Keeping the source list in one readable YAML file (not scattered across code) is
deliberate: a person can open sources.yml, see every place the graph's knowledge
comes from, and repoint it without touching Python. This module is the thin
loader every ingester shares.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SOURCES_YML = ROOT / "sources.yml"


def load(path: str | Path | None = None) -> dict:
    p = Path(path) if path else SOURCES_YML
    with open(p, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def trackers(cfg: dict | None = None, tier: str | None = None) -> list[dict]:
    """Tracker entries, optionally filtered to a tier ('primary'|'secondary')."""
    cfg = cfg or load()
    out = list(cfg.get("trackers") or [])
    if tier:
        out = [t for t in out if (t.get("tier") or "primary") == tier]
    return out
