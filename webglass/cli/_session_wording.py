"""Canonical wording for the throwaway/reuse session contract (build plan t15).

Four user-facing surfaces describe the same three facts about the session a
navigating ``page`` verb runs in when the caller does not name one with
``--session-id``:

* ``webglass page``'s module docstring (:mod:`webglass.cli._commands.page`),
* ``webglass page overview``'s "Naming the page" section,
* ``webglass explain page open`` (:mod:`webglass.explain.catalog`), and
* ``webglass session overview``'s Persistence section
  (:mod:`webglass.cli._commands.session`).

Before build plan t15 those four surfaces had drifted: the module docstring
already described flow-scoped reuse (build plan t13), while the other three
still described only the pre-t13 throwaway-only posture, and even the two
surfaces that *did* describe the default case used different wording for it
("closed within the invocation" vs. "closed inside this invocation").

Rather than re-checking four hand-written paragraphs for agreement every time
one of them changes, each surface renders these three fragments verbatim.
``tests/test_cli.py::test_session_contract_wording_is_consistent`` then
checks all three fragments appear in all four rendered surfaces — a
mechanical guard in the same spirit as
``test_every_catalog_path_resolves``, which guards the catalog itself.

This module is deliberately import-light (only the standard library, and
nothing else in ``webglass``) so :mod:`webglass.explain.catalog` — loaded on
every ``webglass explain`` invocation — can depend on it without pulling in
:mod:`webglass.cli._factory`'s much heavier adapter/service graph.

The fragments carry no markup of their own (no backticks, no ``*emphasis*``):
each surface wraps them in whatever inline styling that surface's format
uses (RST double-backticks in the docstring, Markdown single backticks in
the explain catalog, plain text in CLI overview output), which the
consistency test accounts for by stripping backtick/asterisk markup before
comparing.
"""

from __future__ import annotations

__all__ = [
    "SESSION_OWNER_ENV",
    "DEFAULT_EPHEMERAL_CLAIM",
    "FLOW_REUSE_CLAIM",
    "FRESH_SESSION_OPT_OUT_CLAIM",
    "SWEEP_DISCLOSURE_CLAIM",
]

#: Mirrors :data:`webglass.cli._factory.SESSION_OWNER_ENV` — duplicated here
#: (rather than imported) because that module is the heavy one this module
#: exists to let :mod:`webglass.explain.catalog` avoid depending on. Both
#: names must name the same environment variable; nothing checks that
#: automatically, so change them together.
SESSION_OWNER_ENV = "WEBGLASS_SESSION_OWNER"

#: The default posture: no ``--session-id``, no flow — every call opens and
#: closes its own session.
DEFAULT_EPHEMERAL_CLAIM = "created and closed within this invocation"

#: The opt-in: naming a flow gets continuation instead of a fresh session.
FLOW_REUSE_CLAIM = f"${SESSION_OWNER_ENV} declares this invocation part of a flow"

#: The per-call opt-out of that continuation.
FRESH_SESSION_OPT_OUT_CLAIM = "--fresh-session opts out per call"

#: What the opportunistic sweep (build plan t10) discloses about itself
#: (build plan t11). A session-creating invocation reaps expired sessions on
#: its way past; the records it took are named in the result rather than
#: vanishing silently, because terminating a browser and deleting its profile
#: directory is irreversible and happened as a side effect of asking for
#: something else.
SWEEP_DISCLOSURE_CLAIM = "swept_sessions names any expired sessions reaped on the way past"
