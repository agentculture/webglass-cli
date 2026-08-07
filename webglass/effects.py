"""Effect classification for WebGlass operations.

Three explicit effect classes govern authorization requirements for every web
operation (issue #1 section 3; CLAUDE.md "Target architecture" section 3):

- ``OBSERVE`` — search, open/follow, read, inspect, extract, list links or
  controls, screenshot, revalidate. Executes when the caller's capability
  authorizes it; the result acknowledges that network and ordinary
  browser-state effects may occur.
- ``LOCAL_STATE`` — effects confined to WebGlass-owned state: create/close a
  session, update its cookies, cache a snapshot, record an exploration edge,
  save evidence, compact memory. Needs an authorized state scope and
  retention policy, not remote-action approval.
- ``REMOTE_ACTION`` — submit, send, publish, purchase, delete, vote, confirm,
  upload, download, authenticate, or any control whose effect cannot be
  proven navigational. Previews by default and requires explicit apply
  authorization (see ``operations.ApplyState``); the prepare -> commit ->
  verify protocol that follows from this classification is a service-layer
  concern (t9), not modelled here.

Classification never guesses permissively: any kind absent from
``EFFECT_CLASS_BY_KIND`` — including a kind this module does not yet know
about, or a raw string that does not resolve to a known
:class:`OperationKind` value — classifies as ``REMOTE_ACTION``, the most
conservative class. This is the "classify upward" rule from issue #1
section 3, and it is unit-tested explicitly in ``tests/test_effects.py``.
"""

from __future__ import annotations

from enum import Enum


class EffectClass(str, Enum):
    """The three effect classes an operation kind must declare exactly one of."""

    OBSERVE = "observe"
    LOCAL_STATE = "local-state"
    REMOTE_ACTION = "remote-action"


class OperationKind(str, Enum):
    """Closed set of operation kinds in the M0-M2 delivery surface.

    The full noun/verb surface (CLAUDE.md "Target architecture" section 4)
    includes ``exploration``, ``evidence``, ``memory``, ``policy``,
    ``operation``, and the rest of ``action`` (``fill``/``select``/``submit``/
    ``download``/``upload``); those land in later milestones. A kind not
    listed here is unknown to this module and, per :func:`classify`,
    classifies upward rather than being silently treated as safe.
    """

    SEARCH = "search"

    PAGE_OPEN = "page.open"
    PAGE_READ = "page.read"
    PAGE_INSPECT = "page.inspect"
    PAGE_EXTRACT = "page.extract"
    PAGE_LINKS = "page.links"
    PAGE_SCREENSHOT = "page.screenshot"

    ACTION_FOLLOW = "action.follow"
    ACTION_PRESS = "action.press"

    SESSION_CREATE = "session.create"
    SESSION_LIST = "session.list"
    SESSION_SHOW = "session.show"
    SESSION_CLOSE = "session.close"
    SESSION_CLEAN = "session.clean"


# Every operation kind declares exactly one effect class (issue #1 section 3).
#
# ``action.press`` classifies REMOTE_ACTION here deliberately. The exported
# spec's decision (2026-08-07, resolving q4) lets a *declared test profile*
# treat press as observe for the app under test — but that is a policy-layer
# override evaluated with the effective profile (policy.py, a sibling module
# this task does not import), not a fact about the operation kind in
# isolation. Outside that override, a key press cannot be proven navigational,
# so the conservative default applies per "classify upward".
EFFECT_CLASS_BY_KIND: dict[OperationKind, EffectClass] = {
    OperationKind.SEARCH: EffectClass.OBSERVE,
    OperationKind.PAGE_OPEN: EffectClass.OBSERVE,
    OperationKind.PAGE_READ: EffectClass.OBSERVE,
    OperationKind.PAGE_INSPECT: EffectClass.OBSERVE,
    OperationKind.PAGE_EXTRACT: EffectClass.OBSERVE,
    OperationKind.PAGE_LINKS: EffectClass.OBSERVE,
    OperationKind.PAGE_SCREENSHOT: EffectClass.OBSERVE,
    OperationKind.ACTION_FOLLOW: EffectClass.OBSERVE,
    OperationKind.ACTION_PRESS: EffectClass.REMOTE_ACTION,
    OperationKind.SESSION_CREATE: EffectClass.LOCAL_STATE,
    OperationKind.SESSION_LIST: EffectClass.LOCAL_STATE,
    OperationKind.SESSION_SHOW: EffectClass.LOCAL_STATE,
    OperationKind.SESSION_CLOSE: EffectClass.LOCAL_STATE,
    OperationKind.SESSION_CLEAN: EffectClass.LOCAL_STATE,
}


def classify(kind: OperationKind | str) -> EffectClass:
    """Return the effect class declared for ``kind``.

    ``kind`` may be an :class:`OperationKind` member, or a plain string that
    matches a known member's value (so callers holding a kind string off the
    wire — CLI args, a deserialized JSON payload — do not need to construct
    the enum themselves first).

    Any value that does not resolve to a known :class:`OperationKind` —
    a typo, a not-yet-implemented kind such as ``"action.submit"``, or an
    arbitrary string — classifies upward to :attr:`EffectClass.REMOTE_ACTION`.
    Classification never falls back to a more permissive class than that.
    """
    try:
        resolved = kind if isinstance(kind, OperationKind) else OperationKind(kind)
    except ValueError:
        return EffectClass.REMOTE_ACTION
    return EFFECT_CLASS_BY_KIND.get(resolved, EffectClass.REMOTE_ACTION)
