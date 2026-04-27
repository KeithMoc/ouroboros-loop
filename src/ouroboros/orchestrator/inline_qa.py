"""Inline QA helpers for serial-compounding mode.

This module is DELIBERATELY decoupled from SerialCompoundingExecutor —
zero imports of that class — so every function here is unit-testable in
isolation with lightweight fakes.

Key public surface:
    _assemble_qa_artifact   — compose the text block sent to QAHandler as artifact
    _assemble_qa_quality_bar — compose the quality-bar prompt
    _format_qa_feedback_section — build the retry-context injection block
    _serialize_qa_verdict   — convert QAVerdict → JSON-safe dict
    InlineQAOutcome         — frozen dataclass wrapping loop_action + score
    run_inline_qa           — async entry-point called by the executor
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from typing import TYPE_CHECKING, Any
import uuid

import yaml

from ouroboros.mcp.tools.qa import QAHandler, QAVerdict
from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.level_context import ACPostmortem
from ouroboros.orchestrator.parallel_executor_models import ACExecutionResult

if TYPE_CHECKING:
    from ouroboros.core.seed import Seed

log = get_logger(__name__)

# Default per-call timeout for QAHandler.handle.  Configurable via env var
# OUROBOROS_INLINE_QA_TIMEOUT_S; values <= 0 disable the timeout.  Chosen
# to be larger than typical LLM round-trips but short enough that a hung
# adapter cannot block a whole compounding run indefinitely.
DEFAULT_INLINE_QA_TIMEOUT_S: float = 120.0


def _resolve_qa_timeout() -> float | None:
    """Return the QA call timeout in seconds, or None when disabled."""
    raw = os.environ.get("OUROBOROS_INLINE_QA_TIMEOUT_S")
    if raw is None or raw == "":
        return DEFAULT_INLINE_QA_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_INLINE_QA_TIMEOUT_S
    return value if value > 0 else None


# Set of loop_action values that the executor recognizes.  Anything outside
# this set is treated as malformed and short-circuited to skipped_delegated.
_VALID_LOOP_ACTIONS: frozenset[str] = frozenset(
    {"pass", "revise", "fail", "skipped_delegated", "skipped_error"}
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _assemble_qa_artifact(result: ACExecutionResult, postmortem: ACPostmortem) -> str:
    """Compose the artifact text block for QAHandler.

    Combines the agent's final message with the diff summary.  The
    ``(empty)`` and ``(no changes detected)`` sentinels are intentional —
    they are the QA-detectable signal for a run that produced no output
    or no file changes.

    Args:
        result: Execution result carrying the agent's final message.
        postmortem: Postmortem carrying the computed diff summary.

    Returns:
        Formatted text block with two headed sections.
    """
    final_message = result.final_message or "(empty)"
    diff_summary = postmortem.diff_summary or "(no changes detected)"
    return (
        f"## Agent's final message\n{final_message}\n\n"
        f"## Diff summary (`git diff --stat`, pre-AC \u2192 post-AC)\n"
        f"```\n{diff_summary}\n```"
    )


def _assemble_qa_quality_bar(ac_index: int, ac_content: str, seed_goal: str) -> str:
    """Compose the quality-bar prompt for QAHandler.

    The quality bar is scoped to the AC text and the seed goal only.
    It deliberately excludes ``seed.quality_bar`` (used by post-run QA)
    and chain invariants (injected separately by the executor).

    Args:
        ac_index: 0-based AC index (displayed as 1-based).
        ac_content: The acceptance criterion text.
        seed_goal: The seed's top-level goal string.

    Returns:
        Formatted quality-bar string.
    """
    return (
        f"Acceptance criterion AC-{ac_index + 1}: {ac_content}\n\n"
        f"Seed goal: {seed_goal}"
    )


def _format_qa_feedback_section(
    verdict_dict: dict[str, Any],
    attempt_number: int,
    max_attempts: int,
) -> str:
    """Build the prompt-injection block for the next retry attempt.

    This block is appended to ``context_override`` before re-executing the
    AC so the agent understands what the QA judge flagged and what to
    address.  The previous commits are KEPT — the agent should build on
    them, not revert.

    Args:
        verdict_dict: Serialized QAVerdict dict (from _serialize_qa_verdict).
        attempt_number: The attempt number that produced this verdict (1-based).
        max_attempts: Total maximum attempts allowed (budget + 1).

    Returns:
        Formatted feedback section string.
    """
    score: float = verdict_dict.get("score", 0.0)
    verdict_label: str = verdict_dict.get("verdict", "").upper()
    differences: list[str] = list(verdict_dict.get("differences") or [])
    suggestions: list[str] = list(verdict_dict.get("suggestions") or [])
    reasoning: str = verdict_dict.get("reasoning", "")

    diff_lines = "\n".join(f"- {d}" for d in differences) if differences else "- (none)"
    sug_lines = "\n".join(f"- {s}" for s in suggestions) if suggestions else "- (none)"

    return (
        f"\n\n## QA verdict on previous attempt "
        f"(attempt {attempt_number} of {max_attempts})\n\n"
        f"Score: {score:.2f} / 1.00 \u2014 {verdict_label}\n\n"
        f"### Differences flagged\n"
        f"{diff_lines}\n\n"
        f"### Suggested revisions\n"
        f"{sug_lines}\n\n"
        f"### Reasoning\n{reasoning}\n\n"
        f"The previous attempt's commits are **kept on the branch**. "
        f"Build on them \u2014 do NOT revert. Address the differences and "
        f"suggestions above in a follow-up commit.\n"
    )


def _serialize_qa_verdict(verdict: QAVerdict) -> dict[str, Any]:
    """Convert a parsed QAVerdict to a JSON-safe dict for postmortem storage.

    Tuple fields (``differences``, ``suggestions``) become lists.

    Intended for callers that already hold a parsed :class:`QAVerdict`
    dataclass — :func:`run_inline_qa` reads the same fields directly off
    ``MCPToolResult.meta`` and does not flow through this helper, which is
    deliberate: ``meta`` is already a JSON-shaped dict, so an extra
    serialization round-trip would be wasted work.

    Args:
        verdict: The QAVerdict dataclass instance.

    Returns:
        JSON-safe dict with keys: score, verdict, dimensions, differences,
        suggestions, reasoning.
    """
    return {
        "score": float(verdict.score),
        "verdict": verdict.verdict,
        "dimensions": dict(verdict.dimensions),
        "differences": list(verdict.differences),
        "suggestions": list(verdict.suggestions),
        "reasoning": verdict.reasoning,
    }


# ---------------------------------------------------------------------------
# InlineQAOutcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InlineQAOutcome:
    """Outcome of a single inline-QA evaluation.

    Attributes:
        loop_action: One of "pass", "revise", "fail", "skipped_delegated",
            "skipped_error". The two skipped variants distinguish intentional
            opencode plugin-dispatch (delegated_to_subagent) from handler /
            timeout failures, which simplifies postmortem auditing.
        score: QA score (0.0 when skipped/delegated/errored).
        verdict_dict: Serialized verdict dict, or None when skipped.
        qa_session_id: Session ID used for the QA call.
    """

    loop_action: str  # see class docstring for the closed enum
    score: float
    verdict_dict: dict[str, Any] | None  # None when skipped/errored
    qa_session_id: str


# ---------------------------------------------------------------------------
# Async entry-point
# ---------------------------------------------------------------------------


async def run_inline_qa(
    qa_handler: QAHandler,
    *,
    postmortem: ACPostmortem,
    ac_index: int,
    ac_content: str,
    seed: Seed,
    qa_session_id: str | None = None,
    final_message: str | None = None,
    iteration_history: list[dict[str, Any]] | None = None,
    timeout_s: float | None = None,
) -> InlineQAOutcome:
    """Run a single inline-QA evaluation against QAHandler.

    Builds the artifact from ``final_message`` (the agent's full output for the
    AC's last attempt) when provided, or falls back to
    ``postmortem.summary.key_output`` (a short excerpt) when the executor cannot
    thread the raw message — the full text gives the QA judge much better
    context, since key_output is a summarization of the same data.

    Degrades gracefully on every error path — handler failures and timeouts
    return ``loop_action="skipped_error"`` so the executor distinguishes them
    from the intentional opencode plugin-dispatch path
    (``loop_action="skipped_delegated"``); both bypass the retry loop without
    blocking the run.

    Args:
        qa_handler: The QAHandler instance to call.
        postmortem: The AC postmortem to evaluate.
        ac_index: 0-based AC index.
        ac_content: The acceptance criterion text.
        seed: The Seed object (serialized as YAML for ``seed_content`` arg).
        qa_session_id: Optional explicit session ID; auto-generated when None.
        final_message: Optional agent's full final-message text for this attempt;
            recommended whenever the caller has it (the executor passes it
            from the latest ``ACExecutionResult``).
        iteration_history: Optional list of prior verdicts for the same AC,
            forwarded to the QA judge so its multi-iteration loop has context.
            Each entry should match the QA tool's ``iteration_history`` schema
            (typically the previous attempts' meta dicts).
        timeout_s: Per-call timeout override; ``None`` consults
            ``OUROBOROS_INLINE_QA_TIMEOUT_S`` and falls back to
            :data:`DEFAULT_INLINE_QA_TIMEOUT_S`.

    Returns:
        InlineQAOutcome describing what the QA judge decided.
    """
    # Auto-generate session ID if not provided
    effective_session_id = qa_session_id or f"qa-ac{ac_index}-{uuid.uuid4().hex[:8]}"

    # Prefer the caller-supplied full final_message; key_output is a
    # short summarization fallback used only when threading isn't possible.
    final_text = (
        final_message if final_message is not None else postmortem.summary.key_output
    )
    synthetic_result = ACExecutionResult(
        ac_index=postmortem.summary.ac_index,
        ac_content=postmortem.summary.ac_content,
        success=postmortem.summary.success,
        final_message=final_text,
    )
    artifact = _assemble_qa_artifact(synthetic_result, postmortem)
    quality_bar = _assemble_qa_quality_bar(ac_index, ac_content, seed.goal)

    try:
        seed_content = yaml.dump(seed.model_dump(), allow_unicode=True, sort_keys=False)
    except (AttributeError, TypeError, ValueError, yaml.YAMLError) as exc:
        log.warning(
            "inline_qa.seed_serialize_failed",
            qa_session_id=effective_session_id,
            error=str(exc),
        )
        seed_content = None

    arguments: dict[str, Any] = {
        "artifact": artifact,
        "quality_bar": quality_bar,
        "artifact_type": "code",
        "pass_threshold": 0.80,
        "qa_session_id": effective_session_id,
    }
    if seed_content is not None:
        arguments["seed_content"] = seed_content
    if iteration_history:
        arguments["iteration_history"] = iteration_history

    effective_timeout = timeout_s if timeout_s is not None else _resolve_qa_timeout()

    try:
        if effective_timeout is None:
            result = await qa_handler.handle(arguments)
        else:
            result = await asyncio.wait_for(
                qa_handler.handle(arguments), timeout=effective_timeout
            )
    except TimeoutError:
        log.warning(
            "inline_qa.handle_timeout",
            qa_session_id=effective_session_id,
            timeout_s=effective_timeout,
        )
        return InlineQAOutcome(
            loop_action="skipped_error",
            score=0.0,
            verdict_dict=None,
            qa_session_id=effective_session_id,
        )

    # --- Handler-level error: distinct from intentional plugin-dispatch ---
    if result.is_err:
        log.warning(
            "inline_qa.handle_failed",
            qa_session_id=effective_session_id,
            error=str(result.error),
        )
        return InlineQAOutcome(
            loop_action="skipped_error",
            score=0.0,
            verdict_dict=None,
            qa_session_id=effective_session_id,
        )

    # --- Success path ---
    tool_result = result.value
    meta = tool_result.meta or {}

    # Detect opencode plugin-dispatch path
    if meta.get("status") == "delegated_to_subagent":
        return InlineQAOutcome(
            loop_action="skipped_delegated",
            score=0.0,
            verdict_dict=None,
            qa_session_id=effective_session_id,
        )

    # Extract verdict fields from meta.  Default to skipped_delegated on
    # missing/unexpected loop_action so malformed payloads don't masquerade
    # as a recoverable FAIL and burn unnecessary retries.
    raw_loop_action = meta.get("loop_action", "skipped_delegated")
    if raw_loop_action not in _VALID_LOOP_ACTIONS:
        log.warning(
            "inline_qa.unexpected_loop_action",
            qa_session_id=effective_session_id,
            raw=str(raw_loop_action),
        )
        raw_loop_action = "skipped_delegated"
    loop_action: str = raw_loop_action
    score: float = float(meta.get("score", 0.0))

    verdict_dict: dict[str, Any] = {
        "score": score,
        "verdict": meta.get("verdict", ""),
        "dimensions": meta.get("dimensions", {}),
        "differences": list(meta.get("differences") or []),
        "suggestions": list(meta.get("suggestions") or []),
        "reasoning": meta.get("reasoning", ""),
    }

    return InlineQAOutcome(
        loop_action=loop_action,
        score=score,
        verdict_dict=verdict_dict,
        qa_session_id=effective_session_id,
    )
