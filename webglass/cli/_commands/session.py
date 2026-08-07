"""``webglass session`` — create/list/show/close/clean browser sessions.

Sessions are backed by
:class:`webglass.adapters.session_store.FileSessionStore` (wired in
:mod:`webglass.cli._factory`): ``session create`` in one ``webglass`` process
is visible to ``session show``/``page open --session-id ...``/``session
close`` in a *separate* later process, because the record — including the
browser's connect endpoint — lives in a ``0600`` file under the per-user
state directory. With a browser backend configured, ``create`` launches a
detached Chromium and ``close``/``clean`` terminate it.

Every handler here only builds a :class:`~webglass.operations.WebOperation`
and renders the result — see CLAUDE.md's CLI skeleton section, "no operation
logic in handlers" (spec honesty h2).
"""

from __future__ import annotations

import argparse
from typing import Any

from webglass.cli import _factory
from webglass.cli._commands.overview import emit_overview
from webglass.effects import OperationKind

_OVERVIEW_SECTIONS = [
    {
        "title": "Verbs",
        "items": [
            "session create — create a new browser session record.",
            "session list — this caller's own sessions.",
            "session show <session-id> — one session's public record.",
            "session close <session-id> — close a session (touches no evidence/exploration).",
            "session clean — reap this store's expired sessions.",
            "session overview — this description.",
        ],
    },
    {
        "title": "Persistence",
        "items": [
            "Sessions persist on disk under $WEBGLASS_STATE_DIR, else "
            "$XDG_STATE_HOME/webglass, else ~/.local/state/webglass — one 0600 record "
            "per session in a 0700 directory, so a session created by one CLI process "
            "is usable by the next.",
            "The connect endpoint stored in that record is secret-equivalent: it never "
            "appears in JSON output, text output, logs, or evidence.",
            "Concurrent use is serialized by a lease: a second holder gets a structured "
            "refusal rather than sharing one live browser, and a crashed holder's lease "
            "frees itself at expiry.",
            "session clean reaps expired sessions, terminates their browser processes, "
            "and removes their profile directories.",
        ],
    },
    {
        "title": "Browser status",
        "items": [
            "session create launches a real detached browser only when a browser backend "
            "is configured (WEBGLASS_BROWSER_BACKEND=playwright); otherwise it records a "
            "session without one. Making the browser the default is build plan t13, which "
            "owns the page/action verbs.",
        ],
    },
]


def cmd_session_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "webglass session", _OVERVIEW_SECTIONS, json_mode=bool(getattr(args, "json", False))
    )
    return 0


def _no_verb(args: argparse.Namespace) -> int:
    # `webglass session` with no sub-verb prints the noun's overview.
    return cmd_session_overview(args)


def _run(
    kind: OperationKind, args: argparse.Namespace, *, normalized_args: dict[str, Any] | None = None
) -> int:
    service = _factory.build_service()
    context = _factory.build_context()
    operation = _factory.build_operation(service, context, kind, normalized_args=normalized_args)
    result = service.execute(operation, context)
    return _factory.render_operation_result(result, json_mode=bool(getattr(args, "json", False)))


def cmd_session_create(args: argparse.Namespace) -> int:
    normalized: dict[str, Any] = {}
    if args.ttl_seconds is not None:
        normalized["ttl_seconds"] = args.ttl_seconds
    if args.session_id is not None:
        normalized["session_id"] = args.session_id
    return _run(OperationKind.SESSION_CREATE, args, normalized_args=normalized)


def cmd_session_list(args: argparse.Namespace) -> int:
    return _run(OperationKind.SESSION_LIST, args)


def cmd_session_show(args: argparse.Namespace) -> int:
    return _run(OperationKind.SESSION_SHOW, args, normalized_args={"session_id": args.session_id})


def cmd_session_close(args: argparse.Namespace) -> int:
    return _run(OperationKind.SESSION_CLOSE, args, normalized_args={"session_id": args.session_id})


def cmd_session_clean(args: argparse.Namespace) -> int:
    return _run(OperationKind.SESSION_CLEAN, args)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("session", help="Create/list/show/close/clean browser sessions.")
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=_no_verb, json=False)
    # `p` is a _CliArgumentParser; propagate it so every `session <verb>` parse
    # error routes through the structured error contract (see
    # webglass/cli/_commands/cli.py).
    noun_sub = p.add_subparsers(dest="session_command", parser_class=type(p))

    ov = noun_sub.add_parser("overview", help="Describe the session noun's verbs.")
    ov.add_argument("--json", action="store_true", help="Emit structured JSON.")
    ov.set_defaults(func=cmd_session_overview)

    cr = noun_sub.add_parser("create", help="Create a new browser session record.")
    cr.add_argument("--ttl-seconds", type=float, default=None, help="Session lifetime in seconds.")
    cr.add_argument("--session-id", default=None, help="Use this id instead of a minted one.")
    cr.add_argument("--json", action="store_true", help="Emit structured JSON.")
    cr.set_defaults(func=cmd_session_create)

    ls = noun_sub.add_parser("list", help="List this caller's own sessions.")
    ls.add_argument("--json", action="store_true", help="Emit structured JSON.")
    ls.set_defaults(func=cmd_session_list)

    sh = noun_sub.add_parser("show", help="Show one session's public record.")
    sh.add_argument("session_id", help="The session id to show.")
    sh.add_argument("--json", action="store_true", help="Emit structured JSON.")
    sh.set_defaults(func=cmd_session_show)

    cl = noun_sub.add_parser("close", help="Close a session.")
    cl.add_argument("session_id", help="The session id to close.")
    cl.add_argument("--json", action="store_true", help="Emit structured JSON.")
    cl.set_defaults(func=cmd_session_close)

    cn = noun_sub.add_parser("clean", help="Reap this store's expired sessions.")
    cn.add_argument("--json", action="store_true", help="Emit structured JSON.")
    cn.set_defaults(func=cmd_session_clean)
