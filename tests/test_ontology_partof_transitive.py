"""Verify kg:partOf is declared owl:TransitiveProperty in the base ontology.

Loads the ontology via kg_ingest.ontology.load (the namespace-substitution path)
so that any regression introduced by the new @prefix schema: line is caught here.
"""
from rdflib import URIRef
from rdflib.namespace import OWL, RDF

from kg_ingest import ontology as _onto


def test_partof_is_transitive_property():
    g = _onto.load("kg.ttl")
    assert len(g) > 0, "ontology graph should not be empty"

    # Locate kg:partOf IRI from the graph bindings
    kg_ns = dict(g.namespaces()).get("kg")
    assert kg_ns is not None, "kg prefix must be bound in ontology/kg.ttl"
    part_of = kg_ns + "partOf"

    assert (URIRef(part_of), RDF.type, OWL.TransitiveProperty) in g, (
        "kg:partOf must be declared owl:TransitiveProperty"
    )


if __name__ == "__main__":
    test_partof_is_transitive_property()
    print("PASS: kg:partOf correctly declared owl:TransitiveProperty")
