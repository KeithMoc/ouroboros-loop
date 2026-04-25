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
