"""Console entry point: `kg-query` (installed script) / `python -m kg_query`.

Subcommands wrap the existing modules, which all stay runnable directly via
`python -m kg_query.<module>`:

  kg-query search <term>|--neighbors <iri>|--provenance <iri>
                             terminal search over the KG (kg_query.search)
  kg-query serve             run the read-only MCP server — stdio by default,
                             streamable-HTTP when KG_HTTP is set (kg_query.server)

`kg-query "webhook"` without a subcommand is shorthand for `search`.
Run `kg-query search -h` for the search flags.
"""
from __future__ import annotations

import sys

_SUBCOMMANDS = ("search", "serve", "server")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    cmd, rest = (argv[0], argv[1:]) if argv[0] in _SUBCOMMANDS else ("search", argv)
    if cmd == "search":
        from . import search
        return search.main(rest) or 0
    # serve/server takes no arguments — config comes from the environment
    # (KG_BACKEND, KG_SERVER_NAME, KG_HTTP / KG_HTTP_HOST / KG_HTTP_PORT).
    if rest:
        print("kg-query serve takes no arguments; configure via env (see `kg-query -h`)")
        return 0 if rest[0] in ("-h", "--help") else 2
    from . import server
    server.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
