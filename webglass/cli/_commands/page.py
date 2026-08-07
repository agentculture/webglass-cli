"""``webglass page`` — open/read/inspect/extract/links/screenshot one page.

Noun group over :mod:`webglass.effects`'s ``page.*`` operation kinds. Every
sub-verb handler here does exactly one thing: translate argv into a
:class:`~webglass.operations.WebOperation` + ``WebContext`` via the
:mod:`_factory` seam, call
:meth:`~webglass.service.WebGlassService.execute`, and render the result.
Dispatch, policy, extraction, and lens projection all stay in
:mod:`webglass.service` — see CLAUDE.md's CLI skeleton section, "no operation
logic in handlers" (spec honesty h2).

Bare ``webglass page`` (no sub-verb) prints this noun's ``overview``,
mirroring ``webglass cli``'s own bare-noun behavior
(:mod:`webglass.cli._commands.cli`).

M1 has no browser backend wired in by default (see ``_factory``'s module
docstring), so every verb here reports a structured ``backend_unavailable``
result (exit 1) until build plan t11/t13 land the Playwright adapter.
"""

from __future__ import annotations

import argparse
from typing import Any

from webglass.cli import _factory
from webglass.cli._commands.overview import emit_overview
from webglass.effects import OperationKind
from webglass.operations import OperationTarget
from webglass.service import INSPECT_LENSES

_OVERVIEW_SECTIONS = [
    {
        "title": "Verbs",
        "items": [
            "page open <url> — navigate a session to a URL; returns a PageSnapshot summary.",
            "page read — ordered readable blocks from a retained snapshot, with a cursor.",
            "page inspect — a lens (outline/controls/metadata/console/structure) over a "
            "snapshot.",
            "page extract <query> — query-focused block selection, deterministic, never a "
            "summary.",
            "page links — every link on a retained snapshot.",
            "page screenshot — capture the current page as a stored PNG artifact.",
            "page overview — this description.",
        ],
    },
    {
        "title": "Backend status",
        "items": [
            "M1: no browser backend is wired in yet, so every verb above returns a "
            "structured 'backend_unavailable' result (exit 1) until the Playwright "
            "adapter lands (build plan t11/t13).",
        ],
    },
]


def cmd_page_overview(args: argparse.Namespace) -> int:
    emit_overview("webglass page", _OVERVIEW_SECTIONS, json_mode=bool(getattr(args, "json", False)))
    return 0


def _no_verb(args: argparse.Namespace) -> int:
    # `webglass page` with no sub-verb prints the noun's overview.
    return cmd_page_overview(args)


def _run(
    kind: OperationKind,
    args: argparse.Namespace,
    *,
    normalized_args: dict[str, Any] | None = None,
    target: OperationTarget | None = None,
    session_id: str | None = None,
) -> int:
    service = _factory.build_service()
    context = _factory.build_context()
    operation = _factory.build_operation(
        service,
        context,
        kind,
        normalized_args=normalized_args,
        target=target,
        session_id=session_id,
    )
    result = service.execute(operation, context)
    return _factory.render_operation_result(result, json_mode=bool(getattr(args, "json", False)))


def cmd_page_open(args: argparse.Namespace) -> int:
    return _run(
        OperationKind.PAGE_OPEN,
        args,
        target=OperationTarget(url=args.url),
        session_id=args.session_id,
    )


def cmd_page_read(args: argparse.Namespace) -> int:
    normalized: dict[str, Any] = {}
    if args.cursor is not None:
        normalized["cursor"] = args.cursor
    return _run(
        OperationKind.PAGE_READ,
        args,
        normalized_args=normalized,
        target=OperationTarget(page_ref=args.page_ref, url=args.url),
    )


def cmd_page_inspect(args: argparse.Namespace) -> int:
    return _run(
        OperationKind.PAGE_INSPECT,
        args,
        normalized_args={"lens": args.lens},
        target=OperationTarget(page_ref=args.page_ref, url=args.url),
    )


def cmd_page_extract(args: argparse.Namespace) -> int:
    return _run(
        OperationKind.PAGE_EXTRACT,
        args,
        normalized_args={"query": args.query},
        target=OperationTarget(page_ref=args.page_ref, url=args.url),
    )


def cmd_page_links(args: argparse.Namespace) -> int:
    return _run(
        OperationKind.PAGE_LINKS,
        args,
        target=OperationTarget(page_ref=args.page_ref, url=args.url),
    )


def cmd_page_screenshot(args: argparse.Namespace) -> int:
    return _run(
        OperationKind.PAGE_SCREENSHOT,
        args,
        target=OperationTarget(page_ref=args.page_ref),
        session_id=args.session_id,
    )


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("page", help="Open/read/inspect/extract/links/screenshot one page.")
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=_no_verb, json=False)
    # `p` is a _CliArgumentParser (the top-level subparsers were built with that
    # parser_class); propagate it so every `page <verb>` parse error routes
    # through the structured error contract (see webglass/cli/_commands/cli.py).
    noun_sub = p.add_subparsers(dest="page_command", parser_class=type(p))

    ov = noun_sub.add_parser("overview", help="Describe the page noun's verbs.")
    ov.add_argument("--json", action="store_true", help="Emit structured JSON.")
    ov.set_defaults(func=cmd_page_overview)

    op = noun_sub.add_parser("open", help="Navigate a session to a URL.")
    op.add_argument("url", help="The URL to open.")
    op.add_argument("--session-id", default=None, help="Reuse an existing session id.")
    op.add_argument("--json", action="store_true", help="Emit structured JSON.")
    op.set_defaults(func=cmd_page_open)

    rd = noun_sub.add_parser("read", help="Ordered readable blocks from a retained snapshot.")
    rd.add_argument("--page-ref", default=None, help="A snapshot id from a previous page open.")
    rd.add_argument("--url", default=None, help="Open this URL first, then read it.")
    rd.add_argument(
        "--cursor",
        default=None,
        help=(
            "Resume from this block reference (e.g. 'block:5'), taken from a previous "
            "'page read' result's content.derived.read.cursor — not a raw integer offset."
        ),
    )
    rd.add_argument("--json", action="store_true", help="Emit structured JSON.")
    rd.set_defaults(func=cmd_page_read)

    ins = noun_sub.add_parser("inspect", help="Project one lens over a retained snapshot.")
    ins.add_argument("--page-ref", default=None, help="A snapshot id from a previous page open.")
    ins.add_argument("--url", default=None, help="Open this URL first, then inspect it.")
    ins.add_argument(
        "--lens",
        default="outline",
        choices=sorted(INSPECT_LENSES),
        help="Which lens to project (default outline).",
    )
    ins.add_argument("--json", action="store_true", help="Emit structured JSON.")
    ins.set_defaults(func=cmd_page_inspect)

    ex = noun_sub.add_parser(
        "extract", help="Query-focused block selection over a retained snapshot."
    )
    ex.add_argument("query", help="Query terms to rank blocks by.")
    ex.add_argument("--page-ref", default=None, help="A snapshot id from a previous page open.")
    ex.add_argument("--url", default=None, help="Open this URL first, then extract from it.")
    ex.add_argument("--json", action="store_true", help="Emit structured JSON.")
    ex.set_defaults(func=cmd_page_extract)

    lk = noun_sub.add_parser("links", help="Every link on a retained snapshot.")
    lk.add_argument("--page-ref", default=None, help="A snapshot id from a previous page open.")
    lk.add_argument("--url", default=None, help="Open this URL first, then list its links.")
    lk.add_argument("--json", action="store_true", help="Emit structured JSON.")
    lk.set_defaults(func=cmd_page_links)

    sc = noun_sub.add_parser(
        "screenshot", help="Capture the current page as a stored PNG artifact."
    )
    sc.add_argument(
        "--page-ref", default=None, help="A retained snapshot naming the session to capture."
    )
    sc.add_argument("--session-id", default=None, help="Capture this session directly.")
    sc.add_argument("--json", action="store_true", help="Emit structured JSON.")
    sc.set_defaults(func=cmd_page_screenshot)
