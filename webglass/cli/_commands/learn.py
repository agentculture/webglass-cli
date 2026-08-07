"""``webglass learn`` — the learnability affordance.

Prints a structured self-teaching prompt. Must satisfy the agent-first rubric:
>=200 chars and mention purpose, command map, exit codes, --json, and explain.

Two things this prompt must keep right, because agents act on it directly:

* **The executable name.** Command examples use ``webglass`` — the console
  script ``[project.scripts]`` actually binds. ``webglass-cli`` is the
  distribution name and is *not* an invocable binary; printing it in a command
  map sends an agent straight to "command not found".
* **The backend-honest status.** Both the text body and the JSON payload
  (``status`` / ``status_detail``, and a parenthetical in ``purpose``) flag that
  the M1 operation surface (search/page/action/session) is implemented and
  executes real operations, but no live search or browser backend is wired in
  yet — those verbs report a structured ``backend_unavailable`` result until
  M2. A JSON consumer that reads only ``purpose`` must not infer live-web
  capabilities that do not exist yet.

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
Pre-implementation overall (see
https://github.com/agentculture/webglass-cli/issues/1) — but the M1 operation
surface is now real: `search`, `page`, and `action` build and execute genuine
WebOperation objects through WebGlassService, and `session` runs against a
real (in-process) session store. No live search or browser backend is wired
in by default yet, so those verbs report a structured `backend_unavailable`
result until the Playwright and search-provider adapters land (M2, build plan
tasks t11/t13/t15). The exploration, evidence, memory, policy, and operation
nouns are not built yet.

Commands
--------
  webglass whoami               Identity from culture.yaml.
  webglass learn                This self-teaching prompt.
  webglass explain <path>...    Markdown docs for any noun/verb path.
  webglass overview             Descriptive snapshot of the agent.
  webglass doctor               Check the agent-identity invariants.
  webglass cli overview         Describe the CLI surface itself.
  webglass search <query>       Run a search operation (needs a backend).
  webglass page overview        Page verbs: open/read/inspect/extract/links/screenshot.
  webglass action overview      Action verbs: follow/press.
  webglass session overview     Session verbs: create/list/show/close/clean.

Machine-readable output
-----------------------
Every command supports --json. Errors in JSON mode emit
{"code", "message", "remediation"} to stderr. Stdout and stderr never mix.
Web-operation verbs (search/page/action/session) always carry their full
WebOperationResult on stdout in --json mode, success or failure — see
`webglass explain page` for that contract.

Exit-code policy
----------------
  0 success (including a web-operation 'previewed' result)
  1 user-input error, or a web-operation 'denied'/'blocked'/'failed'/
    'timed_out'/'cancelled' result
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
            "(pre-implementation overall — the M1 operation surface for search/page/"
            "action/session is implemented over injectable fakes; live backends land "
            "at M2)."
        ),
        "status": "pre-implementation",
        "status_detail": (
            "The M1 operation surface (search, page, action, session) is implemented: "
            "every verb builds a real WebOperation and executes it through "
            "WebGlassService, returning the same structured WebOperationResult the "
            "library API returns. No live search or browser backend is wired in by "
            "default, so those verbs report a structured 'backend_unavailable' result "
            "until the Playwright and search-provider adapters land (build plan tasks "
            "t11/t13/t15). The exploration, evidence, memory, policy, and operation "
            "nouns are not built yet. See "
            "https://github.com/agentculture/webglass-cli/issues/1"
        ),
        "commands": [
            {"path": ["whoami"], "summary": "Identity probe from culture.yaml."},
            {"path": ["learn"], "summary": "Self-teaching prompt."},
            {"path": ["explain"], "summary": "Markdown docs by path."},
            {"path": ["overview"], "summary": "Descriptive snapshot of the agent."},
            {"path": ["doctor"], "summary": "Check the agent-identity invariants."},
            {"path": ["cli", "overview"], "summary": "Describe the CLI surface."},
            {
                "path": ["search"],
                "summary": "Run a search operation (needs a search backend).",
            },
            {
                "path": ["page", "overview"],
                "summary": "Page verbs: open/read/inspect/extract/links/screenshot.",
            },
            {"path": ["action", "overview"], "summary": "Action verbs: follow/press."},
            {
                "path": ["session", "overview"],
                "summary": "Session verbs: create/list/show/close/clean.",
            },
        ],
        "exit_codes": {
            "0": "success (including a web-operation 'previewed' result)",
            "1": (
                "user-input error, or a web-operation 'denied'/'blocked'/'failed'/"
                "'timed_out'/'cancelled' result"
            ),
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
