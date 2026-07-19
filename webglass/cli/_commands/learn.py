"""``webglass learn`` — the learnability affordance.

Prints a structured self-teaching prompt. Must satisfy the agent-first rubric:
>=200 chars and mention purpose, command map, exit codes, --json, and explain.

Two things this prompt must keep right, because agents act on it directly:

* **The executable name.** Command examples use ``webglass`` — the console
  script ``[project.scripts]`` actually binds. ``webglass-cli`` is the
  distribution name and is *not* an invocable binary; printing it in a command
  map sends an agent straight to "command not found".
* **The pre-implementation status.** Both the text body and the JSON payload
  (``status`` / ``status_detail``, and a parenthetical in ``purpose``) flag that
  the web operation surface is specified but not built, so a JSON consumer that
  reads only ``purpose`` cannot infer capabilities that do not exist.

Keep both in sync with the ``explain`` root entry in
:mod:`webglass.explain.catalog`.
"""

from __future__ import annotations

import argparse

from webglass import __version__
from webglass.cli._output import emit_result

_TEXT = """\
webglass — WebGlass, the guarded web operations and evidence plane for AI agents.

Invocation
----------
The console script is `webglass`. The distribution/PyPI name is `webglass-cli`
and the import package is `webglass`; run the commands below as `webglass ...`.

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
  webglass whoami             Identity from culture.yaml.
  webglass learn              This self-teaching prompt.
  webglass explain <path>...  Markdown docs for any noun/verb path.
  webglass overview           Descriptive snapshot of the agent.
  webglass doctor             Check the agent-identity invariants.
  webglass cli overview       Describe the CLI surface itself.

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
  webglass explain webglass
"""


def _as_json_payload() -> dict[str, object]:
    return {
        "tool": "webglass-cli",
        "console_script": "webglass",
        "version": __version__,
        "purpose": (
            "The guarded web operations and evidence plane for AI agents "
            "(pre-implementation — the web operation surface is specified but not built)."
        ),
        "status": "pre-implementation",
        "status_detail": (
            "The web operation surface (search, page, action, session, exploration, "
            "evidence, memory, policy, operation) is specified but not built. The "
            "commands listed here are the complete implemented surface. See "
            "https://github.com/agentculture/webglass-cli/issues/1"
        ),
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
        "explain_pointer": "webglass explain <path>",
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
