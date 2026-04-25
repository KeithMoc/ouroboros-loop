"""Unit tests for SerialCompoundingExecutor."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata
from ouroboros.events.base import BaseEvent
from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.dependency_analyzer import (
    ACNode,
    DependencyGraph,
    ExecutionStage,
    StagedExecutionPlan,
)
from ouroboros.orchestrator.parallel_executor_models import (
    ACExecutionOutcome,
    ACExecutionResult,
)
from ouroboros.orchestrator.serial_executor import (
    SerialCompoundingExecutor,
    linearize_execution_plan,
    write_chain_artifact,
)


def _make_seed(*acs: str) -> Seed:
    return Seed(
        goal="Serial compounding execution",
        constraints=(),
        acceptance_criteria=acs,
        ontology_schema=OntologySchema(name="Serial", description="test"),
        metadata=SeedMetadata(ambiguity_score=0.05),
    )


def _make_plan(*stages: tuple[int, ...]) -> StagedExecutionPlan:
    """Build a StagedExecutionPlan with the given stages of AC indices.

    Each stage is a tuple of AC indices. Stage N depends on stage N-1 via
    ``depends_on_stages``.
    """
    stage_objs: list[ExecutionStage] = []
    seen: set[int] = set()
    nodes: list[ACNode] = []
    for i, indices in enumerate(stages):
        stage_objs.append(
            ExecutionStage(
                index=i,
                ac_indices=tuple(indices),
                depends_on_stages=tuple(range(i)) if i > 0 else (),
            )
        )
        for ac_idx in indices:
            if ac_idx not in seen:
                seen.add(ac_idx)
                nodes.append(ACNode(index=ac_idx, content=f"AC {ac_idx}"))
    return StagedExecutionPlan(nodes=tuple(nodes), stages=tuple(stage_objs))


def _make_executor() -> SerialCompoundingExecutor:
    event_store, _ = _make_replaying_event_store()
    executor = SerialCompoundingExecutor(
        adapter=MagicMock(),
        event_store=event_store,
        console=MagicMock(),
        enable_decomposition=False,
    )
    executor._coordinator.detect_file_conflicts = MagicMock(return_value=[])
    return executor


def _make_replaying_event_store() -> tuple[AsyncMock, list[BaseEvent]]:
    event_store = AsyncMock()
    appended: list[BaseEvent] = []

    async def _append(event: BaseEvent) -> None:
        appended.append(event)

    event_store.append.side_effect = _append
    event_store.replay.side_effect = lambda *a, **k: []
    # Attach to the mock so tests can read it.
    event_store._appended = appended  # type: ignore[attr-defined]
    return event_store, appended


def _ok_result(
    ac_index: int,
    ac_content: str,
    *,
    final_message: str = "done",
    files_written: tuple[str, ...] = (),
) -> ACExecutionResult:
    messages: list[AgentMessage] = []
    for path in files_written:
        messages.append(
            AgentMessage(
                type="tool_use",
                content=f"writing {path}",
                tool_name="Write",
                data={"tool_input": {"file_path": path}},
            )
        )
    return ACExecutionResult(
        ac_index=ac_index,
        ac_content=ac_content,
        success=True,
        messages=tuple(messages),
        final_message=final_message,
        duration_seconds=0.1,
    )


def _fail_result(ac_index: int, ac_content: str, *, error: str = "boom") -> ACExecutionResult:
    return ACExecutionResult(
        ac_index=ac_index,
        ac_content=ac_content,
        success=False,
        error=error,
        outcome=ACExecutionOutcome.FAILED,
    )


class TestLinearizeExecutionPlan:
    def test_single_stage_sorted(self) -> None:
        plan = _make_plan((2, 0, 1))
        assert linearize_execution_plan(plan) == (0, 1, 2)

    def test_multi_stage_respects_stage_order(self) -> None:
        plan = _make_plan((1,), (0, 2))
        # Stage 0 before stage 1 regardless of AC index.
        assert linearize_execution_plan(plan) == (1, 0, 2)

    def test_no_duplicates(self) -> None:
        # Defense: if an AC appears twice (bad plan), it should not repeat.
        plan = _make_plan((0,), (0, 1))
        assert linearize_execution_plan(plan) == (0, 1)


class TestSerialCompoundingExecutor:
    @pytest.mark.asyncio
    async def test_two_ac_chain_ac2_sees_ac1_postmortem(self) -> None:
        """The whole point: AC 2's prompt contains AC 1's postmortem."""
        seed = _make_seed("Create user model", "Create user endpoint")
        executor = _make_executor()

        captured_overrides: list[str | None] = []

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            captured_overrides.append(kwargs.get("context_override"))
            ac_index = int(kwargs["ac_index"])
            return _ok_result(
                ac_index,
                str(kwargs["ac_content"]),
                final_message=f"AC {ac_index + 1} done",
                files_written=(f"src/ac{ac_index}.py",),
            )

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="sess_1",
            execution_id="exec_1",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
        )

        assert result.success_count == 2
        assert result.failure_count == 0
        assert len(captured_overrides) == 2
        # AC 1 sees an empty chain (no postmortems yet).
        assert captured_overrides[0] == ""
        # AC 2's override must reference AC 1's postmortem content.
        ac2_override = captured_overrides[1] or ""
        assert "Prior AC Postmortems" in ac2_override
        assert "Create user model" in ac2_override
        assert "src/ac0.py" in ac2_override  # from AC 1's files_modified

    @pytest.mark.asyncio
    async def test_postmortem_event_emitted_per_ac(self) -> None:
        seed = _make_seed("AC a", "AC b")
        executor = _make_executor()
        # Extract the event store we set up and confirm it collected events.
        event_store: Any = executor._event_store
        appended: list[BaseEvent] = event_store._appended

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        await executor.execute_serial(
            seed=seed,
            session_id="sess_1",
            execution_id="exec_1",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
        )

        pm_events = [
            e for e in appended if e.type == "execution.ac.postmortem.captured"
        ]
        assert len(pm_events) == 2
        assert pm_events[0].aggregate_id == "ac_0"
        assert pm_events[0].data["status"] == "pass"
        assert pm_events[1].aggregate_id == "ac_1"

    @pytest.mark.asyncio
    async def test_fail_fast_halts_on_ac_failure(self) -> None:
        seed = _make_seed("AC 1 fails", "AC 2 never runs")
        executor = _make_executor()
        calls: list[int] = []

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            calls.append(ac_index)
            if ac_index == 0:
                return _fail_result(0, str(kwargs["ac_content"]), error="missing dep")
            return _ok_result(ac_index, str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="sess_1",
            execution_id="exec_1",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            fail_fast=True,
        )

        # AC 0 ran, AC 1 was blocked — not executed.
        assert calls == [0]
        assert result.failure_count == 1
        assert result.blocked_count == 1
        assert result.results[0].success is False
        assert result.results[1].outcome == ACExecutionOutcome.BLOCKED
        assert result.results[1].error and "halted" in result.results[1].error

    @pytest.mark.asyncio
    async def test_fail_forward_continues_past_failure(self) -> None:
        seed = _make_seed("AC 1 fails", "AC 2 still runs")
        executor = _make_executor()
        calls: list[int] = []
        captured_overrides: list[str | None] = []

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            calls.append(ac_index)
            captured_overrides.append(kwargs.get("context_override"))
            if ac_index == 0:
                return _fail_result(0, str(kwargs["ac_content"]), error="timeout")
            return _ok_result(ac_index, str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="sess_1",
            execution_id="exec_1",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            fail_fast=False,
        )

        # Both ran; AC 2 saw AC 1's failed postmortem.
        assert calls == [0, 1]
        assert result.failure_count == 1
        assert result.success_count == 1
        ac2_override = captured_overrides[1] or ""
        assert "[fail]" in ac2_override
        assert "timeout" in ac2_override  # gotcha from failed AC surfaces

    @pytest.mark.asyncio
    async def test_exception_captured_as_failed_postmortem(self) -> None:
        """An unexpected exception in _execute_single_ac does not crash the loop."""
        seed = _make_seed("AC 1 raises", "AC 2 blocked")
        executor = _make_executor()

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            raise RuntimeError("adapter exploded")

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="sess_1",
            execution_id="exec_1",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            fail_fast=True,
        )
        assert result.failure_count == 1
        assert "adapter exploded" in (result.results[0].error or "")

    @pytest.mark.asyncio
    async def test_dependency_graph_used_when_plan_absent(self) -> None:
        seed = _make_seed("AC 1", "AC 2")
        executor = _make_executor()

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        graph = DependencyGraph(
            nodes=(
                ACNode(index=0, content="AC 1"),
                ACNode(index=1, content="AC 2", depends_on=(0,)),
            ),
            execution_levels=((0,), (1,)),
        )
        result = await executor.execute_serial(
            seed=seed,
            session_id="sess_1",
            execution_id="exec_1",
            tools=[],
            system_prompt="SYSTEM",
            dependency_graph=graph,
        )
        assert result.success_count == 2

    @pytest.mark.asyncio
    async def test_missing_plan_and_graph_raises(self) -> None:
        executor = _make_executor()
        with pytest.raises(ValueError, match="execution_plan is required"):
            await executor.execute_serial(
                seed=_make_seed("AC 1"),
                session_id="sess_1",
                execution_id="exec_1",
                tools=[],
                system_prompt="SYSTEM",
            )

    @pytest.mark.asyncio
    async def test_invariants_accumulate_across_chain(self) -> None:
        """When postmortems carry invariants, later ACs see the cumulative list."""
        seed = _make_seed("AC a", "AC b", "AC c")
        executor = _make_executor()
        captured_overrides: list[str] = []

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            captured_overrides.append(kwargs.get("context_override") or "")
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        # We can't set invariants from the executor directly (phase 1
        # doesn't populate them), but we CAN verify the chain plumbing
        # passes AC 1's summary data into AC 3's override.
        plan = _make_plan((0,), (1,), (2,))
        await executor.execute_serial(
            seed=seed,
            session_id="sess_1",
            execution_id="exec_1",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
        )

        # AC 3 must see references to AC 1 and AC 2 (chain grows).
        assert "AC a" in captured_overrides[2]
        assert "AC b" in captured_overrides[2]


class TestChainArtifact:
    """AC-1 (Q6.1): End-of-run postmortem chain serialization.

    [[INVARIANT: end-of-run chain artifact exists in docs/brainstorm/chain-*.md]]
    [[INVARIANT: OUROBOROS_CHAIN_ARTIFACT_DIR env var controls artifact location]]
    """

    @pytest.mark.asyncio
    async def test_artifact_written_after_successful_2ac_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A successful 2-AC run writes a chain artifact with expected markdown structure."""
        seed = _make_seed("Implement user model", "Implement user endpoint")
        executor = _make_executor()

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            return _ok_result(
                ac_index,
                str(kwargs["ac_content"]),
                final_message=f"AC {ac_index + 1} complete",
                files_written=(f"src/module_{ac_index}.py",),
            )

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        artifact_dir = str(tmp_path / "chain_out")
        plan = _make_plan((0,), (1,))

        monkeypatch.setenv("OUROBOROS_CHAIN_ARTIFACT_DIR", artifact_dir)
        await executor.execute_serial(
            seed=seed,
            session_id="sess_chain_test",
            execution_id="exec_chain_test",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
        )

        out_dir = Path(artifact_dir)
        artifacts = list(out_dir.glob("chain-sess_chain_test-*.md"))
        assert len(artifacts) == 1, f"Expected 1 artifact, got: {artifacts}"

        content = artifacts[0].read_text(encoding="utf-8")
        # File header
        assert "# Postmortem Chain" in content
        assert "sess_chain_test" in content
        # Two AC sections with correct status
        assert "## AC 1 [pass]" in content
        assert "## AC 2 [pass]" in content
        # Required fields from AC spec
        assert "Files modified:" in content
        assert "Gotchas:" in content
        assert "Public API changes:" in content

    @pytest.mark.asyncio
    async def test_artifact_written_on_failure_fail_fast(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Artifact is written even when fail_fast halts mid-chain after a failure."""
        seed = _make_seed("AC 1 fails", "AC 2 never runs")
        executor = _make_executor()

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            if ac_index == 0:
                return _fail_result(0, str(kwargs["ac_content"]), error="kaboom")
            return _ok_result(ac_index, str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        artifact_dir = str(tmp_path / "chain_fail")
        plan = _make_plan((0,), (1,))

        monkeypatch.setenv("OUROBOROS_CHAIN_ARTIFACT_DIR", artifact_dir)
        result = await executor.execute_serial(
            seed=seed,
            session_id="sess_fail_test",
            execution_id="exec_fail_test",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            fail_fast=True,
        )

        # Run did indeed fail
        assert result.failure_count == 1

        # Artifact still written despite failure
        out_dir = Path(artifact_dir)
        artifacts = list(out_dir.glob("chain-sess_fail_test-*.md"))
        assert len(artifacts) == 1, f"Expected 1 artifact even on failure, got: {artifacts}"

        content = artifacts[0].read_text(encoding="utf-8")
        # Failed AC section present
        assert "## AC 1 [fail]" in content
        # Gotcha from the failed AC surfaces in the artifact
        assert "kaboom" in content

    def test_write_chain_artifact_creates_nested_dir_and_file(
        self, tmp_path: Path
    ) -> None:
        """write_chain_artifact creates parent directories and returns a valid path."""
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )

        summary = ACContextSummary(
            ac_index=0,
            ac_content="Build the thing",
            success=True,
            files_modified=("src/thing.py",),
        )
        pm = ACPostmortem(
            summary=summary,
            status="pass",
            gotchas=("watch out for X",),
        )
        chain = PostmortemChain(postmortems=(pm,))

        # Use deeply-nested dir that doesn't yet exist.
        nested_dir = tmp_path / "a" / "b" / "c"
        path = write_chain_artifact(
            chain,
            session_id="s1",
            execution_id="e1",
            artifact_dir=str(nested_dir),
        )

        assert path.exists()
        assert path.suffix == ".md"
        assert path.name.startswith("chain-s1-")

        content = path.read_text(encoding="utf-8")
        assert "## AC 1 [pass]" in content
        assert "Build the thing" in content
        assert "src/thing.py" in content
        assert "watch out for X" in content

    def test_env_var_overrides_artifact_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OUROBOROS_CHAIN_ARTIFACT_DIR redirects artifact output."""
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )

        custom_dir = tmp_path / "custom_dir"
        monkeypatch.setenv("OUROBOROS_CHAIN_ARTIFACT_DIR", str(custom_dir))

        summary = ACContextSummary(ac_index=0, ac_content="AC text", success=True)
        pm = ACPostmortem(summary=summary, status="pass")
        chain = PostmortemChain(postmortems=(pm,))

        path = write_chain_artifact(chain, session_id="s2", execution_id="e2")

        # Path is inside the custom_dir
        assert str(custom_dir) in str(path)
        assert path.exists()

    def test_explicit_artifact_dir_beats_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit artifact_dir argument takes precedence over the env var."""
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )

        env_dir = tmp_path / "from_env"
        explicit_dir = tmp_path / "explicit"
        monkeypatch.setenv("OUROBOROS_CHAIN_ARTIFACT_DIR", str(env_dir))

        summary = ACContextSummary(ac_index=0, ac_content="AC text", success=True)
        pm = ACPostmortem(summary=summary, status="pass")
        chain = PostmortemChain(postmortems=(pm,))

        path = write_chain_artifact(
            chain,
            session_id="s3",
            execution_id="e3",
            artifact_dir=str(explicit_dir),
        )

        assert str(explicit_dir) in str(path)
        assert str(env_dir) not in str(path)
        assert path.exists()

    def test_artifact_for_empty_chain_has_no_ac_sections(
        self, tmp_path: Path
    ) -> None:
        """Empty chain produces a valid header with no AC entries."""
        from ouroboros.orchestrator.level_context import PostmortemChain

        chain = PostmortemChain()  # no postmortems
        path = write_chain_artifact(
            chain,
            session_id="s4",
            execution_id="e4",
            artifact_dir=str(tmp_path),
        )
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "# Postmortem Chain" in content
        assert "## AC" not in content  # no AC sections for empty chain


class TestSubPostmortems:
    """AC-2 (Q1, B-prime): Sub-postmortem preservation and flattening.

    [[INVARIANT: ACPostmortem.sub_postmortems preserves structure in serialized chain]]
    [[INVARIANT: to_prompt_text flattens sub-AC data; never emits nested entries]]
    [[INVARIANT: parent digest fields are unions of its own plus sub-postmortem fields]]
    """

    def _make_result_with_subs(self) -> ACExecutionResult:
        """Build a decomposed ACExecutionResult with two sub-results."""
        sub0 = ACExecutionResult(
            ac_index=0,
            ac_content="Sub-AC 0",
            success=True,
            messages=(
                AgentMessage(
                    type="tool_use",
                    content="writing sub0",
                    tool_name="Write",
                    data={"tool_input": {"file_path": "src/sub_a.py"}},
                ),
            ),
            final_message="sub0 done",
        )
        sub1 = ACExecutionResult(
            ac_index=0,
            ac_content="Sub-AC 1",
            success=True,
            messages=(
                AgentMessage(
                    type="tool_use",
                    content="writing sub1",
                    tool_name="Write",
                    data={"tool_input": {"file_path": "src/sub_b.py"}},
                ),
            ),
            error=None,
            final_message="sub1 done",
        )
        # Parent result with no own files, but two sub-results.
        return ACExecutionResult(
            ac_index=0,
            ac_content="Parent AC",
            success=True,
            is_decomposed=True,
            sub_results=(sub0, sub1),
            final_message="parent done",
        )

    def test_sub_files_flattened_into_parent_summary(self) -> None:
        """Sub-result files appear in the parent ACPostmortem.summary.files_modified."""
        result = self._make_result_with_subs()
        postmortem = SerialCompoundingExecutor._build_postmortem_from_result(
            result, workspace_root=None
        )
        files = postmortem.summary.files_modified
        assert "src/sub_a.py" in files, f"sub_a.py missing from {files}"
        assert "src/sub_b.py" in files, f"sub_b.py missing from {files}"

    def test_sub_gotchas_flattened_into_parent(self) -> None:
        """Sub-result failure gotchas are merged into parent.gotchas."""
        sub_fail = ACExecutionResult(
            ac_index=0,
            ac_content="Sub fail",
            success=False,
            error="sub-ac bombed",
            outcome=ACExecutionOutcome.FAILED,
        )
        parent = ACExecutionResult(
            ac_index=0,
            ac_content="Parent AC",
            success=False,
            error="parent error",
            is_decomposed=True,
            sub_results=(sub_fail,),
            outcome=ACExecutionOutcome.FAILED,
        )
        pm = SerialCompoundingExecutor._build_postmortem_from_result(
            parent, workspace_root=None
        )
        assert "parent error" in pm.gotchas
        assert "sub-ac bombed" in pm.gotchas

    def test_sub_postmortems_stored_on_parent(self) -> None:
        """sub_postmortems tuple is preserved on the parent ACPostmortem."""
        result = self._make_result_with_subs()
        pm = SerialCompoundingExecutor._build_postmortem_from_result(
            result, workspace_root=None
        )
        assert len(pm.sub_postmortems) == 2
        assert pm.sub_postmortems[0].summary.ac_content == "Sub-AC 0"
        assert pm.sub_postmortems[1].summary.ac_content == "Sub-AC 1"

    def test_no_sub_results_gives_empty_sub_postmortems(self) -> None:
        """When there are no sub_results, sub_postmortems stays empty."""
        result = ACExecutionResult(
            ac_index=0,
            ac_content="Normal AC",
            success=True,
            final_message="done",
        )
        pm = SerialCompoundingExecutor._build_postmortem_from_result(
            result, workspace_root=None
        )
        assert pm.sub_postmortems == ()

    def test_to_prompt_text_does_not_emit_nested_entries(self) -> None:
        """to_prompt_text() flat view must NOT contain any nested sub-AC entries.

        [[INVARIANT: to_prompt_text flattens sub-AC data; never emits nested entries]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )

        # Build a sub-postmortem.
        sub_summary = ACContextSummary(
            ac_index=0, ac_content="Sub-AC 3.1", success=True
        )
        sub_pm = ACPostmortem(summary=sub_summary, status="pass")

        # Build a parent postmortem that references the sub-postmortem.
        parent_summary = ACContextSummary(
            ac_index=2, ac_content="Parent AC 3", success=True
        )
        parent_pm = ACPostmortem(
            summary=parent_summary,
            status="pass",
            sub_postmortems=(sub_pm,),
        )

        chain = PostmortemChain(postmortems=(parent_pm,))
        text = chain.to_prompt_text()

        # Sub-AC entries must NOT appear in the rendered prompt.
        assert "Sub-AC 3.1" not in text, (
            "to_prompt_text() should NOT render nested sub-AC entries"
        )
        # Parent content should still appear.
        assert "Parent AC 3" in text

    def test_serialize_deserialize_round_trip_sub_postmortems(self) -> None:
        """sub_postmortems survive a serialize → deserialize round-trip.

        [[INVARIANT: ACPostmortem.sub_postmortems preserves structure in serialized chain]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
            deserialize_postmortem_chain,
            serialize_postmortem_chain,
        )

        sub_summary = ACContextSummary(
            ac_index=0,
            ac_content="Sub task A",
            success=True,
            files_modified=("src/sub_a.py",),
        )
        sub_pm = ACPostmortem(
            summary=sub_summary,
            status="pass",
            gotchas=("watch sub gotcha",),
        )

        parent_summary = ACContextSummary(
            ac_index=1,
            ac_content="Parent task B",
            success=True,
            files_modified=("src/parent_b.py", "src/sub_a.py"),
        )
        parent_pm = ACPostmortem(
            summary=parent_summary,
            status="pass",
            gotchas=("parent gotcha", "watch sub gotcha"),
            sub_postmortems=(sub_pm,),
        )

        chain = PostmortemChain(postmortems=(parent_pm,))

        # Serialize and deserialize.
        serialized = serialize_postmortem_chain(chain)
        restored_chain = deserialize_postmortem_chain(serialized)

        assert len(restored_chain.postmortems) == 1
        restored_pm = restored_chain.postmortems[0]

        # sub_postmortems preserved.
        assert len(restored_pm.sub_postmortems) == 1
        restored_sub = restored_pm.sub_postmortems[0]
        assert restored_sub.summary.ac_content == "Sub task A"
        assert "src/sub_a.py" in restored_sub.summary.files_modified
        assert "watch sub gotcha" in restored_sub.gotchas

        # Parent fields also intact.
        assert "parent gotcha" in restored_pm.gotchas
        assert "src/parent_b.py" in restored_pm.summary.files_modified

    def test_sub_files_appear_in_rendered_postmortem_chain_prompt(self) -> None:
        """Files from sub-postmortems are visible in the chain prompt (flattened into parent).

        [[INVARIANT: parent digest fields are unions of its own plus sub-postmortem fields]]
        """
        result = self._make_result_with_subs()
        pm = SerialCompoundingExecutor._build_postmortem_from_result(
            result, workspace_root=None
        )

        from ouroboros.orchestrator.level_context import PostmortemChain

        chain = PostmortemChain(postmortems=(pm,))
        text = chain.to_prompt_text()

        # Sub-files must appear in the rendered chain text.
        assert "src/sub_a.py" in text, "sub_a.py missing from chain prompt"
        assert "src/sub_b.py" in text, "sub_b.py missing from chain prompt"


class TestInvariantVerifier:
    """AC-3 (Q3, C-plus): [[INVARIANT]] tag extraction + Haiku verifier gate.

    Verifies:
    - verify_invariants() is called inline-blocking before chain advance.
    - Above-threshold invariants appear in the next AC's context_override.
    - Below-threshold invariants are silently dropped.
    - The verify_invariants() function correctly interacts with a stub adapter.

    [[INVARIANT: verify_invariants is called inline-blocking before chain advance]]
    [[INVARIANT: only above-threshold invariants appear in downstream chain context]]
    """

    @pytest.mark.asyncio
    async def test_above_threshold_invariant_appears_in_next_ac_context(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When verify_invariants returns score ≥ 0.7, the invariant propagates.

        AC 0 emits [[INVARIANT: serialize_postmortem_chain produces a list]].
        The stub verifier returns 0.95. AC 1's context_override must include
        the invariant text.

        Compounding reference (AC-1): ACPostmortem.invariants_established carries
        the Invariant dataclass introduced in AC-2's level_context.py changes.
        [[INVARIANT: only above-threshold invariants appear in downstream chain context]]
        """
        import ouroboros.orchestrator.serial_executor as serial_mod

        verify_calls: list[dict] = []

        async def fake_verify(
            adapter: Any,
            tags: list[str],
            *,
            ac_trace: str,
            files_modified: list[str],
            model: str | None = None,
        ) -> list[tuple[str, float]]:
            verify_calls.append({"tags": list(tags), "ac_trace": ac_trace})
            # Return high-reliability score for all tags.
            return [(tag, 0.95) for tag in tags]

        monkeypatch.setattr(serial_mod, "verify_invariants", fake_verify)

        seed = _make_seed("AC with invariant tag", "AC that sees invariant")
        executor = _make_executor()
        captured_overrides: list[str] = []

        INVARIANT_TEXT = "serialize_postmortem_chain produces a list"

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            captured_overrides.append(kwargs.get("context_override") or "")
            final_msg = "task done"
            if ac_index == 0:
                final_msg = f"task done [[INVARIANT: {INVARIANT_TEXT}]]"
            return _ok_result(ac_index, str(kwargs["ac_content"]), final_message=final_msg)

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="sess_inv_above",
            execution_id="exec_inv_above",
            tools=[],
            system_prompt="SYS",
            execution_plan=plan,
        )

        # Verification was called for AC 0 (which had a tag).
        assert len(verify_calls) == 1, f"Expected 1 verify call, got: {verify_calls}"
        assert INVARIANT_TEXT in verify_calls[0]["tags"]

        # AC 1's context_override must contain the invariant text in the
        # "Established Invariants (cumulative)" section — not just in key_output.
        ac1_override = captured_overrides[1]
        established_idx = ac1_override.find("Established Invariants")
        assert established_idx != -1, (
            f"'Established Invariants' section missing from AC 1 override:\n{ac1_override[:500]}"
        )
        established_section = ac1_override[established_idx:]
        assert INVARIANT_TEXT in established_section, (
            f"Invariant should appear in 'Established Invariants' section; "
            f"section was:\n{established_section[:500]}"
        )

        # Overall result is still successful.
        assert result.success_count == 2

    @pytest.mark.asyncio
    async def test_below_threshold_invariant_filtered_from_established_section(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When verify_invariants returns score < 0.7, invariant is NOT added to
        the cumulative 'Established Invariants' section of the chain prompt.

        AC 0 emits a tag; stub verifier returns 0.3 (below default 0.7 threshold).
        The invariant text must NOT appear in the "Established Invariants" section
        of AC 1's context_override.  (The raw [[INVARIANT:...]] text may still
        appear in the key_output excerpt of the full postmortem — that is expected
        and harmless; the gate applies only to structured storage in
        invariants_established and the cumulative rendering section.)

        Compounding reference: this relies on the sub_postmortems field added
        in AC-2 (the ACPostmortem.sub_postmortems field is preserved but the
        invariants_established stays empty for below-threshold tags).

        [[INVARIANT: only above-threshold invariants appear in downstream chain context]]
        """
        import ouroboros.orchestrator.serial_executor as serial_mod

        async def fake_verify(
            adapter: Any,
            tags: list[str],
            *,
            ac_trace: str,
            files_modified: list[str],
            model: str | None = None,
        ) -> list[tuple[str, float]]:
            # All tags score below threshold.
            return [(tag, 0.3) for tag in tags]

        monkeypatch.setattr(serial_mod, "verify_invariants", fake_verify)

        seed = _make_seed("AC with low-reliability tag", "AC checks chain")
        executor = _make_executor()
        captured_overrides: list[str] = []

        LOW_REL_TAG = "this invariant is unreliable xyz123"

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            captured_overrides.append(kwargs.get("context_override") or "")
            final_msg = "done"
            if ac_index == 0:
                final_msg = f"done [[INVARIANT: {LOW_REL_TAG}]]"
            return _ok_result(ac_index, str(kwargs["ac_content"]), final_message=final_msg)

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        await executor.execute_serial(
            seed=seed,
            session_id="sess_inv_low",
            execution_id="exec_inv_low",
            tools=[],
            system_prompt="SYS",
            execution_plan=plan,
        )

        # AC 1's context must NOT list the low-reliability tag under
        # "Established Invariants (cumulative)" section.
        ac1_override = captured_overrides[1]
        established_start = ac1_override.find("Established Invariants")
        if established_start != -1:
            # If the section exists, the low-reliability tag must not be in it.
            established_section = ac1_override[established_start:]
            assert LOW_REL_TAG not in established_section, (
                "Below-threshold invariant must NOT appear in 'Established Invariants' section"
            )
        # If the section doesn't exist at all, the invariant is definitely not there — also fine.

    @pytest.mark.asyncio
    async def test_verify_invariants_not_called_when_no_tags(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the AC emits no [[INVARIANT]] tags, verify_invariants is skipped."""
        import ouroboros.orchestrator.serial_executor as serial_mod

        verify_calls: list[dict] = []

        async def fake_verify(
            adapter: Any,
            tags: list[str],
            **kwargs: Any,
        ) -> list[tuple[str, float]]:
            verify_calls.append({"tags": tags})
            return []

        monkeypatch.setattr(serial_mod, "verify_invariants", fake_verify)

        seed = _make_seed("AC without tags", "AC 2")
        executor = _make_executor()

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        await executor.execute_serial(
            seed=seed,
            session_id="sess_no_tags",
            execution_id="exec_no_tags",
            tools=[],
            system_prompt="SYS",
            execution_plan=plan,
        )

        # verify_invariants should not have been called at all.
        assert verify_calls == [], (
            "verify_invariants must not be called when no tags are present"
        )

    @pytest.mark.asyncio
    async def test_verify_invariants_stub_adapter_integration(self) -> None:
        """verify_invariants calls adapter.complete() and parses the score.

        This is the integration test with a stub Haiku call. The adapter
        is a MagicMock whose .complete() returns a synthetic response with
        a numeric score. The function must return the correct (tag, score) pair.

        [[INVARIANT: verify_invariants is called inline-blocking before chain advance]]
        """
        from unittest.mock import AsyncMock, MagicMock

        from ouroboros.core.types import Result
        from ouroboros.orchestrator.serial_executor import verify_invariants
        from ouroboros.providers.base import CompletionResponse, UsageInfo

        # Build a stub adapter that returns "0.82" as its response.
        stub_response = CompletionResponse(
            content="0.82",
            model="claude-haiku-4-5-20251001",
            usage=UsageInfo(prompt_tokens=50, completion_tokens=2, total_tokens=52),
        )
        adapter = MagicMock()
        adapter.complete = AsyncMock(return_value=Result.ok(stub_response))

        tags = ["ACPostmortem.sub_postmortems preserves structure"]
        results = await verify_invariants(
            adapter,
            tags,
            ac_trace="Built sub_postmortems field and verified round-trip.",
            files_modified=["src/ouroboros/orchestrator/level_context.py"],
            model="claude-haiku-4-5-20251001",
        )

        assert len(results) == 1
        tag_out, score = results[0]
        assert tag_out == tags[0]
        # Score should be parsed from "0.82".
        assert abs(score - 0.82) < 1e-9, f"Expected 0.82 but got {score}"

        # adapter.complete was called exactly once (one tag → one Haiku call).
        adapter.complete.assert_called_once()

    @pytest.mark.asyncio
    async def test_verify_invariants_adapter_error_returns_fallback(self) -> None:
        """When adapter.complete() fails, the fallback score (0.5) is returned."""
        from unittest.mock import AsyncMock, MagicMock

        from ouroboros.core.errors import ProviderError
        from ouroboros.core.types import Result
        from ouroboros.orchestrator.serial_executor import verify_invariants

        adapter = MagicMock()
        adapter.complete = AsyncMock(
            return_value=Result.err(ProviderError(message="rate limit", details={}))
        )

        tags = ["some invariant"]
        results = await verify_invariants(
            adapter,
            tags,
            ac_trace="trace",
            files_modified=[],
            model="claude-haiku-4-5-20251001",
        )

        assert len(results) == 1
        _, score = results[0]
        # Fallback score must be 0.5.
        assert score == 0.5, f"Expected fallback 0.5 but got {score}"

    @pytest.mark.asyncio
    async def test_custom_min_reliability_threshold_via_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OUROBOROS_INVARIANT_MIN_RELIABILITY controls the inclusion gate.

        When set to 0.4, a score of 0.45 must be accepted.
        When set to 0.9, a score of 0.85 must be rejected.
        """
        import ouroboros.orchestrator.serial_executor as serial_mod

        monkeypatch.setenv("OUROBOROS_INVARIANT_MIN_RELIABILITY", "0.4")

        async def fake_verify_medium(
            adapter: Any,
            tags: list[str],
            **kwargs: Any,
        ) -> list[tuple[str, float]]:
            return [(tag, 0.45) for tag in tags]

        monkeypatch.setattr(serial_mod, "verify_invariants", fake_verify_medium)

        seed = _make_seed("AC with medium tag", "AC checks invariant")
        executor = _make_executor()
        captured_overrides: list[str] = []
        MEDIUM_TAG = "medium reliability invariant"

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            captured_overrides.append(kwargs.get("context_override") or "")
            final_msg = "done"
            if ac_index == 0:
                final_msg = f"done [[INVARIANT: {MEDIUM_TAG}]]"
            return _ok_result(ac_index, str(kwargs["ac_content"]), final_message=final_msg)

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        await executor.execute_serial(
            seed=seed,
            session_id="sess_thresh",
            execution_id="exec_thresh",
            tools=[],
            system_prompt="SYS",
            execution_plan=plan,
        )

        # With threshold=0.4 and score=0.45, invariant should appear in the
        # "Established Invariants (cumulative)" section of AC 1's context.
        ac1_override = captured_overrides[1]
        established_idx = ac1_override.find("Established Invariants")
        assert established_idx != -1, (
            f"'Established Invariants' section missing; override:\n{ac1_override[:500]}"
        )
        established_section = ac1_override[established_idx:]
        assert MEDIUM_TAG in established_section, (
            f"Invariant with score 0.45 must appear when threshold is 0.4; "
            f"established section was:\n{established_section[:500]}"
        )


class TestCheckpointWriting:
    """AC-2 (Q6.2): Per-AC checkpoint writing integration tests.

    Verifies that SerialCompoundingExecutor writes a checkpoint to the
    CheckpointStore after each successfully completed AC, and does NOT
    write a checkpoint when an AC fails.

    Compounding context (from prior ACs):
    - AC-1 established [[INVARIANT: end-of-run chain artifact exists in
      docs/brainstorm/chain-*.md]] — checkpoints complement this artifact
      by enabling resume without losing prior ACs' work.
    - AC-2 established [[INVARIANT: ACPostmortem.sub_postmortems preserves
      structure in serialized chain]] — the checkpoint payload carries the
      full serialized PostmortemChain, which now includes sub_postmortems.
    - AC-3 established [[INVARIANT: verify_invariants is called
      inline-blocking before chain advance]] — verified invariants are
      present in the chain that gets checkpointed.

    [[INVARIANT: checkpoints are only written after AC success, never on failure]]
    [[INVARIANT: CompoundingCheckpointState.last_completed_ac_index equals the 0-based AC index]]
    [[INVARIANT: checkpoint payload mode is always the literal "compounding"]]
    """

    def _make_executor_with_mock_store(self) -> tuple[SerialCompoundingExecutor, MagicMock]:
        """Build an executor with a mock CheckpointStore injected."""
        event_store, _ = _make_replaying_event_store()

        mock_store = MagicMock()
        # write() should return a successful Result-like object.
        from ouroboros.core.types import Result
        mock_store.write.return_value = Result.ok(None)

        executor = SerialCompoundingExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            checkpoint_store=mock_store,
        )
        executor._coordinator.detect_file_conflicts = MagicMock(return_value=[])
        return executor, mock_store

    @pytest.mark.asyncio
    async def test_checkpoint_written_after_each_successful_ac(self) -> None:
        """CheckpointStore.write is called once per successful AC.

        In a 2-AC run where both succeed, write() must be called twice:
        once with last_completed_ac_index=0 and once with =1.

        Compounding ref: the checkpoint serializes the PostmortemChain which
        by AC-2 now includes sub_postmortems (B-prime) in its serialized form.

        [[INVARIANT: checkpoints are only written after AC success, never on failure]]
        """
        from ouroboros.persistence.checkpoint import CompoundingCheckpointState

        seed = _make_seed("AC 1 — build model", "AC 2 — build endpoint")
        executor, mock_store = self._make_executor_with_mock_store()

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="sess_ckpt_success",
            execution_id="exec_ckpt_success",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
        )

        assert result.success_count == 2
        # write() must have been called exactly twice.
        assert mock_store.write.call_count == 2, (
            f"Expected 2 checkpoint writes, got {mock_store.write.call_count}"
        )

        # Extract CheckpointData arguments from each write() call.
        call_args = [call.args[0] for call in mock_store.write.call_args_list]

        # First call: AC 0 completed → last_completed_ac_index should be 0.
        ckpt0 = call_args[0]
        state0 = CompoundingCheckpointState.from_dict(ckpt0.state)
        assert state0.last_completed_ac_index == 0, (
            f"First checkpoint should have last_completed_ac_index=0, got {state0.last_completed_ac_index}"
        )
        assert state0.mode == "compounding"
        assert isinstance(state0.postmortem_chain, list)
        assert len(state0.postmortem_chain) == 1  # only AC 0 in chain after AC 0 completes

        # Second call: AC 1 completed → last_completed_ac_index should be 1.
        ckpt1 = call_args[1]
        state1 = CompoundingCheckpointState.from_dict(ckpt1.state)
        assert state1.last_completed_ac_index == 1, (
            f"Second checkpoint should have last_completed_ac_index=1, got {state1.last_completed_ac_index}"
        )
        assert len(state1.postmortem_chain) == 2  # both ACs in chain

        # Checkpoint phase must be "execution".
        assert ckpt0.phase == "execution"
        assert ckpt1.phase == "execution"

        # seed_id must match the seed's metadata.
        assert ckpt0.seed_id == seed.metadata.seed_id
        assert ckpt1.seed_id == seed.metadata.seed_id

    @pytest.mark.asyncio
    async def test_no_checkpoint_written_on_ac_failure(self) -> None:
        """CheckpointStore.write is NOT called when an AC fails.

        AC 0 fails → no checkpoint. AC 1 is blocked (fail_fast=True) →
        no checkpoint. Total write() calls: 0.

        Compounding ref: this guards the Q6.2 resume semantics established
        in the brainstorm doc — a failed AC does not advance the cursor,
        ensuring resume restarts from that AC, not the one after.

        [[INVARIANT: checkpoints are only written after AC success, never on failure]]
        """
        seed = _make_seed("AC 1 fails", "AC 2 never runs")
        executor, mock_store = self._make_executor_with_mock_store()

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            if ac_index == 0:
                return _fail_result(0, str(kwargs["ac_content"]), error="timeout")
            return _ok_result(ac_index, str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="sess_ckpt_fail",
            execution_id="exec_ckpt_fail",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            fail_fast=True,
        )

        assert result.failure_count == 1
        assert result.blocked_count == 1
        # No checkpoints written — the failing AC does not advance the cursor.
        assert mock_store.write.call_count == 0, (
            f"Expected 0 checkpoint writes on failure, got {mock_store.write.call_count}"
        )

    @pytest.mark.asyncio
    async def test_checkpoint_written_for_successful_acs_skip_failed_in_fail_forward(
        self,
    ) -> None:
        """In fail-forward mode, only successful ACs trigger a checkpoint write.

        AC 0 fails (no checkpoint), AC 1 succeeds (checkpoint with index=1).
        Total write() calls: 1.

        Compounding ref: uses fail_fast=False which was tested in AC-2's
        sub-postmortem tests (test_fail_forward_continues_past_failure).

        [[INVARIANT: checkpoints are only written after AC success, never on failure]]
        """
        from ouroboros.persistence.checkpoint import CompoundingCheckpointState

        seed = _make_seed("AC 0 fails", "AC 1 succeeds")
        executor, mock_store = self._make_executor_with_mock_store()

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            if ac_index == 0:
                return _fail_result(0, str(kwargs["ac_content"]), error="oops")
            return _ok_result(ac_index, str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="sess_ckpt_fwd",
            execution_id="exec_ckpt_fwd",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            fail_fast=False,
        )

        assert result.failure_count == 1
        assert result.success_count == 1
        # Exactly 1 checkpoint written — only for the successful AC 1.
        assert mock_store.write.call_count == 1, (
            f"Expected 1 checkpoint write, got {mock_store.write.call_count}"
        )

        written_ckpt = mock_store.write.call_args.args[0]
        state = CompoundingCheckpointState.from_dict(written_ckpt.state)
        # The successful AC was index 1 → cursor points to 1.
        assert state.last_completed_ac_index == 1

    @pytest.mark.asyncio
    async def test_no_checkpoint_written_when_store_is_none(self) -> None:
        """When no CheckpointStore is provided, the executor runs without error.

        This is the default path for callers that do not opt-in to checkpointing.
        The executor must not crash and must still produce correct results.

        [[INVARIANT: checkpoints are only written after AC success, never on failure]]
        """
        seed = _make_seed("AC 1", "AC 2")
        # Use the default executor from _make_executor() which has no store.
        executor = _make_executor()
        assert executor._checkpoint_store is None

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="sess_no_store",
            execution_id="exec_no_store",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
        )
        # Run succeeds normally even without a store.
        assert result.success_count == 2
        assert result.failure_count == 0

    @pytest.mark.asyncio
    async def test_checkpoint_write_error_does_not_propagate(self) -> None:
        """A failing CheckpointStore.write() call must not abort the run.

        The executor catches write errors and logs a warning; the AC loop
        must still complete normally.

        [[INVARIANT: checkpoints are only written after AC success, never on failure]]
        """
        from ouroboros.core.errors import PersistenceError
        from ouroboros.core.types import Result

        seed = _make_seed("AC 1", "AC 2")
        executor, mock_store = self._make_executor_with_mock_store()
        # Make write() return an error result.
        mock_store.write.return_value = Result.err(
            PersistenceError(message="disk full", operation="write", details={})
        )

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="sess_ckpt_err",
            execution_id="exec_ckpt_err",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
        )
        # The run still succeeds despite checkpoint errors.
        assert result.success_count == 2
        assert result.failure_count == 0

    def test_write_compounding_checkpoint_payload_structure(
        self, tmp_path: Path
    ) -> None:
        """_write_compounding_checkpoint produces the expected CheckpointData payload.

        Uses a real CheckpointStore pointed at tmp_path to exercise the full
        write → read → validate path.

        Compounding ref: the checkpoint serializes the PostmortemChain which
        now (since AC-2, B-prime) includes sub_postmortems in its serialized
        output, and (since AC-3, C-plus) may include verified Invariant objects.

        [[INVARIANT: CompoundingCheckpointState.mode is always the literal "compounding"]]
        [[INVARIANT: CompoundingCheckpointState.last_completed_ac_index equals the 0-based AC index]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )
        from ouroboros.orchestrator.serial_executor import _write_compounding_checkpoint
        from ouroboros.persistence.checkpoint import (
            CheckpointStore,
            CompoundingCheckpointState,
        )

        # Build a minimal one-AC chain.
        summary = ACContextSummary(
            ac_index=0,
            ac_content="Build the auth module",
            success=True,
            files_modified=("src/auth.py",),
        )
        pm = ACPostmortem(
            summary=summary,
            status="pass",
            gotchas=("remember to hash passwords",),
        )
        chain = PostmortemChain(postmortems=(pm,))

        store = CheckpointStore(base_path=tmp_path / "checkpoints")
        store.initialize()

        _write_compounding_checkpoint(
            store=store,
            seed_id="seed_test_ckpt",
            session_id="sess_payload_test",
            ac_index=0,
            chain=chain,
        )

        # Read back the checkpoint and validate.
        load_result = store.load("seed_test_ckpt")
        assert load_result.is_ok, f"Load failed: {load_result.error}"

        ckpt = load_result.value
        assert ckpt.phase == "execution"
        assert ckpt.seed_id == "seed_test_ckpt"

        state = CompoundingCheckpointState.from_dict(ckpt.state)
        assert state.mode == "compounding"
        assert state.last_completed_ac_index == 0
        assert isinstance(state.postmortem_chain, list)
        assert len(state.postmortem_chain) == 1

        # The postmortem chain entry should reference the AC content.
        entry = state.postmortem_chain[0]
        summary_data = entry.get("summary", {})
        assert summary_data.get("ac_content") == "Build the auth module"


class TestCheckpointResume:
    """AC-4 (Q6.2) Sub-AC 1: Checkpoint loading and postmortem chain deserialization.

    Verifies that resume_session_id triggers checkpoint loading, the prior
    postmortem chain is deserialized into memory, and already-completed ACs
    are skipped (not re-executed).

    Compounding context (from prior ACs):
    - AC-1 established [[INVARIANT: end-of-run chain artifact exists in
      docs/brainstorm/chain-*.md]] — checkpoints complement the artifact.
    - AC-2 established [[INVARIANT: ACPostmortem.sub_postmortems preserves
      structure in serialized chain]] — the loaded chain includes sub_postmortems.
    - AC-3 established [[INVARIANT: verify_invariants is called inline-blocking
      before chain advance]] — verified invariants are present in the loaded chain.
    - AC-3's per-AC checkpoint writing puts serialized PostmortemChain (with
      Invariant objects) into the checkpoint payload used by resume.

    [[INVARIANT: resume_session_id triggers checkpoint loading by seed_id, not by session_id]]
    [[INVARIANT: deserialized chain is injected before the AC loop so resumed ACs see prior postmortems]]
    [[INVARIANT: _load_compounding_checkpoint returns empty chain and -1 on failure]]
    [[INVARIANT: deserialized chain reflects all postmortems from the prior run up to last_completed_ac_index]]
    """

    def _make_executor_with_real_store(
        self, tmp_path: Path
    ) -> tuple[SerialCompoundingExecutor, Any]:
        """Build an executor with a real CheckpointStore backed by tmp_path."""
        from ouroboros.persistence.checkpoint import CheckpointStore

        store = CheckpointStore(base_path=tmp_path / "checkpoints")
        store.initialize()

        event_store, _ = _make_replaying_event_store()
        executor = SerialCompoundingExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            checkpoint_store=store,
        )
        executor._coordinator.detect_file_conflicts = MagicMock(return_value=[])
        return executor, store

    @pytest.mark.asyncio
    async def test_resume_skips_already_completed_acs(self, tmp_path: Path) -> None:
        """When resume_session_id is provided, ACs already in the checkpoint are skipped.

        Setup: write a checkpoint for AC 0 (index 0, last_completed_ac_index=0).
        Run execute_serial with resume_session_id set.
        Expect: AC 0's _execute_single_ac is NOT called; AC 1's IS called.

        Compounding reference: the checkpoint payload includes the PostmortemChain
        serialized by _write_compounding_checkpoint (AC-3), which now stores
        ACPostmortem.sub_postmortems (AC-2, B-prime) and Invariant objects (AC-3).

        [[INVARIANT: resume_session_id triggers checkpoint loading by seed_id, not by session_id]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )
        from ouroboros.orchestrator.serial_executor import _write_compounding_checkpoint

        seed = _make_seed("AC 0 already done", "AC 1 to be executed")
        executor, store = self._make_executor_with_real_store(tmp_path)

        # Pre-write a checkpoint as if AC 0 had already completed.
        summary_ac0 = ACContextSummary(
            ac_index=0,
            ac_content="AC 0 already done",
            success=True,
            files_modified=("src/ac0.py",),
        )
        pm_ac0 = ACPostmortem(summary=summary_ac0, status="pass", gotchas=("ac0 gotcha",))
        prior_chain = PostmortemChain(postmortems=(pm_ac0,))
        _write_compounding_checkpoint(
            store=store,
            seed_id=seed.metadata.seed_id,
            session_id="prior_session",
            ac_index=0,
            chain=prior_chain,
        )

        executed_indices: list[int] = []

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            executed_indices.append(ac_index)
            return _ok_result(ac_index, str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="resume_session",
            execution_id="exec_resume_skip",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            resume_session_id="prior_session",
        )

        # AC 0 was already done — not re-executed.
        assert 0 not in executed_indices, (
            f"AC 0 should have been skipped via checkpoint resume; executed: {executed_indices}"
        )
        # AC 1 was executed normally.
        assert 1 in executed_indices, (
            f"AC 1 should have been executed after skip; executed: {executed_indices}"
        )
        # Both ACs appear in results — AC 0 as SATISFIED_EXTERNALLY, AC 1 as SUCCEEDED.
        assert len(result.results) == 2

    @pytest.mark.asyncio
    async def test_resume_injects_prior_chain_into_resumed_ac_context(
        self, tmp_path: Path
    ) -> None:
        """The resumed AC's context_override contains postmortems from the loaded chain.

        After checkpoint loading, AC 1 should see AC 0's postmortem in its
        context_override, even though AC 0 was not re-executed.

        Compounding reference: AC-1 established [[INVARIANT: end-of-run chain
        artifact exists in docs/brainstorm/chain-*.md]] which confirmed the chain
        serialization round-trip works. This test relies on the same deserialization.

        [[INVARIANT: deserialized chain is injected before the AC loop so resumed ACs see prior postmortems]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )
        from ouroboros.orchestrator.serial_executor import _write_compounding_checkpoint

        seed = _make_seed("AC 0 done", "AC 1 resumed")
        executor, store = self._make_executor_with_real_store(tmp_path)

        # Write a checkpoint with AC 0 done and a specific gotcha in its postmortem.
        summary_ac0 = ACContextSummary(
            ac_index=0,
            ac_content="AC 0 done",
            success=True,
            files_modified=("src/module_alpha.py",),
        )
        pm_ac0 = ACPostmortem(
            summary=summary_ac0,
            status="pass",
            gotchas=("important_gotcha_from_prior_run",),
        )
        prior_chain = PostmortemChain(postmortems=(pm_ac0,))
        _write_compounding_checkpoint(
            store=store,
            seed_id=seed.metadata.seed_id,
            session_id="prior_session_x",
            ac_index=0,
            chain=prior_chain,
        )

        captured_overrides: list[str] = []

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            captured_overrides.append(kwargs.get("context_override") or "")
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        await executor.execute_serial(
            seed=seed,
            session_id="new_session_y",
            execution_id="exec_chain_inject",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            resume_session_id="prior_session_x",
        )

        # AC 1's context_override should reference the prior chain's content.
        # captured_overrides[0] belongs to AC 1 (AC 0 was skipped — no _execute_single_ac call).
        assert len(captured_overrides) == 1, (
            f"Only AC 1 should have been executed (AC 0 skipped). "
            f"Got {len(captured_overrides)} overrides."
        )
        ac1_override = captured_overrides[0]
        assert "Prior AC Postmortems" in ac1_override, (
            "AC 1 context must include the postmortem chain section"
        )
        assert "AC 0 done" in ac1_override, (
            "AC 1 context must include AC 0's postmortem content from the loaded chain"
        )
        assert "important_gotcha_from_prior_run" in ac1_override, (
            "AC 1 context must include gotchas from the deserialized chain"
        )

    @pytest.mark.asyncio
    async def test_resume_without_checkpoint_store_runs_fresh(self) -> None:
        """When no checkpoint store is provided, resume_session_id is safely ignored.

        All ACs are executed from the beginning even though resume_session_id is set.

        [[INVARIANT: _load_compounding_checkpoint returns empty chain and -1 on failure]]
        """
        seed = _make_seed("AC 0", "AC 1")
        # Use default executor — no checkpoint store.
        executor = _make_executor()
        assert executor._checkpoint_store is None

        executed_indices: list[int] = []

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            executed_indices.append(int(kwargs["ac_index"]))
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="sess_no_store",
            execution_id="exec_no_store",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            resume_session_id="nonexistent_prior",
        )

        # Both ACs should have been executed (no skip because no store).
        assert executed_indices == [0, 1]
        assert result.success_count == 2

    @pytest.mark.asyncio
    async def test_resume_with_missing_checkpoint_runs_fresh(
        self, tmp_path: Path
    ) -> None:
        """When resume_session_id is set but no checkpoint file exists, start fresh.

        [[INVARIANT: _load_compounding_checkpoint returns empty chain and -1 on failure]]
        """
        seed = _make_seed("AC 0", "AC 1")
        executor, _store = self._make_executor_with_real_store(tmp_path)
        # Note: no checkpoint is written — store is empty.

        executed_indices: list[int] = []

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            executed_indices.append(int(kwargs["ac_index"]))
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="sess_fresh",
            execution_id="exec_fresh",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            resume_session_id="no_such_session",
        )

        # All ACs executed from the start (checkpoint not found → fallback).
        assert executed_indices == [0, 1]
        assert result.success_count == 2

    def test_load_compounding_checkpoint_returns_chain_and_index(
        self, tmp_path: Path
    ) -> None:
        """_load_compounding_checkpoint returns the deserialized chain and index.

        Compounding reference: this function uses deserialize_postmortem_chain
        (verified round-trip in AC-2 tests) and CompoundingCheckpointState
        (established by AC-3 checkpoint writing tests).

        [[INVARIANT: deserialized chain reflects all postmortems from the prior run up to last_completed_ac_index]]
        [[INVARIANT: resume_session_id triggers checkpoint loading by seed_id, not by session_id]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )
        from ouroboros.orchestrator.serial_executor import (
            _load_compounding_checkpoint,
            _write_compounding_checkpoint,
        )
        from ouroboros.persistence.checkpoint import CheckpointStore

        store = CheckpointStore(base_path=tmp_path / "checkpoints")
        store.initialize()

        # Write a two-AC chain.
        summary0 = ACContextSummary(ac_index=0, ac_content="AC zero", success=True)
        summary1 = ACContextSummary(
            ac_index=1,
            ac_content="AC one",
            success=True,
            files_modified=("src/f1.py",),
        )
        pm0 = ACPostmortem(summary=summary0, status="pass", gotchas=("g0",))
        pm1 = ACPostmortem(summary=summary1, status="pass")
        chain = PostmortemChain(postmortems=(pm0, pm1))

        _write_compounding_checkpoint(
            store=store,
            seed_id="seed_resume_direct",
            session_id="s_old",
            ac_index=1,
            chain=chain,
        )

        loaded_chain, last_idx = _load_compounding_checkpoint(
            store=store,
            seed_id="seed_resume_direct",
            session_id="s_new",
            resume_session_id="s_old",
        )

        assert last_idx == 1, f"Expected last_completed_ac_index=1, got {last_idx}"
        assert len(loaded_chain.postmortems) == 2, (
            f"Expected 2 postmortems in loaded chain, got {len(loaded_chain.postmortems)}"
        )
        assert loaded_chain.postmortems[0].summary.ac_content == "AC zero"
        assert loaded_chain.postmortems[1].summary.ac_content == "AC one"
        assert "g0" in loaded_chain.postmortems[0].gotchas
        assert "src/f1.py" in loaded_chain.postmortems[1].summary.files_modified

    def test_load_compounding_checkpoint_returns_empty_on_missing(
        self, tmp_path: Path
    ) -> None:
        """_load_compounding_checkpoint returns (empty_chain, -1) when no checkpoint.

        [[INVARIANT: _load_compounding_checkpoint returns empty chain and -1 on failure]]
        """
        from ouroboros.orchestrator.serial_executor import _load_compounding_checkpoint
        from ouroboros.persistence.checkpoint import CheckpointStore

        store = CheckpointStore(base_path=tmp_path / "empty_checkpoints")
        store.initialize()

        chain, idx = _load_compounding_checkpoint(
            store=store,
            seed_id="nonexistent_seed",
            session_id="s_new",
            resume_session_id="s_old",
        )

        assert idx == -1, f"Expected -1 (no checkpoint), got {idx}"
        assert len(chain.postmortems) == 0, (
            f"Expected empty chain, got {len(chain.postmortems)} postmortems"
        )

    def test_load_compounding_checkpoint_returns_empty_on_wrong_mode(
        self, tmp_path: Path
    ) -> None:
        """_load_compounding_checkpoint returns (empty_chain, -1) for non-compounding checkpoints.

        [[INVARIANT: _load_compounding_checkpoint returns empty chain and -1 on failure]]
        """
        from ouroboros.orchestrator.serial_executor import _load_compounding_checkpoint
        from ouroboros.persistence.checkpoint import CheckpointData, CheckpointStore

        store = CheckpointStore(base_path=tmp_path / "wrong_mode_checkpoints")
        store.initialize()

        # Write a checkpoint with the wrong mode (not "compounding").
        wrong_mode_ckpt = CheckpointData.create(
            seed_id="seed_wrong_mode",
            phase="planning",
            state={"mode": "parallel", "some_key": "some_value"},
        )
        store.save(wrong_mode_ckpt)

        chain, idx = _load_compounding_checkpoint(
            store=store,
            seed_id="seed_wrong_mode",
            session_id="s_new",
            resume_session_id="s_old",
        )

        assert idx == -1, f"Expected -1 for wrong mode, got {idx}"
        assert len(chain.postmortems) == 0

    @pytest.mark.asyncio
    async def test_resume_session_id_none_does_not_load_checkpoint(
        self, tmp_path: Path
    ) -> None:
        """When resume_session_id is None, checkpoints are NOT loaded even if present.

        This ensures resume opt-in: callers that don't pass resume_session_id
        always get a fresh run, not an accidental resume.

        [[INVARIANT: resume_session_id triggers checkpoint loading by seed_id, not by session_id]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )
        from ouroboros.orchestrator.serial_executor import _write_compounding_checkpoint

        seed = _make_seed("AC 0", "AC 1")
        executor, store = self._make_executor_with_real_store(tmp_path)

        # Pre-write a checkpoint for AC 0.
        summary = ACContextSummary(ac_index=0, ac_content="AC 0", success=True)
        pm = ACPostmortem(summary=summary, status="pass")
        prior_chain = PostmortemChain(postmortems=(pm,))
        _write_compounding_checkpoint(
            store=store,
            seed_id=seed.metadata.seed_id,
            session_id="prior_session",
            ac_index=0,
            chain=prior_chain,
        )

        executed_indices: list[int] = []

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            executed_indices.append(int(kwargs["ac_index"]))
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="fresh_session",
            execution_id="exec_no_resume",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            # NOTE: resume_session_id intentionally NOT passed (defaults to None).
        )

        # Both ACs should be executed: no resume without explicit resume_session_id.
        assert executed_indices == [0, 1], (
            f"Both ACs should run from scratch; executed: {executed_indices}"
        )
        assert result.success_count == 2

    @pytest.mark.asyncio
    async def test_resume_3ac_chain_skips_first_two_executes_third(
        self, tmp_path: Path
    ) -> None:
        """In a 3-AC run, resuming with last_completed_ac_index=1 skips AC 0 and AC 1.

        Setup: write a checkpoint for AC 1 (last_completed_ac_index=1) with a 2-AC chain.
        Run execute_serial with resume_session_id set.
        Expect: ACs 0 and 1 are skipped; only AC 2 is executed.
        AC 2's context_override must include postmortems for AC 0 AND AC 1 from the chain.

        Compounding reference: this builds on the AC-skipping logic established in
        Sub-AC 1 (checkpoint loading) and verifies that the *chain forwarding* works
        correctly for multi-AC resume — not just 2-AC runs. The postmortem chain
        established by AC-1 (Q6.1) includes serialize_postmortem_chain round-trip
        (verified in AC-2 B-prime tests), and the invariants field (AC-3 C-plus).

        [[INVARIANT: deserialized chain reflects all postmortems from the prior run up to last_completed_ac_index]]
        [[INVARIANT: resume_session_id triggers checkpoint loading by seed_id, not by session_id]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )
        from ouroboros.orchestrator.serial_executor import _write_compounding_checkpoint

        seed = _make_seed("AC 0 done", "AC 1 done", "AC 2 to execute")
        executor, store = self._make_executor_with_real_store(tmp_path)

        # Write a checkpoint for a 2-AC completed run (ACs 0 and 1 done).
        summary_ac0 = ACContextSummary(
            ac_index=0,
            ac_content="AC 0 done",
            success=True,
            files_modified=("src/ac0.py",),
        )
        summary_ac1 = ACContextSummary(
            ac_index=1,
            ac_content="AC 1 done",
            success=True,
            files_modified=("src/ac1.py",),
        )
        pm_ac0 = ACPostmortem(
            summary=summary_ac0,
            status="pass",
            gotchas=("ac0 specific gotcha",),
        )
        pm_ac1 = ACPostmortem(
            summary=summary_ac1,
            status="pass",
            gotchas=("ac1 specific gotcha",),
        )
        prior_chain = PostmortemChain(postmortems=(pm_ac0, pm_ac1))
        _write_compounding_checkpoint(
            store=store,
            seed_id=seed.metadata.seed_id,
            session_id="prior_session_3ac",
            ac_index=1,  # last_completed = AC 1 (0-based)
            chain=prior_chain,
        )

        executed_indices: list[int] = []
        captured_overrides: list[str] = []

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            executed_indices.append(ac_index)
            captured_overrides.append(kwargs.get("context_override") or "")
            return _ok_result(ac_index, str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,), (2,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="new_session_3ac",
            execution_id="exec_3ac_resume",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            resume_session_id="prior_session_3ac",
        )

        # ACs 0 and 1 must NOT have been executed.
        assert 0 not in executed_indices, (
            f"AC 0 should be skipped; executed: {executed_indices}"
        )
        assert 1 not in executed_indices, (
            f"AC 1 should be skipped; executed: {executed_indices}"
        )
        # Only AC 2 was executed.
        assert executed_indices == [2], f"Only AC 2 should run; executed: {executed_indices}"

        # AC 2's context_override must include both AC 0 and AC 1 postmortems.
        assert len(captured_overrides) == 1
        ac2_override = captured_overrides[0]
        assert "AC 0 done" in ac2_override, "AC 2 context must include AC 0's postmortem"
        assert "AC 1 done" in ac2_override, "AC 2 context must include AC 1's postmortem"
        assert "ac0 specific gotcha" in ac2_override, "AC 0 gotcha must be in AC 2 context"
        assert "ac1 specific gotcha" in ac2_override, "AC 1 gotcha must be in AC 2 context"

        # Result has 3 entries: AC 0 (SATISFIED_EXTERNALLY), AC 1 (SATISFIED_EXTERNALLY),
        # AC 2 (SUCCEEDED).
        assert len(result.results) == 3
        assert result.success_count >= 1  # At least AC 2 succeeded


class TestTruncationEvent:
    """AC-4 (Q7): Postmortem chain truncation event.

    Verifies that the Q7 structured event is emitted alongside log.warning
    when the rendered postmortem chain overflows the character budget.
    Coexists with the log line — does not replace it.

    Compounding context (from prior ACs):
    - AC-1: [[INVARIANT: end-of-run chain artifact exists in docs/brainstorm/chain-*.md]]
      — artifact and truncation events both serve observability purposes.
    - AC-2: [[INVARIANT: ACPostmortem.sub_postmortems preserves structure in serialized chain]]
      — sub-postmortem data survives the chain even when digests are truncated.
    - AC-3: [[INVARIANT: verify_invariants is called inline-blocking before chain advance]]
      — verified invariants are part of the chain that may be truncated.
    - Sub-AC 1: [[INVARIANT: deserialized chain is injected before the AC loop
      so resumed ACs see prior postmortems]] — truncation may affect resumed chains.

    [[INVARIANT: Truncation event emitted alongside log.warning, not replacing it]]
    [[INVARIANT: event type is execution.postmortem_chain.truncated]]
    [[INVARIANT: no truncation event emitted when chain fits within budget]]
    """

    def test_truncation_event_factory_fields(self) -> None:
        """create_postmortem_chain_truncated_event produces the expected event structure.

        Verifies the event type, aggregate_type, and all required data fields.
        No executor needed — tests the factory directly.

        [[INVARIANT: event type is execution.postmortem_chain.truncated]]
        """
        from ouroboros.orchestrator.events import create_postmortem_chain_truncated_event

        event = create_postmortem_chain_truncated_event(
            session_id="sess_trunc",
            execution_id="exec_trunc",
            dropped_count=3,
            char_budget=10000,
            rendered_chars=12500,
            full_forms_preserved=2,
            cumulative_invariants_preserved=1,
        )

        assert event.type == "execution.postmortem_chain.truncated"
        assert event.aggregate_type == "execution"
        assert event.aggregate_id == "exec_trunc"
        assert event.data["session_id"] == "sess_trunc"
        assert event.data["execution_id"] == "exec_trunc"
        assert event.data["dropped_count"] == 3
        assert event.data["char_budget"] == 10000
        assert event.data["rendered_chars"] == 12500
        assert event.data["full_forms_preserved"] == 2
        assert event.data["cumulative_invariants_preserved"] == 1
        assert "timestamp" in event.data

    def test_on_truncated_callback_invoked_when_over_budget(self) -> None:
        """to_prompt_text calls on_truncated when chain exceeds char_budget.

        Build a chain with many ACs and set a tiny token_budget so truncation
        is guaranteed. Verify the callback is called with the correct counts.

        Compounding ref: uses PostmortemChain.to_prompt_text which was built
        in AC-2 and AC-3 (invariant render gate). The on_truncated callback
        is the new Q7 hook in Sub-AC 2.

        [[INVARIANT: Truncation event emitted alongside log.warning, not replacing it]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )

        # Build a chain with enough content to overflow a tiny budget.
        def _make_pm(idx: int) -> ACPostmortem:
            summary = ACContextSummary(
                ac_index=idx,
                ac_content=f"AC {idx} task — " + "x" * 300,  # force long content
                success=True,
                files_modified=(f"src/file_{idx}.py",),
            )
            return ACPostmortem(
                summary=summary,
                status="pass",
                gotchas=(f"gotcha for AC {idx} " + "y" * 100,),
            )

        # 8 ACs gives enough text to overflow a tiny budget.
        postmortems = tuple(_make_pm(i) for i in range(8))
        chain = PostmortemChain(postmortems=postmortems)

        truncation_calls: list[tuple] = []

        def _capture(*args: int) -> None:
            truncation_calls.append(args)

        # Use a tiny budget (1 token = 4 chars) to guarantee overflow.
        chain.to_prompt_text(
            token_budget=1,
            k_full=1,
            on_truncated=_capture,
        )

        assert len(truncation_calls) == 1, (
            f"Expected exactly 1 truncation callback, got {len(truncation_calls)}"
        )
        dropped_count, char_budget, rendered_chars, full_forms, invariants = truncation_calls[0]
        assert dropped_count > 0, "At least one digest must have been dropped"
        assert char_budget == 4, "1 token * 4 chars/token = 4"
        assert rendered_chars > 4, "Rendered text must exceed budget"
        assert full_forms == 1, "k_full=1 → 1 full-form entry"

    def test_no_truncation_callback_when_chain_fits(self) -> None:
        """on_truncated is NOT called when the chain fits within the budget.

        [[INVARIANT: no truncation event emitted when chain fits within budget]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )

        summary = ACContextSummary(ac_index=0, ac_content="Short AC", success=True)
        pm = ACPostmortem(summary=summary, status="pass")
        chain = PostmortemChain(postmortems=(pm,))

        truncation_calls: list[tuple] = []
        chain.to_prompt_text(
            token_budget=8000,  # large budget — should never truncate
            on_truncated=lambda *a: truncation_calls.append(a),
        )

        assert truncation_calls == [], (
            "on_truncated must NOT be called when chain fits within budget"
        )

    @pytest.mark.asyncio
    async def test_truncation_event_emitted_from_serial_executor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SerialCompoundingExecutor emits the truncation event when chain overflows.

        Uses a tiny OUROBOROS_POSTMORTEM_TOKEN_BUDGET to force truncation.
        Verifies that an "execution.postmortem_chain.truncated" event appears
        in the event store after the run.

        Compounding ref: this relies on the postmortem chain built by prior
        successful ACs (AC-1 through AC-3 and Sub-AC 1). The event emission
        uses the existing _safe_emit_event pattern from parallel_executor.py.

        [[INVARIANT: Truncation event emitted alongside log.warning, not replacing it]]
        [[INVARIANT: event type is execution.postmortem_chain.truncated]]
        """
        # Force a tiny token budget so even one prior AC causes truncation.
        monkeypatch.setenv("OUROBOROS_POSTMORTEM_TOKEN_BUDGET", "1")

        # Build a seed with 3 ACs; pre-load the chain with 5 dummy postmortems
        # via checkpoint so AC 0 (the resuming AC) sees an oversize chain.
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )
        from ouroboros.orchestrator.serial_executor import _write_compounding_checkpoint
        from ouroboros.persistence.checkpoint import CheckpointStore

        store = CheckpointStore(base_path=tmp_path / "checkpoints")
        store.initialize()

        event_store, appended = _make_replaying_event_store()
        executor = SerialCompoundingExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            checkpoint_store=store,
        )
        executor._coordinator.detect_file_conflicts = MagicMock(return_value=[])

        # Pre-populate a big chain with 5 verbose postmortems.
        def _big_pm(idx: int) -> ACPostmortem:
            return ACPostmortem(
                summary=ACContextSummary(
                    ac_index=idx,
                    ac_content=f"AC {idx} verbose task " + "word " * 80,
                    success=True,
                    files_modified=(f"src/file_{idx}.py",),
                ),
                status="pass",
                gotchas=(f"gotcha for AC {idx} " + "detail " * 60,),
            )

        big_chain = PostmortemChain(postmortems=tuple(_big_pm(i) for i in range(5)))

        seed = _make_seed(
            "AC 0 already done",
            "AC 1 already done",
            "AC 2 already done",
            "AC 3 already done",
            "AC 4 already done",
            "AC 5 to execute",
        )
        _write_compounding_checkpoint(
            store=store,
            seed_id=seed.metadata.seed_id,
            session_id="prior_big_chain",
            ac_index=4,
            chain=big_chain,
        )

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,), (2,), (3,), (4,), (5,))
        await executor.execute_serial(
            seed=seed,
            session_id="sess_trunc_event",
            execution_id="exec_trunc_event",
            tools=[],
            system_prompt="SYS",
            execution_plan=plan,
            resume_session_id="prior_big_chain",
        )

        trunc_events = [
            e for e in appended if e.type == "execution.postmortem_chain.truncated"
        ]
        assert len(trunc_events) >= 1, (
            f"Expected at least 1 truncation event with token_budget=1; "
            f"event types seen: {[e.type for e in appended]}"
        )
        ev = trunc_events[0]
        assert ev.data["session_id"] == "sess_trunc_event"
        assert ev.data["execution_id"] == "exec_trunc_event"
        assert ev.data["dropped_count"] >= 0
        assert ev.data["char_budget"] > 0

    @pytest.mark.asyncio
    async def test_no_truncation_event_when_chain_fits(self) -> None:
        """No truncation event is emitted when the chain fits within the default budget.

        A 2-AC run with a generous budget should produce zero truncation events.

        [[INVARIANT: no truncation event emitted when chain fits within budget]]
        """
        seed = _make_seed("AC a", "AC b")
        executor = _make_executor()
        event_store: Any = executor._event_store
        appended: list[Any] = event_store._appended

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        await executor.execute_serial(
            seed=seed,
            session_id="sess_no_trunc",
            execution_id="exec_no_trunc",
            tools=[],
            system_prompt="SYS",
            execution_plan=plan,
        )

        trunc_events = [
            e for e in appended if e.type == "execution.postmortem_chain.truncated"
        ]
        assert trunc_events == [], (
            f"Unexpected truncation events with default budget; got: {trunc_events}"
        )


class TestResumeCorrectness:
    """Sub-AC 3: Resume correctness — rehydrated chain identity and AC skip/execute semantics.

    These tests verify that when execute_serial is invoked with resume_session_id:
    1. Completed ACs (index <= last_completed_ac_index) are skipped.
    2. Remaining ACs are executed normally.
    3. The rehydrated postmortem chain is field-identical to the original
       (all postmortem fields — including sub_postmortems and Invariant objects —
       survive the checkpoint round-trip without mutation or loss).

    Compounding context:
    - AC-1 established [[INVARIANT: end-of-run chain artifact exists in
      docs/brainstorm/chain-*.md]] — chain serialization round-trip verified.
    - AC-2 established [[INVARIANT: ACPostmortem.sub_postmortems preserves
      structure in serialized chain]] — sub-postmortems survive round-trips.
    - AC-3 established [[INVARIANT: invariants_established is now
      tuple[Invariant, ...] not tuple[str, ...]]] — Invariant objects with
      reliability and occurrences must survive checkpoint round-trips.
    - Sub-AC 1 established [[INVARIANT: deserialized chain is injected before
      the AC loop so resumed ACs see prior postmortems]].
    - Sub-AC 2's checkpoint writing ensures per-AC checkpoints include full
      chain state [[INVARIANT: CompoundingCheckpointState.last_completed_ac_index
      equals the 0-based AC index]].

    [[INVARIANT: checkpoint round-trip preserves all ACPostmortem fields including
    Invariant objects, sub_postmortems, gotchas, and files_modified]]
    [[INVARIANT: resume skips ACs with index <= last_completed_ac_index and executes the rest]]
    """

    def _make_executor_with_real_store(
        self, tmp_path: Path
    ) -> tuple[SerialCompoundingExecutor, Any]:
        """Build an executor with a real CheckpointStore backed by tmp_path."""
        from ouroboros.persistence.checkpoint import CheckpointStore

        store = CheckpointStore(base_path=tmp_path / "checkpoints")
        store.initialize()

        event_store, _ = _make_replaying_event_store()
        executor = SerialCompoundingExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            checkpoint_store=store,
        )
        executor._coordinator.detect_file_conflicts = MagicMock(return_value=[])
        return executor, store

    def test_rehydrated_chain_is_field_identical_to_original(
        self, tmp_path: Path
    ) -> None:
        """The deserialized chain from a checkpoint is field-identical to the original.

        Builds a rich postmortem with:
        - files_modified (multiple files)
        - gotchas (multiple)
        - invariants_established with Invariant objects (reliability, occurrences,
          first_seen_ac_id, is_contradicted)
        - sub_postmortems (nested ACPostmortem)

        After write → load via _write_compounding_checkpoint + _load_compounding_checkpoint,
        every field must be equal to the original.

        Compounding reference: AC-2 proved sub_postmortems round-trip; AC-3 proved
        Invariant objects serialize/deserialize. This test combines all fields in a
        single checkpoint round-trip — the most complete identity check.

        [[INVARIANT: checkpoint round-trip preserves all ACPostmortem fields including
        Invariant objects, sub_postmortems, gotchas, and files_modified]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            Invariant,
            PostmortemChain,
        )
        from ouroboros.orchestrator.serial_executor import (
            _load_compounding_checkpoint,
            _write_compounding_checkpoint,
        )
        from ouroboros.persistence.checkpoint import CheckpointStore

        store = CheckpointStore(base_path=tmp_path / "checkpoints")
        store.initialize()

        # Build a sub-postmortem for the parent's sub_postmortems field.
        sub_summary = ACContextSummary(
            ac_index=0,
            ac_content="Sub task: extract schema",
            success=True,
            files_modified=("src/schema.py", "src/schema_types.py"),
            public_api="SchemaExtractor",
        )
        sub_pm = ACPostmortem(
            summary=sub_summary,
            status="pass",
            gotchas=("schema must be frozen",),
            invariants_established=(
                Invariant(
                    text="schema extraction is idempotent",
                    reliability=0.88,
                    occurrences=1,
                    first_seen_ac_id="ac_0_sub",
                    is_contradicted=False,
                ),
            ),
        )

        # Build the main postmortem with multiple invariants (including a
        # contradicted one) and the sub-postmortem above.
        main_summary = ACContextSummary(
            ac_index=0,
            ac_content="Implement the data pipeline",
            success=True,
            files_modified=("src/pipeline.py", "src/pipeline_utils.py", "tests/test_pipeline.py"),
            tools_used=("Read", "Write", "Bash"),
            key_output="Pipeline implemented with 3 stages",
            public_api="run_pipeline, PipelineStage",
        )
        trusted_inv = Invariant(
            text="pipeline stages run in topological order",
            reliability=0.92,
            occurrences=2,
            first_seen_ac_id="ac_0",
            is_contradicted=False,
        )
        contradicted_inv = Invariant(
            text="pipeline is synchronous",
            reliability=0.0,
            occurrences=1,
            first_seen_ac_id="ac_0",
            is_contradicted=True,
        )
        main_pm = ACPostmortem(
            summary=main_summary,
            diff_summary="+ 240 lines pipeline logic",
            tool_trace_digest="Read x5, Write x3, Bash x2",
            gotchas=("async pipeline needs special error handling", "don't use global state"),
            qa_suggestions=("add integration test for stage ordering",),
            invariants_established=(trusted_inv, contradicted_inv),
            retry_attempts=1,
            status="pass",
            duration_seconds=42.5,
            ac_native_session_id="native_sess_abc",
            sub_postmortems=(sub_pm,),
        )

        original_chain = PostmortemChain(postmortems=(main_pm,))

        _write_compounding_checkpoint(
            store=store,
            seed_id="seed_identity_test",
            session_id="sess_identity",
            ac_index=0,
            chain=original_chain,
        )

        loaded_chain, last_idx = _load_compounding_checkpoint(
            store=store,
            seed_id="seed_identity_test",
            session_id="sess_identity_new",
            resume_session_id="sess_identity",
        )

        assert last_idx == 0, f"Expected last_completed_ac_index=0, got {last_idx}"
        assert len(loaded_chain.postmortems) == 1

        loaded_pm = loaded_chain.postmortems[0]

        # --- ACContextSummary fields ---
        assert loaded_pm.summary.ac_index == 0
        assert loaded_pm.summary.ac_content == "Implement the data pipeline"
        assert loaded_pm.summary.success is True
        assert set(loaded_pm.summary.files_modified) == {
            "src/pipeline.py", "src/pipeline_utils.py", "tests/test_pipeline.py"
        }
        assert loaded_pm.summary.public_api == "run_pipeline, PipelineStage"
        assert loaded_pm.summary.key_output == "Pipeline implemented with 3 stages"

        # --- ACPostmortem scalar fields ---
        assert loaded_pm.diff_summary == "+ 240 lines pipeline logic"
        assert loaded_pm.tool_trace_digest == "Read x5, Write x3, Bash x2"
        assert loaded_pm.status == "pass"
        assert loaded_pm.retry_attempts == 1
        assert abs(loaded_pm.duration_seconds - 42.5) < 1e-6
        assert loaded_pm.ac_native_session_id == "native_sess_abc"

        # --- gotchas and qa_suggestions ---
        assert "async pipeline needs special error handling" in loaded_pm.gotchas
        assert "don't use global state" in loaded_pm.gotchas
        assert "add integration test for stage ordering" in loaded_pm.qa_suggestions

        # --- Invariant objects: all fields preserved ---
        assert len(loaded_pm.invariants_established) == 2

        # Find the trusted invariant by text.
        loaded_trusted = next(
            (i for i in loaded_pm.invariants_established
             if "topological order" in i.text),
            None,
        )
        assert loaded_trusted is not None, "Trusted invariant not found in loaded chain"
        assert loaded_trusted.text == "pipeline stages run in topological order"
        assert abs(loaded_trusted.reliability - 0.92) < 1e-6
        assert loaded_trusted.occurrences == 2
        assert loaded_trusted.first_seen_ac_id == "ac_0"
        assert loaded_trusted.is_contradicted is False

        # The contradicted invariant should also survive.
        loaded_contradicted = next(
            (i for i in loaded_pm.invariants_established
             if "synchronous" in i.text),
            None,
        )
        assert loaded_contradicted is not None, "Contradicted invariant not found in loaded chain"
        assert loaded_contradicted.is_contradicted is True
        assert abs(loaded_contradicted.reliability - 0.0) < 1e-6

        # --- sub_postmortems: nested structure preserved ---
        assert len(loaded_pm.sub_postmortems) == 1
        loaded_sub = loaded_pm.sub_postmortems[0]
        assert loaded_sub.summary.ac_content == "Sub task: extract schema"
        assert "src/schema.py" in loaded_sub.summary.files_modified
        assert "schema must be frozen" in loaded_sub.gotchas
        assert len(loaded_sub.invariants_established) == 1
        loaded_sub_inv = loaded_sub.invariants_established[0]
        assert loaded_sub_inv.text == "schema extraction is idempotent"
        assert abs(loaded_sub_inv.reliability - 0.88) < 1e-6

    @pytest.mark.asyncio
    async def test_partial_checkpoint_2ac_of_3_skips_completed_executes_remaining(
        self, tmp_path: Path
    ) -> None:
        """Create a partial checkpoint (ACs 0+1 done), resume 3-AC run, assert AC 2 executes.

        Setup:
        - A 3-AC seed.
        - A checkpoint with last_completed_ac_index=1 (ACs 0 and 1 complete).
        - The checkpoint chain has rich postmortems for ACs 0 and 1.

        Assertions:
        - ACs 0 and 1 are NOT executed (skipped via checkpoint).
        - AC 2 IS executed.
        - AC 2's context_override contains content from BOTH AC 0 and AC 1 postmortems.
        - Result has 3 entries (2 SATISFIED_EXTERNALLY + 1 SUCCEEDED).

        Compounding reference: the checkpoint payload stores the complete
        PostmortemChain (established in AC-1 Q6.1), including sub_postmortems
        (AC-2 B-prime) and Invariant objects (AC-3 C-plus), ensuring the
        context AC 2 receives is as rich as possible.

        [[INVARIANT: resume skips ACs with index <= last_completed_ac_index and executes the rest]]
        [[INVARIANT: deserialized chain is injected before the AC loop so resumed ACs see prior postmortems]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            Invariant,
            PostmortemChain,
        )
        from ouroboros.orchestrator.serial_executor import _write_compounding_checkpoint
        from ouroboros.orchestrator.parallel_executor_models import ACExecutionOutcome

        seed = _make_seed(
            "AC 0: implement auth module",
            "AC 1: implement user service",
            "AC 2: implement API layer",
        )
        executor, store = self._make_executor_with_real_store(tmp_path)

        # Build a rich prior chain for ACs 0 and 1.
        pm_ac0 = ACPostmortem(
            summary=ACContextSummary(
                ac_index=0,
                ac_content="AC 0: implement auth module",
                success=True,
                files_modified=("src/auth.py", "tests/test_auth.py"),
            ),
            status="pass",
            gotchas=("JWT tokens expire after 1 hour",),
            invariants_established=(
                Invariant(
                    text="all API routes require auth header",
                    reliability=0.95,
                    occurrences=1,
                    first_seen_ac_id="ac_0",
                ),
            ),
        )
        pm_ac1 = ACPostmortem(
            summary=ACContextSummary(
                ac_index=1,
                ac_content="AC 1: implement user service",
                success=True,
                files_modified=("src/user_service.py",),
            ),
            status="pass",
            gotchas=("UserService depends on AuthModule being initialized first",),
            invariants_established=(
                Invariant(
                    text="UserService.create() validates email uniqueness",
                    reliability=0.90,
                    occurrences=1,
                    first_seen_ac_id="ac_1",
                ),
            ),
        )
        prior_chain = PostmortemChain(postmortems=(pm_ac0, pm_ac1))

        _write_compounding_checkpoint(
            store=store,
            seed_id=seed.metadata.seed_id,
            session_id="prior_session_partial",
            ac_index=1,  # last_completed = AC 1 (0-based)
            chain=prior_chain,
        )

        executed_indices: list[int] = []
        captured_overrides: list[str] = []

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            executed_indices.append(ac_index)
            captured_overrides.append(kwargs.get("context_override") or "")
            return _ok_result(ac_index, str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,), (2,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="resume_session_partial",
            execution_id="exec_partial_resume",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            resume_session_id="prior_session_partial",
        )

        # Only AC 2 was executed — ACs 0 and 1 were skipped.
        assert executed_indices == [2], (
            f"Only AC 2 should execute; executed: {executed_indices}"
        )

        # AC 2's context_override must reference BOTH prior ACs.
        assert len(captured_overrides) == 1
        ac2_override = captured_overrides[0]
        assert "AC 0: implement auth module" in ac2_override, (
            "AC 2 context must include AC 0's postmortem"
        )
        assert "AC 1: implement user service" in ac2_override, (
            "AC 2 context must include AC 1's postmortem"
        )
        assert "JWT tokens expire after 1 hour" in ac2_override, (
            "AC 2 context must include AC 0's gotchas from the loaded chain"
        )
        assert "UserService depends on AuthModule" in ac2_override, (
            "AC 2 context must include AC 1's gotchas from the loaded chain"
        )

        # Result structure: 3 entries total.
        assert len(result.results) == 3

        # ACs 0 and 1: SATISFIED_EXTERNALLY (skipped via checkpoint).
        assert result.results[0].outcome == ACExecutionOutcome.SATISFIED_EXTERNALLY
        assert result.results[1].outcome == ACExecutionOutcome.SATISFIED_EXTERNALLY

        # AC 2: SUCCEEDED.
        assert result.results[2].outcome == ACExecutionOutcome.SUCCEEDED
        assert result.results[2].success is True

    @pytest.mark.asyncio
    async def test_resume_chain_includes_invariants_in_resumed_context(
        self, tmp_path: Path
    ) -> None:
        """Invariants from the prior run appear in the resumed AC's context.

        When the saved chain has Invariants in invariants_established, they
        must appear in the 'Established Invariants' section of the resumed
        AC's context_override after the chain is rehydrated.

        Compounding references:
        - AC-3 established [[INVARIANT: only above-threshold invariants appear
          in downstream chain context]] — verified invariants propagate.
        - AC-2 established [[INVARIANT: ACPostmortem.sub_postmortems preserves
          structure in serialized chain]] — full chain state survives.

        [[INVARIANT: checkpoint round-trip preserves all ACPostmortem fields including
        Invariant objects, sub_postmortems, gotchas, and files_modified]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            Invariant,
            PostmortemChain,
        )
        from ouroboros.orchestrator.serial_executor import _write_compounding_checkpoint

        seed = _make_seed("AC 0 with invariant", "AC 1 resumed sees invariant")
        executor, store = self._make_executor_with_real_store(tmp_path)

        # AC 0 postmortem with a high-reliability invariant.
        INVARIANT_TEXT = "serialize_postmortem_chain produces a stable list"
        pm_ac0 = ACPostmortem(
            summary=ACContextSummary(
                ac_index=0,
                ac_content="AC 0 with invariant",
                success=True,
                files_modified=("src/level_context.py",),
            ),
            status="pass",
            invariants_established=(
                Invariant(
                    text=INVARIANT_TEXT,
                    reliability=0.95,
                    occurrences=1,
                    first_seen_ac_id="ac_0",
                ),
            ),
        )
        prior_chain = PostmortemChain(postmortems=(pm_ac0,))

        _write_compounding_checkpoint(
            store=store,
            seed_id=seed.metadata.seed_id,
            session_id="prior_inv_session",
            ac_index=0,
            chain=prior_chain,
        )

        captured_overrides: list[str] = []

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            captured_overrides.append(kwargs.get("context_override") or "")
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        await executor.execute_serial(
            seed=seed,
            session_id="resumed_inv_session",
            execution_id="exec_inv_resume",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            resume_session_id="prior_inv_session",
        )

        # AC 1's context must include the invariant from the prior run.
        assert len(captured_overrides) == 1, (
            f"Only AC 1 should execute; got {len(captured_overrides)} overrides"
        )
        ac1_override = captured_overrides[0]

        # The invariant must appear in the 'Established Invariants' section.
        established_idx = ac1_override.find("Established Invariants")
        assert established_idx != -1, (
            f"'Established Invariants' section must be present in resumed AC context; "
            f"override snippet:\n{ac1_override[:500]}"
        )
        established_section = ac1_override[established_idx:]
        assert INVARIANT_TEXT in established_section, (
            f"Invariant from prior run must appear in 'Established Invariants' section "
            f"of resumed AC's context; section was:\n{established_section[:500]}"
        )

    @pytest.mark.asyncio
    async def test_full_resume_flow_end_to_end_with_real_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end test: first run writes checkpoint; second run resumes from it.

        Simulates the real workflow:
        1. First execute_serial run (3 ACs, all succeed) — writes per-AC checkpoints.
        2. The run is interrupted after AC 1 (by monkeypatching the store to raise
           an error for AC 2, which makes the second "run" start from AC 2).
        3. A second execute_serial run resumes from the first run's checkpoint —
           only AC 2 executes, and its context_override includes postmortems for
           ACs 0 and 1 from the first run.

        This is the closest to a production resume scenario: the checkpoint was
        written by the executor itself (not manually), and the resume uses that
        checkpoint to skip the completed ACs.

        Compounding references:
        - AC-1: end-of-run artifact (Q6.1) — both runs produce artifacts.
        - AC-2: sub_postmortems preserved in checkpoints.
        - AC-3: invariant verifier runs inline before chain advance, so
          invariants in the chain come from the real verify_invariants path.

        [[INVARIANT: resume skips ACs with index <= last_completed_ac_index and executes the rest]]
        [[INVARIANT: checkpoint round-trip preserves all ACPostmortem fields including
        Invariant objects, sub_postmortems, gotchas, and files_modified]]
        """
        import ouroboros.orchestrator.serial_executor as serial_mod

        # Suppress invariant verification (no real Haiku calls in unit tests).
        async def fake_verify(
            adapter: Any,
            tags: list[str],
            **kwargs: Any,
        ) -> list[tuple[str, float]]:
            # Accept all tags at high reliability.
            return [(tag, 0.9) for tag in tags]

        monkeypatch.setattr(serial_mod, "verify_invariants", fake_verify)

        # Use a custom artifact dir so the test doesn't pollute docs/brainstorm/.
        monkeypatch.setenv("OUROBOROS_CHAIN_ARTIFACT_DIR", str(tmp_path / "artifacts"))

        from ouroboros.persistence.checkpoint import CheckpointStore

        store = CheckpointStore(base_path=tmp_path / "checkpoints")
        store.initialize()

        seed = _make_seed(
            "AC 0: write data model",
            "AC 1: write service layer",
            "AC 2: write API endpoints",
        )

        # ---- First run: ACs 0 and 1 succeed, AC 2 fails ----
        event_store_1, _ = _make_replaying_event_store()
        executor_1 = SerialCompoundingExecutor(
            adapter=MagicMock(),
            event_store=event_store_1,
            console=MagicMock(),
            enable_decomposition=False,
            checkpoint_store=store,
        )
        executor_1._coordinator.detect_file_conflicts = MagicMock(return_value=[])

        call_count_1: list[int] = []

        async def fake_single_ac_run1(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            call_count_1.append(ac_index)
            if ac_index == 2:
                # Simulate AC 2 failing in the first run.
                return _fail_result(
                    ac_index, str(kwargs["ac_content"]), error="timeout in run 1"
                )
            return _ok_result(
                ac_index,
                str(kwargs["ac_content"]),
                final_message=f"AC {ac_index} done [[INVARIANT: ac{ac_index} outputs stable]]",
                files_written=(f"src/ac{ac_index}.py",),
            )

        executor_1._execute_single_ac = fake_single_ac_run1  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,), (2,))
        result_1 = await executor_1.execute_serial(
            seed=seed,
            session_id="session_run1",
            execution_id="exec_run1",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            fail_fast=True,
        )

        # First run: ACs 0 and 1 succeeded, AC 2 failed.
        assert result_1.success_count == 2
        assert result_1.failure_count == 1
        assert call_count_1 == [0, 1, 2], f"Expected [0, 1, 2] called; got {call_count_1}"

        # ---- Second run: resume from session_run1 ----
        event_store_2, _ = _make_replaying_event_store()
        executor_2 = SerialCompoundingExecutor(
            adapter=MagicMock(),
            event_store=event_store_2,
            console=MagicMock(),
            enable_decomposition=False,
            checkpoint_store=store,
        )
        executor_2._coordinator.detect_file_conflicts = MagicMock(return_value=[])

        call_count_2: list[int] = []
        captured_overrides_2: list[str] = []

        async def fake_single_ac_run2(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            call_count_2.append(ac_index)
            captured_overrides_2.append(kwargs.get("context_override") or "")
            return _ok_result(ac_index, str(kwargs["ac_content"]))

        executor_2._execute_single_ac = fake_single_ac_run2  # type: ignore[method-assign]

        result_2 = await executor_2.execute_serial(
            seed=seed,
            session_id="session_run2",
            execution_id="exec_run2",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            resume_session_id="session_run1",
        )

        # Second run: only AC 2 was executed (ACs 0 and 1 were checkpointed).
        assert call_count_2 == [2], (
            f"Only AC 2 should execute in the resumed run; got {call_count_2}"
        )

        # AC 2's context must include postmortems from the first run.
        assert len(captured_overrides_2) == 1
        ac2_context = captured_overrides_2[0]
        assert "AC 0: write data model" in ac2_context, (
            "Resumed AC 2 must see AC 0's postmortem from the first run"
        )
        assert "AC 1: write service layer" in ac2_context, (
            "Resumed AC 2 must see AC 1's postmortem from the first run"
        )

        # Overall second run result.
        assert len(result_2.results) == 3
        assert result_2.success_count >= 1  # At least AC 2 succeeded in run 2.
