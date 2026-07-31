"""Frontmatter normalization — the single biggest ingest task (recon §2.1/§2.5).

The docs/solutions/ corpus has ~130 distinct frontmatter keys with heavy
synonym/case/plural drift. This module collapses that mess into a small set of
canonical predicates + normalized enum values. It is deterministic and tested.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

# raw frontmatter key -> canonical logical field.
# Fields consumed by the semantic ingester; unknown keys are dropped (logged).
KEY_MAP: dict[str, str] = {
    # component / module cluster
    "component": "component", "components": "component", "module": "component",
    "modules": "component", "modules_affected": "component",
    "affected_components": "component", "affected_files": "component",
    "related_components": "component",
    "files_changed": "component", "files_modified": "component",
    "files_affected": "component", "key_function": "component",
    # symptom cluster
    "symptom": "symptom", "symptoms": "symptom",
    # root cause cluster
    "root_cause": "root_cause", "root_causes": "root_cause",
    "root_cause_summary": "root_cause",
    # problem type (solutions-doc frontmatter) -> category-like facet
    "problem_type": "problem_type",
    # fix / solution / resolution cluster
    "fix": "fix", "solution": "fix", "resolution": "fix",
    "solution_summary": "fix", "resolution_summary": "fix",
    "resolution_type": "resolution_type",
    # impact
    "impact": "impact", "financial_impact": "impact",
    # date cluster -> found date
    "date": "found_date", "date_solved": "found_date", "date_resolved": "found_date",
    "date_discovered": "found_date", "date_found": "found_date",
    "incident_date": "found_date", "found_date": "found_date",
    "resolution_date": "found_date",
    # identity / tracking
    "learning_id": "learning_id", "learning_ids": "learning_id",
    "status": "status", "category": "category", "severity": "severity",
    "priority": "severity", "title": "title", "tags": "tags", "keywords": "tags",
    # issue / pr / ticket cross-links
    "issue": "issues", "issues": "issues", "github_issue": "issues",
    "github_issues": "issues", "related_issues": "issues",
    "related_github_issues": "issues", "issues_fixed": "issues",
    "review_issues": "issues", "followup_issues": "issues",
    "pr": "prs", "prs": "prs", "related_pr": "prs", "related_prs": "prs",
    "pull_requests": "prs", "pr_number": "prs", "parent_pr": "prs",
    "case_study_pr": "prs", "regressing_pr": "regressing_pr",
    "fix_commit": "fix_commit", "regressing_commit": "regressing_commit",
    # doc/learning cross-links
    "related_solutions": "related_solutions", "related_docs": "related_solutions",
    "related": "related_solutions", "related_learnings": "related_learnings",
    "related_findings": "related_learnings",
    "supersedes": "supersedes", "recurrence_of": "recurrence_of",
    "recurs": "recurrence_of", "follow_up_to": "follow_up_to",
    "regressing": "regressing_pr",
}

# canonical priority (P0..P3) — the severity field conflates two scales.
_PRIORITY_RE = re.compile(r"\bP\s?([0-3])\b", re.I)
_LEVELS = {"critical", "high", "medium", "low", "info", "improvement",
           "enhancement", "major", "minor"}


def normalize_severity(raw: Any) -> tuple[str | None, str | None]:
    """Return (priority in {P0..P3} | None, level in _LEVELS | None).

    Handles 'P1', 'p1', 'HIGH', 'High', 'critical', 'mixed (1 P1, 4 P2)' etc.
    """
    if raw is None:
        return None, None
    s = str(raw).strip()
    m = _PRIORITY_RE.search(s)
    priority = f"P{m.group(1)}" if m else None
    low = s.lower().strip()
    level = low if low in _LEVELS else None
    return priority, level


def normalize_category(raw: Any) -> str | None:
    """Collapse plural/underscore/case drift to a hyphenated canonical form."""
    if raw is None:
        return None
    s = str(raw).strip().lower().replace("_", "-").replace(" ", "-")
    # de-pluralize the known drift pairs
    aliases = {
        "feature-pattern": "feature-patterns",
        "architecture-pattern": "architecture-patterns",
        "logic-error": "logic-errors",
        "database-issue": "database-issues",
        "integration-issue": "integration-issues",
        "runtime-error": "runtime-errors",
        "feature-implementation": "feature-patterns",
        "feature": "feature-patterns",
    }
    return aliases.get(s, s)


def normalize_status(raw: Any) -> str | None:
    """Collapse the done-synonym cluster (recon §3.3)."""
    if raw is None:
        return None
    s = str(raw).strip().lower()
    done = {"complete", "completed", "resolved", "done", "fixed", "closed",
            "verified", "confirmed"}
    if s in done:
        return "done"
    if s in {"pending", "open", "found", "planned"}:
        return "open"
    if s in {"in-progress", "in_progress", "wip"}:
        return "in-progress"
    if s in {"deferred", "wont_fix", "wont-fix", "not-planned", "retracted"}:
        return s.replace("_", "-")
    return s


_ISSUE_RE = re.compile(r"#(\d+)")
# Tracker keys recognised in prose (commit messages, PR bodies, ADR refs).
# Derived from the team keys configured in sources.yml so onboarding a tracker
# needs no code change; with no teams configured, a generic PREFIX-123 pattern
# applies (uppercase alpha prefix, 2+ chars, to avoid over-matching).
def _tracker_key_re() -> "re.Pattern[str]":
    try:
        from . import sources
        keys = [t["team"] for t in sources.trackers() if t.get("team")]
    except Exception:
        keys = []
    alt = "|".join(re.escape(k) for k in keys) if keys else r"[A-Z][A-Z0-9]{1,9}"
    return re.compile(rf"\b({alt})-(\d+)\b", re.I)


TRACKER_KEY_RE = _tracker_key_re()


def parse_refs(raw: Any) -> list[str]:
    """Extract numeric #NNNN (GitHub-style) refs from a scalar/list value."""
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    out: list[str] = []
    for it in items:
        for m in _ISSUE_RE.finditer(str(it)):
            out.append(m.group(1))
    return out


def parse_tracker_keys(raw: Any) -> list[str]:
    """Extract tracker issue keys (e.g. PROJ-123) from a scalar/list/text."""
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    out = []
    for it in items:
        out += [f"{m.group(1).upper()}-{m.group(2)}"
                for m in TRACKER_KEY_RE.finditer(str(it))]
    return out


def parse_tags(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        vals = [str(v) for v in raw]
    else:
        # "[a, b, c]" or "a, b, c"
        vals = re.split(r"[,\n]", str(raw).strip("[]"))
    return [t.strip().strip("'\"").lower() for t in vals if str(t).strip()]


def parse_date(raw: Any) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, date):
        return raw
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(raw))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def normalize_frontmatter(fm: dict) -> dict:
    """Fold a raw frontmatter dict into canonical logical fields."""
    out: dict[str, Any] = {"issues": [], "prs": [], "tags": [],
                           "related_solutions": [], "related_learnings": [],
                           "tracker": []}
    for raw_key, val in fm.items():
        canon = KEY_MAP.get(str(raw_key).strip().lower())
        if canon is None:
            continue
        if canon in ("issues", "prs"):
            out[canon] += parse_refs(val)
            out["tracker"] += parse_tracker_keys(val)
        elif canon == "tags":
            out["tags"] += parse_tags(val)
        elif canon in ("related_solutions", "related_learnings"):
            vals = val if isinstance(val, list) else [val]
            out[canon] += [str(v).strip() for v in vals if v]
        elif canon == "severity":
            p, lvl = normalize_severity(val)
            out["priority"], out["severity_level"] = p, lvl
        elif canon == "category":
            out["category"] = normalize_category(val)
        elif canon == "status":
            out["status"] = normalize_status(val)
        elif canon == "found_date":
            d = parse_date(val)
            if d and "found_date" not in out:  # earliest-wins is fine; keep first
                out["found_date"] = d
        elif canon == "learning_id":
            ids = val if isinstance(val, list) else [val]
            out["learning_id"] = str(ids[0]).strip().strip("'\"[]") if ids else None
        elif isinstance(val, (list, tuple)):
            # list-valued scalar (e.g. symptoms:) -> one readable string
            out[canon] = "; ".join(str(v).strip() for v in val if str(v).strip())
        else:
            out[canon] = val
    # de-dup ref lists
    for k in ("issues", "prs", "tags", "tracker", "related_solutions",
              "related_learnings"):
        out[k] = sorted(set(out[k]))
    return out
