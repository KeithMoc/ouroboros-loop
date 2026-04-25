"""Serial compounding AC executor.

Subclass of :class:`ouroboros.orchestrator.parallel_executor.ParallelACExecutor`
that runs acceptance criteria strictly one at a time, threading a rolling
postmortem chain from each AC into the prompt of the next.

Design (phase 1):
- Reuses ``_execute_single_ac`` from the parallel base class via the
  ``context_override`` kwarg so the ~1150-line prompt+runtime machinery is
  NOT duplicated or extracted.
- Linearizes the dependency plan into a single total order by walking
  stages then AC indices; dependency semantics are respected because
  ``StagedExecutionPlan`` already produces stages in topological order.
- After each AC, builds an :class:`ACPostmortem` from the existing
  :func:`extract_level_context` summarization machinery, appends it to the
  rolling chain, and emits an ``execution.ac.postmortem.captured`` event.
- On failure after retries, the loop halts (fail-fast) matching the
  "atomic" semantics requested by the user. The accumulated postmortems
  are still returned for inspection.

Out of scope for phase 1 (follow-up milestones):
- Per-AC git commits + diff_summary population (M5).
- AC-granular checkpoint/resume (M6).
- Inline QA + retry-with-QA feedback (M7).
- Prompt-cache-friendly structured system blocks (phase 2).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ouroboros.orchestrator.events import create_ac_postmortem_captured_event
from ouroboros.orchestrator.level_context import (
    ACContextSummary,
    ACPostmortem,
    PostmortemChain,
    PostmortemStatus,
    build_postmortem_chain_prompt,
    extract_level_context,
    serialize_postmortem_chain,
)
from ouroboros.orchestrator.parallel_executor import (
    ParallelACExecutor,
    _STALL_SENTINEL,
)
from ouroboros.orchestrator.parallel_executor_models import (
    ACExecutionOutcome,
    ACExecutionResult,
    ParallelExecutionResult,
    ParallelExecutionStageResult,
)
from ouroboros.observability.logging import get_logger

if TYPE_CHECKING:
    from ouroboros.core.seed import Seed
    from ouroboros.orchestrator.dependency_analyzer import (
        DependencyGraph,
        StagedExecutionPlan,
    )
    from ouroboros.orchestrator.mcp_config import MCPToolDefinition

log = get_logger(__name__)

# Default directory for chain artifact output. Override with OUROBOROS_CHAIN_ARTIFACT_DIR.
_DEFAULT_CHAIN_ARTIFACT_DIR = "docs/brainstorm"


def _render_chain_as_markdown(
    chain: PostmortemChain,
    session_id: str,
    execution_id: str,
) -> str:
    """Render a PostmortemChain as a human-readable markdown artifact.

    Uses ``serialize_postmortem_chain`` as the single data source so there
    is no second serialization path. Format per AC:

        ## AC <n> [<status>]
        - Files modified: ...
        - Gotchas: ...
        - Public API changes: ...

    Args:
        chain: The postmortem chain to render.
        session_id: Session ID for the header.
        execution_id: Execution ID for the header.

    Returns:
        Markdown string with one section per AC.
    """
    now_str = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines: list[str] = [
        "# Postmortem Chain",
        "",
        f"**Session:** `{session_id}`  ",
        f"**Execution:** `{execution_id}`  ",
        f"**Written:** {now_str}  ",
        f"**ACs:** {len(chain.postmortems)}",
        "",
    ]

    serialized = serialize_postmortem_chain(chain)
    for entry in serialized:
        summary = entry.get("summary") or {}
        ac_index = summary.get("ac_index", 0)
        ac_content = summary.get("ac_content", "")
        status = entry.get("status", "pass")
        files_modified = summary.get("files_modified") or []
        public_api = summary.get("public_api") or ""
        gotchas = entry.get("gotchas") or []
        invariants = entry.get("invariants_established") or []
        duration = entry.get("duration_seconds", 0.0)
        retry_attempts = entry.get("retry_attempts", 0)

        lines.append(f"## AC {ac_index + 1} [{status}]")
        lines.append("")
        lines.append(f"**Task:** {ac_content}")
        if duration:
            lines.append(f"**Duration:** {duration:.1f}s")
        if retry_attempts:
            lines.append(f"**Retries:** {retry_attempts}")

        if files_modified:
            files_str = ", ".join(str(f) for f in files_modified)
            lines.append(f"- Files modified: {files_str}")
        else:
            lines.append("- Files modified: (none recorded)")

        if gotchas:
            gotchas_str = "; ".join(str(g) for g in gotchas)
            lines.append(f"- Gotchas: {gotchas_str}")
        else:
            lines.append("- Gotchas: (none)")

        if public_api:
            lines.append(f"- Public API changes: {public_api}")
        else:
            lines.append("- Public API changes: (none recorded)")

        if invariants:
            # Invariants are serialized as dicts via dataclasses.asdict(); extract text.
            inv_texts = [
                i.get("text", str(i)) if isinstance(i, dict) else str(i)
                for i in invariants
            ]
            lines.append(f"- Invariants established: {'; '.join(inv_texts)}")

        lines.append("")

    return "\n".join(lines)


def write_chain_artifact(
    chain: PostmortemChain,
    session_id: str,
    execution_id: str,
    *,
    artifact_dir: str | None = None,
) -> Path:
    """Write the PostmortemChain to a markdown artifact file.

    The output directory defaults to ``docs/brainstorm`` but can be
    overridden via the ``OUROBOROS_CHAIN_ARTIFACT_DIR`` environment variable
    or the ``artifact_dir`` argument (explicit arg takes precedence over env var).

    The directory is created defensively (``parents=True, exist_ok=True``) so
    callers do not need to pre-create it.

    This function is intentionally synchronous — it is called after the
    serial loop completes and must not introduce async complexity.

    Args:
        chain: The chain to serialize.
        session_id: Session id used in the filename.
        execution_id: Execution id used in the file header.
        artifact_dir: Override directory. Falls back to env var, then default.

    Returns:
        Path of the written artifact file.
    """
    if artifact_dir is None:
        artifact_dir = os.environ.get(
            "OUROBOROS_CHAIN_ARTIFACT_DIR", _DEFAULT_CHAIN_ARTIFACT_DIR
        )

    out_dir = Path(artifact_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    filename = f"chain-{session_id}-{timestamp}.md"
    artifact_path = out_dir / filename

    content = _render_chain_as_markdown(chain, session_id, execution_id)
    artifact_path.write_text(content, encoding="utf-8")

    log.info(
        "serial_executor.chain_artifact.written",
        path=str(artifact_path),
        session_id=session_id,
        postmortems=len(chain.postmortems),
    )
    return artifact_path


def linearize_execution_plan(execution_plan: "StagedExecutionPlan") -> tuple[int, ...]:
    """Flatten a staged execution plan into a total AC order.

    Walks stages in order (which are already topologically sorted by the
    planner), emitting AC indices within each stage in sorted order so the
    result is deterministic given the same plan.

    Returns:
        Tuple of AC indices in the order serial execution should visit them.
    """
    ordered: list[int] = []
    for stage in execution_plan.stages:
        for ac_index in sorted(stage.ac_indices):
            if ac_index not in ordered:
                ordered.append(ac_index)
    return tuple(ordered)


class SerialCompoundingExecutor(ParallelACExecutor):
    """Run ACs one at a time, compounding context via postmortems.

    Extends :class:`ParallelACExecutor` to reuse the per-AC runtime, retry,
    decomposition, and event-emission machinery without extracting it.
    Only the outer orchestration (linearization + postmortem threading)
    differs.
    """

    async def execute_serial(
        self,
        seed: "Seed",
        *,
        session_id: str,
        execution_id: str,
        tools: list[str],
        system_prompt: str,
        tool_catalog: "tuple[MCPToolDefinition, ...] | None" = None,
        dependency_graph: "DependencyGraph | None" = None,
        execution_plan: "StagedExecutionPlan | None" = None,
        fail_fast: bool = True,
        externally_satisfied_acs: "dict[int, dict[str, Any]] | None" = None,
    ) -> ParallelExecutionResult:
        """Execute ACs strictly serially with compounding postmortems.

        Args:
            seed: Seed specification whose ACs are being executed.
            session_id: Parent session id for tracking and event aggregation.
            execution_id: Execution id for event tracking.
            tools: Tool names available to the agent.
            system_prompt: System prompt used for every AC (pinned for the
                whole run to keep the prefix stable for prompt-cache hits
                when the adapter supports them).
            tool_catalog: Optional tool metadata catalog.
            dependency_graph: Dependency graph; used only when
                ``execution_plan`` is not supplied.
            execution_plan: Pre-built staged plan. When absent,
                ``dependency_graph.to_execution_plan()`` is used.
            fail_fast: When True (default), halt at the first AC that
                fails after retries. The compounding chain up to that
                point is still returned. When False, continue to the
                next AC with a failed postmortem recorded.
            externally_satisfied_acs: Map of AC indices already satisfied
                externally. When provided, those ACs will be skipped and
                recorded with SATISFIED_EXTERNALLY outcome.

        Returns:
            ParallelExecutionResult with one stage per AC so downstream
            progress tooling sees a structurally similar shape to the
            parallel path.
        """
        if execution_plan is None:
            if dependency_graph is None:
                msg = "execution_plan is required when dependency_graph is not provided"
                raise ValueError(msg)
            execution_plan = dependency_graph.to_execution_plan()

        ac_order = linearize_execution_plan(execution_plan)
        start_time = datetime.now(UTC)

        chain = PostmortemChain()
        results: list[ACExecutionResult] = []
        stages: list[ParallelExecutionStageResult] = []
        execution_counters = {"messages_count": 0, "tool_calls_count": 0}
        external_completed = externally_satisfied_acs or {}

        log.info(
            "serial_executor.started",
            session_id=session_id,
            execution_id=execution_id,
            total_acs=len(ac_order),
            fail_fast=fail_fast,
        )

        halted = False
        for position, ac_index in enumerate(ac_order):
            if halted:
                # Record remaining ACs as blocked so downstream tooling sees
                # a complete picture without the serial loop running them.
                blocked = ACExecutionResult(
                    ac_index=ac_index,
                    ac_content=seed.acceptance_criteria[ac_index],
                    success=False,
                    error="blocked: serial loop halted after upstream AC failure",
                    outcome=ACExecutionOutcome.BLOCKED,
                )
                results.append(blocked)
                stages.append(
                    ParallelExecutionStageResult(
                        stage_index=position,
                        ac_indices=(ac_index,),
                        results=(blocked,),
                        started=False,
                    )
                )
                continue

            # Check if AC is externally satisfied; skip execution if so.
            if ac_index in external_completed:
                metadata = external_completed.get(ac_index, {})
                reason = metadata.get("reason")
                commit = metadata.get("commit")
                notes: list[str] = [
                    "Skipped via --skip-completed; existing working tree state is treated as satisfied."
                ]
                if isinstance(reason, str) and reason.strip():
                    notes.append(f"Reason: {reason.strip()}")
                if isinstance(commit, str) and commit.strip():
                    notes.append(f"Commit: {commit.strip()}")

                satisfied_result = ACExecutionResult(
                    ac_index=ac_index,
                    ac_content=seed.acceptance_criteria[ac_index],
                    success=True,
                    final_message="\n".join(notes),
                    retry_attempt=0,
                    outcome=ACExecutionOutcome.SATISFIED_EXTERNALLY,
                )
                results.append(satisfied_result)
                stages.append(
                    ParallelExecutionStageResult(
                        stage_index=position,
                        ac_indices=(ac_index,),
                        results=(satisfied_result,),
                        started=False,
                    )
                )
                log.info(
                    "serial_executor.ac.satisfied_externally",
                    session_id=session_id,
                    ac_index=ac_index,
                    reason=reason,
                    commit=commit,
                )
                # Still add to postmortem chain to provide context
                postmortem = self._build_postmortem_from_result(
                    satisfied_result, workspace_root=self._task_cwd
                )
                chain = chain.append(postmortem)
                continue

            # Compose the compounding-context section from the current chain.
            context_section = build_postmortem_chain_prompt(chain)

            ac_content = seed.acceptance_criteria[ac_index]

            self._console.print(
                f"[bold cyan]Serial AC {ac_index + 1}/{len(ac_order)}[/bold cyan]"
                f" [{len(chain.postmortems)} postmortems in chain]"
            )
            self._flush_console()

            try:
                result = await self._execute_single_ac(
                    ac_index=ac_index,
                    ac_content=ac_content,
                    session_id=session_id,
                    tools=tools,
                    tool_catalog=tool_catalog,
                    system_prompt=system_prompt,
                    seed_goal=seed.goal,
                    depth=0,
                    execution_id=execution_id,
                    level_contexts=None,
                    sibling_acs=None,  # serial: no siblings
                    retry_attempt=0,
                    execution_counters=execution_counters,
                    context_override=context_section,
                )
            except Exception as exc:  # noqa: BLE001
                log.exception(
                    "serial_executor.ac.unexpected_error",
                    session_id=session_id,
                    ac_index=ac_index,
                    error=str(exc),
                )
                result = ACExecutionResult(
                    ac_index=ac_index,
                    ac_content=ac_content,
                    success=False,
                    error=f"unexpected executor error: {exc}",
                    outcome=ACExecutionOutcome.FAILED,
                )

            results.append(result)

            postmortem = self._build_postmortem_from_result(
                result, workspace_root=self._task_cwd
            )
            chain = chain.append(postmortem)

            await self._safe_emit_event(
                create_ac_postmortem_captured_event(
                    session_id=session_id,
                    ac_index=ac_index,
                    ac_id=f"ac_{ac_index}",
                    postmortem=postmortem,
                    execution_id=execution_id,
                    retry_attempt=result.retry_attempt,
                )
            )

            stages.append(
                ParallelExecutionStageResult(
                    stage_index=position,
                    ac_indices=(ac_index,),
                    results=(result,),
                    started=True,
                )
            )

            if not result.success and fail_fast:
                log.warning(
                    "serial_executor.halting_on_failure",
                    session_id=session_id,
                    ac_index=ac_index,
                    error=result.error,
                )
                halted = True

        total_duration = (datetime.now(UTC) - start_time).total_seconds()
        success_count = sum(
            1 for r in results if r.outcome == ACExecutionOutcome.SUCCEEDED
        )
        externally_satisfied_count = sum(
            1 for r in results if r.outcome == ACExecutionOutcome.SATISFIED_EXTERNALLY
        )
        failure_count = sum(
            1 for r in results if r.outcome == ACExecutionOutcome.FAILED
        )
        blocked_count = sum(
            1 for r in results if r.outcome == ACExecutionOutcome.BLOCKED
        )
        # Serial execution has no INVALID outcomes (all ACs are in the linearized plan),
        # so skipped_count equals blocked_count.
        skipped_count = blocked_count

        log.info(
            "serial_executor.completed",
            session_id=session_id,
            total_acs=len(ac_order),
            success=success_count,
            externally_satisfied=externally_satisfied_count,
            failed=failure_count,
            blocked=blocked_count,
            skipped=skipped_count,
            duration_seconds=total_duration,
            postmortems_captured=len(chain.postmortems),
        )

        # AC-1 (Q6.1): Write end-of-run chain artifact. Always produced — even
        # for failed/partial runs — so crashed runs leave an inspectable chain.
        # Failures here are logged but never propagate to the caller.
        if chain.postmortems:
            try:
                write_chain_artifact(
                    chain,
                    session_id=session_id,
                    execution_id=execution_id,
                )
            except Exception as artifact_exc:  # noqa: BLE001
                log.warning(
                    "serial_executor.chain_artifact.write_failed",
                    session_id=session_id,
                    error=str(artifact_exc),
                )

        return ParallelExecutionResult(
            results=tuple(results),
            success_count=success_count,
            failure_count=failure_count,
            externally_satisfied_count=externally_satisfied_count,
            blocked_count=blocked_count,
            skipped_count=skipped_count,
            stages=tuple(stages),
            total_messages=execution_counters.get("messages_count", 0),
            total_duration_seconds=total_duration,
        )

    @staticmethod
    def _build_postmortem_from_result(
        result: ACExecutionResult,
        *,
        workspace_root: str | None,
    ) -> ACPostmortem:
        """Derive an ACPostmortem from an ACExecutionResult.

        Uses the existing :func:`extract_level_context` summarization
        (which already folds tool-use events into files_modified, tools_used,
        key_output, and public_api) for a deterministic reconstruction of
        the factual half of the postmortem. The compounding-specific fields
        (diff_summary, gotchas, qa_suggestions, invariants_established)
        remain empty in phase 1 — populated by later milestones.
        """
        # extract_level_context expects a list[tuple[idx, content, success, msgs, final_msg]]
        level_ctx = extract_level_context(
            ac_results=[
                (
                    result.ac_index,
                    result.ac_content,
                    result.success,
                    result.messages,
                    result.final_message,
                )
            ],
            level_num=0,
            workspace_root=workspace_root or "",
        )
        if level_ctx.completed_acs:
            summary = level_ctx.completed_acs[0]
        else:  # pragma: no cover — extract_level_context always returns one summary per input
            summary = ACContextSummary(
                ac_index=result.ac_index,
                ac_content=result.ac_content,
                success=result.success,
            )

        status: PostmortemStatus
        if result.success:
            status = "pass"
        elif result.outcome == ACExecutionOutcome.BLOCKED:
            status = "partial"
        elif (
            result.error == _STALL_SENTINEL
            or result.outcome == ACExecutionOutcome.FAILED
        ):
            status = "fail"
        else:
            status = "fail"

        gotchas: tuple[str, ...] = ()
        if not result.success and result.error:
            gotchas = (result.error,)

        # B-prime: if the result has sub-results (decomposed AC), recursively build
        # sub-postmortems and flatten their data into the parent postmortem.
        sub_pms: tuple[ACPostmortem, ...] = ()
        if result.sub_results:
            sub_pms = tuple(
                SerialCompoundingExecutor._build_postmortem_from_result(
                    sub_result,
                    workspace_root=workspace_root,
                )
                for sub_result in result.sub_results
            )

            # Flatten files_modified: union of parent + all sub-postmortems (order-preserving, dedup).
            seen_files: dict[str, None] = dict.fromkeys(summary.files_modified)
            for sub_pm in sub_pms:
                for f in sub_pm.summary.files_modified:
                    seen_files.setdefault(f, None)
            flat_files: tuple[str, ...] = tuple(seen_files)

            # Flatten gotchas: parent's + all sub-postmortems' gotchas.
            flat_gotchas: tuple[str, ...] = gotchas + tuple(
                g for sub_pm in sub_pms for g in sub_pm.gotchas
            )

            # Flatten public_api: join non-empty strings, order-preserving, dedup.
            api_parts: list[str] = []
            if summary.public_api:
                api_parts.append(summary.public_api)
            for sub_pm in sub_pms:
                if sub_pm.summary.public_api and sub_pm.summary.public_api not in api_parts:
                    api_parts.append(sub_pm.summary.public_api)
            flat_public_api = "; ".join(api_parts)

            # Replace summary with the flattened version (frozen dataclass — create new).
            summary = ACContextSummary(
                ac_index=summary.ac_index,
                ac_content=summary.ac_content,
                success=summary.success,
                tools_used=summary.tools_used,
                files_modified=flat_files,
                key_output=summary.key_output,
                public_api=flat_public_api,
            )
            gotchas = flat_gotchas

        return ACPostmortem(
            summary=summary,
            status=status,
            retry_attempts=result.retry_attempt,
            duration_seconds=result.duration_seconds,
            ac_native_session_id=result.session_id,
            gotchas=gotchas,
            sub_postmortems=sub_pms,
        )


__all__ = [
    "SerialCompoundingExecutor",
    "linearize_execution_plan",
    "write_chain_artifact",
]