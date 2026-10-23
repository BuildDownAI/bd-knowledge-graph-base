"""Test that issues_override in add_tracker produces correct triples from
the pipeline's JSON shape (no LINEAR_API_KEY required).

Also tests the `refresh` subcommand end-to-end.

Run: PYTHONPATH=. ./.venv/bin/python tests/test_tracker_data.py
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest.mock
from pathlib import Path

from rdflib import Graph, Literal, Namespace
from rdflib.namespace import RDF

from kg_ingest import iris
from kg_ingest.cli import secondary_repo_path
from kg_ingest.tracker import ISSUE_FIELD_NAMES, ISSUE_FIELDS, _ISSUES_QUERY, add_tracker

KG = iris.KG
PROV = Namespace("http://www.w3.org/ns/prov#")
DCTERMS = Namespace("http://purl.org/dc/terms/")

REPO_ROOT = Path(__file__).parent.parent
FIXTURE_FILE = REPO_ROOT / "tests" / "fixtures" / "tracker-data-kga7.json"

FIXTURE_ISSUES = [
    {
        "id": "fixture-001",
        "identifier": "AII-100",
        "title": "Fix the widget",
        "description": "Widget is broken and needs fixing.",
        "state": {"name": "In Progress", "type": "started"},
        "comments": [
            {
                "body": "# ai-implement-build-up-learnings\n\nWidgets need grease to work.",
                "createdAt": "2026-09-01T10:00:00Z",
            },
            {
                "body": "Short note.",
                "createdAt": "2026-09-01T11:00:00Z",
            },
        ],
    },
    {
        "id": "fixture-002",
        "identifier": "AII-101",
        "title": "Improve the gadget",
        "description": "Gadget needs improvement",
        "state": {"name": "Done", "type": "completed"},
        "comments": [],
    },
]

FULL_ISSUE = {
    "id": "fixture-003",
    "identifier": "AII-102",
    "title": "Full-field issue",
    "description": "Carries every ISSUE_FIELDS field.",
    "branchName": "ai-implement/aii-102-full-field",
    "state": {"name": "Done", "type": "completed"},
    "labels": {"nodes": [{"name": "Bug"}, {"name": "AI-Implement"}]},
    "project": {"name": "Parity"},
    "parent": {"identifier": "AII-100"},
    "comments": {"nodes": [{"body": "# ai-implement-build-up-learnings\n\nFull-field learning body.",
                            "user": {"name": "Tester"}}]},
    "relations": {"nodes": [{"type": "blocks", "relatedIssue": {"identifier": "AII-101"}}]},
}

JIRA_FIXTURE_FILE = REPO_ROOT / "tests" / "fixtures" / "tracker-data-jira.json"


def check(name: str, got, want) -> None:
    assert got == want, f"FAIL {name}: got {got!r}, want {want!r}"
    print(f"PASS: {name}")


def test_add_tracker():
    iss100 = iris.tracker_issue("AII-100")
    iss101 = iris.tracker_issue("AII-101")

    # ----- add_tracker with issues_override -----
    spine_g = Graph()
    run_g = Graph()
    stats = add_tracker(
        spine_g, run_g, "test/repo", "test-run-001",
        pipeline_ver="0.1.0",
        issues_override=FIXTURE_ISSUES,
    )

    check("AII-100 type kg:Issue", (iss100, RDF.type, KG.Issue) in spine_g, True)
    check("AII-100 trackerKey", (iss100, KG.trackerKey, Literal("AII-100")) in spine_g, True)
    check("AII-100 title", (iss100, DCTERMS.title, Literal("Fix the widget")) in spine_g, True)
    check("AII-100 state", (iss100, KG.state, Literal("In Progress")) in spine_g, True)
    check("AII-100 status set", spine_g.value(iss100, KG.status) is not None, True)

    check("AII-101 type kg:Issue", (iss101, RDF.type, KG.Issue) in spine_g, True)
    check("AII-101 trackerKey", (iss101, KG.trackerKey, Literal("AII-101")) in spine_g, True)
    check("AII-101 title", (iss101, DCTERMS.title, Literal("Improve the gadget")) in spine_g, True)
    check("AII-101 state", (iss101, KG.state, Literal("Done")) in spine_g, True)
    check("AII-101 status set", spine_g.value(iss101, KG.status) is not None, True)

    cnode = iris.comment("AII-100", 0)
    check("learning comment is kg:Learning", (cnode, RDF.type, KG.Learning) in run_g, True)
    check("learning comment prov:wasDerivedFrom AII-100 issue",
          (cnode, PROV.wasDerivedFrom, iss100) in run_g, True)
    check("learning comment prov:wasGeneratedBy a run node",
          run_g.value(cnode, PROV.wasGeneratedBy) is not None, True)

    cnode1 = iris.comment("AII-100", 1)
    check("short comment not classified",
          (cnode1, RDF.type, KG.Learning) in run_g or (cnode1, RDF.type, KG.Decision) in run_g,
          False)

    check("stats issues count", stats["issues"], 2)
    check("stats learning count", stats["comment_learnings"], 1)
    check("team entry in stats", any("AII(override)" in t for t in stats["teams"]), True)

    # ----- file wins: LINEAR_API_KEY set + issues_override; requests.post must not fire -----
    _saved_key = os.environ.get("LINEAR_API_KEY")
    os.environ["LINEAR_API_KEY"] = "DUMMY-KEY-NOT-USED"
    _post_calls: list = []

    def _raising_post(*args, **kwargs):
        _post_calls.append(1)
        raise RuntimeError("requests.post was called — file-wins path is broken")

    with unittest.mock.patch("kg_ingest.tracker.requests.post", side_effect=_raising_post):
        _fw_spine = Graph()
        _fw_run = Graph()
        try:
            _fw_stats = add_tracker(
                _fw_spine, _fw_run, "test/repo", "test-run-fw",
                pipeline_ver="0.1.0",
                issues_override=FIXTURE_ISSUES,
            )
        except RuntimeError as exc:
            assert False, f"file-wins mock failed: {exc}"

    if _saved_key is None:
        os.environ.pop("LINEAR_API_KEY", None)
    else:
        os.environ["LINEAR_API_KEY"] = _saved_key

    check("file-wins: requests.post not called when issues_override given", len(_post_calls), 0)
    check("file-wins: issues still ingested with override", _fw_stats["issues"], 2)

    # --- fixture file and in-memory override produce the same issue triples ---
    with open(FIXTURE_FILE, encoding="utf-8") as f:
        file_issues = json.load(f)
    spine_file = Graph()
    run_file = Graph()
    add_tracker(spine_file, run_file, "test/repo", "test-run-file",
                pipeline_ver="0.1.0", issues_override=file_issues)
    check("file-loaded AII-100 matches in-memory triple",
          (iss100, KG.trackerKey, Literal("AII-100")) in spine_file, True)
    check("file-loaded AII-101 matches in-memory triple",
          (iss101, KG.trackerKey, Literal("AII-101")) in spine_file, True)

    # ----- KGA-8: ISSUE_FIELDS is the single source for the live query -----
    for sel in ISSUE_FIELDS:
        check(f"query carries selection {sel.split(' ')[0]!r}", sel in _ISSUES_QUERY, True)
    check("ISSUE_FIELD_NAMES covers the five fields the pipeline once omitted",
          all(n in ISSUE_FIELD_NAMES for n in ("branchName", "labels", "project", "parent", "relations")),
          True)

    # ----- KGA-8: a full-field record yields the same triples on the file path as live -----
    check("FULL_ISSUE carries every ISSUE_FIELD_NAMES key",
          all(n in FULL_ISSUE for n in ISSUE_FIELD_NAMES), True)

    _live_spine, _live_run = Graph(), Graph()
    _live_page = {"data": {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                                       "nodes": [FULL_ISSUE]}}}
    _live_resp = unittest.mock.Mock()
    _live_resp.raise_for_status = lambda: None
    _live_resp.json = lambda: _live_page
    _saved_key2 = os.environ.get("LINEAR_API_KEY")
    os.environ["LINEAR_API_KEY"] = "DUMMY-KEY-FOR-LIVE-MOCK"
    with unittest.mock.patch("kg_ingest.tracker.requests.post", return_value=_live_resp):
        add_tracker(_live_spine, _live_run, "test/repo", "test-run-live",
                    pipeline_ver="0.1.0",
                    cfg={"trackers": [{"kind": "linear", "team": "AII", "tier": "primary"}]})
    if _saved_key2 is None:
        os.environ.pop("LINEAR_API_KEY", None)
    else:
        os.environ["LINEAR_API_KEY"] = _saved_key2

    _file_spine, _file_run = Graph(), Graph()
    _file_out = io.StringIO()
    with contextlib.redirect_stdout(_file_out):
        _file_stats = add_tracker(_file_spine, _file_run, "test/repo", "test-run-live",
                                  pipeline_ver="0.1.0", issues_override=[FULL_ISSUE])
    check("contract: file path yields the same spine triples as the live path",
          set(_file_spine) == set(_live_spine), True)
    check("contract: file path yields the same run triples as the live path",
          set(_file_run) == set(_live_run), True)
    check("contract: full-field record reports no missing fields",
          _file_stats["missing_fields"], [])
    check("contract: no missing-field log for a full-field record",
          "missing from" in _file_out.getvalue(), False)

    # ----- KGA-8: a narrower record is reported, once per field -----
    _narrow_out = io.StringIO()
    with contextlib.redirect_stdout(_narrow_out):
        _narrow_stats = add_tracker(Graph(), Graph(), "test/repo", "test-run-narrow",
                                    pipeline_ver="0.1.0", issues_override=FIXTURE_ISSUES)
    _narrow_lines = [l for l in _narrow_out.getvalue().splitlines() if "missing from" in l]
    check("narrow record: stats name the missing fields",
          _narrow_stats["missing_fields"], ["branchName", "labels", "parent", "project", "relations"])
    check("narrow record: one log line per missing field", len(_narrow_lines), 5)
    check("narrow record: log line carries the record count",
          any("branchName' missing from 2/2" in l for l in _narrow_lines), True)

    # ----- KGA-8: --repos-root path resolution -----
    _entry = {"slug": "BuildDownAI/docs", "path": "../docs"}
    check("repos-root: <root>/<basename(slug)>",
          secondary_repo_path(_entry, "/tmp/repos-root"), Path("/tmp/repos-root/docs").resolve())
    check("repos-root absent: entry path relative to the KG repo",
          secondary_repo_path(_entry, None), (REPO_ROOT / "../docs").resolve())


def test_tracker_data_jira():
    # ----- KGB-33: tracker-neutral contract tests -----
    with open(JIRA_FIXTURE_FILE, encoding="utf-8") as f:
        jira_issues = json.load(f)

    iss100 = iris.tracker_issue("AII-100")
    iss101 = iris.tracker_issue("AII-101")

    _jira_spine = Graph()
    _jira_run = Graph()
    _jira_out = io.StringIO()
    with contextlib.redirect_stdout(_jira_out):
        _jira_stats = add_tracker(
            _jira_spine, _jira_run, "test/repo", "test-run-jira",
            pipeline_ver="0.1.0",
            issues_override=jira_issues,
        )

    proj1 = iris.tracker_issue("PROJ-1")
    proj2 = iris.tracker_issue("PROJ-2")
    proj3 = iris.tracker_issue("PROJ-3")

    check("jira PROJ-1 sourceSystem is 'jira'",
          _jira_spine.value(proj1, KG.sourceSystem), Literal("jira"))
    check("jira PROJ-1 trackerTeam is 'acme-app'",
          _jira_spine.value(proj1, KG.trackerTeam), Literal("acme-app"))
    check("jira PROJ-1 partOf PROJ-3", (proj1, KG.partOf, proj3) in _jira_spine, True)
    check("jira PROJ-1 dependsOn PROJ-2", (proj1, KG.dependsOn, proj2) in _jira_spine, True)

    jira_cnode = iris.comment("PROJ-3", 0)
    check("jira PROJ-3 comment 0 is kg:Learning",
          (jira_cnode, RDF.type, KG.Learning) in _jira_run, True)
    check("jira Learning prov:wasDerivedFrom PROJ-3 issue",
          (jira_cnode, PROV.wasDerivedFrom, proj3) in _jira_run, True)
    check("jira Learning prov:wasGeneratedBy a run node",
          _jira_run.value(jira_cnode, PROV.wasGeneratedBy) is not None, True)
    check("jira stats: acme-app(override) in teams",
          any("acme-app(override)" in t for t in _jira_stats["teams"]), True)

    # SHACL conformance for jira graph
    from pyshacl import validate as _shacl_validate
    from kg_ingest import ontology as _onto

    _shapes = _onto.load("shapes.ttl")
    _kg_onto = _onto.load("kg.ttl")
    _combined = _jira_spine + _jira_run + _kg_onto
    _conforms, _, _shacl_txt = _shacl_validate(
        _combined, shacl_graph=_shapes, inference="rdfs", abort_on_first=False
    )
    check("jira fixture SHACL conforms", _conforms, True)

    # --- Linear fixture: same triples as before (no regression) ---
    _lin_spine_a = Graph()
    _lin_run_a = Graph()
    _lin_out_a = io.StringIO()
    with contextlib.redirect_stdout(_lin_out_a):
        _lin_stats_a = add_tracker(_lin_spine_a, _lin_run_a, "test/repo", "test-run-linear-a",
                                   pipeline_ver="0.1.0", issues_override=FIXTURE_ISSUES)

    _GOLDEN_LINEAR_FILE = REPO_ROOT / "tests" / "fixtures" / "tracker-data-linear-golden.nt"
    _GOLDEN_NS = "https://example.org/kg/"

    def _normalize_nt(g: Graph) -> list[str]:
        from kg_ingest.iris import NAMESPACE as _NS
        raw = g.serialize(format="nt")
        lines = sorted(l for l in raw.splitlines() if l.strip())
        if _NS != _GOLDEN_NS:
            lines = [l.replace(_NS, _GOLDEN_NS) for l in lines]
        return lines

    with open(REPO_ROOT / "tests" / "fixtures" / "tracker-data.json", encoding="utf-8") as f:
        _lin_file_issues = json.load(f)
    _golden_spine = Graph()
    _golden_run = Graph()
    with contextlib.redirect_stdout(io.StringIO()):
        add_tracker(_golden_spine, _golden_run, "test/repo", "golden-run",
                    pipeline_ver="0.1.0", issues_override=_lin_file_issues)
    _actual_nt = _normalize_nt(_golden_spine + _golden_run)

    # Under pytest, --write-golden is never in sys.argv; the comparison always runs.
    _golden_nt = [l for l in _GOLDEN_LINEAR_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    _added_nt = sorted(set(_actual_nt) - set(_golden_nt))
    _removed_nt = sorted(set(_golden_nt) - set(_actual_nt))
    if _added_nt or _removed_nt:
        for l in _removed_nt:
            print(f"- {l}")
        for l in _added_nt:
            print(f"+ {l}")
    check("linear golden: sorted N-Triples match the pinned file",
          _actual_nt == _golden_nt, True)

    check("linear fixture: AII-100 sourceSystem defaults to 'linear'",
          _lin_spine_a.value(iss100, KG.sourceSystem), Literal("linear"))
    check("linear fixture: team still inferred from identifier prefix (no team field)",
          any("AII(override)" in t for t in _lin_stats_a["teams"]), True)


def test_refresh_subcommand(snapshot_sources_backup):
    env_no_key = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
    env_no_key["PYTHONPATH"] = str(REPO_ROOT)

    # Test 1: refresh --help lists --code-repo and --tracker-data
    result = subprocess.run(
        [sys.executable, "-m", "kg_ingest", "refresh", "--help"],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    )
    assert "--code-repo" in result.stdout, (
        f"FAIL: refresh --help missing --code-repo\n{result.stdout}"
    )
    assert "--tracker-data" in result.stdout, (
        f"FAIL: refresh --help missing --tracker-data\n{result.stdout}"
    )
    assert "--no-docs-sites" in result.stdout, (
        f"FAIL: refresh --help missing --no-docs-sites\n{result.stdout}"
    )
    print("PASS: refresh --help lists --code-repo and --tracker-data")

    # Test 2: refresh exits 0 with fixture data and produces a parseable stats line
    result = subprocess.run(
        [
            sys.executable, "-m", "kg_ingest", "refresh",
            "--code-repo", str(REPO_ROOT),
            "--tracker-data", str(FIXTURE_FILE),
            "--no-embed",
            "--no-docs-sites",
            "--max-prs", "0",
        ],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
        env=env_no_key,
        timeout=300,
    )
    assert result.returncode == 0, (
        f"FAIL: refresh exited {result.returncode}\n"
        f"STDOUT: {result.stdout[-3000:]}\n"
        f"STDERR: {result.stderr[-1000:]}"
    )

    stdout_lines = [l for l in result.stdout.splitlines() if l.strip()]
    assert stdout_lines, "FAIL: refresh produced no stdout"
    last_line = stdout_lines[-1]
    try:
        stats_json = json.loads(last_line)
    except json.JSONDecodeError as exc:
        assert False, f"FAIL: last stdout line is not JSON: {last_line!r}\n{exc}"

    for key in ("quads", "vectors", "docPages", "durationSec", "parts"):
        assert key in stats_json, f"FAIL: stats missing key {key!r}: {stats_json}"
    check("stats line has required keys", all(
        k in stats_json for k in ("quads", "vectors", "docPages", "durationSec", "parts")
    ), True)

    # Test 3: issue.nt contains both identifiers
    issue_nt = REPO_ROOT / "snapshot" / "parts" / "issue.nt"
    if issue_nt.exists():
        content = issue_nt.read_text(encoding="utf-8")
        check("issue.nt contains AII-100", "AII-100" in content, True)
        check("issue.nt contains AII-101", "AII-101" in content, True)
    else:
        print("NOTE: issue.nt not found — snapshot may not have been written")

    # Test 4: "file wins" log line emitted when LINEAR_API_KEY is set alongside --tracker-data
    env_with_key = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
    env_with_key["PYTHONPATH"] = str(REPO_ROOT)
    env_with_key["LINEAR_API_KEY"] = "DUMMY-KEY-NOT-USED"
    result4 = subprocess.run(
        [
            sys.executable, "-m", "kg_ingest", "refresh",
            "--code-repo", str(REPO_ROOT),
            "--tracker-data", str(FIXTURE_FILE),
            "--no-embed",
            "--no-docs-sites",
            "--max-prs", "0",
        ],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
        env=env_with_key,
        timeout=300,
    )
    assert result4.returncode == 0, (
        f"FAIL: file-wins subprocess exited {result4.returncode}\n"
        f"STDOUT: {result4.stdout[-3000:]}\n"
        f"STDERR: {result4.stderr[-1000:]}"
    )
    check("file-wins log line in subprocess stdout",
          "file wins" in result4.stdout, True)
    print("PASS: file-wins subprocess exits 0 and emits 'file wins' log line")

    # Test 5: --repos-root with no clones under it skips every secondary and still exits 0
    _empty_root = tempfile.mkdtemp(prefix="kg-repos-root-")
    result5 = subprocess.run(
        [
            sys.executable, "-m", "kg_ingest", "refresh",
            "--code-repo", str(REPO_ROOT),
            "--tracker-data", str(FIXTURE_FILE),
            "--repos-root", _empty_root,
            "--no-embed",
            "--no-docs-sites",
            "--max-prs", "0",
        ],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
        env=env_no_key,
        timeout=300,
    )
    assert result5.returncode == 0, (
        f"FAIL: repos-root subprocess exited {result5.returncode}\n"
        f"STDOUT: {result5.stdout[-3000:]}\n"
        f"STDERR: {result5.stderr[-1000:]}"
    )
    from kg_ingest import sources as _sources
    if _sources.load(REPO_ROOT / "sources.yml").get("secondary_repos"):
        check("repos-root: absent secondaries are skipped with a log line",
              "SKIPPED (not under --repos-root)" in result5.stdout, True)
        print("PASS: refresh --repos-root skips absent secondaries and exits 0")
    else:
        print("PASS: refresh --repos-root exits 0 (sources.yml declares no secondary_repos; skip-line check not applicable)")
    shutil.rmtree(_empty_root, ignore_errors=True)
