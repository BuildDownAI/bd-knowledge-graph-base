"""Test that the repo-root sources.yml is valid, and exercise validate() rules.

Run: PYTHONPATH=. ./.venv/bin/python tests/test_sources_manifest.py
"""
import sys
from pathlib import Path

import pytest
import yaml

from kg_ingest.sources import validate

REPO_ROOT = Path(__file__).parent.parent
SOURCES_FILE = REPO_ROOT / "sources.yml"


def check(name: str, got, want) -> None:
    assert got == want, f"FAIL {name}: got {got!r}, want {want!r}"
    print(f"PASS: {name}")


def test_repo_sources_manifest():
    # ----- repo-root sources.yml: must parse and validate -----
    try:
        cfg = yaml.safe_load(SOURCES_FILE.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        pytest.fail(f"repo-root sources.yml parse error: {exc}  ({SOURCES_FILE})")

    problems = validate(cfg)
    if problems:
        details = "\n".join(f"  problem: {p}" for p in problems)
        pytest.fail(
            f"repo-root sources.yml: {len(problems)} validation error(s) ({SOURCES_FILE}):\n{details}"
        )
    print(f"PASS: repo-root sources.yml parses and validates ({SOURCES_FILE})")


def test_broken_manifest():
    # ----- fixture: broken manifest (missing colon after trackers key) -----
    _BROKEN_YAML = "trackers\n  - kind: linear\n    team: PROJ\n"
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load(_BROKEN_YAML)
    print("PASS: broken-manifest (missing colon) raises yaml.YAMLError")


def test_validate_rules():
    # ----- validate() fixture cases -----
    check("empty mapping is valid",
          validate({}), [])

    check("trackers: {} fails",
          len(validate({"trackers": {}})) > 0, True)

    check("tracker missing team fails",
          len(validate({"trackers": [{"kind": "linear"}]})) > 0, True)

    check("kind gitlab fails",
          len(validate({"trackers": [{"kind": "gitlab", "team": "T"}]})) > 0, True)

    check("duplicate team fails",
          len(validate({"trackers": [
              {"kind": "linear", "team": "T"},
              {"kind": "jira", "team": "T"},
          ]})) > 0, True)

    check("secondary_repo slug with no slash fails",
          len(validate({"secondary_repos": [{"slug": "just-a-name"}]})) > 0, True)

    # template shape
    check("template shape passes",
          validate({
              "code_repo": {"slug": "your-org/your-project"},
              "trackers": [],
              "secondary_repos": [],
          }), [])

    # knowledge-graph-ai-implement shape
    check("kga-shaped manifest passes",
          validate({
              "code_repo": {"slug": "your-org/your-project", "branch": "main"},
              "trackers": [
                  {"kind": "linear", "team": "PROJ", "tier": "primary"},
                  {"kind": "jira", "team": "acme-app", "tier": "secondary"},
              ],
              "secondary_repos": [
                  {"slug": "your-org/related-repo", "path": "../related-repo", "tier": "secondary"},
              ],
          }), [])

    # bare string code_repo
    check("code_repo as bare string passes",
          validate({"code_repo": "your-org/your-project"}), [])

    # secondary_repo entry missing slug key
    check("secondary_repo missing slug key fails",
          len(validate({"secondary_repos": [{"path": "../foo"}]})) > 0, True)
