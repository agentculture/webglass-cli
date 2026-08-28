"""CLI adapter-wiring seam — the one place CLI handlers get a WebGlassService.

CLAUDE.md's CLI skeleton section fixes the handler contract: a handler builds
a ``WebOperation`` + ``WebContext``, hands both to
:meth:`~webglass.service.WebGlassService.execute`, and renders the result —
*no operation logic in the handler itself* (spec honesty h2). This module is
the other half of that contract: it is the single place that constructs the
``WebGlassService`` and the ``WebContext`` a CLI invocation runs against, plus
the shared ``WebOperation``-building and result-rendering helpers every noun
module (``search.py``, ``page.py``, ``action.py``, ``session.py``) calls — so
no ``_commands`` module ever imports an adapter, constructs a
``WebGlassService`` directly, or duplicates the exit-code/output mapping.

Default posture
----------------
:func:`build_service` wires a real :class:`Clock`/:class:`IdProvider` (this
module's :class:`SystemClock`/:class:`UuidIds`), a
:class:`~webglass.adapters.session_store.FileSessionStore`, a **live browser
backend**, an in-process artifact store, the effective web policy, and — when
its API key is configured — the Brave search provider.

Four of those are conditional, and each condition is an explicit, reported
state rather than a silent degradation:

``browser``
    :data:`BROWSER_BACKEND_ENV` selects the backend and now defaults to
    ``playwright`` (build plan t13; t11 built the adapter, t12 the session
    store underneath it). ``WEBGLASS_BROWSER_BACKEND=none`` is the explicit
    browser-less posture the non-browser test suite and every browser-free
    caller run in, and every other value is a structured environment error —
    never a silent fallback to "none", because a caller who asked for a
    browser and got a no-op would be told their page verbs are unsupported
    when in fact their spelling was wrong.
``search``
    Wired to :class:`~webglass.adapters.brave.BraveSearchProvider` when
    :data:`~webglass.adapters.brave.WEBGLASS_BRAVE_API_KEY_ENV` is set (t15).
    Unset, no provider is injected and ``webglass search`` reports the
    structured ``backend_unavailable`` result naming the variable to set — a
    configuration answer, not a crash. The key is read at call time and never
    stored, logged, or persisted (spec claim c21).
``policy``
    The built-in deny-by-default profile unless a profile file is named by
    ``--policy-profile`` or :data:`POLICY_PROFILE_ENV`. See
    :func:`build_policy_evaluator`: an *absent* profile and a *malformed*
    profile are different states, and the malformed one fails closed with
    exit 2.
``artifacts``
    An in-process :class:`~webglass.adapters.artifacts.FakeArtifactStore`, so
    ``page screenshot`` can hash and hand back the PNG (and write it to
    ``--out``) within the invocation that took it. Durable,
    cross-invocation artifact storage is M3; this store deliberately does not
    pretend to be it.

Ephemeral sessions
-------------------
A page/action verb given no ``--session-id`` runs in a throwaway session:
:func:`ephemeral_session` creates one, launches its browser, and closes it
(terminating the process and removing its profile directory) in a ``finally``
— so a one-shot ``webglass page open URL`` leaves no browser behind and no
record to reap. The alternative, an unstored ephemeral id, cannot work here:
the browser backend resolves endpoints *through the store*, so a session with
no record has no browser to reach.

This is deliberately a **CLI-invocation convenience**, not an operation: a
library caller creates and closes sessions explicitly through
``session.create`` / ``session.close``. The ``page.open`` operation itself is
identical either way, which is what the library/CLI parity contract (spec
honesty h2) actually requires.

Sessions across invocations
----------------------------
The store is on disk, under ``$WEBGLASS_STATE_DIR`` /
``$XDG_STATE_HOME/webglass`` / ``~/.local/state/webglass`` (see
:mod:`webglass.adapters.session_store`), so ``session create`` in one
``webglass`` process is visible to ``session show`` in the next — the M2
cross-invocation session promise (spec claim c29). A fresh store object is
built per :func:`build_service` call because it holds no state of its own:
every fact lives in the files, which is exactly what makes two processes
agree.

The seam, precisely
--------------------
:func:`build_service` merges a caller-supplied mapping over
``_DEFAULT_SERVICE_KWARGS`` and constructs one ``WebGlassService``.
``_DEFAULT_SERVICE_KWARGS`` is deliberately a **module-level dict**, not a
function-local literal, so:

* a later task can rebind one entry at process start (e.g.
  ``_factory._DEFAULT_SERVICE_KWARGS["browser"] = PlaywrightBrowserBackend()``)
  without touching any ``_commands`` module, and
* tests can monkeypatch either the dict (``monkeypatch.setitem``) or the whole
  ``build_service``/``build_context`` function to inject fakes and drive a
  complete operation lifecycle — see ``tests/test_cli_webverbs.py``.

:func:`build_context` is the same pattern for the per-invocation
``WebContext``. ``caller``/``task`` are deliberately **fixed constants**, not
minted per invocation: ``WebGlassService`` scopes session visibility
(``session list``) and lease-conflict resolution by ``(caller, task)``, so a
CLI that minted a fresh task id on every invocation would make ``session
create`` in one call invisible to (or lease-conflicting with) ``session
show``/``page open --session-id ...`` in the next — breaking exactly the
cross-invocation session flow this milestone promises (build plan t10's
design guidance). That is now load-bearing across *processes* too: the fixed
holder string is why a second CLI invocation renews its own lease instead of
being refused by the first one's. A real multi-caller/multi-task CLI identity
(and with it, genuine per-caller session isolation) is a later decision.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from webglass.adapters.artifacts import ArtifactStore, FakeArtifactStore
from webglass.adapters.brave import (
    WEBGLASS_BRAVE_API_KEY_ENV,
    BraveSearchProvider,
    SearchProviderError,
)
from webglass.adapters.browser import BrowserBackend
from webglass.adapters.clock import Clock, IdProvider  # noqa: F401 - re-exported for callers
from webglass.adapters.search import SearchProvider
from webglass.adapters.session_store import (
    FileSessionStore,
    SessionLauncher,
    SessionLaunchError,
    default_sessions_dir,
    make_playwright_launcher,
)
from webglass.cli._browser_doctor import _quiet_playwright_teardown_chatter
from webglass.cli._errors import EXIT_ENV_ERROR, EXIT_SUCCESS, EXIT_USER_ERROR, CliError
from webglass.cli._output import emit_error, emit_result
from webglass.context import WebContext
from webglass.effects import EffectClass, OperationKind
from webglass.operations import ApplyState, CallerContext, OperationTarget, WebOperation
from webglass.policy import PolicyError, WebPolicyEvaluator, WebPolicyProfile
from webglass.results import LifecycleState, OperationError, WebOperationResult
from webglass.service import WebGlassService

__all__ = [
    "BROWSER_BACKEND_ENV",
    "BROWSER_BACKENDS",
    "POLICY_PROFILE_ENV",
    "ProvisionedSession",
    "SystemClock",
    "UuidIds",
    "add_policy_profile_argument",
    "browser_backend_name",
    "build_browser_backend",
    "build_context",
    "build_invocation",
    "build_operation",
    "build_policy_evaluator",
    "build_search_provider",
    "build_service",
    "build_session_store",
    "declared_target_effect_class",
    "ephemeral_session",
    "render_operation_result",
    "reset_browser_backends",
]


class SystemClock:
    """Real :class:`~webglass.adapters.clock.Clock`: wall-clock time via ``time.time()``."""

    def now(self) -> float:
        return time.time()


class UuidIds:
    """Real :class:`~webglass.adapters.clock.IdProvider`: ``<kind>-<uuid4 hex>`` ids."""

    def new_id(self, kind: str) -> str:
        return f"{kind}-{uuid.uuid4().hex}"


#: Selects this process's browser backend: ``playwright`` (the default, as of
#: build plan t13) or ``none``. Setting it to ``none`` is the explicit
#: browser-less posture — the page/action verbs then report a structured
#: ``backend_unavailable`` instead of contacting anything.
BROWSER_BACKEND_ENV = "WEBGLASS_BROWSER_BACKEND"

#: Every value :data:`BROWSER_BACKEND_ENV` accepts. An unrecognized value is a
#: structured environment error (exit 2), never a silent fallback to "none":
#: a caller who asked for a browser and got a no-op would be told their page
#: verbs are unsupported when in fact their spelling was wrong.
BROWSER_BACKENDS = ("none", "playwright")

#: The browser this process uses when :data:`BROWSER_BACKEND_ENV` is unset.
#: Flipped from ``"none"`` to ``"playwright"`` by build plan t13 — the point
#: of M2 is that ``webglass page open URL`` observes a real page.
DEFAULT_BROWSER_BACKEND = "playwright"

#: Points at a JSON file holding the effective web policy profile
#: (:meth:`webglass.policy.WebPolicyProfile.from_dict`'s shape). The
#: ``--policy-profile`` flag overrides it per invocation.
POLICY_PROFILE_ENV = "WEBGLASS_POLICY_PROFILE"

#: Fixed caller/task/evidence-namespace identity for every CLI-issued
#: operation — see the module docstring for why these are constants.
_DEFAULT_CALLER = "cli"
_DEFAULT_TASK = "cli"
_DEFAULT_EVIDENCE_NAMESPACE = "cli"
_DEFAULT_POLICY_PROFILE_REF = "built-in-default"


def _new_owner_token() -> str:
    """Mint a fresh owner token for one throwaway session (build plan t7).

    ``_DEFAULT_CALLER`` is a fixed constant shared by every CLI invocation —
    issue #14's measurement found all 134 records on the reporting host
    carrying ``caller="cli"``, so ``caller`` distinguishes nothing between
    concurrent invocations. The owner token exists to fix exactly that gap,
    but only for **reuse eligibility** (claims c38/c39 supersede c35's
    earlier sweep-filtering design): ``clean()`` stays liveness-gated and
    owner-agnostic, because an expired record's owner is finished or
    crashed and reaping it is safe regardless of who owned it.

    Because reuse is the only consumer, a per-invocation unique id is
    sufficient — it does not need to identify a long-lived client or survive
    past this process. A fresh ``uuid4`` per throwaway session is exactly
    that: unique, un-guessable, and cheap enough to mint on every call
    without memoizing it anywhere.
    """
    return uuid.uuid4().hex


#: How long an auto-created throwaway session is allowed to live. It is closed
#: in a ``finally`` long before this matters; the TTL is the backstop for a
#: caller killed mid-operation, so ``session clean`` reaps it promptly.
_EPHEMERAL_SESSION_TTL_SECONDS = 300.0

#: The default adapter set :func:`build_service` merges caller overrides
#: over. Deliberately module-level (not a function-local literal) — see the
#: module docstring's "the seam, precisely" section.
#: ``sessions`` and ``browser`` are present but ``None``: they are built per
#: invocation by :func:`build_session_store` / :func:`build_browser_backend`
#: (both read the environment, which a module-level literal frozen at import
#: time could not). Rebinding either entry still overrides that construction
#: — the seam is unchanged, only its default is computed rather than fixed.
_DEFAULT_SERVICE_KWARGS: dict[str, Any] = {
    "clock": SystemClock(),
    "ids": UuidIds(),
    "sessions": None,
    "browser": None,
    "search": None,
    "policy": None,
    "artifacts": None,
    "effect_class_resolver": None,
}

#: The default ``WebContext`` field set :func:`build_context` merges caller
#: overrides over. ``workspace`` is intentionally absent here — it is
#: computed fresh on every call (the caller's cwd can change between
#: invocations) rather than frozen at import time.
_DEFAULT_CONTEXT_KWARGS: dict[str, Any] = {
    "caller": _DEFAULT_CALLER,
    "task": _DEFAULT_TASK,
    "policy_profile_ref": _DEFAULT_POLICY_PROFILE_REF,
    "evidence_namespace": _DEFAULT_EVIDENCE_NAMESPACE,
}


def browser_backend_name(environ: Mapping[str, str] | None = None) -> str:
    """Which browser backend this process is configured for.

    :raises CliError: (exit 2, environment error) on an unrecognized value.
    """
    env = os.environ if environ is None else environ
    name = env.get(BROWSER_BACKEND_ENV, "").strip().lower() or DEFAULT_BROWSER_BACKEND
    if name not in BROWSER_BACKENDS:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"{BROWSER_BACKEND_ENV}={name!r} is not a known browser backend",
            remediation=f"set it to one of: {', '.join(BROWSER_BACKENDS)} (or leave it unset)",
        )
    return name


def build_session_store(environ: Mapping[str, str] | None = None) -> FileSessionStore:
    """The on-disk session store this invocation reads and writes.

    Given a browser backend, the store also gets its *launcher*, so ``session
    create`` starts the detached browser whose endpoint later invocations
    reattach to. With no browser backend it gets none, and ``session create``
    records a session without starting anything — the posture the default
    test suite and every browser-less caller run in.
    """
    launcher: SessionLauncher | None = None
    if browser_backend_name(environ) == "playwright":
        # Launching resolves the Chromium path through Playwright, which
        # starts its driver — and its asyncio teardown chatter would otherwise
        # break the "no traceback ever reaches stderr" contract.
        _quiet_playwright_teardown_chatter()
        launcher = make_playwright_launcher()
    return FileSessionStore(default_sessions_dir(environ), launcher=launcher)


#: One browser backend per process per store directory — see
#: :func:`build_browser_backend` on why this cache is a correctness
#: requirement rather than an optimization.
_BACKEND_CACHE: dict[Path, BrowserBackend] = {}


def reset_browser_backends() -> None:
    """Drop the cached backends (test helper / long-lived-process reset).

    Does **not** disconnect them: dropping a reference is not the same as
    tearing down a live CDP connection, and a caller that wants a fresh
    backend for a different store must not thereby close the previous one's
    pages.
    """
    _BACKEND_CACHE.clear()


def build_browser_backend(
    store: FileSessionStore, environ: Mapping[str, str] | None = None
) -> BrowserBackend | None:
    """The browser backend for this process, or ``None`` if not configured.

    The backend resolves each session id to a connect endpoint through
    ``store.endpoint_for`` — the callable seam
    :class:`~webglass.adapters.playwright.PlaywrightBrowserBackend` documents
    for exactly this. That single line is what makes a browser session
    reattachable from a process that never launched it: the endpoint comes out
    of the file store, not out of this process's memory.

    **Per process, not per operation.** Playwright's sync API refuses to start
    a second driver inside a thread that already has one running ("It looks
    like you are using Playwright Sync API inside the asyncio loop"), so a
    process performing two web operations must reuse one backend — a fresh
    instance per :func:`build_service` call would make the *second* operation
    fail with a bare backend error. The cache is keyed by the store's
    directory, since that is what determines which endpoints resolve; the
    backend's own connections stay lazy, so an unused backend costs nothing.

    Playwright is imported lazily so the browser-less posture never pays for
    (or requires) the import.
    """
    if browser_backend_name(environ) != "playwright":
        return None
    cached = _BACKEND_CACHE.get(store.directory)
    if cached is not None:
        return cached
    from webglass.adapters.playwright import PlaywrightBrowserBackend

    # Playwright's sync API logs "Task was destroyed but it is pending!" and a
    # TargetClosedError traceback through the `asyncio` logger at interpreter
    # teardown (reproducible with no WebGlass code at all — see
    # webglass/adapters/playwright.py's module docstring). The CLI contract is
    # that no Python traceback ever reaches stderr, so the layer that wires the
    # browser in silences it. One shared, idempotent implementation lives in
    # `_browser_doctor`, which was the first layer to need it.
    _quiet_playwright_teardown_chatter()
    backend = PlaywrightBrowserBackend(store.endpoint_for)
    _BACKEND_CACHE[store.directory] = backend
    return backend


# ---------------------------------------------------------------------------
# Policy, search, and effect-class wiring
# ---------------------------------------------------------------------------


def build_policy_evaluator(
    profile_path: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> WebPolicyEvaluator | None:
    """The effective web policy for this invocation, or ``None`` for the default.

    Resolution order: the explicit ``profile_path`` (the ``--policy-profile``
    flag), then :data:`POLICY_PROFILE_ENV`, then nothing. Returning ``None`` is
    meaningful and is *not* the same as returning a permissive evaluator: the
    service falls back to its built-in deny-by-default profile, which denies
    loopback, link-local, private-network, and cloud-metadata targets. Enabling
    an app under test therefore always takes an explicit declared-target allow
    in a profile file — never a relaxed default and never an environment flag
    that widens policy on its own (spec claim c19 / honesty h12).

    **Malformed policy fails closed.** An unreadable file, invalid JSON, an
    unknown key, a bad type, or an unparseable declared target all raise
    :class:`~webglass.cli._errors.CliError` with exit 2. Absent policy and
    malformed policy are different states, and neither one ever becomes an
    allow (CLAUDE.md "Target architecture" section 8).
    """
    env = os.environ if environ is None else environ
    raw = profile_path if profile_path is not None else env.get(POLICY_PROFILE_ENV, "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"could not read the policy profile at {path}: {exc.strerror or exc}",
            remediation=(
                "point --policy-profile (or $" + POLICY_PROFILE_ENV + ") at a readable "
                "JSON file; WebGlass refuses to run rather than fall back to an "
                "unstated policy"
            ),
        ) from exc
    except ValueError as exc:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"the policy profile at {path} is not valid JSON: {exc}",
            remediation=(
                "fix the JSON syntax; a malformed policy profile never silently "
                "falls open to the built-in default"
            ),
        ) from exc
    try:
        profile = WebPolicyProfile.from_dict(data)
    except PolicyError as exc:
        field = getattr(exc, "field", None)
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"the policy profile at {path} is malformed: {exc}",
            remediation=(
                (f"fix the {field!r} entry; " if field else "")
                + "known keys are name, allowed_schemes, denied_schemes, "
                "declared_targets, max_redirects, max_response_bytes — a malformed "
                "profile fails closed and is never treated as an allow"
            ),
        ) from exc
    return WebPolicyEvaluator(profile)


def build_search_provider(environ: Mapping[str, str] | None = None) -> SearchProvider | None:
    """The search provider for this invocation, or ``None`` if unconfigured.

    ``None`` is the honest answer when
    :data:`~webglass.adapters.brave.WEBGLASS_BRAVE_API_KEY_ENV` is unset: the
    service reports a structured ``backend_unavailable`` result naming what is
    missing, which is a *configuration* answer a caller can act on — not a
    crash, and not a silent substitution of some other provider.

    The key is read here, at call time, and handed straight to the provider.
    It is never cached in this module, written to a session record, or put on
    a result (spec claim c21 / honesty h13).
    """
    env = os.environ if environ is None else environ
    if not env.get(WEBGLASS_BRAVE_API_KEY_ENV, "").strip():
        return None
    try:
        return BraveSearchProvider.from_env(env=env)
    except SearchProviderError:  # pragma: no cover - guarded by the check above
        return None


def declared_target_effect_class(operation: WebOperation, profile: WebPolicyProfile) -> EffectClass:
    """Effect-class resolution under a declared app-under-test profile (c37).

    ``action.press`` classifies upward to ``remote-action`` by default: a key
    press on the open web cannot be proven navigational (``Enter`` submits
    forms), so it previews and never fires. That is the right default and it
    stays the default here.

    The spec's 2026-08-07 decision (resolving challenge question q4) makes one
    scoped exception: *under a declared test profile*, ``press`` executes as
    ``observe`` for all keys including ``Enter``. The authorization is the
    profile itself — a caller who wrote ``declared_targets`` into an effective
    policy file has said, explicitly and in data, "this origin is my app under
    test, drive it". No environment flag and no code-path bypass can produce
    that state (spec instruction on c19).

    Scope, stated honestly
    -----------------------
    A press targets a *session*, not a URL, so this resolver cannot check the
    key against an origin the way :mod:`webglass.policy` checks a navigation.
    What bounds it instead is that a session can only be showing a page policy
    already allowed: every navigation — the original ``page.open``, every
    redirect hop, and every live re-read — is evaluated against this same
    profile. A profile with no declared targets cannot reach a private app at
    all, and a profile with them has authorized exactly that traffic. The
    residual is real and worth naming: with a profile that declares an app
    under test, a press also classifies as ``observe`` on a *public* page the
    same session happens to be on. Per-session origin scoping is a session-state
    question, not a resolver one, and belongs with the M5 prepare/commit/verify
    work that gives remote actions their own binding to a page generation.
    """
    if operation.effect_class is not EffectClass.REMOTE_ACTION:
        return operation.effect_class
    if operation.kind is not OperationKind.ACTION_PRESS:
        return operation.effect_class
    if not profile.declared_targets:
        return operation.effect_class
    return EffectClass.OBSERVE


def build_service(overrides: Mapping[str, Any] | None = None) -> WebGlassService:
    """Construct the ``WebGlassService`` one CLI invocation executes operations through.

    Every ``_commands`` handler calls this (never ``WebGlassService(...)``
    directly) so the adapter set lives in exactly one place. ``overrides``
    replaces individual constructor kwargs (``search=``, ``browser=``,
    ``artifacts=``, ``policy=``, ``clock=``, ``ids=``, ``sessions=``, ...)
    without the caller needing to know the rest of the default set — and an
    override wins over the environment-driven defaults below.
    """
    kwargs: dict[str, Any] = dict(_DEFAULT_SERVICE_KWARGS)
    if overrides:
        kwargs.update(overrides)
    store = kwargs.get("sessions")
    if store is None:
        store = build_session_store()
        kwargs["sessions"] = store
    if kwargs.get("browser") is None and isinstance(store, FileSessionStore):
        kwargs["browser"] = build_browser_backend(store)
    if kwargs.get("search") is None:
        kwargs["search"] = build_search_provider()
    if kwargs.get("policy") is None:
        kwargs["policy"] = build_policy_evaluator()
    if kwargs.get("artifacts") is None:
        kwargs["artifacts"] = _build_artifact_store()
    if kwargs.get("effect_class_resolver") is None:
        kwargs["effect_class_resolver"] = declared_target_effect_class
    # `WebGlassService` treats a `None` adapter as "not injected"; only
    # `policy=None` would be wrong to pass through (it has its own default),
    # and it happens to accept None for exactly that meaning. Drop the keys
    # that stayed None so the service's own documented defaults apply.
    return WebGlassService(**{k: v for k, v in kwargs.items() if v is not None})


def _build_artifact_store() -> ArtifactStore:
    """The artifact store one CLI invocation hashes its screenshots into.

    In-process and process-lifetime only: the durable, content-addressed store
    of issue #1 section 8 is an M3 module. What this buys today is that
    ``page screenshot`` can report a real content hash and size, and write the
    PNG to ``--out``, inside the invocation that captured it — without
    pretending an artifact id survives the process.
    """
    return FakeArtifactStore()


def build_context(overrides: Mapping[str, Any] | None = None) -> WebContext:
    """Construct the ``WebContext`` one CLI invocation runs its operation in."""
    kwargs: dict[str, Any] = dict(_DEFAULT_CONTEXT_KWARGS)
    kwargs["workspace"] = str(Path.cwd())
    if overrides:
        kwargs.update(overrides)
    return WebContext(**kwargs)


def add_policy_profile_argument(parser: argparse.ArgumentParser) -> None:
    """Register ``--policy-profile`` on one web verb's parser.

    Every verb that can reach the network takes it, because policy is
    evaluated per operation and a caller should never have to guess which verb
    in a pipeline the profile applied to.
    """
    parser.add_argument(
        "--policy-profile",
        default=None,
        metavar="FILE",
        help=(
            "JSON policy profile to evaluate this operation under (or set $"
            + POLICY_PROFILE_ENV
            + "). Declaring an app under test in its 'declared_targets' is the only "
            "way to reach a loopback or private-network target; a malformed profile "
            "fails closed."
        ),
    )


def build_invocation(args: argparse.Namespace) -> tuple[WebGlassService, WebContext]:
    """The service + context one web-verb invocation runs against.

    The single entry point ``page``/``action`` handlers use, so the
    ``--policy-profile`` flag, the effective profile's name, and the adapter
    set are wired in exactly one place rather than in each handler.

    ``build_service``/``build_context`` are looked up through the module
    namespace on purpose: tests monkeypatch those two names to inject fakes
    (``tests/test_cli_webverbs.py``), and going through this function must not
    bypass that seam.
    """
    overrides: dict[str, Any] = {}
    evaluator = build_policy_evaluator(getattr(args, "policy_profile", None))
    if evaluator is not None:
        overrides["policy"] = evaluator
    service = build_service(overrides)
    # The context records *which* profile authorized the operation, so the
    # caller identity on every result names the policy it ran under.
    profile_ref = getattr(getattr(service, "policy", None), "profile", None)
    context_overrides: dict[str, Any] = {}
    if profile_ref is not None:
        context_overrides["policy_profile_ref"] = profile_ref.name
    return service, build_context(context_overrides)


@dataclass(frozen=True)
class ProvisionedSession:
    """The session an operation will run in, and who owns its lifetime.

    :func:`ephemeral_session` yields this rather than a bare id because the
    two facts are inseparable and only the provisioner knows the second one.
    A CLI throwaway is a *real stored* session (the browser backend resolves
    an endpoint through the store), so downstream code cannot recover
    ``ephemeral`` by looking at the id — which is precisely how every CLI
    navigation came to report ``ephemeral: false`` in issue #14.
    """

    session_id: str | None
    ephemeral: bool = False


@contextmanager
def ephemeral_session(
    service: WebGlassService, requested: str | None, *, provision: bool = True
) -> Iterator[ProvisionedSession]:
    """Yield the session an operation should run in, creating one if needed.

    ``provision=False`` makes this a pass-through unconditionally — for a verb
    that will not drive a browser to a URL (a lens over a retained snapshot,
    say), launching and stopping a Chromium would be pure cost.

    With ``--session-id`` the caller owns the session's lifetime and this is a
    pass-through. Without one, and with a browser actually wired, a throwaway
    session is created for this operation and closed on the way out —
    terminating its browser process and removing its profile directory — so a
    one-shot ``webglass page open URL`` never leaks a Chromium.

    Why a *stored* session rather than an unstored ephemeral id: the browser
    backend resolves a session id to a connect endpoint **through the store**
    (see :func:`build_browser_backend`), so an id with no record has no browser
    to reach. Creating and immediately closing a real record is the honest
    implementation of "anonymous, unshared, bounded lifetime".

    With no browser wired (``WEBGLASS_BROWSER_BACKEND=none``) this yields
    ``None`` unchanged: the service then reports the structured
    ``backend_unavailable`` result, which is the correct answer and is not
    improved by provisioning a session first.

    Only the branch that actually mints a throwaway sets
    :attr:`ProvisionedSession.ephemeral`; every pass-through yields ``False``,
    because a session this function did not create is one whose lifetime it
    does not own.
    """
    if requested is not None or not provision:
        yield ProvisionedSession(requested)
        return
    store = getattr(service, "sessions", None)
    if service.browser is None or not isinstance(store, FileSessionStore):
        yield ProvisionedSession(None)
        return
    if store.launcher is None:
        # A file store with no launcher records sessions without starting a
        # browser; creating one here would hand the backend an endpoint-less
        # record. Let the operation report the missing capability instead.
        yield ProvisionedSession(None)
        return

    session_id = service.ids.new_id("ephemeral")
    now = service.clock.now()
    try:
        store.create(
            session_id=session_id,
            owner=_DEFAULT_CALLER,
            caller=_DEFAULT_CALLER,
            task=_DEFAULT_TASK,
            backend_id=type(service.browser).__name__,
            now=now,
            expires_at=now + _EPHEMERAL_SESSION_TTL_SECONDS,
            capability_profile_ref=_DEFAULT_POLICY_PROFILE_REF,
            owner_token=_new_owner_token(),
        )
    except SessionLaunchError as exc:
        # The sandbox-unavailable path lands here. It is an environment/setup
        # problem with a specific remediation, so it keeps that remediation
        # instead of being flattened into a generic backend failure.
        raise CliError(
            code=EXIT_ENV_ERROR, message=exc.message, remediation=exc.remediation
        ) from exc
    try:
        yield ProvisionedSession(session_id, ephemeral=True)
    finally:
        # Best-effort by design: a browser that already died must not turn a
        # completed observation into a failure.
        with suppress(Exception):
            store.close(session_id)


def build_operation(
    service: WebGlassService,
    context: WebContext,
    kind: OperationKind,
    *,
    normalized_args: Mapping[str, Any] | None = None,
    target: OperationTarget | None = None,
    session_id: str | None = None,
    session_ephemeral: bool = False,
    apply_state: ApplyState = ApplyState.PREVIEW,
) -> WebOperation:
    """Build one ``WebOperation`` from parsed CLI args and the current context.

    The one place every noun handler goes to translate argv into the shared
    operation model — never a per-handler ad hoc dict. The operation id is
    minted from the *service's own* injected ``IdProvider`` (the same one
    ``service.execute`` itself would use for any internal id), so a
    CLI-issued operation id and a library-issued one are indistinguishable.

    ``session_ephemeral`` comes straight from :class:`ProvisionedSession` and
    defaults to ``False`` — the operation model's own default, so a library
    caller and a CLI caller who both named their own session build an
    identical operation.
    """
    return WebOperation(
        operation_id=service.ids.new_id("operation"),
        kind=kind,
        normalized_args=dict(normalized_args or {}),
        caller=CallerContext(
            caller_id=context.caller,
            task_id=context.task,
            workspace_id=context.workspace,
            policy_profile_ref=(
                str(context.policy_profile_ref) if context.policy_profile_ref is not None else None
            ),
        ),
        session_id=session_id,
        session_ephemeral=session_ephemeral,
        target=target if target is not None else OperationTarget(),
        apply_state=apply_state,
    )


#: Lifecycle states rendered as CLI success (exit 0). ``PREVIEWED`` counts:
#: the operation did exactly what a preview is supposed to do (build plan
#: t10's exit-code design guidance — "Preview is SUCCESS exit 0, it did what
#: was asked").
_EXIT_SUCCESS_STATES = frozenset({LifecycleState.SUCCEEDED, LifecycleState.PREVIEWED})


def render_operation_result(result: WebOperationResult, *, json_mode: bool) -> int:
    """Render one ``WebOperationResult`` through the CLI output/exit contract.

    - **JSON mode** always writes ``result.to_dict()`` to stdout, success or
      not — the error, when present, is *part of* that structured payload, so
      nothing goes to stderr. This is deliberately different from a plain
      ``CliError``: a non-success ``WebOperationResult`` is a normal, fully
      structured operation outcome (policy denial, missing backend, timeout,
      ...), not a CLI usage error.
    - **Text mode** renders a compact human summary to stdout on success/
      preview, or the ``error:``/``hint:`` shape to stderr on any other
      lifecycle state — preserving the "results stdout, errors stderr, never
      mixed" contract.
    - **Exit code**: 0 for ``succeeded``/``previewed``; 1 for
      ``denied``/``blocked``/``failed``/``timed_out``/``cancelled``.
    """
    ok = result.lifecycle_state in _EXIT_SUCCESS_STATES
    if json_mode:
        emit_result(result.to_dict(), json_mode=True)
    elif ok:
        emit_result(_render_text(result), json_mode=False)
    else:
        error = result.error or OperationError(
            code="unknown_error",
            message=(
                f"operation ended in state {result.lifecycle_state.value!r} without a "
                "structured error (this is a WebGlassService bug)"
            ),
            remediation=f"file a bug with operation_id={result.operation_id}",
        )
        emit_error(
            CliError(code=EXIT_USER_ERROR, message=error.message, remediation=error.remediation),
            json_mode=False,
        )
    return EXIT_SUCCESS if ok else EXIT_USER_ERROR


def _render_text(result: WebOperationResult) -> str:
    """Compact human summary derived only from the structured result.

    Trusted control metadata (lifecycle, backend, policy decision, warnings)
    comes first; page- or provider-authored content is printed last, under an
    explicit "content (untrusted)" header — never presented as if it were a
    WebGlass diagnostic (CLAUDE.md "Target architecture" section 7).
    """
    kind = result.kind.value if isinstance(result.kind, OperationKind) else str(result.kind)
    lines = [f"{kind}: {result.lifecycle_state.value}", f"operation_id: {result.operation_id}"]
    if result.backend:
        lines.append(f"backend: {result.backend}")
    if result.policy_verdict.decision is not None:
        lines.append(f"policy: {result.policy_verdict.decision}")

    if result.lifecycle_state is LifecycleState.PREVIEWED:
        preview = result.content.trusted.get("preview")
        if isinstance(preview, dict):
            lines.append("")
            lines.append("preview (not applied — pass --apply once prepare/commit/verify lands):")
            lines.append(_indented_json(preview))

    if result.warnings:
        lines.append("")
        lines.append("warnings (webglass):")
        for warning in result.warnings:
            lines.append(f"  - {warning}")

    lines.extend(_console_summary_lines(result))

    if result.content.untrusted:
        lines.append("")
        lines.append("content (untrusted):")
        lines.append(_indented_json(result.content.untrusted))

    return "\n".join(lines)


#: Printed when a console observation found nothing. The wording is
#: load-bearing: "a clean page yields an *explicitly empty* error list" (spec
#: honesty h24) has to be visible in text mode too, not only as a ``[]`` in
#: JSON. Silence is the one answer a dead-canvas hunt must never get.
NO_CONSOLE_OUTPUT_MARKER = "no console messages and no uncaught page errors were observed"


def _console_summary_lines(result: WebOperationResult) -> list[str]:
    """A WebGlass-authored *count* of the page's console output, never its text.

    Only emitted when the operation actually observed the console — the
    ``console`` lens always reports both lists, so an empty pair there means
    "observed, and there was nothing", while their absence means "this
    operation did not look". Those are different facts and this renders them
    differently.

    The counts are WebGlass's own measurements, so they belong above the
    untrusted section; the messages themselves stay below it, under "content
    (untrusted)", where a page logging ``WEBGLASS WARNING: ...`` cannot be
    mistaken for a WebGlass diagnostic (spec honesty h28).
    """
    untrusted = result.content.untrusted
    messages = untrusted.get("console_messages")
    errors = untrusted.get("page_errors")
    if not isinstance(messages, list) or not isinstance(errors, list):
        return []
    lines = ["", "console (untrusted page output):"]
    if not messages and not errors:
        lines.append(f"  {NO_CONSOLE_OUTPUT_MARKER}")
    else:
        lines.append(f"  {len(messages)} console message(s), {len(errors)} uncaught page error(s)")
        lines.append("  their text is page-authored and appears under content (untrusted) below")
    return lines


def _indented_json(payload: Any) -> str:
    rendered = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    return "\n".join(f"  {line}" for line in rendered.splitlines())
