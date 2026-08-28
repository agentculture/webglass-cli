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

Since build plan t13 these verbs drive a real Chromium by default (see
``_factory``'s module docstring). Three ways to name the page to act on, and
they are checked in this order:

``--page-ref``
    Project a snapshot this *process* already retained — no network at all.
``--url``
    Open the URL first, then apply the verb, in one operation.
``--session-id``
    Re-read that session's **live** page without navigating it. This is how a
    later one-shot invocation observes what an earlier one's ``page open`` or
    ``action press`` did: navigating again would destroy exactly the in-memory
    page state being asked about.

With none of them, a verb that needs a page reports a structured
``invalid_argument``; a verb that navigates (``open``) runs in a throwaway
session that is created and closed within this invocation — unless
``$WEBGLASS_SESSION_OWNER`` declares this invocation part of a *flow*, in
which case it may continue a session an earlier step of the same flow opened
(``--fresh-session`` opts out per call; see :func:`_factory.ephemeral_session`).

This is the same throwaway/reuse contract described in ``page overview``'s
"Naming the page" section, ``webglass explain page open``, and ``webglass
session overview``'s Persistence section — see
:data:`webglass.cli._factory.DEFAULT_EPHEMERAL_CLAIM`,
:data:`~webglass.cli._factory.FLOW_REUSE_CLAIM`, and
:data:`~webglass.cli._factory.FRESH_SESSION_OPT_OUT_CLAIM`, which those four
surfaces all render verbatim (build plan t15, issue #14).
"""

from __future__ import annotations

import argparse
from typing import Any

from webglass.cli import _factory
from webglass.cli._commands.overview import emit_overview
from webglass.effects import OperationKind
from webglass.operations import OperationTarget
from webglass.service import INSPECT_LENSES

#: Shared ``--json`` help text; every verb in this noun takes the flag.
_JSON_HELP = "Emit structured JSON."

_OVERVIEW_SECTIONS = [
    {
        "title": "Verbs",
        "items": [
            "page open <url> — navigate a session to a URL; returns a PageSnapshot summary.",
            "page read — ordered readable blocks from a retained snapshot, with a cursor.",
            "page inspect — a lens (outline/controls/metadata/console/structure) over a "
            "snapshot; --lens console reports the page's console messages and uncaught "
            "errors, always as explicit lists (empty means 'observed, and there was "
            "nothing').",
            "page extract <query> — query-focused block selection, deterministic, never a "
            "summary; --selector returns exactly one element's own content instead.",
            "page links — every link on a retained snapshot.",
            "page screenshot — capture the current page as a stored PNG artifact; --out "
            "also writes it to a path you choose.",
            "page overview — this description.",
        ],
    },
    {
        "title": "Naming the page",
        "items": [
            "--page-ref <snapshot-id> projects a snapshot this process retained, with no "
            "network access.",
            "--url <url> opens the URL and applies the verb in one operation.",
            "--session-id <id> re-reads that session's live page without navigating it — "
            "the way to see what an earlier CLI invocation's press or open did.",
            "With none of them, 'page open' runs in a throwaway session "
            f"{_factory.DEFAULT_EPHEMERAL_CLAIM}, leaving no browser behind — unless "
            f"{_factory.FLOW_REUSE_CLAIM}, in which case it may continue a session an "
            "earlier step of the same flow opened instead of opening a new one "
            f"({_factory.FRESH_SESSION_OPT_OUT_CLAIM}).",
        ],
    },
    {
        "title": "Backend status",
        "items": [
            "A real Chromium is the default backend (build plan t13). Set "
            "WEBGLASS_BROWSER_BACKEND=none for the explicit browser-less posture, in "
            "which every verb above returns a structured 'backend_unavailable' result "
            "(exit 1).",
            "Loopback and private-network targets stay denied by default: reaching a "
            "local app under test requires naming its origin in a --policy-profile "
            "JSON file's declared_targets.",
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
) -> int:
    """Build one operation, execute it, render it. No operation logic here.

    Session provisioning is the one thing this wraps around the call: with no
    ``--session-id``, :func:`webglass.cli._factory.ephemeral_session` supplies
    a throwaway session for the duration of the operation and closes it
    afterwards (see that function on why a *stored* record is required).

    Because that throwaway *is* a stored session, its ephemerality has to ride
    along on the operation: nothing downstream can tell it apart from a
    caller-owned session by looking at the id, which is why every CLI
    navigation used to report ``ephemeral: false`` (issue #14).

    ``ephemeral_session`` also runs the opportunistic store sweep (build plan
    t10) before it ever yields, so ``session.swept`` is already known here.
    It rides into ``execute`` as the separate ``swept=`` parameter rather than
    onto the operation, so a caller can see what disappeared without that
    disclosure being able to change what this operation itself reports
    (build plan t11).
    """
    service, context = _factory.build_invocation(args)
    requested = getattr(args, "session_id", None)
    with _factory.ephemeral_session(
        service,
        requested,
        provision=_navigates(kind, target),
        reuse=not bool(getattr(args, "fresh_session", False)),
        hosts=_factory.target_hosts(target.url if target is not None else None),
    ) as session:
        operation = _factory.build_operation(
            service,
            context,
            kind,
            normalized_args=normalized_args,
            target=target,
            session_id=session.session_id,
            session_ephemeral=session.ephemeral,
            session_reused=session.reused,
        )
        result = service.execute(operation, context, swept=session.swept)
    return _factory.render_operation_result(result, json_mode=bool(getattr(args, "json", False)))


def _navigates(kind: OperationKind, target: OperationTarget | None) -> bool:
    """Whether this invocation will actually drive a browser to a URL.

    Only then is a throwaway session worth launching: a lens over a retained
    ``--page-ref`` touches no browser at all, and provisioning one for it would
    start (and stop) a Chromium for nothing.
    """
    if kind is OperationKind.PAGE_OPEN:
        return True
    return bool(target is not None and target.url)


def cmd_page_open(args: argparse.Namespace) -> int:
    return _run(OperationKind.PAGE_OPEN, args, target=OperationTarget(url=args.url))


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
    normalized: dict[str, Any] = {}
    if args.query is not None:
        normalized["query"] = args.query
    if args.selector is not None:
        normalized["selector"] = args.selector
    return _run(
        OperationKind.PAGE_EXTRACT,
        args,
        normalized_args=normalized,
        target=OperationTarget(page_ref=args.page_ref, url=args.url),
    )


def cmd_page_links(args: argparse.Namespace) -> int:
    return _run(
        OperationKind.PAGE_LINKS,
        args,
        target=OperationTarget(page_ref=args.page_ref, url=args.url),
    )


def cmd_page_screenshot(args: argparse.Namespace) -> int:
    normalized: dict[str, Any] = {}
    if args.out is not None:
        normalized["out"] = args.out
    return _run(
        OperationKind.PAGE_SCREENSHOT,
        args,
        normalized_args=normalized,
        target=OperationTarget(page_ref=args.page_ref, url=args.url),
    )


_SESSION_ID_HELP = (
    "Run in this session (from 'session create'). Without it, a page verb that "
    f"navigates runs in a throwaway session {_factory.DEFAULT_EPHEMERAL_CLAIM} — unless "
    f"{_factory.FLOW_REUSE_CLAIM}, in which case it may continue that flow's own session "
    f"instead ({_factory.FRESH_SESSION_OPT_OUT_CLAIM})."
)


def _add_page_selection(parser: argparse.ArgumentParser, verb: str) -> None:
    """The three ways every lens verb names the page it acts on."""
    parser.add_argument("--page-ref", default=None, help="A snapshot id from a previous page open.")
    parser.add_argument("--url", default=None, help=f"Open this URL first, then {verb} it.")
    parser.add_argument(
        "--session-id",
        default=None,
        help=(
            f"Re-read this session's live page without navigating, then {verb} it — "
            "how a later invocation observes what an earlier one did."
        ),
    )


def _finish(parser: argparse.ArgumentParser, handler: Any) -> None:
    """The flags and defaults every web verb shares."""
    _factory.add_policy_profile_argument(parser)
    parser.add_argument(
        "--fresh-session",
        action="store_true",
        help=(
            "Never continue an earlier session: run in a brand-new anonymous one, "
            "created and closed inside this invocation. Only meaningful when "
            f"${_factory.SESSION_OWNER_ENV} declares this call part of a flow; "
            "without that variable every call is already anonymous."
        ),
    )
    parser.add_argument("--json", action="store_true", help=_JSON_HELP)
    parser.set_defaults(func=handler)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("page", help="Open/read/inspect/extract/links/screenshot one page.")
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.set_defaults(func=_no_verb, json=False)
    # `p` is a _CliArgumentParser (the top-level subparsers were built with that
    # parser_class); propagate it so every `page <verb>` parse error routes
    # through the structured error contract (see webglass/cli/_commands/cli.py).
    noun_sub = p.add_subparsers(dest="page_command", parser_class=type(p))

    ov = noun_sub.add_parser("overview", help="Describe the page noun's verbs.")
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_page_overview)

    op = noun_sub.add_parser("open", help="Navigate a session to a URL.")
    op.add_argument("url", help="The URL to open.")
    op.add_argument("--session-id", default=None, help=_SESSION_ID_HELP)
    _finish(op, cmd_page_open)

    rd = noun_sub.add_parser("read", help="Ordered readable blocks from a retained snapshot.")
    _add_page_selection(rd, "read")
    rd.add_argument(
        "--cursor",
        default=None,
        help=(
            "Resume from this block reference (e.g. 'block:5'), taken from a previous "
            "'page read' result's content.derived.read.cursor — not a raw integer offset."
        ),
    )
    _finish(rd, cmd_page_read)

    ins = noun_sub.add_parser("inspect", help="Project one lens over a retained snapshot.")
    _add_page_selection(ins, "inspect")
    ins.add_argument(
        "--lens",
        default="outline",
        choices=sorted(INSPECT_LENSES),
        help=(
            "Which lens to project (default outline). 'console' reports the page's "
            "console messages and uncaught page errors — always as explicit lists, so "
            "an empty pair means the page produced nothing, never that nobody looked."
        ),
    )
    _finish(ins, cmd_page_inspect)

    ex = noun_sub.add_parser(
        "extract", help="Query-focused or selector-scoped extraction over a snapshot."
    )
    ex.add_argument(
        "query",
        nargs="?",
        default=None,
        help="Query terms to rank readable blocks by. Omit it when using --selector.",
    )
    _add_page_selection(ex, "extract from")
    ex.add_argument(
        "--selector",
        default=None,
        help=(
            "Return exactly the matching elements' own content instead of ranked "
            "blocks — e.g. '#agent-state' for a machine-readable state node. Supported "
            "forms: tag, #id, .class, [attr], [attr=value], and combinations."
        ),
    )
    _finish(ex, cmd_page_extract)

    lk = noun_sub.add_parser("links", help="Every link on a retained snapshot.")
    _add_page_selection(lk, "list links from")
    _finish(lk, cmd_page_links)

    sc = noun_sub.add_parser(
        "screenshot", help="Capture the current page as a stored PNG artifact."
    )
    sc.add_argument(
        "--page-ref", default=None, help="A retained snapshot naming the session to capture."
    )
    sc.add_argument("--url", default=None, help="Open this URL first, then capture it.")
    sc.add_argument("--session-id", default=None, help="Capture this session's live page.")
    sc.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help=(
            "Also write the PNG here. These are WebGlass-rendered bytes from the "
            "browser's own screenshotter — never remote-origin response bytes, which "
            "stay quarantined."
        ),
    )
    _finish(sc, cmd_page_screenshot)
