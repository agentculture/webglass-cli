"""Characterization tests for ``webglass.context``, and the shared shape of
``webglass.exploration`` / ``webglass.memory`` (build plan task t8).

Covers the "WebContext holds only references and policy" invariant from
``CLAUDE.md`` "Target architecture" section 2 (issue #1 implementation spec
claim c3, honesty h3): constructing a :class:`WebContext` never touches a
store, deriving a reduced-capability child never mutates the parent, and
``exploration.py`` / ``memory.py`` stay pure typed interface stubs deferred
to M3 (issue #8) with no implementation entangled.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from pathlib import Path
from typing import Protocol

import pytest

import webglass
import webglass.exploration as exploration_module
import webglass.memory as memory_module
from webglass.context import WebContext
from webglass.exploration import ExplorationStore
from webglass.memory import MemoryStore

_PACKAGE_ROOT = Path(webglass.__file__).resolve().parent

_STATE_MODULES = {"sessions", "context", "exploration", "memory"}
_SIBLING_MODULES = {
    "operations",
    "results",
    "effects",
    "policy",
    "pages",
    "extraction",
    "references",
    "adapters",
    "service",
    "artifacts",
}

# Type-name substrings that would indicate WebContext leaked a concrete
# store/record type into its field annotations instead of an opaque
# reference (str / int / float / Any).
_STORE_TYPE_NAME_MARKERS = (
    "SessionRecord",
    "SessionStore",
    "InMemorySessionStore",
    "ExplorationStore",
    "MemoryStore",
)


def _import_tokens(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    tokens: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                tokens.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                tokens.add(node.module)
            elif node.level:
                for alias in node.names:
                    tokens.add(alias.name)
    return tokens


def _root_component(token: str) -> str:
    if token.startswith("webglass."):
        token = token[len("webglass.") :]
    return token.split(".")[0]


def _assert_no_forbidden_imports(module_filename: str, *, own_name: str) -> None:
    path = _PACKAGE_ROOT / module_filename
    tokens = {_root_component(token) for token in _import_tokens(path)}
    forbidden = (_STATE_MODULES - {own_name}) | _SIBLING_MODULES
    offenders = tokens & forbidden
    assert not offenders, f"{module_filename} must not import: {sorted(offenders)}"


def test_context_module_has_no_cross_state_or_sibling_imports() -> None:
    _assert_no_forbidden_imports("context.py", own_name="context")


def test_exploration_module_has_no_cross_state_or_sibling_imports() -> None:
    _assert_no_forbidden_imports("exploration.py", own_name="exploration")


def test_memory_module_has_no_cross_state_or_sibling_imports() -> None:
    _assert_no_forbidden_imports("memory.py", own_name="memory")


def test_context_fields_reference_no_store_or_record_types() -> None:
    """WebContext fields are opaque references/policy, never store objects."""
    for f in dataclasses.fields(WebContext):
        annotation = str(f.type)
        for marker in _STORE_TYPE_NAME_MARKERS:
            assert marker not in annotation, (
                f"WebContext.{f.name} annotation {annotation!r} references a "
                f"concrete store/record type ({marker}) — it must hold only an "
                "opaque reference"
            )


# --- WebContext construction and derivation -------------------------------


def _make_context(**overrides: object) -> WebContext:
    fields = dict(
        caller="colleague",
        task="task-1",
        workspace="/workspace/task-1",
        policy_profile_ref="policy-ref-1",
        evidence_namespace="ns-1",
    )
    fields.update(overrides)
    return WebContext(**fields)  # type: ignore[arg-type]


def test_minimal_construction_defaults_optional_fields_to_none() -> None:
    ctx = _make_context()
    assert ctx.session_id is None
    assert ctx.exploration_id is None
    assert ctx.memory_read_scope is None
    assert ctx.memory_write_scope is None
    assert ctx.request_budget is None
    assert ctx.byte_budget is None
    assert ctx.time_budget_seconds is None
    assert ctx.token_budget is None


def test_construction_is_pure_and_touches_no_store() -> None:
    """Constructing WebContext performs no I/O and instantiates no store.

    Structurally guaranteed by ``test_context_module_has_no_cross_state_or_sibling_imports``
    (context.py imports no store module at all, so it has nothing to
    instantiate) -- this test additionally checks the *runtime* behavior: a
    plain dataclass construction with no side effects observable via its
    public state.
    """
    ctx = _make_context(session_id="s1", exploration_id="e1")
    assert ctx.session_id == "s1"
    assert ctx.exploration_id == "e1"
    # A WebContext is just data: it round-trips through dataclasses.asdict
    # with no custom __post_init__ side effects to worry about.
    as_dict = dataclasses.asdict(ctx)
    assert as_dict["session_id"] == "s1"


def test_web_context_is_frozen() -> None:
    ctx = _make_context()
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.session_id = "s1"  # type: ignore[misc]


def test_with_reduced_derives_child_dropping_session_reference() -> None:
    parent = _make_context(session_id="s1", exploration_id="e1", evidence_namespace="parent-ns")

    child = parent.with_reduced(session_id=None, evidence_namespace="child-ns")

    assert child is not parent
    assert child.session_id is None
    assert child.evidence_namespace == "child-ns"
    # Everything not overridden carries over unchanged.
    assert child.exploration_id == "e1"
    assert child.caller == parent.caller
    assert child.task == parent.task
    assert child.workspace == parent.workspace
    assert child.policy_profile_ref == parent.policy_profile_ref

    # The parent is never mutated by deriving a child.
    assert parent.session_id == "s1"
    assert parent.evidence_namespace == "parent-ns"


def test_with_reduced_can_narrow_budgets() -> None:
    parent = _make_context(request_budget=100, token_budget=5000)

    child = parent.with_reduced(request_budget=10, token_budget=500)

    assert child.request_budget == 10
    assert child.token_budget == 500
    assert parent.request_budget == 100
    assert parent.token_budget == 5000


# --- exploration.py / memory.py: typed stubs only -------------------------


def _own_classes(module: object) -> list[type]:
    """Classes defined *in* ``module`` (not merely imported into it)."""
    return [
        obj
        for _, obj in inspect.getmembers(module, inspect.isclass)
        if obj.__module__ == module.__name__  # type: ignore[attr-defined]
    ]


def test_exploration_module_defines_only_the_protocol_no_implementation() -> None:
    classes = _own_classes(exploration_module)
    assert classes == [ExplorationStore], (
        "exploration.py must contain only the ExplorationStore Protocol stub — "
        f"found: {[c.__name__ for c in classes]}"
    )


def test_memory_module_defines_only_the_protocol_no_implementation() -> None:
    classes = _own_classes(memory_module)
    assert classes == [MemoryStore], (
        "memory.py must contain only the MemoryStore Protocol stub — "
        f"found: {[c.__name__ for c in classes]}"
    )


def test_exploration_and_memory_docstrings_declare_m3_deferral() -> None:
    for module in (exploration_module, memory_module):
        assert module.__doc__ is not None
        assert "M3" in module.__doc__
        assert "issue #8" in module.__doc__ or "issue 8" in module.__doc__


def test_exploration_store_is_a_protocol_and_runtime_checkable() -> None:
    assert issubclass(ExplorationStore, Protocol)  # type: ignore[misc]
    # isinstance() on a non-runtime-checkable Protocol raises TypeError;
    # this must not raise, proving @runtime_checkable was applied.
    assert isinstance(object(), ExplorationStore) is False

    class FakeExploration:
        def record_edge(self, exploration_id, *, parent_ref, child_ref, reason, status, now):
            return None

        def get_graph(self, exploration_id):
            return None

        def resume(self, exploration_id):
            return None

    assert isinstance(FakeExploration(), ExplorationStore) is True


def test_memory_store_is_a_protocol_and_runtime_checkable() -> None:
    assert issubclass(MemoryStore, Protocol)  # type: ignore[misc]
    assert isinstance(object(), MemoryStore) is False

    class FakeMemory:
        def find(self, query, *, scope):
            return None

        def show(self, memory_id):
            return None

        def forget(self, memory_id):
            return None

        def compact(self, *, scope, now):
            return None

    assert isinstance(FakeMemory(), MemoryStore) is True


def test_exploration_module_imports_cleanly_with_no_module_level_state() -> None:
    # A fresh, successful import (already exercised by the module-level
    # `import webglass.exploration` above) plus: no non-callable, non-dunder
    # module attributes beyond the Protocol class and its typing imports --
    # i.e. no accidental in-memory store/dict living at module scope.
    disallowed_state = [
        name
        for name, value in vars(exploration_module).items()
        if not name.startswith("_")
        and name != "ExplorationStore"
        and not inspect.ismodule(value)
        and not callable(value)
        and name not in {"annotations"}
    ]
    assert not disallowed_state, f"unexpected module-level state: {disallowed_state}"


def test_memory_module_imports_cleanly_with_no_module_level_state() -> None:
    disallowed_state = [
        name
        for name, value in vars(memory_module).items()
        if not name.startswith("_")
        and name != "MemoryStore"
        and not inspect.ismodule(value)
        and not callable(value)
        and name not in {"annotations"}
    ]
    assert not disallowed_state, f"unexpected module-level state: {disallowed_state}"
