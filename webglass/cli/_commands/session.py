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
import re
from typing import Any

from webglass.cli import _factory
from webglass.cli._commands.overview import emit_overview
from webglass.cli._errors import EXIT_USER_ERROR, CliError
from webglass.effects import OperationKind
from webglass.sessions import SessionStatus

#: Accepts a bare number of seconds ("90"), or a number suffixed with one of
#: s(econds)/m(inutes)/h(ours)/d(ays) ("30s", "10m", "2h", "7d"). Anchored on
#: both ends so trailing garbage ("10mX") is rejected rather than truncated.
_DURATION_RE = re.compile(r"^(?P<amount>\d+(?:\.\d+)?)(?P<unit>[smhd]?)$")
_DURATION_UNIT_SECONDS = {"": 1.0, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}

#: Shared ``--json`` help text; every verb in this noun takes the flag.
_JSON_HELP = "Emit structured JSON."


def _parse_older_than(raw: str) -> float:
    """Parse ``--older-than`` into seconds, or raise a structured ``CliError``.

    A malformed duration is a user-input error surfaced through the CLI's
    own error contract (exit 1, ``{code, message, remediation}``) — never a
    silently-substituted default. A typo'd duration must fail loudly rather
    than quietly reap nothing (or, worse, fall back to unfiltered) (issue
    #14 build plan t9, spec honesty h5).
    """
    match = _DURATION_RE.match(raw.strip()) if raw else None
    if match is None:
        raise CliError(
            EXIT_USER_ERROR,
            f"invalid --older-than duration: {raw!r}",
            "use a number of seconds, optionally suffixed with s/m/h/d, "
            "e.g. '90', '30s', '10m', '2h', '7d'",
        )
    amount = float(match.group("amount"))
    return amount * _DURATION_UNIT_SECONDS[match.group("unit")]


_OVERVIEW_SECTIONS = [
    {
        "title": "Verbs",
        "items": [
            "session create — create a new browser session record.",
            "session list — this caller's own sessions.",
            "session show <session-id> — one session's public record.",
            "session close <session-id> — close a session (touches no evidence/exploration).",
            "session clean [--older-than DURATION] [--status STATUS] [--site HOST] — "
            "reap this store's expired sessions, optionally filtered.",
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
            "and removes their profile directories. --older-than/--status/--site narrow "
            "which records are eligible and compose as AND; an unmatched filter reaps "
            "nothing rather than falling back to reaping everything.",
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
    kind: OperationKind,
    args: argparse.Namespace,
    *,
    normalized_args: dict[str, Any] | None = None,
    sweep: bool = False,
) -> int:
    """Build one operation, execute it, render it. No operation logic here.

    ``sweep`` is the opportunistic session-store sweep (build plan t10), and
    only ``create`` passes it. It runs before the operation and entirely
    outside its result: whatever it reaped — or failed to — cannot change
    what the caller is told about the session they asked for. Every other
    verb in this noun *reads* the store (or, for ``clean``, sweeps it because
    that is what was asked), and a read verb that quietly rewrote the store
    it was asked to describe would be deleting the evidence out from under
    exactly the investigation issue #14 was.
    """
    service = _factory.build_service()
    context = _factory.build_context()
    if sweep:
        _factory.sweep_session_store(service)
    operation = _factory.build_operation(service, context, kind, normalized_args=normalized_args)
    result = service.execute(operation, context)
    return _factory.render_operation_result(result, json_mode=bool(getattr(args, "json", False)))


def cmd_session_create(args: argparse.Namespace) -> int:
    normalized: dict[str, Any] = {}
    if args.ttl_seconds is not None:
        normalized["ttl_seconds"] = args.ttl_seconds
    if args.session_id is not None:
        normalized["session_id"] = args.session_id
    return _run(OperationKind.SESSION_CREATE, args, normalized_args=normalized, sweep=True)


def cmd_session_list(args: argparse.Namespace) -> int:
    return _run(OperationKind.SESSION_LIST, args)


def cmd_session_show(args: argparse.Namespace) -> int:
    return _run(OperationKind.SESSION_SHOW, args, normalized_args={"session_id": args.session_id})


def cmd_session_close(args: argparse.Namespace) -> int:
    return _run(OperationKind.SESSION_CLOSE, args, normalized_args={"session_id": args.session_id})


def cmd_session_clean(args: argparse.Namespace) -> int:
    normalized: dict[str, Any] = {}
    if args.older_than is not None:
        normalized["older_than_seconds"] = _parse_older_than(args.older_than)
    if args.status is not None:
        normalized["status"] = args.status
    if args.site is not None:
        normalized["site"] = args.site
    return _run(OperationKind.SESSION_CLEAN, args, normalized_args=normalized)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("session", help="Create/list/show/close/clean browser sessions.")
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.set_defaults(func=_no_verb, json=False)
    # `p` is a _CliArgumentParser; propagate it so every `session <verb>` parse
    # error routes through the structured error contract (see
    # webglass/cli/_commands/cli.py).
    noun_sub = p.add_subparsers(dest="session_command", parser_class=type(p))

    ov = noun_sub.add_parser("overview", help="Describe the session noun's verbs.")
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_session_overview)

    cr = noun_sub.add_parser("create", help="Create a new browser session record.")
    cr.add_argument("--ttl-seconds", type=float, default=None, help="Session lifetime in seconds.")
    cr.add_argument("--session-id", default=None, help="Use this id instead of a minted one.")
    cr.add_argument("--json", action="store_true", help=_JSON_HELP)
    cr.set_defaults(func=cmd_session_create)

    ls = noun_sub.add_parser("list", help="List this caller's own sessions.")
    ls.add_argument("--json", action="store_true", help=_JSON_HELP)
    ls.set_defaults(func=cmd_session_list)

    sh = noun_sub.add_parser("show", help="Show one session's public record.")
    sh.add_argument("session_id", help="The session id to show.")
    sh.add_argument("--json", action="store_true", help=_JSON_HELP)
    sh.set_defaults(func=cmd_session_show)

    cl = noun_sub.add_parser("close", help="Close a session.")
    cl.add_argument("session_id", help="The session id to close.")
    cl.add_argument("--json", action="store_true", help=_JSON_HELP)
    cl.set_defaults(func=cmd_session_close)

    cn = noun_sub.add_parser("clean", help="Reap this store's expired sessions.")
    cn.add_argument(
        "--older-than",
        default=None,
        metavar="DURATION",
        help=(
            "Only reap records at least this old. A number of seconds, optionally "
            "suffixed with s/m/h/d, e.g. '90', '30s', '10m', '2h', '7d'. Composes as "
            "AND with --status/--site."
        ),
    )
    cn.add_argument(
        "--status",
        choices=[member.value for member in SessionStatus],
        default=None,
        help="Only reap records currently at this status. Composes as AND with the others.",
    )
    cn.add_argument(
        "--site",
        default=None,
        metavar="HOST",
        help=(
            "Only reap records that navigated to this host. A record with no tracked "
            "navigation history (pre-upgrade, or never navigated) is never matched. "
            "Composes as AND with the others."
        ),
    )
    cn.add_argument("--json", action="store_true", help=_JSON_HELP)
    cn.set_defaults(func=cmd_session_clean)
