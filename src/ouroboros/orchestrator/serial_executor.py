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
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ouroboros.orchestrator.events import create_ac_postmortem_captured_event
from ouroboros.orchestrator.level_context import (
    ACContextSummary,
    ACPostmortem,
    Invariant,
    PostmortemChain,
    PostmortemStatus,
    build_postmortem_chain_prompt,
    deserialize_postmortem_chain,
    extract_invariant_tags,
    extract_level_context,
    serialize_postmortem_chain,
)
from ouroboros.persistence.checkpoint import (
    CheckpointData,
    CompoundingCheckpointState,
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

# Q3 (C-plus): Invariant reliability gate defaults.
# OUROBOROS_INVARIANT_MIN_RELIABILITY — minimum score for an invariant to be stored.
_DEFAULT_INVARIANT_MIN_RELIABILITY = 0.7
# Regex to extract a float score from a Haiku response (first 0.0-1.0 match).
_HAIKU_SCORE_RE = re.compile(r"\b(1\.0+|0\.\d+)\b")
# Fallback reliability when the verifier response cannot be parsed.
_HAIKU_SCORE_FALLBACK = 0.5


def _get_min_reliability() -> float:
    """Return the minimum reliability threshold for invariant inclusion.

    Reads ``OUROBOROS_INVARIANT_MIN_RELIABILITY`` env var; defaults to 0.7.
    Invalid values fall back to the default silently.
    """
    raw = os.environ.get("OUROBOROS_INVARIANT_MIN_RELIABILITY", "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return _DEFAULT_INVARIANT_MIN_RELIABILITY


async def _verify_single_tag(
    adapter: Any,
    tag: str,
    *,
    ac_trace: str,
    files_modified: list[str],
    model: str,
) -> float:
    """Ask the Haiku verifier to score one [[INVARIANT]] tag.

    Sends a short prompt asking the model to return a reliability score
    0.0–1.0. Parses the first float in the response. On any error
    (API failure, unparseable response) returns :data:`_HAIKU_SCORE_FALLBACK`.

    Args:
        adapter: LLM adapter with a :meth:`complete` method.
        tag: Invariant text to verify.
        ac_trace: Final message from the AC (work summary / trace).
        files_modified: Files changed during the AC.
        model: Model identifier to use for the call.

    Returns:
        Reliability score in [0.0, 1.0].
    """
    from ouroboros.providers.base import CompletionConfig, Message, MessageRole

    files_str = ", ".join(files_modified) if files_modified else "(none)"
    trace_preview = (ac_trace or "")[:800]

    user_content = (
        "You are a fact-checking assistant for a software development workflow.\n\n"
        "An AI agent declared the following invariant after completing a task:\n\n"
        f'  Invariant: "{tag}"\n\n'
        "Context:\n"
        f"  Files modified: {files_str}\n"
        f"  Agent trace / final output:\n  {trace_preview}\n\n"
        "Is this invariant actually supported by the evidence above?\n"
        "Reply with ONLY a single number between 0.0 and 1.0, where:\n"
        "  1.0 = definitely supported\n"
        "  0.5 = uncertain\n"
        "  0.0 = not supported or contradicted\n"
        "Be conservative — prefer 0.5 when evidence is ambiguous.\n"
        "Reply with the number only, nothing else."
    )

    config = CompletionConfig(model=model, temperature=0.0, max_tokens=16)
    messages = [Message(role=MessageRole.USER, content=user_content)]

    try:
        result = await adapter.complete(messages, config)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "invariant_verifier.adapter_error",
            tag=tag[:60],
            error=str(exc),
        )
        return _HAIKU_SCORE_FALLBACK

    if result.is_err:
        log.warning(
            "invariant_verifier.llm_failed",
            tag=tag[:60],
            error=str(result.error),
        )
        return _HAIKU_SCORE_FALLBACK

    raw_text = (result.value.content or "").strip()
    match = _HAIKU_SCORE_RE.search(raw_text)
    if match:
        try:
            score = float(match.group(1))
            return max(0.0, min(1.0, score))
        except ValueError:
            pass

    log.warning(
        "invariant_verifier.unparseable_score",
        tag=tag[:60],
        response_preview=raw_text[:80],
    )
    return _HAIKU_SCORE_FALLBACK


async def verify_invariants(
    adapter: Any,
    tags: list[str],
    *,
    ac_trace: str,
    files_modified: list[str],
    model: str | None = None,
) -> list[tuple[str, float]]:
    """Verify ``[[INVARIANT]]`` tags via a Haiku model call per tag.

    Implements the Q3 (C-plus) Haiku verifier gate.  For each tag, a short
    prompt is sent to the ``model`` asking for a reliability score 0.0–1.0.
    Results are returned in input order; errors result in :data:`_HAIKU_SCORE_FALLBACK`.

    This function is intentionally **inline / blocking** — callers must
    ``await`` it before advancing the postmortem chain so the verified
    invariants are visible to the next AC's prompt.

    Args:
        adapter: LLM adapter used for completion calls (must implement
            the :class:`~ouroboros.providers.base.LLMAdapter` protocol).
        tags: Extracted invariant text strings (from :func:`~ouroboros.orchestrator.level_context.extract_invariant_tags`).
        ac_trace: Final message from the AC, used as evidence for verification.
        files_modified: Files changed during the AC, used as evidence.
        model: Override model. When ``None``, resolved via
            :func:`~ouroboros.config.loader.get_invariant_verifier_model`.

    Returns:
        List of ``(tag_text, reliability_score)`` pairs in input order.
        Empty when ``tags`` is empty.

    [[INVARIANT: verify_invariants is called inline-blocking before chain advance]]
    [[INVARIANT: only above-threshold invariants appear in downstream chain context]]
    """
    if not tags:
        return []

    if model is None:
        from ouroboros.config.loader import get_invariant_verifier_model

        model = get_invariant_verifier_model()

    results: list[tuple[str, float]] = []
    for tag in tags:
        score = await _verify_single_tag(
            adapter,
            tag,
            ac_trace=ac_trace,
            files_modified=files_modified,
            model=model,
        )
        log.info(
            "invariant_verifier.tag_scored",
            tag=tag[:80],
            score=score,
            model=model,
        )
        results.append((tag, score))
    return results


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


def _write_compounding_checkpoint(
    store: Any,
    *,
    seed_id: str,
    session_id: str,
    ac_index: int,
    chain: PostmortemChain,
) -> None:
    """Write a per-AC compounding checkpoint after successful completion.

    Serializes the current postmortem chain into a
    :class:`~ouroboros.persistence.checkpoint.CompoundingCheckpointState`
    and persists it via ``store.write``.  Failures are caught and logged so
    a checkpoint write error never propagates to the caller.

    This function is synchronous — it is called inside the serial loop
    after each successful AC and must not introduce async complexity.

    Args:
        store: :class:`~ouroboros.persistence.checkpoint.CheckpointStore`
            instance (or any object with a ``write`` method that accepts
            a :class:`~ouroboros.persistence.checkpoint.CheckpointData`).
        seed_id: Seed identifier used as the checkpoint key.
        session_id: Session identifier — included in log context only.
        ac_index: 0-based index of the *just-completed* successful AC.
        chain: Current postmortem chain (already includes the postmortem
            for ``ac_index``).

    [[INVARIANT: checkpoints are only written after AC success, never on failure]]
    [[INVARIANT: CompoundingCheckpointState.last_completed_ac_index equals the 0-based AC index]]
    """
    try:
        serialized_chain = serialize_postmortem_chain(chain)
        state = CompoundingCheckpointState(
            last_completed_ac_index=ac_index,
            postmortem_chain=serialized_chain,
        )
        checkpoint = CheckpointData.create(
            seed_id=seed_id,
            phase="execution",
            state=state.to_dict(),
        )
        result = store.write(checkpoint)
        if result.is_err:
            log.warning(
                "serial_executor.checkpoint.write_failed",
                session_id=session_id,
                ac_index=ac_index,
                error=str(result.error),
            )
        else:
            log.info(
                "serial_executor.checkpoint.written",
                session_id=session_id,
                ac_index=ac_index,
                seed_id=seed_id,
                postmortems=len(chain.postmortems),
            )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "serial_executor.checkpoint.unexpected_error",
            session_id=session_id,
            ac_index=ac_index,
            error=str(exc),
        )


def _load_compounding_checkpoint(
    store: Any,
    *,
    seed_id: str,
    session_id: str,
    resume_session_id: str,
) -> tuple[PostmortemChain, int]:
    """Load a compounding checkpoint and deserialize the postmortem chain.

    Attempts to load the checkpoint stored under ``seed_id`` from ``store``.
    On success, deserializes the saved :class:`PostmortemChain` and returns
    it along with the ``last_completed_ac_index`` from the checkpoint state.

    On any failure (missing checkpoint, wrong mode, deserialization error),
    logs a warning and returns an empty chain with ``last_completed_ac_index=-1``
    so the caller falls back to a fresh run.

    Args:
        store: :class:`~ouroboros.persistence.checkpoint.CheckpointStore`
            instance (or any object with a ``load`` method).
        seed_id: Seed identifier — the key used to look up the checkpoint.
        session_id: Current session id (used for log context only).
        resume_session_id: Session id of the run being resumed (log context).

    Returns:
        ``(chain, last_completed_ac_index)`` where ``chain`` is the
        deserialized :class:`PostmortemChain` (empty on failure) and
        ``last_completed_ac_index`` is the 0-based index of the last
        successfully completed AC (-1 if nothing was found).

    [[INVARIANT: _load_compounding_checkpoint returns empty chain and -1 on failure]]
    [[INVARIANT: deserialized chain reflects all postmortems from the prior run up to last_completed_ac_index]]
    """
    try:
        load_result = store.load(seed_id)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "serial_executor.resume.store_error",
            session_id=session_id,
            seed_id=seed_id,
            resume_session_id=resume_session_id,
            error=str(exc),
        )
        return PostmortemChain(), -1

    if load_result.is_err:
        log.warning(
            "serial_executor.resume.no_checkpoint",
            session_id=session_id,
            seed_id=seed_id,
            resume_session_id=resume_session_id,
            error=str(load_result.error),
        )
        return PostmortemChain(), -1

    checkpoint = load_result.value

    try:
        state = CompoundingCheckpointState.from_dict(checkpoint.state)
    except (ValueError, KeyError, TypeError) as exc:
        log.warning(
            "serial_executor.resume.invalid_checkpoint",
            session_id=session_id,
            seed_id=seed_id,
            resume_session_id=resume_session_id,
            error=str(exc),
        )
        return PostmortemChain(), -1

    try:
        chain = deserialize_postmortem_chain(state.postmortem_chain)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "serial_executor.resume.deserialize_error",
            session_id=session_id,
            seed_id=seed_id,
            resume_session_id=resume_session_id,
            error=str(exc),
        )
        return PostmortemChain(), -1

    log.info(
        "serial_executor.resume.checkpoint_loaded",
        session_id=session_id,
        seed_id=seed_id,
        resume_session_id=resume_session_id,
        last_completed_ac_index=state.last_completed_ac_index,
        postmortems_in_chain=len(chain.postmortems),
    )
    return chain, state.last_completed_ac_index


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
        resume_session_id: str | None = None,
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
            resume_session_id: When provided, attempt to load a saved
                compounding checkpoint for this seed and deserialize the
                postmortem chain so that already-completed ACs are skipped.
                The checkpoint is keyed by ``seed.metadata.seed_id`` —
                ``resume_session_id`` is used for logging/diagnostics only
                and does not change the storage key.  If no checkpoint is
                found or the checkpoint is not a valid compounding checkpoint,
                a warning is logged and execution continues from the beginning.

        Returns:
            ParallelExecutionResult with one stage per AC so downstream
            progress tooling sees a structurally similar shape to the
            parallel path.

        [[INVARIANT: resume_session_id triggers checkpoint loading by seed_id, not by session_id]]
        [[INVARIANT: deserialized chain is injected before the AC loop so resumed ACs see prior postmortems]]
        """
        if execution_plan is None:
            if dependency_graph is None:
                msg = "execution_plan is required when dependency_graph is not provided"
                raise ValueError(msg)
            execution_plan = dependency_graph.to_execution_plan()

        ac_order = linearize_execution_plan(execution_plan)
        start_time = datetime.now(UTC)

        # Q6.2: Checkpoint loading for resume.
        # When resume_session_id is supplied and a checkpoint store is available,
        # attempt to load the persisted compounding state so prior ACs are skipped
        # and their postmortems are injected into the rolling chain immediately.
        chain = PostmortemChain()
        last_completed_ac_index: int = -1  # -1 means "nothing completed yet"

        if resume_session_id is not None and self._checkpoint_store is not None:
            chain, last_completed_ac_index = _load_compounding_checkpoint(
                store=self._checkpoint_store,
                seed_id=seed.metadata.seed_id,
                session_id=session_id,
                resume_session_id=resume_session_id,
            )

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

            # Q6.2: Skip ACs that were already completed in a prior run (checkpoint resume).
            # The chain is already seeded with their postmortems from deserialization,
            # so we do NOT add to the chain here — just record the skipped result.
            if ac_index <= last_completed_ac_index:
                resumed_result = ACExecutionResult(
                    ac_index=ac_index,
                    ac_content=seed.acceptance_criteria[ac_index],
                    success=True,
                    final_message=(
                        f"Skipped via checkpoint resume (session {resume_session_id}); "
                        f"this AC (index {ac_index}) was already completed in the prior run."
                    ),
                    retry_attempt=0,
                    outcome=ACExecutionOutcome.SATISFIED_EXTERNALLY,
                )
                results.append(resumed_result)
                stages.append(
                    ParallelExecutionStageResult(
                        stage_index=position,
                        ac_indices=(ac_index,),
                        results=(resumed_result,),
                        started=False,
                    )
                )
                log.info(
                    "serial_executor.ac.skipped_via_resume",
                    session_id=session_id,
                    ac_index=ac_index,
                    resume_session_id=resume_session_id,
                    last_completed_ac_index=last_completed_ac_index,
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

            # Q3 (C-plus): Extract [[INVARIANT: ...]] tags inline-blocking before
            # chain advance so the next AC's prompt sees verified invariants.
            # Scan final_message first; fall back to full message list.
            inv_tags = extract_invariant_tags(result.final_message or "")
            if not inv_tags and result.messages:
                inv_tags = extract_invariant_tags(result.messages)

            if inv_tags:
                verified_pairs = await verify_invariants(
                    self._adapter,
                    inv_tags,
                    ac_trace=result.final_message or "",
                    files_modified=list(postmortem.summary.files_modified),
                )
                min_rel = _get_min_reliability()
                trusted: list[Invariant] = [
                    Invariant(
                        text=tag,
                        reliability=score,
                        occurrences=1,
                        first_seen_ac_id=f"ac_{ac_index}",
                    )
                    for tag, score in verified_pairs
                    if score >= min_rel
                ]
                if trusted:
                    postmortem = ACPostmortem(
                        summary=postmortem.summary,
                        diff_summary=postmortem.diff_summary,
                        tool_trace_digest=postmortem.tool_trace_digest,
                        gotchas=postmortem.gotchas,
                        qa_suggestions=postmortem.qa_suggestions,
                        invariants_established=tuple(trusted),
                        retry_attempts=postmortem.retry_attempts,
                        status=postmortem.status,
                        duration_seconds=postmortem.duration_seconds,
                        ac_native_session_id=postmortem.ac_native_session_id,
                        sub_postmortems=postmortem.sub_postmortems,
                    )
                    log.info(
                        "serial_executor.invariants.captured",
                        session_id=session_id,
                        ac_index=ac_index,
                        total_tags=len(inv_tags),
                        trusted_count=len(trusted),
                        min_reliability=min_rel,
                    )
                else:
                    log.info(
                        "serial_executor.invariants.all_below_threshold",
                        session_id=session_id,
                        ac_index=ac_index,
                        total_tags=len(inv_tags),
                        min_reliability=min_rel,
                    )

            chain = chain.append(postmortem)

            # Q6.2: Write per-AC checkpoint after successful completion.
            # Checkpoints are ONLY written on success — failed ACs do NOT advance
            # the checkpoint cursor, so a resume will retry the failing AC.
            if result.success and self._checkpoint_store is not None:
                _write_compounding_checkpoint(
                    store=self._checkpoint_store,
                    seed_id=seed.metadata.seed_id,
                    session_id=session_id,
                    ac_index=ac_index,
                    chain=chain,
                )

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
    "verify_invariants",
    "write_chain_artifact",
    "_load_compounding_checkpoint",
    "_write_compounding_checkpoint",
]