"""Classifier tests — a comment is a Learning only when a build-up/down marker is
on its FIRST line (not merely present somewhere in the body).

Run: PYTHONPATH=. ./.venv/bin/python tests/test_classify.py
"""
from kg_ingest.tracker import _classify, _classify_pr_comment, _comment_title, _add_issue, _run_node
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF
from kg_ingest import iris
from kg_ingest.tracker import KG, PROV, DCTERMS, _bind

BODY_LONG = "x " * 250  # ~500 chars, comfortably over _DECISION_MIN_CHARS (400)


def check(name: str, got, want) -> None:
    assert got == want, f"FAIL {name}: got {got!r}, want {want!r}"
    print(f"PASS: {name}")


# 1. Marker on the first line -> learning (both phases, with/without leading '#')
check("first-line marker (#, up)",
      _classify("# ai-implement-build-up-learnings\n\nFeature: foo\n" + BODY_LONG),
      "learning")
check("first-line marker (no #, down)",
      _classify("ai-implement-build-down-learnings\nbody text " + BODY_LONG),
      "learning")

# 2. Marker only DEEPER in the body -> NOT a learning (the over-catch we're fixing).
#    Long enough -> decision; short -> dropped (None).
check("marker deeper + long -> decision (not learning)",
      _classify("AI Planning: Cross-Story Context\n\nSee the "
                "ai-implement-build-up-learnings comment elsewhere. " + BODY_LONG),
      "decision")
check("marker deeper + short -> None",
      _classify("Quick note.\nmentions ai-implement-build-down-learnings once."),
      None)

# 3. Long human rationale, no marker -> decision
check("long rationale, no marker -> decision", _classify(BODY_LONG), "decision")

# 4. Short comment, no marker -> None
check("short, no marker -> None", _classify("lgtm, merging"), None)

# 5. _comment_title still yields the phase title for a first-line-marker learning
title = _comment_title("PROJ-259", "Dispatch dedup has no TTL",
                       "# ai-implement-build-up-learnings\n\nFeature: TTL", "learning")
check("title uses phase form",
      title, "PROJ-259 build-up learnings — Dispatch dedup has no TTL")

print("\nall classify tests passed")

# ---- _classify_pr_comment tests ----

# kg-refresh learnings marker -> learning
check("pr: kg-refresh marker -> learning",
      _classify_pr_comment(
          "# ai-implement-kg-refresh-learnings\n**Refresh:** 2026-09-01\n## What happened\nX",
          "ai-implement[bot]"),
      "learning")

# Smoke-jumper report heading -> verification
check("pr: smoke-jumper heading -> verification",
      _classify_pr_comment("## \U0001f525 Smoke-Jumper Report\n\nAll clear.", "smoke-jumper[bot]"),
      "verification")

# claude-review-verdict marker -> verification
check("pr: claude-review-verdict marker -> verification",
      _classify_pr_comment("<!-- claude-review-verdict pass -->\nLGTM.", "reviewer"),
      "verification")

# ai-implement post-push marker -> verification
check("pr: ai-implement post-push marker -> verification",
      _classify_pr_comment("<!-- ai-implement post-push -->\nPushed fix.", "reviewer"),
      "verification")

# Bot author >=400 chars -> None (bots can't be decision)
check("pr: bot [bot] >=400 -> None",
      _classify_pr_comment(BODY_LONG, "gh-actions[bot]"),
      None)
check("pr: orchestrator-bot >=400 -> None",
      _classify_pr_comment(BODY_LONG, "ai-implement-orchestrator-bot"),
      None)

# Human >=400 chars -> decision
check("pr: human >=400 -> decision",
      _classify_pr_comment(BODY_LONG, "alice"),
      "decision")

# Short comment, no marker -> None
check("pr: short -> None",
      _classify_pr_comment("lgtm", "alice"),
      None)

print("\nall _classify_pr_comment tests passed")

# ---- _classify: new KGB-16 cases ----

# Smoke-jumper heading on an issue comment -> verification
check("issue: smoke-jumper heading -> verification",
      _classify("## \U0001f525 Smoke-Jumper Report\n\nAll clear."),
      "verification")

# AI Planning: first line with headings kwarg -> planning_note
check("issue: AI Planning: first line -> planning_note",
      _classify("AI Planning: Sprint scope\n" + "x " * 250,
                headings=("AI Planning:",)),
      "planning_note")

# AI Planning: first line WITHOUT headings kwarg -> decision (old callers unchanged)
check("issue: AI Planning: first line, no headings -> decision",
      _classify("AI Planning: Sprint scope\n" + "x " * 250),
      "decision")

# is_bot=True suppresses Decision
check("issue: bot author >=400 -> None",
      _classify(BODY_LONG, is_bot=True),
      None)

# Verification heading takes precedence over bot=True
check("issue: smoke-jumper + bot=True -> verification",
      _classify("## \U0001f525 Smoke-Jumper Report\nAll clear.", is_bot=True),
      "verification")

# ---- _classify_pr_comment: status=start exclusion (KGB-7 residual) ----

check("pr: status=start -> None (not verification)",
      _classify_pr_comment(
          "<!-- ai-implement post-push status=start -->\nRunning post-implementation review…",
          "ai-implement[bot]"),
      None)

# A valid post-push marker (no status=start) still classifies as verification
check("pr: post-push no status=start -> verification",
      _classify_pr_comment("<!-- ai-implement post-push -->\nPushed fix.", "ai-implement[bot]"),
      "verification")

# ---- _add_issue round-trip: AI Planning: comment -> kg:PlanningNote ----

def _make_graphs():
    spine_g, run_g = Graph(), Graph()
    _bind(spine_g); _bind(run_g)
    return spine_g, run_g

_planning_body = "AI Planning: Sprint scope\n" + "x " * 250  # 500+ chars

spine_g, run_g = _make_graphs()
run = _run_node(run_g, "test-run-1", "0.0.0-test")
_add_issue(spine_g, run_g, run, "TEST",
           {"identifier": "TEST-1", "title": "Test issue",
            "comments": {"nodes": [{"body": _planning_body, "user": {"name": "orchestrator"}}]},
            "labels": {"nodes": []}, "relations": {"nodes": []}},
           {"issues": 0, "comment_learnings": 0, "comment_decisions": 0,
            "comment_planning_notes": 0, "comment_verifications": 0},
           headings=("AI Planning:",))

cnode = iris.comment("TEST-1", 0)
types_in_run_g = set(run_g.objects(cnode, RDF.type))
check("add_issue: AI Planning: -> kg:PlanningNote in run_g",
      KG.PlanningNote in types_in_run_g,
      True)
check("add_issue: AI Planning: -> provenance wasDerivedFrom",
      (cnode, PROV.wasDerivedFrom, iris.tracker_issue("TEST-1")) in run_g,
      True)
check("add_issue: AI Planning: -> provenance wasGeneratedBy",
      len(list(run_g.objects(cnode, PROV.wasGeneratedBy))) >= 1,
      True)

print("\nall KGB-16 tests passed")

# ---- _add_issue round-trip: bot-authored 400+ char comment -> no semantic node ----

_SEMANTIC_TYPES = {KG.Learning, KG.Decision, KG.Constraint, KG.FailureMode,
                   KG.Observation, KG.Verification, KG.PlanningNote}

spine_g2, run_g2 = _make_graphs()
run2 = _run_node(run_g2, "test-run-2", "0.0.0-test")
_add_issue(spine_g2, run_g2, run2, "TEST",
           {"identifier": "TEST-2", "title": "Test issue",
            "comments": {"nodes": [{"body": BODY_LONG, "user": {"name": "orchestrator-bot"}}]},
            "labels": {"nodes": []}, "relations": {"nodes": []}},
           {"issues": 0, "comment_learnings": 0, "comment_decisions": 0,
            "comment_planning_notes": 0, "comment_verifications": 0},
           headings=())

cnode2 = iris.comment("TEST-2", 0)
types_in_run_g2 = set(run_g2.objects(cnode2, RDF.type))
check("add_issue: bot-named author 400+ chars -> no Decision in run_g",
      KG.Decision not in types_in_run_g2,
      True)
check("add_issue: bot-named author 400+ chars -> no semantic node type in run_g",
      types_in_run_g2.isdisjoint(_SEMANTIC_TYPES),
      True)

# Also verify with a name in the bot_users set rather than name-heuristic
spine_g3, run_g3 = _make_graphs()
run3 = _run_node(run_g3, "test-run-3", "0.0.0-test")
_add_issue(spine_g3, run_g3, run3, "TEST",
           {"identifier": "TEST-3", "title": "Test issue",
            "comments": {"nodes": [{"body": BODY_LONG, "user": {"name": "ai-orchestrator"}}]},
            "labels": {"nodes": []}, "relations": {"nodes": []}},
           {"issues": 0, "comment_learnings": 0, "comment_decisions": 0,
            "comment_planning_notes": 0, "comment_verifications": 0},
           headings=(),
           bot_users={"ai-orchestrator"})

cnode3 = iris.comment("TEST-3", 0)
types_in_run_g3 = set(run_g3.objects(cnode3, RDF.type))
check("add_issue: bot_users-listed author 400+ chars -> no Decision in run_g",
      KG.Decision not in types_in_run_g3,
      True)
check("add_issue: bot_users-listed author 400+ chars -> no semantic node type in run_g",
      types_in_run_g3.isdisjoint(_SEMANTIC_TYPES),
      True)

print("\nall bot-suppression round-trip tests passed")
