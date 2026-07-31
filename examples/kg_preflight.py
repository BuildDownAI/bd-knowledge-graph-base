"""KG preflight for bd-build-down — surface prior learnings for a PR/gap and
format an 'institutional memory' block to inject into the triage assessment or
the @agent fix comment.

This is the deterministic helper behind the prompt-engineering integration in
docs/INTEGRATION_bd-build-down.md. In a live session the bd-build-down agent
calls the kg_search MCP tool directly with terms it picks from the gap text;
this script does the same thing offline so the integration is testable.

Usage:
  PYTHONPATH=. ./.venv/bin/python examples/kg_preflight.py "fail-closed" "screener" "directional"
"""
from __future__ import annotations

import sys

from kg_query import queries
from kg_query.store import get_store

_PRIO_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def preflight(store, terms: list[str], per_term: int = 5, top: int = 5) -> dict:
    """Aggregate kg_search over several terms; rank by term-hit count then priority."""
    hits: dict[str, dict] = {}
    for term in terms:
        for r in queries.kg_search(store, term, limit=per_term)["results"]:
            rec = hits.setdefault(r["iri"], {**r, "matched_terms": set()})
            rec["matched_terms"].add(term)
    ranked = sorted(
        hits.values(),
        key=lambda r: (-len(r["matched_terms"]), _PRIO_RANK.get(r.get("priority") or "", 9)),
    )[:top]
    return {"terms": terms, "backend": store.backend(), "results": ranked}


def render_block(pf: dict) -> str:
    """Markdown block ready to paste into a triage note or an @agent comment."""
    if not pf["results"]:
        return "_KG preflight: no prior learnings matched._"
    lines = ["**🧠 Institutional memory (KG preflight — prior learnings for this gap):**", ""]
    for r in pf["results"]:
        pri = f"[{r['priority']}] " if r.get("priority") else ""
        terms = ", ".join(sorted(r["matched_terms"]))
        lines.append(f"- {pri}**{r['title']}**  _(matched: {terms})_")
        if r.get("fix"):
            lines.append(f"    - Known fix/prevention: {r['fix'][:220]}")
    lines += ["", "_If a prior fix contradicts this PR's approach, treat it as a "
              "regression signal and escalate rather than re-deriving._"]
    return "\n".join(lines)


if __name__ == "__main__":
    terms = sys.argv[1:] or ["webhook", "gap-fill", "envelope"]
    print(render_block(preflight(get_store(), terms)))
