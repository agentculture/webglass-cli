"""``webglass-cli learn`` — the learnability affordance.

Prints a structured self-teaching prompt. Must satisfy the agent-first rubric:
>=200 chars and mention purpose, command map, exit codes, --json, and explain.

The purpose text describes WebGlass (the product this repo is building, see
issue #1) and flags that the web operation surface is not implemented yet, so an
agent reading ``learn`` is not misled into calling verbs that do not exist. Keep
it in sync with the ``explain`` root entry in :mod:`webglass.explain.catalog`.
"""

from __future__ import annotations

import argparse

from webglass import __version__
from webglass.cli._output import emit_result

_TEXT = """\
webglass-cli — WebGlass, the guarded web operations and evidence plane for AI agents.

Purpose
-------
Turn agent intent into normalized web operations: apply web-specific policy,
drive search/fetch/browser backends, return token-efficient page state, record
navigational provenance, and produce durable, inspectable evidence. WebGlass
records what it observed; the calling agent draws the conclusions. It is not a
thin Playwright wrapper, not a generic scraper, and not a fact checker.

Status
------
Pre-implementation. The web operation surface (search, page, action, session,
exploration, evidence, memory, policy, operation) is specified but not built —
see https://github.com/agentculture/webglass-cli/issues/1. What ships today is
the agent-first introspection CLI below, plus the contracts every future verb
registers onto.

Commands
--------
  webglass-cli whoami             Identity from culture.yaml.
  webglass-cli learn              This self-teaching prompt.
  webglass-cli explain <path>...  Markdown docs for any noun/verb path.
  webglass-cli overview           Descriptive snapshot of the agent.
  webglass-cli doctor             Check the agent-identity invariants.
  webglass-cli cli overview       Describe the CLI surface itself.

Machine-readable output
-----------------------
Every command supports --json. Errors in JSON mode emit
{"code", "message", "remediation"} to stderr. Stdout and stderr never mix.

Exit-code policy
----------------
  0 success
  1 user-input error (bad flag, bad path, missing arg)
  2 environment / setup error
  3+ reserved

More detail
-----------
  webglass-cli explain webglass-cli
"""


def _as_json_payload() -> dict[str, object]:
    return {
        "tool": "webglass-cli",
        "version": __version__,
        "purpose": "The guarded web operations and evidence plane for AI agents.",
        "commands": [
            {"path": ["whoami"], "summary": "Identity probe from culture.yaml."},
            {"path": ["learn"], "summary": "Self-teaching prompt."},
            {"path": ["explain"], "summary": "Markdown docs by path."},
            {"path": ["overview"], "summary": "Descriptive snapshot of the agent."},
            {"path": ["doctor"], "summary": "Check the agent-identity invariants."},
            {"path": ["cli", "overview"], "summary": "Describe the CLI surface."},
        ],
        "exit_codes": {
            "0": "success",
            "1": "user-input error",
            "2": "environment/setup error",
        },
        "json_support": True,
        "explain_pointer": "webglass-cli explain <path>",
    }


def cmd_learn(args: argparse.Namespace) -> int:
    if getattr(args, "json", False):
        emit_result(_as_json_payload(), json_mode=True)
    else:
        emit_result(_TEXT, json_mode=False)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "learn",
        help="Print a structured self-teaching prompt for agent consumers.",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_learn)
