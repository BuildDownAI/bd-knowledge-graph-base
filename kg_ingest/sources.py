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


def validate(cfg: object) -> list[str]:
    """Return one message per problem in cfg; empty list means valid."""
    if not isinstance(cfg, dict):
        return ["top level must be a mapping"]
    errors: list[str] = []

    code_repo = cfg.get("code_repo")
    if code_repo is not None:
        if isinstance(code_repo, str):
            parts = code_repo.split("/")
            if len(parts) != 2 or not parts[0] or not parts[1]:
                errors.append(f"code_repo string must be owner/name, got {code_repo!r}")
        elif isinstance(code_repo, dict):
            slug = code_repo.get("slug")
            if slug is None:
                errors.append("code_repo mapping must have a 'slug' key")
            else:
                parts = str(slug).split("/")
                if len(parts) != 2 or not parts[0] or not parts[1]:
                    errors.append(f"code_repo.slug must be owner/name, got {slug!r}")
        else:
            errors.append(f"code_repo must be a string or mapping, got {type(code_repo).__name__}")

    trackers_val = cfg.get("trackers")
    if trackers_val is not None:
        if not isinstance(trackers_val, list):
            errors.append(f"trackers must be a list or null, got {type(trackers_val).__name__}")
        else:
            seen_teams: set[str] = set()
            for i, entry in enumerate(trackers_val):
                if not isinstance(entry, dict):
                    errors.append(f"trackers[{i}] must be a mapping")
                    continue
                team = entry.get("team")
                if not team or not isinstance(team, str):
                    errors.append(f"trackers[{i}] must have a non-empty string 'team'")
                else:
                    if team in seen_teams:
                        errors.append(f"trackers: duplicate team {team!r}")
                    seen_teams.add(team)
                kind = entry.get("kind")
                if kind not in ("linear", "jira"):
                    errors.append(f"trackers[{i}] kind must be 'linear' or 'jira', got {kind!r}")
                tier = entry.get("tier")
                if tier is not None and tier not in ("primary", "secondary"):
                    errors.append(f"trackers[{i}] tier must be 'primary' or 'secondary', got {tier!r}")

    secondary_repos = cfg.get("secondary_repos")
    if secondary_repos is not None:
        if not isinstance(secondary_repos, list):
            errors.append(f"secondary_repos must be a list or null, got {type(secondary_repos).__name__}")
        else:
            for i, entry in enumerate(secondary_repos):
                if not isinstance(entry, dict):
                    errors.append(f"secondary_repos[{i}] must be a mapping")
                    continue
                slug = entry.get("slug")
                if slug is None:
                    errors.append(f"secondary_repos[{i}] must have a 'slug' key")
                else:
                    parts = str(slug).split("/")
                    if len(parts) != 2 or not parts[0] or not parts[1]:
                        errors.append(f"secondary_repos[{i}].slug must be owner/name, got {slug!r}")

    return errors


def trackers(cfg: dict | None = None, tier: str | None = None) -> list[dict]:
    """Tracker entries, optionally filtered to a tier ('primary'|'secondary')."""
    cfg = cfg or load()
    out = list(cfg.get("trackers") or [])
    if tier:
        out = [t for t in out if (t.get("tier") or "primary") == tier]
    return out
