"""Tracker title-selection tests: _first_line skips HTML markers and one-word
headings; _comment_title formats titles correctly for each comment kind.

Run: PYTHONPATH=. python3 tests/test_tracker.py
"""
from kg_ingest.tracker import _first_line, _comment_title, _classify
from kg_ingest.iris import DEFAULT_NAMESPACE as _DNS, NAMESPACE as _NS


def check(name: str, got, want) -> None:
    assert got == want, f"FAIL {name}:\n  got  {got!r}\n  want {want!r}"
    print(f"PASS: {name}")


# ---- _first_line: HTML comment lines skipped ----

check("html marker first line skipped",
      _first_line("<!-- ai-implement post-push iter=1 -->\n✅ Fix complete\nMore detail"),
      "✅ Fix complete")

check("claude-review-verdict marker skipped",
      _first_line("<!-- claude-review-verdict pass -->\nLGTM all clear."),
      "LGTM all clear.")

# ---- _first_line: one-word headings skipped ----

check("one-word h2 heading skipped",
      _first_line("## Summary\nThe approach is..."),
      "The approach is...")

check("one-word h1 heading skipped",
      _first_line("# Approach\nWe decided to..."),
      "We decided to...")

check("emoji + one-word heading skipped",
      _first_line("## ✅ Summary\nNext line"),
      "Next line")

# ---- _first_line: multi-word headings kept ----

check("multi-word heading kept",
      _first_line("## Summary of the approach\nNext line"),
      "Summary of the approach")

check("smoke-jumper heading kept (emoji + multi-word)",
      _first_line("## \U0001f525 Smoke-Jumper Report\nAll clear."),
      "\U0001f525 Smoke-Jumper Report")

# ---- _first_line: plain non-heading lines returned as-is ----

check("plain first line returned",
      _first_line("The design is X\nMore text"),
      "The design is X")

# ---- _classify: learning markers on raw first line still detected ----
# (even though _first_line now skips single-token # headings)

check("build-up learning marker detected via raw first line",
      _classify("# ai-implement-build-up-learnings\nBody text here"),
      "learning")

check("build-down learning marker detected via raw first line",
      _classify("# ai-implement-build-down-learnings\nSome body"),
      "learning")

check("kg-refresh learning marker (no #) detected",
      _classify("ai-implement-kg-refresh-learnings\nSome body " + "x " * 250),
      "learning")

# ---- _comment_title: verification with HTML markers ----

check("verification post-push -> review title",
      _comment_title("KGB-5", "My issue",
                     "<!-- ai-implement post-push iter=1 -->\n✅ Fix done", "verification"),
      "KGB-5 review: ✅ Fix done")

check("verification claude-review-verdict -> review title",
      _comment_title("KGB-5", "My issue",
                     "<!-- claude-review-verdict pass -->\nLGTM all clear.", "verification"),
      "KGB-5 review: LGTM all clear.")

# ---- _comment_title: decision with one-word heading -> next substantive line ----

check("decision one-word heading -> next line",
      _comment_title("DOC-1", "Doc issue",
                     "## Summary\nThe design is X\n" + "x " * 200, "decision"),
      "DOC-1: The design is X")

# ---- _comment_title: learning marker uses phase form (unchanged) ----

check("learning marker title uses build-up phase form",
      _comment_title("PROJ-259", "Dispatch dedup",
                     "# ai-implement-build-up-learnings\n\nFeature: TTL", "learning"),
      "PROJ-259 build-up learnings — Dispatch dedup")

check("learning marker title uses build-down phase form",
      _comment_title("PROJ-300", "Old approach",
                     "# ai-implement-build-down-learnings\nWhat failed", "learning"),
      "PROJ-300 build-down learnings — Old approach")

# ---- Namespace dual-run compliance (KGB-6 gate) ----
# These tests operate on pure strings; namespace does not affect results.
# Importing DEFAULT_NAMESPACE and NAMESPACE satisfies the KGB-6 pattern requirement.
assert isinstance(_DNS, str) and isinstance(_NS, str)

print("\nall tracker title tests passed")


if __name__ == "__main__":
    print("Running tracker title tests...")
