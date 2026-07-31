"""Prove the provenance invariant is enforced, not just documented.

A kg:Learning with no prov:wasDerivedFrom / prov:wasGeneratedBy MUST fail SHACL;
adding both MUST make it pass. Run: python -m pytest tests/  (or plain python).
"""
from pathlib import Path

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, XSD, Namespace
from pyshacl import validate

ONTO = Path(__file__).resolve().parent.parent / "ontology"
KG = Namespace("https://example.org/kg/onto#")
PROV = Namespace("http://www.w3.org/ns/prov#")
DCTERMS = Namespace("http://purl.org/dc/terms/")
EX = Namespace("https://example.org/kg/resource/")


def _base() -> tuple[Graph, Graph]:
    shapes = Graph().parse(ONTO / "shapes.ttl", format="turtle")
    onto = Graph().parse(ONTO / "kg.ttl", format="turtle")
    return shapes, onto


def _validate(data: Graph):
    shapes, onto = _base()
    data = data + onto
    conforms, _g, txt = validate(data, shacl_graph=shapes, inference="rdfs",
                                 abort_on_first=False)
    return conforms, txt


def test_learning_without_provenance_fails():
    g = Graph()
    lrn = EX["learning/x/solutions/L-9999"]
    g.add((lrn, RDF.type, KG.Learning))
    g.add((lrn, DCTERMS.title, Literal("orphan learning", datatype=XSD.string)))
    conforms, txt = _validate(g)
    assert conforms is False, "orphan learning should violate the provenance shape"
    assert "wasDerivedFrom" in txt or "wasGeneratedBy" in txt


def test_learning_with_provenance_passes():
    g = Graph()
    lrn = EX["learning/x/solutions/L-9999"]
    doc = EX["doc/x/docs/foo.md"]
    run = EX["run/deadbeef"]
    g.add((doc, RDF.type, KG.Doc))
    g.add((run, RDF.type, KG.ExtractionRun))
    g.add((run, KG.pipelineVer, Literal("0.1.0")))
    g.add((run, KG.model, Literal("deterministic-parser")))
    g.add((run, KG.promptHash, Literal("n/a")))
    g.add((lrn, RDF.type, KG.Learning))
    g.add((lrn, DCTERMS.title, Literal("good learning", datatype=XSD.string)))
    g.add((lrn, PROV.wasDerivedFrom, doc))
    g.add((lrn, PROV.wasGeneratedBy, run))
    conforms, txt = _validate(g)
    assert conforms is True, f"well-formed learning should pass; got:\n{txt[:1500]}"


if __name__ == "__main__":
    test_learning_without_provenance_fails()
    print("PASS: orphan learning correctly rejected")
    test_learning_with_provenance_passes()
    print("PASS: provenanced learning correctly accepted")
