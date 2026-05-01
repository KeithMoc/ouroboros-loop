"""Tests for ``ouroboros.mcp.tools.execution_handlers``.

This module focuses on the Q4.1 / AC-2 caller-vs-seed mode resolution
inside :class:`ExecuteSeedHandler`:

* matching caller+seed mode emits no warning / event / counter increment;
* caller-wins on disagreement (warning + event + ``_mode_conflict_counter``
  incremented to 1);
* seed-wins when the caller omits ``mode``;
* the soft-flip global default (``parallel`` → ``compounding``) emits its
  one-time-per-session deprecation warning on the first no-mode call;
* the soft-flip warning fires only **once** across two consecutive
  same-session calls — the module-level
  ``_SOFT_FLIP_WARNED_THIS_SESSION`` flag is the suppression mechanism;
* an invalid caller mode returns ``Result.err`` with the original error
  shape (``MCPToolError`` with ``"Invalid mode"`` in the message).

These are sub-AC-4 of AC-2 (Q4.1 hardening cycle).  Tests 28–33 in the
overall AC-2 numbering — the prior 27 tests live in the seed-schema and
event-factory modules.

Strategy
--------
The handler does its mode-resolution work *before* any agent runtime is
constructed (see ``execution_handlers.py:386–465``).  We exercise the
resolution by:

1. injecting a real in-memory :class:`EventStore` so we can verify any
   ``mcp.execute_seed.mode_conflict`` events that get emitted;
2. patching :func:`create_agent_runtime` to raise a sentinel — this
   forces the handler to bail out at ``execution_handlers.py:~568``,
   *after* the resolution + event-flush block has already executed,
   *before* any real agent / runner / checkpoint construction; and
3. capturing structlog events with :func:`structlog.testing.capture_logs`
   so we can assert on the warning shape.

[[INVARIANT: caller-wins on conflict — seed_mode is recorded but not honored]]
[[INVARIANT: soft-flip default warning fires at most once per session per process]]
[[INVARIANT: invalid caller mode returns Result.err(MCPToolError) with "Invalid mode" prefix]]
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest
import structlog

from ouroboros.mcp.errors import MCPToolError
from ouroboros.mcp.tools import execution_handlers as _exec_module
from ouroboros.mcp.tools.execution_handlers import ExecuteSeedHandler
from ouroboros.orchestrator import _q41_state
from ouroboros.persistence.event_store import EventStore


# ---------------------------------------------------------------------------
# Seed YAML fixtures
# ---------------------------------------------------------------------------
# Two near-identical seeds: one declares ``execution_mode_required`` and one
# leaves it unset.  Keeping the YAML inline (instead of pulling from the
# project's seed dir) keeps these tests hermetic and avoids surprise breakage
# when real seeds get re-tuned.
_SEED_NO_MODE = """\
goal: Q4.1 AC-2 mode-resolution test seed
constraints:
  - Python 3.10+
acceptance_criteria:
  - Handler resolves mode without crashing
ontology_schema:
  name: TestOntology
  description: Test ontology
  fields:
    - name: test_field
      field_type: string
      description: A test field
evaluation_principles: []
exit_conditions: []
metadata:
  seed_id: seed-q41-ac2-no-mode
  version: "1.0.0"
  created_at: "2026-04-29T00:00:00Z"
  ambiguity_score: 0.1
  interview_id: null
"""

_SEED_COMPOUNDING_REQUIRED = """\
goal: Q4.1 AC-2 mode-resolution test seed (compounding)
constraints:
  - Python 3.10+
acceptance_criteria:
  - Seed declares execution_mode_required=compounding
ontology_schema:
  name: TestOntology
  description: Test ontology
  fields:
    - name: test_field
      field_type: string
      description: A test field
evaluation_principles: []
exit_conditions: []
metadata:
  seed_id: seed-q41-ac2-compounding
  version: "1.0.0"
  created_at: "2026-04-29T00:00:00Z"
  ambiguity_score: 0.1
  interview_id: null
  execution_mode_required: compounding
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def memory_event_store() -> AsyncIterator[EventStore]:
    """Provide an initialized in-memory event store; close it after the test."""
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    try:
        yield store
    finally:
        await store.close()


@pytest.fixture(autouse=True)
def _reset_module_state() -> AsyncIterator[None]:
    """Reset the module-level resolution state between tests.

    ``_SOFT_FLIP_WARNED_THIS_SESSION`` is a process-lifetime singleton; if
    one test trips it, a later test that depends on the warning firing on
    the *first* no-mode call would silently fail.  Same story for the
    ``_mode_conflict_counter`` dict — leftover entries would corrupt
    subsequent assertions on counter values.

    Q4.1 / AC-4 sub-AC 5 moved the canonical state to
    :mod:`ouroboros.orchestrator._q41_state` to break the
    runner ↔ execution_handlers import cycle.  The dict alias on
    ``execution_handlers`` is by-reference so ``.clear()`` from either
    module is visible from the other; the bool is reset on the canonical
    module since it is rebound by the write site.
    """
    _q41_state._SOFT_FLIP_WARNED_THIS_SESSION = False
    _q41_state._mode_conflict_counter.clear()
    yield
    _q41_state._SOFT_FLIP_WARNED_THIS_SESSION = False
    _q41_state._mode_conflict_counter.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _AgentRuntimeBail(RuntimeError):
    """Sentinel raised by the patched ``create_agent_runtime``.

    Forces the handler to exit at the runtime-construction line *after*
    mode resolution + event-flush have already executed, so the test can
    assert on those side effects without running the real runner.
    """


def _patch_runtime_to_bail():
    """Return a patch context that makes ``create_agent_runtime`` raise."""
    return patch.object(
        _exec_module,
        "create_agent_runtime",
        side_effect=_AgentRuntimeBail("test bail — runtime construction blocked"),
    )


# ---------------------------------------------------------------------------
# AC-2 sub-AC-4 — Tests 28–33
# ---------------------------------------------------------------------------


class TestExecuteSeedModeResolution:
    """Mode-resolution tests for :class:`ExecuteSeedHandler` (Q4.1 / AC-2).

    Each test exercises one branch of the resolution policy declared in
    ``execution_handlers.py:386–465``:

    +---------------------+----------------+------------------------------+
    | caller_mode         | seed_mode      | expected resolution          |
    +=====================+================+==============================+
    | "compounding"       | "compounding"  | "compounding" — silent       |
    | "parallel"          | "compounding"  | "parallel"   — warn + event  |
    | None                | "compounding"  | "compounding" — silent       |
    | None                | None           | "compounding" — soft-flip    |
    | "bogus"             | (any)          | Result.err                   |
    +---------------------+----------------+------------------------------+

    Soft-flip suppression is tested separately (test 32) since it spans
    two consecutive handler calls in the same session.
    """

    async def test_28_matching_caller_and_seed_mode_emits_nothing(
        self,
        memory_event_store: EventStore,
    ) -> None:
        """Test 28: caller="compounding" + seed=compounding → no warning, no event, no counter.

        When caller and seed agree there is no conflict and no need for a
        deprecation warning — the resolution is silent.  This is the
        "happy path" for an external caller that has correctly read the
        seed's metadata before invoking the tool.
        """
        handler = ExecuteSeedHandler(event_store=memory_event_store)

        with structlog.testing.capture_logs() as captured, _patch_runtime_to_bail():
            await handler.handle(
                {
                    "seed_content": _SEED_COMPOUNDING_REQUIRED,
                    "mode": "compounding",
                }
            )

        # No mode-resolution log lines at all — neither conflict nor soft-flip.
        relevant = [
            evt
            for evt in captured
            if evt.get("event")
            in {"mcp.execute_seed.mode_conflict", "mcp.execute_seed.default_flipped"}
        ]
        assert relevant == []

        # No conflict event in the store either.
        conflict_events = await memory_event_store.query_events(
            event_type="mcp.execute_seed.mode_conflict",
        )
        assert conflict_events == []

        # Counter unchanged.
        assert _exec_module._mode_conflict_counter == {}

    async def test_29_caller_wins_on_disagreement_warns_and_emits_event(
        self,
        memory_event_store: EventStore,
    ) -> None:
        """Test 29: caller="parallel" + seed="compounding" → caller wins.

        Caller-wins-and-warn policy.  A disagreement must surface in three
        places at once (the Q2.1 lesson: silent mode-mismatches are the
        worst class of gap):

        1. structlog warning ``mcp.execute_seed.mode_conflict``;
        2. structured event of the same name in the event store, scoped
           to ``aggregate_id == session_id``; and
        3. ``_mode_conflict_counter[session_id] == 1`` for AC-4's Run
           Summary panel to drain at run end.
        """
        handler = ExecuteSeedHandler(event_store=memory_event_store)

        with structlog.testing.capture_logs() as captured, _patch_runtime_to_bail():
            await handler.handle(
                {
                    "seed_content": _SEED_COMPOUNDING_REQUIRED,
                    "mode": "parallel",
                    "session_id": "sess_q41_ac2_test29",
                }
            )

        # 1) structlog warning fired with caller_mode/seed_mode/seed_id payload.
        conflict_logs = [
            evt for evt in captured if evt.get("event") == "mcp.execute_seed.mode_conflict"
        ]
        assert len(conflict_logs) == 1
        assert conflict_logs[0]["caller_mode"] == "parallel"
        assert conflict_logs[0]["seed_mode"] == "compounding"
        assert conflict_logs[0]["seed_id"] == "seed-q41-ac2-compounding"
        assert conflict_logs[0]["log_level"] == "warning"

        # 2) Structured event in the store, keyed on session_id.
        conflict_events = await memory_event_store.query_events(
            event_type="mcp.execute_seed.mode_conflict",
        )
        assert len(conflict_events) == 1
        evt = conflict_events[0]
        assert evt.aggregate_type == "session"
        assert evt.aggregate_id == "sess_q41_ac2_test29"
        assert evt.data["caller_mode"] == "parallel"
        assert evt.data["seed_mode"] == "compounding"
        assert evt.data["seed_id"] == "seed-q41-ac2-compounding"

        # 3) Session-scoped counter for AC-4's Run Summary panel.
        assert _exec_module._mode_conflict_counter["sess_q41_ac2_test29"] == 1

    async def test_30_seed_wins_when_caller_omits_mode(
        self,
        memory_event_store: EventStore,
    ) -> None:
        """Test 30: caller omits mode, seed declares compounding → seed wins, silent.

        Per the design doc, when only one side specifies a mode the other
        defers to it without surfacing anything.  This is the
        "well-annotated seed" path: the seed encodes the mode it needs
        and external callers can rely on the metadata.
        """
        handler = ExecuteSeedHandler(event_store=memory_event_store)

        with structlog.testing.capture_logs() as captured, _patch_runtime_to_bail():
            await handler.handle(
                {
                    "seed_content": _SEED_COMPOUNDING_REQUIRED,
                    # NB: ``mode`` deliberately omitted.
                    "session_id": "sess_q41_ac2_test30",
                }
            )

        # No conflict, no soft-flip — the seed answered the question.
        noisy = [
            evt
            for evt in captured
            if evt.get("event")
            in {"mcp.execute_seed.mode_conflict", "mcp.execute_seed.default_flipped"}
        ]
        assert noisy == []

        # No conflict event persisted.
        conflict_events = await memory_event_store.query_events(
            event_type="mcp.execute_seed.mode_conflict",
        )
        assert conflict_events == []

        # Soft-flip flag must remain pristine — seed answered, default unused.
        assert _q41_state._SOFT_FLIP_WARNED_THIS_SESSION is False

        # Counter never grew.
        assert _exec_module._mode_conflict_counter == {}

    async def test_31_soft_flip_default_warning_fires_on_first_no_mode_call(
        self,
        memory_event_store: EventStore,
    ) -> None:
        """Test 31: caller omits mode AND seed silent → soft-flip warning fires.

        Both sides silent → the handler defaults to ``"compounding"``
        (Q4.1 soft-flipped global default; was ``"parallel"`` pre-Q4.1).
        That branch fires a one-time-per-session deprecation warning so
        external MCP callers learn to set ``mode`` or
        ``metadata.execution_mode_required`` explicitly.

        The flag flip from ``False`` → ``True`` is the suppression
        mechanism for subsequent calls (test 32).
        """
        # Pre-condition: the autouse fixture cleared the flag.
        assert _q41_state._SOFT_FLIP_WARNED_THIS_SESSION is False

        handler = ExecuteSeedHandler(event_store=memory_event_store)

        with structlog.testing.capture_logs() as captured, _patch_runtime_to_bail():
            await handler.handle(
                {
                    "seed_content": _SEED_NO_MODE,
                    # No ``mode`` arg.
                    "session_id": "sess_q41_ac2_test31",
                }
            )

        # Exactly one default_flipped warning, with the deprecation message.
        flip_logs = [
            evt for evt in captured if evt.get("event") == "mcp.execute_seed.default_flipped"
        ]
        assert len(flip_logs) == 1
        assert flip_logs[0]["log_level"] == "warning"
        assert "compounding" in flip_logs[0]["message"]
        assert "parallel" in flip_logs[0]["message"]

        # No conflict (no caller mode + no seed mode = no disagreement).
        assert not any(
            evt.get("event") == "mcp.execute_seed.mode_conflict" for evt in captured
        )

        # The suppression flag flipped to True so subsequent calls in this
        # process are silent.
        assert _q41_state._SOFT_FLIP_WARNED_THIS_SESSION is True

    async def test_32_soft_flip_warning_fires_only_once_across_two_calls(
        self,
        memory_event_store: EventStore,
    ) -> None:
        """Test 32: two consecutive same-session no-mode calls → exactly one warning.

        The soft-flip flag is a process-wide singleton (deliberately
        un-synchronized — see the design doc's risk register on benign
        races).  The contract: the first no-mode call in a process emits
        the deprecation; subsequent calls — same session or different —
        are silent.
        """
        handler = ExecuteSeedHandler(event_store=memory_event_store)

        # First call — should emit the warning.
        with structlog.testing.capture_logs() as captured1, _patch_runtime_to_bail():
            await handler.handle(
                {
                    "seed_content": _SEED_NO_MODE,
                    "session_id": "sess_q41_ac2_test32_call1",
                }
            )

        flip_logs_call1 = [
            evt for evt in captured1 if evt.get("event") == "mcp.execute_seed.default_flipped"
        ]
        assert len(flip_logs_call1) == 1, (
            f"first no-mode call must emit the soft-flip warning; got: {flip_logs_call1}"
        )

        # Suppression flag is now set.
        assert _q41_state._SOFT_FLIP_WARNED_THIS_SESSION is True

        # Second call — must be silent (no second warning).
        with structlog.testing.capture_logs() as captured2, _patch_runtime_to_bail():
            await handler.handle(
                {
                    "seed_content": _SEED_NO_MODE,
                    "session_id": "sess_q41_ac2_test32_call2",
                }
            )

        flip_logs_call2 = [
            evt for evt in captured2 if evt.get("event") == "mcp.execute_seed.default_flipped"
        ]
        assert flip_logs_call2 == [], (
            f"second no-mode call must NOT re-emit the soft-flip warning; got: {flip_logs_call2}"
        )

    async def test_33_invalid_caller_mode_returns_result_err(
        self,
        memory_event_store: EventStore,
    ) -> None:
        """Test 33: ``mode="bogus"`` returns Result.err with the original error shape.

        Invalid-mode validation runs *after* resolution so the rejection
        message reflects the resolved value (an opaque caller-supplied
        string here).  The contract is unchanged from pre-Q4.1: caller
        gets a ``Result.err`` wrapping a :class:`MCPToolError` whose
        message starts with ``"Invalid mode"``.
        """
        handler = ExecuteSeedHandler(event_store=memory_event_store)

        # No runtime patch needed — the early-return at the validation
        # block fires before ``create_agent_runtime`` is reached.
        with structlog.testing.capture_logs():
            result = await handler.handle(
                {
                    "seed_content": _SEED_NO_MODE,
                    "mode": "bogus",
                    "session_id": "sess_q41_ac2_test33",
                }
            )

        assert result.is_err
        # Original error shape: MCPToolError with "Invalid mode" prefix and
        # the offending value preserved verbatim.
        assert isinstance(result.error, MCPToolError)
        assert "Invalid mode" in str(result.error)
        assert "'bogus'" in str(result.error)
        assert result.error.tool_name == "ouroboros_execute_seed"

        # Mode-resolution side effects must NOT have leaked: the bogus
        # caller mode was accepted by the resolver (caller_mode is not
        # None and there is no seed mode to compare to), so no conflict
        # warning, no soft-flip warning, no counter increment.
        assert _exec_module._mode_conflict_counter == {}
        assert _q41_state._SOFT_FLIP_WARNED_THIS_SESSION is False
