"""``webglass search`` — run a web search operation (build plan task t10).

A flat verb, not a noun group: search has exactly one action, so there is no
``search overview`` sub-verb to register (contrast with ``page``/``action``/
``session``, each of which groups several action-verbs and therefore must
expose one per the agent-first rubric — CLAUDE.md's "agent-first rubric gate"
section).

The handler's only job is to translate argv into a
:class:`~webglass.operations.WebOperation`, hand it to
:meth:`~webglass.service.WebGlassService.execute` via the :mod:`_factory`
seam, and render the structured
:class:`~webglass.results.WebOperationResult` — it never talks to a search
backend itself (CLAUDE.md's CLI skeleton section, "no operation logic in
handlers").

M1 has no search backend wired in by default (see ``_factory``'s module
docstring), so this verb reports a structured ``backend_unavailable`` result
(exit 1) until build plan t15 lands a real provider.
"""

from __future__ import annotations

import argparse

from webglass.cli import _factory
from webglass.effects import OperationKind


def cmd_search(args: argparse.Namespace) -> int:
    service = _factory.build_service()
    context = _factory.build_context()
    operation = _factory.build_operation(
        service,
        context,
        OperationKind.SEARCH,
        normalized_args={"query": args.query, "limit": args.limit},
    )
    result = service.execute(operation, context)
    return _factory.render_operation_result(result, json_mode=bool(getattr(args, "json", False)))


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "search",
        help="Search the web (needs a search backend; see 'webglass explain search').",
    )
    p.add_argument("query", help="The search query.")
    p.add_argument("--limit", type=int, default=10, help="Maximum results to return (default 10).")
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_search)
