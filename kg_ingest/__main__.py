"""Console entry point: `kg-ingest` (installed script) / `python -m kg_ingest`.

Subcommands wrap the existing modules, which all stay runnable directly via
`python -m kg_ingest.<module>`:

  kg-ingest build [flags]    full ingest -> out/graph.trig + snapshot/ (kg_ingest.cli)
  kg-ingest embed            rebuild only the embeddings sidecar (kg_ingest.embed)
  kg-ingest snapshot         regenerate snapshot/ from out/graph.trig (kg_ingest.snapshot)
  kg-ingest materialize      reconstitute out/graph.trig from snapshot/ (kg_ingest.materialize)

`kg-ingest --repo ...` without a subcommand is shorthand for `build`.
Run `kg-ingest <subcommand> -h` for that subcommand's flags.
"""
from __future__ import annotations

import sys

_SUBCOMMANDS = ("build", "embed", "snapshot", "materialize")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    cmd, rest = (argv[0], argv[1:]) if argv[0] in _SUBCOMMANDS else ("build", argv)
    # embed/snapshot take no flags — answer -h here instead of running the build.
    if cmd in ("embed", "snapshot") and rest:
        print(f"kg-ingest {cmd} takes no arguments (see `kg-ingest -h`)")
        return 0 if rest[0] in ("-h", "--help") else 2
    if cmd == "build":
        from . import cli
        return cli.main(rest) or 0
    if cmd == "embed":
        from . import embed
        embed.main()
        return 0
    if cmd == "snapshot":
        from . import snapshot
        snapshot.main()
        return 0
    from . import materialize
    return materialize.main(rest) or 0


if __name__ == "__main__":
    sys.exit(main())
