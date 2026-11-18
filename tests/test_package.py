"""The Knowledge Graph package kg-ingest package builds: manifest, ontology, one graph per part."""
from __future__ import annotations

import json
import socket
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from rdflib import Graph, URIRef
from rdflib.namespace import RDF, RDFS

from kg_ingest import package, sources
from kg_ingest.iris import KG, KGR, NAMESPACE as NS

ROOT = Path(__file__).resolve().parent.parent


def _parts(d: Path) -> Path:
    parts = d / "parts"
    parts.mkdir()
    learning = f"<{NS}resource/learning/x>"
    (parts / "doc.nt").write_text('<https://e/a> <https://e/p> "x" .\n')
    (parts / "learning.nt").write_text(
        f'{learning} <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <{KG}Learning> .\n'
        f'{learning} <http://purl.org/dc/terms/title> "Retry webhooks" .\n')
    return parts


def test_build_package(tmp_path):
    parts = _parts(tmp_path)
    pkg = package.build_package(tmp_path / "pkg", "kgdb", parts)
    kg = sources.load()["knowledge_graph"]

    kit = yaml.safe_load((pkg / "kit.yaml").read_text())
    assert list(kit) == ["name", "group", "version", "label", "description", "options", "namespaces", "alias", "data"]
    assert (kit["group"], kit["name"], kit["version"]) == (kg["group"], kg["name"], str(kg["version"]))
    assert kit["alias"] == kg["name"].replace("-", "_")
    questions = kit["options"].pop("voicebox.example.questions")
    assert kit["options"] == {"database.name": "kgdb", "search.enabled": True, "graph.aliases": True,
                              "voicebox.enabled": True, "search.index.contexts.excluded": True,
                              "search.semantic.index.contexts.filter": [
                                  f"{KGR}graph/part/doc", f"{KGR}graph/part/learning", f"{KGR}graph/card-fields"]}
    assert 1 <= len(questions) <= 10
    assert all(f"- {q}\n" in kit["description"] for q in questions)
    assert kit["data"] == [
        {"file": "ontology.ttl", "graph": "tag:stardog:api:context:schema"},
        {"file": "data/doc.nt", "graph": f"{KGR}graph/part/doc"},
        {"file": "data/learning.nt", "graph": f"{KGR}graph/part/learning"},
        {"file": "data/cards.nt", "graph": f"{KGR}graph/cards"},
        {"file": "data/card-fields.nt", "graph": f"{KGR}graph/card-fields"},
    ]
    cards = Graph().parse(pkg / "data" / "cards.nt", format="nt")
    assert set(cards.predicates()) == {KG.cardText}
    assert (pkg / "data" / "learning.nt").read_bytes() == (parts / "learning.nt").read_bytes()
    files = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    assert files == {item["file"] for item in kit["data"]} | {"kit.yaml", "package.json"}

    onto = Graph().parse(pkg / "ontology.ttl", format="turtle")
    assert not list(onto.triples((None, RDFS.domain, None)))
    assert not list(onto.triples((None, RDFS.range, None)))
    source = Graph().parse(ROOT / "ontology" / "kg.ttl", format="turtle")
    schema = "https://schema.org/"
    for rdfs_p, name in ((RDFS.domain, "domainIncludes"), (RDFS.range, "rangeIncludes")):
        have = len(list(source.triples((None, rdfs_p, None)))) + len(list(source.triples((None, URIRef(schema + name), None))))
        assert len(list(onto.triples((None, URIRef(schema + name), None)))) == have
    assert (URIRef(str(KG)), RDF.type, URIRef("http://system.stardog.com/DataModel")) in onto

    meta = json.loads((pkg / "package.json").read_text())
    lines = sum(1 for item in kit["data"] if item["file"].endswith(".nt")
                for line in (pkg / item["file"]).read_text().splitlines() if line.strip())
    assert meta == {"name": kg["name"], "version": str(kg["version"]), "alias": kit["alias"],
                    "expectedTriples": len(onto) + lines}


@pytest.mark.parametrize("bad", [None, {"name": "x"}, "blank"])
def test_bad_knowledge_graph_block(tmp_path, monkeypatch, bad):
    kg = dict(sources.load()["knowledge_graph"])
    if bad == "blank":
        kg["questions"] = None
        block, key = kg, "questions"
    else:
        block, key = bad, "knowledge_graph" if bad is None else "group"
    monkeypatch.setattr(sources, "load", lambda *a, **k: {"knowledge_graph": block})
    with pytest.raises(SystemExit, match=key):
        package.build_package(tmp_path / "pkg", "kgdb", _parts(tmp_path))


def test_missing_database_flag(tmp_path):
    """--database is required; argparse should exit non-zero with a message naming it."""
    with pytest.raises(SystemExit) as exc_info:
        package.main(["--out", str(tmp_path / "pkg")])
    assert exc_info.value.code != 0


def test_no_network_call(tmp_path, monkeypatch):
    """build_package must make no outbound network connection."""
    original_connect = socket.socket.connect

    def _fail_connect(self, address):
        raise OSError(f"network calls are forbidden in package; tried to connect to {address}")

    monkeypatch.setattr(socket.socket, "connect", _fail_connect)
    parts = _parts(tmp_path)
    # Should complete without triggering the socket stub
    package.build_package(tmp_path / "pkg", "kgdb", parts)
