"""Build compact 'index cards' — one short text per Learning/Decision/Issue —
for the semantic-search embedding step. Read-only SPARQL over a Store; NO
embedding dependency here so it stays trivially testable.
"""
from __future__ import annotations

from kg_query.queries import PREFIXES

SNIPPET_CHARS = 400

# Linear workflow-STATE labels (mirror kg_ingest/tracker.py::_LIFECYCLE_LABELS).
# Excluded from Issue cards — they are pipeline state, not subject matter, and
# would only add noise to the embedding. Compared case-insensitively.
_LIFECYCLE_LABELS = {
    "ai-implement", "ai-planning", "ai-working",
    "plan-complete", "ready for review",
}

# NOTE: no GROUP_CONCAT/SAMPLE aggregation here. rdflib raises NotBoundError
# from inside an aggregate when its argument variable is never bound in a row
# (e.g. a node with no ?tag at all, since ?tag only comes from an OPTIONAL).
# Real graphs regularly have untagged nodes, so instead we SELECT one row per
# (iri, tag) — same shape as kg_query.queries.kg_search — and aggregate in
# Python: collect distinct tag labels, and take the first non-empty
# fix/detail/description value seen for that iri.
_LEARNING_Q = PREFIXES + """
SELECT ?iri ?title ?tag ?fix WHERE {
  ?iri a kg:Learning ; dcterms:title ?title .
  OPTIONAL { ?iri kg:fix ?fix }
  OPTIONAL { ?iri kg:tagged ?t . ?t skos:prefLabel ?tag }
} ORDER BY ?iri
"""

_DECISION_Q = PREFIXES + """
SELECT ?iri ?title ?detail WHERE {
  ?iri a kg:Decision ; dcterms:title ?title .
  OPTIONAL { ?iri kg:detail ?detail }
} ORDER BY ?iri
"""

# Issues use their non-lifecycle kg:label values directly (per the spec), not
# kg:tagged topics — so a label that never became a topic still reaches the card.
# Lifecycle labels are filtered out in Python (_group_by_iri drop_lc).
_ISSUE_Q = PREFIXES + """
SELECT ?iri ?title ?tag ?description WHERE {
  ?iri a kg:Issue ; dcterms:title ?title .
  OPTIONAL { ?iri kg:label ?tag }
  OPTIONAL { ?iri kg:description ?description }
} ORDER BY ?iri
"""

# Doc pages (AII-345): spine stamps dcterms:title + kg:detail on Doc nodes so
# published documentation is findable by meaning ("what do the docs promise?").
# Docs without a title (pre-AII-345 graphs) simply produce no card — additive.
_DOC_Q = PREFIXES + """
SELECT ?iri ?title ?path ?detail WHERE {
  ?iri a kg:Doc ; dcterms:title ?title .
  OPTIONAL { ?iri kg:path ?path }
  OPTIONAL { ?iri kg:detail ?detail }
} ORDER BY ?iri
"""


def _card(iri: str, ntype: str, title: str, extras: list[str], snippet: str) -> dict:
    parts = [title] + [e for e in extras if e]
    snip = (snippet or "")[:SNIPPET_CHARS]
    if snip:
        parts.append(snip)
    return {"iri": iri, "type": ntype, "title": title,
            "snippet": snip, "card_text": "\n".join(parts)}


def _group_by_iri(rows: list[dict], extra_field: str | None,
                  drop_lc: set[str] | None = None) -> dict[str, dict]:
    """Fold flat SELECT rows (one row per iri x tag) into one record per iri:
    distinct tag labels (order of first appearance) and the first non-empty
    value of `extra_field` (e.g. fix/detail/description) seen for that iri.

    `drop_lc` is a set of lowercased tag values to exclude (e.g. lifecycle labels).
    """
    by_iri: dict[str, dict] = {}
    for r in rows:
        rec = by_iri.setdefault(r["iri"], {"title": r.get("title", ""),
                                           "tags": [], "extra": ""})
        tag = r.get("tag")
        if (tag and (drop_lc is None or tag.lower() not in drop_lc)
                and tag not in rec["tags"]):
            rec["tags"].append(tag)
        if extra_field and not rec["extra"]:
            rec["extra"] = r.get(extra_field, "")
    return by_iri


def build_cards(store) -> list[dict]:
    cards: list[dict] = []
    for iri, rec in _group_by_iri(store.select(_LEARNING_Q), "fix").items():
        cards.append(_card(iri, "Learning", rec["title"],
                           [", ".join(rec["tags"])], rec["extra"]))
    for iri, rec in _group_by_iri(store.select(_DECISION_Q), "detail").items():
        cards.append(_card(iri, "Decision", rec["title"], [], rec["extra"]))
    for iri, rec in _group_by_iri(store.select(_ISSUE_Q), "description",
                                  drop_lc=_LIFECYCLE_LABELS).items():
        cards.append(_card(iri, "Issue", rec["title"],
                           [", ".join(rec["tags"])], rec["extra"]))
    for iri, rec in _group_by_iri(store.select(_DOC_Q), "detail").items():
        cards.append(_card(iri, "Doc", rec["title"], [], rec["extra"]))
    cards.sort(key=lambda c: c["iri"])
    return cards
