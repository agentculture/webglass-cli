"""``webglass action`` — follow a link or press keys on an open page/session.

Noun group over the ``action.*`` operation kinds. Mirrors ``page.py``'s
shape: every handler here only builds a
:class:`~webglass.operations.WebOperation` and renders the result — see
CLAUDE.md's CLI skeleton section, "no operation logic in handlers" (spec
honesty h2).

``action.press`` classifies as ``EffectClass.REMOTE_ACTION`` by default (see
:data:`webglass.effects.EFFECT_CLASS_BY_KIND`) — a key press cannot be proven
navigational on the open web, since ``Enter`` submits forms. So it previews
and dispatches nothing, and an explicit ``--apply`` is denied (the
prepare -> commit -> verify protocol lands at M5).

The one scoped exception is the spec's 2026-08-07 decision: **under a
declared test profile** — a ``--policy-profile`` whose ``declared_targets``
name the app under test — ``press`` classifies as ``observe`` and executes,
all keys included. The authorization is that profile, expressed in data. Both
halves of that live in :func:`webglass.cli._factory.declared_target_effect_class`
and in :class:`~webglass.service.WebGlassService`'s own effect-class gate; no
handler here special-cases anything.

``action follow`` navigates from a *link reference* on a snapshot this
process retained, so it pairs with ``page open`` in the same invocation (or a
``--page-ref`` from one). Cross-invocation snapshot retention is M3.
"""

from __future__ import annotations

import argparse
from typing import Any

from webglass.cli import _factory
from webglass.cli._commands.overview import emit_overview
from webglass.effects import OperationKind
from webglass.operations import ApplyState, OperationTarget

_OVERVIEW_SECTIONS = [
    {
        "title": "Verbs",
        "items": [
            "action follow <page-ref> <link-ref> — follow a link reference from a retained "
            "snapshot (never a raw selector or URL).",
            "action press <keys...> — dispatch a key sequence to a session's focused "
            "page, with an optional --delay-ms between keys.",
            "action overview — this description.",
        ],
    },
    {
        "title": "Effect classes",
        "items": [
            "press classifies upward to remote-action by default and previews without "
            "dispatching anything: on the open web, Enter submits forms.",
            "Under a --policy-profile whose declared_targets name your app under test, "
            "press classifies as observe and executes — the profile is the "
            "authorization, and it is data, not a flag that widens policy.",
            "--apply is denied in both cases: the prepare -> commit -> verify protocol "
            "for real remote actions lands at M5.",
        ],
    },
    {
        "title": "Backend status",
        "items": [
            "A real Chromium is the default backend (build plan t13); set "
            "WEBGLASS_BROWSER_BACKEND=none for the explicit browser-less posture, in "
            "which these verbs report a structured 'backend_unavailable' result.",
            "'action follow' resolves a link reference against a snapshot this process "
            "retained, so pair it with a 'page open' in the same invocation.",
        ],
    },
]


def cmd_action_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "webglass action", _OVERVIEW_SECTIONS, json_mode=bool(getattr(args, "json", False))
    )
    return 0


def _no_verb(args: argparse.Namespace) -> int:
    # `webglass action` with no sub-verb prints the noun's overview.
    return cmd_action_overview(args)


def cmd_action_follow(args: argparse.Namespace) -> int:
    service, context = _factory.build_invocation(args)
    operation = _factory.build_operation(
        service,
        context,
        OperationKind.ACTION_FOLLOW,
        target=OperationTarget(page_ref=args.page_ref, element_ref=args.link_ref),
        session_id=args.session_id,
    )
    result = service.execute(operation, context)
    return _factory.render_operation_result(result, json_mode=bool(getattr(args, "json", False)))


def cmd_action_press(args: argparse.Namespace) -> int:
    service, context = _factory.build_invocation(args)
    apply_state = ApplyState.APPLY if args.apply else ApplyState.PREVIEW
    operation = _factory.build_operation(
        service,
        context,
        OperationKind.ACTION_PRESS,
        normalized_args={"keys": list(args.keys), "delay_ms": args.delay_ms},
        target=OperationTarget(page_ref=args.page_ref),
        session_id=args.session_id,
        apply_state=apply_state,
    )
    result = service.execute(operation, context)
    return _factory.render_operation_result(result, json_mode=bool(getattr(args, "json", False)))


def _finish(parser: argparse.ArgumentParser, handler: Any) -> None:
    """The flags and defaults every web verb shares (mirrors ``page.py``)."""
    _factory.add_policy_profile_argument(parser)
    parser.add_argument("--json", action="store_true", help="Emit structured JSON.")
    parser.set_defaults(func=handler)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("action", help="Follow a link or press keys on an open page/session.")
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=_no_verb, json=False)
    # `p` is a _CliArgumentParser; propagate it so every `action <verb>` parse
    # error routes through the structured error contract (see
    # webglass/cli/_commands/cli.py).
    noun_sub = p.add_subparsers(dest="action_command", parser_class=type(p))

    ov = noun_sub.add_parser("overview", help="Describe the action noun's verbs.")
    ov.add_argument("--json", action="store_true", help="Emit structured JSON.")
    ov.set_defaults(func=cmd_action_overview)

    fl = noun_sub.add_parser("follow", help="Follow a link reference from a retained snapshot.")
    fl.add_argument("page_ref", help="A snapshot id from a previous page open.")
    fl.add_argument("link_ref", help="A 'link:<index>' reference from that snapshot.")
    fl.add_argument("--session-id", default=None, help="Navigate in this session.")
    _finish(fl, cmd_action_follow)

    pr = noun_sub.add_parser("press", help="Dispatch a key sequence.")
    pr.add_argument(
        "keys",
        nargs="+",
        help=(
            "Key names to press, in order (e.g. 'a' 'ArrowRight' 'Enter'). Names are "
            "the browser's own KeyboardEvent.key values."
        ),
    )
    pr.add_argument("--session-id", default=None, help="The session to dispatch keys on.")
    pr.add_argument("--page-ref", default=None, help="Fall back to this snapshot's session.")
    pr.add_argument(
        "--delay-ms",
        type=float,
        default=0,
        help=(
            "Wait this long between consecutive keys. Use it when the page needs time "
            "to react to each key (an animation frame, a debounce)."
        ),
    )
    pr.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Request apply instead of preview. Always denied — the "
            "prepare -> commit -> verify protocol lands at M5. To actually dispatch "
            "keys, run under a --policy-profile declaring your app under test."
        ),
    )
    _finish(pr, cmd_action_press)
