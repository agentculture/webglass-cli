"""``webglass action`` — follow a link or press keys on an open page/session.

Noun group over the ``action.*`` operation kinds. Mirrors ``page.py``'s
shape: every handler here only builds a
:class:`~webglass.operations.WebOperation` and renders the result — see
CLAUDE.md's CLI skeleton section, "no operation logic in handlers" (spec
honesty h2).

``action.press`` classifies as ``EffectClass.REMOTE_ACTION`` by default (see
:data:`webglass.effects.EFFECT_CLASS_BY_KIND`) — a key press cannot be proven
navigational outside a declared test profile (build plan t13 wires that
override in), so at M1 it always previews unless ``--apply`` is passed, and
an explicit ``--apply`` is always denied (the prepare -> commit -> verify
protocol lands at M5). This is :class:`~webglass.service.WebGlassService`'s
own effect-class gate — the CLI does not special-case it.

M1 has no browser backend wired in by default (see ``_factory``'s module
docstring) and no snapshot to follow a link from without one, so ``action
follow`` always reports a structured failure (exit 1) — ``unknown_snapshot``
for a ``page-ref`` that names nothing retained, or ``backend_unavailable``
once a real one does — until build plan t11/t13 land the Playwright adapter.
"""

from __future__ import annotations

import argparse

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
            "action press <keys...> — dispatch a key sequence; classifies as remote-action "
            "and previews unless a declared test profile overrides it.",
            "action overview — this description.",
        ],
    },
    {
        "title": "Backend status",
        "items": [
            "M1: no browser backend is wired in yet, so 'action follow' always reports a "
            "structured failure (exit 1) — 'unknown_snapshot' for a page-ref that names "
            "nothing retained, or 'backend_unavailable' once a real one does — until the "
            "Playwright adapter lands (build plan t11/t13).",
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
    service = _factory.build_service()
    context = _factory.build_context()
    operation = _factory.build_operation(
        service,
        context,
        OperationKind.ACTION_FOLLOW,
        target=OperationTarget(page_ref=args.page_ref, element_ref=args.link_ref),
    )
    result = service.execute(operation, context)
    return _factory.render_operation_result(result, json_mode=bool(getattr(args, "json", False)))


def cmd_action_press(args: argparse.Namespace) -> int:
    service = _factory.build_service()
    context = _factory.build_context()
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
    fl.add_argument("--json", action="store_true", help="Emit structured JSON.")
    fl.set_defaults(func=cmd_action_follow)

    pr = noun_sub.add_parser("press", help="Dispatch a key sequence.")
    pr.add_argument("keys", nargs="+", help="Key names to press in order (e.g. 'a' 'Enter').")
    pr.add_argument("--session-id", default=None, help="The session to dispatch keys on.")
    pr.add_argument("--page-ref", default=None, help="Fall back to this snapshot's session.")
    pr.add_argument("--delay-ms", type=float, default=0, help="Delay between key presses.")
    pr.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Request apply instead of preview (always denied at M1 — "
            "prepare/commit/verify lands at M5)."
        ),
    )
    pr.add_argument("--json", action="store_true", help="Emit structured JSON.")
    pr.set_defaults(func=cmd_action_press)
