"""Load ontology/SHACL files with the configured namespace applied.

The TTL sources are written against the neutral default namespace; a deployment
that sets sources.yml `namespace:` gets the same ontology re-homed onto its own
namespace at parse time (single-substitution — the files reference the namespace
only via their @prefix declarations).
"""
from __future__ import annotations

from pathlib import Path

from rdflib import Graph

from .iris import DEFAULT_NAMESPACE, NAMESPACE

ONTO_DIR = Path(__file__).resolve().parent.parent / "ontology"


def load(name: str) -> Graph:
    text = (ONTO_DIR / name).read_text(encoding="utf-8")
    if NAMESPACE != DEFAULT_NAMESPACE:
        text = text.replace(DEFAULT_NAMESPACE, NAMESPACE)
    g = Graph()
    g.parse(data=text, format="turtle")
    return g
