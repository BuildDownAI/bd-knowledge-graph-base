"""Classifier tests — a comment is a Learning only when a build-up/down marker is
on its FIRST line (not merely present somewhere in the body).

Run: PYTHONPATH=. ./.venv/bin/python tests/test_classify.py
"""
from kg_ingest.tracker import _classify, _classify_pr_comment, _comment_title

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
