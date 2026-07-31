"""cards.build_cards over a tiny fixture graph.
Run: PYTHONPATH=. ./.venv/bin/python tests/test_cards.py
"""
import os, tempfile
from kg_ingest.iris import NAMESPACE as _NS
from kg_ingest.cards import build_cards, SNIPPET_CHARS
from kg_query.store import RdflibStore

FIXTURE_RAW = """
@prefix kg: <https://example.org/kg/onto#> .
@prefix dcterms: <http://purl.org/dc/terms/> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
<https://example.org/kg/resource/graph/spine> {
  <https://example.org/kg/resource/issue/PROJ-259> a kg:Issue ;
    dcterms:title "Dispatch dedup has no TTL" ;
    kg:label "bug" ;
    kg:label "Ready for Review" ;
    kg:description "A torn-down issue can never be re-dispatched." .
  <https://example.org/kg/resource/topic/dedup> a kg:Topic ; skos:prefLabel "dedup" .
  <https://example.org/kg/resource/comment/PROJ-259/0> a kg:Learning ;
    dcterms:title "PROJ-259 build-up learnings - dedup TTL" ;
    kg:fix "Add a TTL so torn-down issues can be re-dispatched." ;
    kg:tagged <https://example.org/kg/resource/topic/dedup> .
  <https://example.org/kg/resource/adr/0001> a kg:Decision ;
    dcterms:title "Use rdflib backend" ;
    kg:detail "Chosen for zero-infra local dev." .
  <https://example.org/kg/resource/comment/PROJ-259/1> a kg:Learning ;
    dcterms:title "Untagged learning with no fix" .
  <https://example.org/kg/resource/issue/PROJ-260> a kg:Issue ;
    dcterms:title "Untagged issue" ;
    kg:description "No tags on this one." .
}
"""
FIXTURE = FIXTURE_RAW.replace(_NS, _NS)

def main():
    with tempfile.NamedTemporaryFile("w", suffix=".trig", delete=False) as f:
        f.write(FIXTURE); path = f.name
    try:
        cards = build_cards(RdflibStore(path))
    finally:
        os.unlink(path)
    types = {c["type"] for c in cards}
    assert types == {"Learning", "Decision", "Issue"}, types
    assert len(cards) == 5, len(cards)
    learning = next(c for c in cards
                     if c["type"] == "Learning" and "dedup TTL" in c["title"])
    assert "dedup" in learning["card_text"].lower(), learning["card_text"]
    assert "TTL" in learning["card_text"], learning["card_text"]
    issue = next(c for c in cards
                  if c["type"] == "Issue" and c["title"] == "Dispatch dedup has no TTL")
    assert "dedup" in issue["card_text"].lower(), issue["card_text"]
    # Non-lifecycle kg:label is embedded; lifecycle label is filtered out.
    assert "bug" in issue["card_text"].lower(), issue["card_text"]
    assert "ready for review" not in issue["card_text"].lower(), issue["card_text"]

    # Regression: nodes with NO kg:tagged (and, for the Learning, no kg:fix
    # either) must not crash the GROUP_CONCAT/SAMPLE aggregation, and must
    # produce a card_text that is exactly the title (no trailing empty lines
    # for absent tags/snippet).
    untagged_learning = next(c for c in cards
                              if c["type"] == "Learning"
                              and c["title"] == "Untagged learning with no fix")
    assert untagged_learning["card_text"] == untagged_learning["title"], \
        untagged_learning["card_text"]
    assert untagged_learning["snippet"] == ""

    untagged_issue = next(c for c in cards
                           if c["type"] == "Issue" and c["title"] == "Untagged issue")
    assert "dedup" not in untagged_issue["card_text"].lower(), untagged_issue["card_text"]
    assert "No tags on this one." in untagged_issue["card_text"]

    assert all(len(c["snippet"]) <= SNIPPET_CHARS for c in cards)
    assert cards == sorted(cards, key=lambda c: c["iri"]), "must be IRI-sorted"
    print(f"PASS: build_cards -> {len(cards)} cards {types}")

if __name__ == "__main__":
    main()
