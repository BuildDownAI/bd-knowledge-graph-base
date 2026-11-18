"""Verify kg:partOf is owl:TransitiveProperty and schema: hints match the reference list.

Loads the ontology via kg_ingest.ontology.load (the namespace-substitution path)
so that any regression introduced by the @prefix schema: line is caught here.
"""
from rdflib import URIRef
from rdflib.namespace import OWL, RDF

from kg_ingest import ontology as _onto

KG = "https://example.org/kg/onto#"
SCHEMA = "https://schema.org/"
DI = URIRef(SCHEMA + "domainIncludes")
RI = URIRef(SCHEMA + "rangeIncludes")


def _kg(name: str) -> URIRef:
    return URIRef(KG + name)


EXPECTED_SCHEMA_TRIPLES = {
    (_kg("aboutLearning"), DI, _kg("StatusUpdate")),
    (_kg("aboutLearning"), RI, _kg("Learning")),
    (_kg("addressedBy"), DI, _kg("Issue")),
    (_kg("addressedBy"), RI, _kg("Commit")),
    (_kg("addressedBy"), RI, _kg("PullRequest")),
    (_kg("affectsComponent"), DI, _kg("Learning")),
    (_kg("affectsFile"), DI, _kg("Learning")),
    (_kg("affectsFile"), RI, _kg("File")),
    (_kg("anchor"), DI, _kg("DocSection")),
    (_kg("branchName"), DI, _kg("Issue")),
    (_kg("category"), DI, _kg("Learning")),
    (_kg("contentHash"), DI, _kg("DocPage")),
    (_kg("dependsOn"), DI, _kg("Issue")),
    (_kg("dependsOn"), RI, _kg("Issue")),
    (_kg("description"), DI, _kg("Issue")),
    (_kg("detail"), DI, _kg("Doc")),
    (_kg("detail"), DI, _kg("DocPage")),
    (_kg("detail"), DI, _kg("SemanticNode")),
    (_kg("docsUrl"), DI, _kg("Repo")),
    (_kg("documentsBranch"), DI, _kg("DocSite")),
    (_kg("duplicateOf"), DI, _kg("Issue")),
    (_kg("duplicateOf"), RI, _kg("Issue")),
    (_kg("filedAs"), DI, _kg("Decision")),
    (_kg("filedAs"), DI, _kg("Learning")),
    (_kg("filedAs"), RI, _kg("Issue")),
    (_kg("fix"), DI, _kg("SemanticNode")),
    (_kg("foundDate"), DI, _kg("Learning")),
    (_kg("heading"), DI, _kg("DocSection")),
    (_kg("impact"), DI, _kg("Learning")),
    (_kg("inProject"), DI, _kg("Issue")),
    (_kg("inProject"), RI, _kg("Project")),
    (_kg("label"), DI, _kg("Issue")),
    (_kg("learningId"), DI, _kg("Learning")),
    (_kg("level"), DI, _kg("DocSection")),
    (_kg("login"), DI, _kg("Person")),
    (_kg("mcpServer"), DI, _kg("McpSource")),
    (_kg("number"), DI, _kg("Issue")),
    (_kg("number"), DI, _kg("PullRequest")),
    (_kg("partOf"), DI, _kg("Commit")),
    (_kg("partOf"), DI, _kg("DocPage")),
    (_kg("partOf"), DI, _kg("DocSection")),
    (_kg("partOf"), DI, _kg("File")),
    (_kg("partOf"), DI, _kg("Issue")),
    (_kg("partOf"), DI, _kg("PullRequest")),
    (_kg("partOf"), RI, _kg("DocPage")),
    (_kg("partOf"), RI, _kg("DocSite")),
    (_kg("partOf"), RI, _kg("Issue")),
    (_kg("partOf"), RI, _kg("Repo")),
    (_kg("path"), DI, _kg("File")),
    (_kg("priority"), DI, _kg("Learning")),
    (_kg("problemType"), DI, _kg("Learning")),
    (_kg("references"), DI, _kg("Commit")),
    (_kg("references"), DI, _kg("PullRequest")),
    (_kg("references"), RI, _kg("Issue")),
    (_kg("regressedBy"), DI, _kg("Learning")),
    (_kg("regressedBy"), RI, _kg("Commit")),
    (_kg("regressedBy"), RI, _kg("PullRequest")),
    (_kg("relatedPr"), DI, _kg("Learning")),
    (_kg("relatedPr"), RI, _kg("PullRequest")),
    (_kg("relatedSolution"), DI, _kg("Learning")),
    (_kg("relatedSolution"), RI, _kg("Doc")),
    (_kg("relatedTo"), DI, _kg("Issue")),
    (_kg("relatedTo"), RI, _kg("Issue")),
    (_kg("resolutionType"), DI, _kg("Learning")),
    (_kg("resolvedBy"), DI, _kg("Learning")),
    (_kg("resolvedBy"), RI, _kg("Commit")),
    (_kg("resolvedBy"), RI, _kg("PullRequest")),
    (_kg("rootCause"), DI, _kg("Learning")),
    (_kg("rootUrl"), DI, _kg("DocSite")),
    (_kg("severityLevel"), DI, _kg("Learning")),
    (_kg("sourceId"), DI, _kg("Commit")),
    (_kg("sourceId"), DI, _kg("Repo")),
    (_kg("sourceSystem"), DI, _kg("Issue")),
    (_kg("sourceSystem"), DI, _kg("Repo")),
    (_kg("state"), DI, _kg("Issue")),
    (_kg("state"), DI, _kg("PullRequest")),
    (_kg("status"), DI, _kg("Decision")),
    (_kg("status"), DI, _kg("Issue")),
    (_kg("status"), DI, _kg("Learning")),
    (_kg("symptom"), DI, _kg("Learning")),
    (_kg("tagged"), DI, _kg("Issue")),
    (_kg("tagged"), DI, _kg("SemanticNode")),
    (_kg("tagged"), RI, _kg("Topic")),
    (_kg("text"), DI, _kg("DocSection")),
    (_kg("touches"), RI, _kg("File")),
    (_kg("trackerKey"), DI, _kg("Issue")),
    (_kg("trackerTeam"), DI, _kg("Issue")),
    (_kg("url"), DI, _kg("DocPage")),
}


def test_partof_is_transitive_property():
    g = _onto.load("kg.ttl")
    assert len(g) > 0, "ontology graph should not be empty"

    kg_ns = dict(g.namespaces()).get("kg")
    assert kg_ns is not None, "kg prefix must be bound in ontology/kg.ttl"
    part_of = kg_ns + "partOf"

    assert (URIRef(part_of), RDF.type, OWL.TransitiveProperty) in g, (
        "kg:partOf must be declared owl:TransitiveProperty"
    )


def test_schema_hints_match_reference():
    g = _onto.load("kg.ttl")

    actual = {
        (s, p, o)
        for s, p, o in g
        if p in (DI, RI)
    }

    extra = actual - EXPECTED_SCHEMA_TRIPLES
    missing = EXPECTED_SCHEMA_TRIPLES - actual

    assert not extra, f"Unexpected schema: triples in ontology:\n" + "\n".join(
        f"  {s}  {p}  {o}" for s, p, o in sorted(str(t) for t in extra)
    )
    assert not missing, f"Missing schema: triples from reference list:\n" + "\n".join(
        f"  {s}  {p}  {o}" for s, p, o in sorted(str(t) for t in missing)
    )


if __name__ == "__main__":
    test_partof_is_transitive_property()
    print("PASS: kg:partOf correctly declared owl:TransitiveProperty")
    test_schema_hints_match_reference()
    print("PASS: schema:domainIncludes / schema:rangeIncludes match reference list exactly")
