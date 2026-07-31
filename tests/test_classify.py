"""Classifier tests — a comment is a Learning only when a build-up/down marker is
on its FIRST line (not merely present somewhere in the body).

Run: PYTHONPATH=. ./.venv/bin/python tests/test_classify.py
"""
from kg_ingest.tracker import _classify, _comment_title

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
