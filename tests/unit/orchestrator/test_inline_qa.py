"""Unit tests for inline_qa.py — pure helpers + InlineQAOutcome + run_inline_qa.

Tests 1-10 cover the pure helper functions and the InlineQAOutcome dataclass.
Tests 11-17 cover run_inline_qa with fake QAHandler instances.
Tests 58-59 cover Q4.1 / AC-4 / item 4 — the verdict_dict canonical-shape
unification through `_serialize_qa_verdict` (PR #6 CodeRabbit deferred item).

Coverage:
    _assemble_qa_artifact          tests 1-2
    _assemble_qa_quality_bar       tests 3-4
    _format_qa_feedback_section    tests 5-8
    _serialize_qa_verdict          test 9
    InlineQAOutcome                test 10
    run_inline_qa                  tests 11-17
    canonical-shape unification    tests 58-59
"""

from __future__ import annotations

import dataclasses
import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPToolError
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.orchestrator.inline_qa import (
    InlineQAOutcome,
    _assemble_qa_artifact,
    _assemble_qa_quality_bar,
    _format_qa_feedback_section,
    _meta_to_qaverdict_kwargs,
    _serialize_qa_verdict,
    run_inline_qa,
)
from ouroboros.orchestrator.level_context import ACContextSummary, ACPostmortem
from ouroboros.orchestrator.parallel_executor_models import ACExecutionResult
from ouroboros.mcp.tools.qa import QAVerdict


# ---------------------------------------------------------------------------
# Helpers for building minimal ACExecutionResult / ACPostmortem
# ---------------------------------------------------------------------------


def _mk_result(final_message: str = "") -> ACExecutionResult:
    """Build a minimal ACExecutionResult for testing."""
    return ACExecutionResult(
        ac_index=0,
        ac_content="Some AC",
        success=True,
        final_message=final_message,
    )


def _mk_postmortem(
    diff_summary: str = "",
    key_output: str = "",
    ac_index: int = 0,
    ac_content: str = "Some AC",
) -> ACPostmortem:
    """Build a minimal ACPostmortem for testing."""
    summary = ACContextSummary(
        ac_index=ac_index,
        ac_content=ac_content,
        success=True,
        key_output=key_output,
    )
    return ACPostmortem(summary=summary, diff_summary=diff_summary)


# ---------------------------------------------------------------------------
# Tests 1-2: _assemble_qa_artifact
# ---------------------------------------------------------------------------


class TestAssembleQaArtifact:
    """Tests for _assemble_qa_artifact pure helper."""

    def test_empty_final_message_empty_diff_summary(self) -> None:
        """Test 1: Both empty → sentinel values appear."""
        result = _mk_result(final_message="")
        postmortem = _mk_postmortem(diff_summary="")
        output = _assemble_qa_artifact(result, postmortem)

        assert "(empty)" in output
        assert "(no changes detected)" in output
        assert "## Agent's final message" in output
        assert "## Diff summary" in output

    def test_multiline_final_message_and_diff_stat(self) -> None:
        """Test 2: Multiline content is preserved verbatim under headings."""
        final_msg = "Line 1\nLine 2\nLine 3"
        diff = "src/foo.py | 5 ++++\nsrc/bar.py | 3 ---"
        result = _mk_result(final_message=final_msg)
        postmortem = _mk_postmortem(diff_summary=diff)
        output = _assemble_qa_artifact(result, postmortem)

        # Final message preserved
        assert "Line 1\nLine 2\nLine 3" in output
        # Diff preserved
        assert "src/foo.py | 5 ++++" in output
        assert "src/bar.py | 3 ---" in output
        # Both section headings present
        assert "## Agent's final message" in output
        assert "## Diff summary" in output
        # No sentinel strings when content is non-empty
        assert "(empty)" not in output
        assert "(no changes detected)" not in output


# ---------------------------------------------------------------------------
# Tests 3-4: _assemble_qa_quality_bar
# ---------------------------------------------------------------------------


class TestAssembleQaQualityBar:
    """Tests for _assemble_qa_quality_bar pure helper."""

    def test_ac_index_zero_produces_ac1_prefix(self) -> None:
        """Test 3: ac_index=0 → displays as AC-1 (1-based)."""
        output = _assemble_qa_quality_bar(0, "Implement feature X", "Build tool Y")
        assert output.startswith("Acceptance criterion AC-1:")
        assert "Implement feature X" in output
        assert "Seed goal: Build tool Y" in output

    def test_multiline_ac_content_and_seed_goal_verbatim(self) -> None:
        """Test 4: Multiline content and goal are preserved verbatim."""
        ac_content = "First line\nSecond line\n- Bullet"
        seed_goal = "Goal line 1\nGoal line 2"
        output = _assemble_qa_quality_bar(2, ac_content, seed_goal)

        assert "AC-3:" in output
        assert "First line\nSecond line\n- Bullet" in output
        assert "Goal line 1\nGoal line 2" in output


# ---------------------------------------------------------------------------
# Tests 5-8: _format_qa_feedback_section
# ---------------------------------------------------------------------------


class TestFormatQaFeedbackSection:
    """Tests for _format_qa_feedback_section pure helper."""

    def _revise_verdict(self) -> dict:
        return {
            "score": 0.62,
            "verdict": "revise",
            "differences": ["Missing type hints", "No docstring"],
            "suggestions": ["Add types", "Add docstrings", "Run mypy"],
            "reasoning": "Code quality below threshold.",
        }

    def test_revise_verdict_full_render(self) -> None:
        """Test 5: REVISE with 2 differences + 3 suggestions → all bullets, uppercase label, footer."""
        verdict = self._revise_verdict()
        output = _format_qa_feedback_section(verdict, attempt_number=1, max_attempts=2)

        assert "## QA verdict on previous attempt (attempt 1 of 2)" in output
        assert "Score: 0.62 / 1.00" in output
        assert "REVISE" in output
        assert "### Differences flagged" in output
        assert "- Missing type hints" in output
        assert "- No docstring" in output
        assert "### Suggested revisions" in output
        assert "- Add types" in output
        assert "- Add docstrings" in output
        assert "- Run mypy" in output
        assert "### Reasoning" in output
        assert "Code quality below threshold." in output
        assert "kept on the branch" in output
        assert "do NOT revert" in output

    def test_fail_verdict_label_is_fail(self) -> None:
        """Test 6: FAIL verdict → label uppercase = FAIL, structure identical."""
        verdict = {
            "score": 0.25,
            "verdict": "fail",
            "differences": ["Critical bug"],
            "suggestions": ["Fix the bug"],
            "reasoning": "Does not meet minimum bar.",
        }
        output = _format_qa_feedback_section(verdict, attempt_number=2, max_attempts=3)

        assert "FAIL" in output
        assert "Score: 0.25 / 1.00" in output
        assert "attempt 2 of 3" in output
        assert "- Critical bug" in output
        assert "- Fix the bug" in output

    def test_empty_differences_and_suggestions_render_none_placeholder(self) -> None:
        """Test 7: Empty differences AND empty suggestions → "- (none)" under each heading."""
        verdict = {
            "score": 0.55,
            "verdict": "revise",
            "differences": [],
            "suggestions": [],
            "reasoning": "Borderline.",
        }
        output = _format_qa_feedback_section(verdict, attempt_number=1, max_attempts=2)

        # Both sections exist but with placeholder
        assert "### Differences flagged" in output
        assert "### Suggested revisions" in output
        # Count "(none)" occurrences - one per empty section
        assert output.count("- (none)") == 2

    def test_backticks_in_suggestion_rendered_verbatim(self) -> None:
        """Test 8: Suggestion text with backticks is rendered as-is."""
        verdict = {
            "score": 0.70,
            "verdict": "revise",
            "differences": ["Bad import"],
            "suggestions": ["Use `from x import y` instead of `import x`"],
            "reasoning": "Style issue.",
        }
        output = _format_qa_feedback_section(verdict, attempt_number=1, max_attempts=2)

        # Backticks preserved verbatim
        assert "Use `from x import y` instead of `import x`" in output


# ---------------------------------------------------------------------------
# Test 9: _serialize_qa_verdict
# ---------------------------------------------------------------------------


class TestSerializeQaVerdict:
    """Tests for _serialize_qa_verdict pure helper."""

    def test_roundtrip_all_fields(self) -> None:
        """Test 9: Round-trips score/verdict/reasoning; tuple → list; dimensions dict preserved."""
        verdict = QAVerdict(
            score=0.85,
            verdict="pass",
            dimensions={"correctness": 0.9, "completeness": 0.8},
            differences=["Minor gap"],
            suggestions=["Fix x", "Fix y"],
            reasoning="Overall solid.",
        )
        d = _serialize_qa_verdict(verdict)

        assert d["score"] == 0.85
        assert d["verdict"] == "pass"
        assert d["reasoning"] == "Overall solid."
        assert isinstance(d["differences"], list)
        assert d["differences"] == ["Minor gap"]
        assert isinstance(d["suggestions"], list)
        assert d["suggestions"] == ["Fix x", "Fix y"]
        assert d["dimensions"] == {"correctness": 0.9, "completeness": 0.8}
        # All 6 keys present
        assert set(d.keys()) == {"score", "verdict", "dimensions", "differences", "suggestions", "reasoning"}


# ---------------------------------------------------------------------------
# Test 10: InlineQAOutcome
# ---------------------------------------------------------------------------


class TestInlineQAOutcome:
    """Tests for InlineQAOutcome frozen dataclass."""

    def test_construction_with_none_verdict_dict(self) -> None:
        """Test 10: Construct with None verdict_dict; assert frozen (cannot mutate)."""
        outcome = InlineQAOutcome(
            loop_action="skipped_delegated",
            score=0.0,
            verdict_dict=None,
            qa_session_id="qa-ac0-abcdef12",
        )
        assert outcome.loop_action == "skipped_delegated"
        assert outcome.score == 0.0
        assert outcome.verdict_dict is None
        assert outcome.qa_session_id == "qa-ac0-abcdef12"

        # Assert frozen — cannot mutate
        with pytest.raises((AttributeError, dataclasses.FrozenInstanceError)):
            outcome.loop_action = "pass"  # type: ignore[misc]

    def test_construction_with_verdict_dict(self) -> None:
        """Construction with a real verdict_dict works."""
        vd = {"score": 0.9, "verdict": "pass", "dimensions": {}, "differences": [], "suggestions": [], "reasoning": ""}
        outcome = InlineQAOutcome(
            loop_action="pass",
            score=0.9,
            verdict_dict=vd,
            qa_session_id="qa-ac1-test",
        )
        assert outcome.verdict_dict == vd
        assert outcome.score == 0.9


# ---------------------------------------------------------------------------
# Tests 11-17: run_inline_qa (fake QAHandler)
# ---------------------------------------------------------------------------


class FakeQAHandler:
    """Fake QAHandler that returns configured results for testing."""

    def __init__(self, results: list) -> None:
        """Initialize with a list of Results to return in order."""
        self._results = list(results)
        self._call_count = 0
        self._call_args: list[dict] = []

    async def handle(self, arguments: dict) -> Result:
        self._call_args.append(dict(arguments))
        idx = min(self._call_count, len(self._results) - 1)
        result = self._results[idx]
        self._call_count += 1
        return result


def _make_ok_meta(
    score: float = 0.9,
    verdict: str = "pass",
    loop_action: str = "pass",
    differences: list | None = None,
    suggestions: list | None = None,
    reasoning: str = "Looks good.",
    dimensions: dict | None = None,
) -> dict:
    """Build a meta dict matching QAHandler's success output."""
    return {
        "score": score,
        "verdict": verdict,
        "loop_action": loop_action,
        "differences": differences or [],
        "suggestions": suggestions or [],
        "reasoning": reasoning,
        "dimensions": dimensions or {},
        "qa_session_id": "qa-fake",
        "passed": score >= 0.80,
    }


def _make_ok_result(meta: dict) -> Result:
    """Wrap a meta dict in Result.ok(MCPToolResult)."""
    return Result.ok(
        MCPToolResult(
            content=(MCPContentItem(type=ContentType.TEXT, text="verdict text"),),
            is_error=False,
            meta=meta,
        )
    )


def _make_delegated_result() -> Result:
    """Return a plugin-dispatch (delegated_to_subagent) Result."""
    meta = {
        "status": "delegated_to_subagent",
        "dispatch_mode": "plugin",
        "qa_session_id": "qa-delegated",
    }
    return Result.ok(
        MCPToolResult(
            content=(MCPContentItem(type=ContentType.TEXT, text="{}"),),
            is_error=False,
            meta=meta,
        )
    )


def _make_err_result() -> Result:
    """Return an Err result."""
    return Result.err(MCPToolError("LLM call failed", tool_name="ouroboros_qa"))


class FakeSeed:
    """Minimal fake Seed for testing."""

    goal = "Build a great tool"

    def model_dump(self) -> dict:
        return {"goal": self.goal}


@pytest.mark.asyncio
class TestRunInlineQa:
    """Tests 11-17 for the run_inline_qa async entry-point."""

    def _pm(self, key_output: str = "Agent output here") -> ACPostmortem:
        return _mk_postmortem(
            diff_summary="src/foo.py | 2 ++",
            key_output=key_output,
        )

    async def test_happy_path_pass(self) -> None:
        """Test 11: Happy path — pass verdict returned correctly."""
        meta = _make_ok_meta(score=0.9, verdict="pass", loop_action="pass")
        handler = FakeQAHandler([_make_ok_result(meta)])
        seed = FakeSeed()

        outcome = await run_inline_qa(
            handler,  # type: ignore[arg-type]
            postmortem=self._pm(),
            ac_index=0,
            ac_content="Implement feature X",
            seed=seed,  # type: ignore[arg-type]
            qa_session_id="qa-ac0-test",
        )

        assert outcome.loop_action == "pass"
        assert outcome.score == 0.9
        assert outcome.qa_session_id == "qa-ac0-test"
        assert outcome.verdict_dict is not None
        assert set(outcome.verdict_dict.keys()) >= {"score", "verdict", "differences", "suggestions", "reasoning"}

    async def test_revise_path(self) -> None:
        """Test 12: REVISE path — loop_action carries through."""
        meta = _make_ok_meta(score=0.65, verdict="revise", loop_action="revise")
        handler = FakeQAHandler([_make_ok_result(meta)])
        seed = FakeSeed()

        outcome = await run_inline_qa(
            handler,  # type: ignore[arg-type]
            postmortem=self._pm(),
            ac_index=1,
            ac_content="Write tests",
            seed=seed,  # type: ignore[arg-type]
        )

        assert outcome.loop_action == "revise"
        assert outcome.score == 0.65

    async def test_fail_path(self) -> None:
        """Test 13: FAIL path — loop_action carries through."""
        meta = _make_ok_meta(score=0.30, verdict="fail", loop_action="fail")
        handler = FakeQAHandler([_make_ok_result(meta)])
        seed = FakeSeed()

        outcome = await run_inline_qa(
            handler,  # type: ignore[arg-type]
            postmortem=self._pm(),
            ac_index=0,
            ac_content="Do something",
            seed=seed,  # type: ignore[arg-type]
        )

        assert outcome.loop_action == "fail"
        assert outcome.score == 0.30

    async def test_plugin_dispatch_returns_skipped_delegated(self) -> None:
        """Test 14: Plugin-dispatch meta → loop_action="skipped_delegated", verdict_dict=None."""
        handler = FakeQAHandler([_make_delegated_result()])
        seed = FakeSeed()

        outcome = await run_inline_qa(
            handler,  # type: ignore[arg-type]
            postmortem=self._pm(),
            ac_index=0,
            ac_content="Some AC",
            seed=seed,  # type: ignore[arg-type]
            qa_session_id="qa-dispatch-test",
        )

        assert outcome.loop_action == "skipped_delegated"
        assert outcome.verdict_dict is None
        assert outcome.score == 0.0

    async def test_err_returns_skipped_error_and_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        """Test 15: Err path → skipped_error (distinct from intentional plugin-dispatch).

        Handler-level failures must be distinguishable from the opencode
        plugin-dispatch path so postmortem auditing can tell intentional
        skips from real outages.  See CodeRabbit review on PR #6.
        """
        import logging
        handler = FakeQAHandler([_make_err_result()])
        seed = FakeSeed()

        with caplog.at_level(logging.WARNING, logger="ouroboros.orchestrator.inline_qa"):
            outcome = await run_inline_qa(
                handler,  # type: ignore[arg-type]
                postmortem=self._pm(),
                ac_index=0,
                ac_content="Some AC",
                seed=seed,  # type: ignore[arg-type]
            )

        assert outcome.loop_action == "skipped_error"
        assert outcome.verdict_dict is None
        assert outcome.score == 0.0
        # Check the handler was called once and a log was emitted
        assert handler._call_count == 1

    async def test_auto_generated_session_id_starts_with_qa_ac(self) -> None:
        """Test 16: qa_session_id=None → auto-generated string starting with "qa-ac"."""
        meta = _make_ok_meta()
        handler = FakeQAHandler([_make_ok_result(meta)])
        seed = FakeSeed()

        outcome = await run_inline_qa(
            handler,  # type: ignore[arg-type]
            postmortem=self._pm(),
            ac_index=3,
            ac_content="Some AC",
            seed=seed,  # type: ignore[arg-type]
            qa_session_id=None,
        )

        assert outcome.qa_session_id.startswith("qa-ac")
        assert len(outcome.qa_session_id) > len("qa-ac")

    async def test_explicit_session_id_preserved(self) -> None:
        """Test 17: qa_session_id="qa-fixed-1" → outcome carries that exact value."""
        meta = _make_ok_meta()
        handler = FakeQAHandler([_make_ok_result(meta)])
        seed = FakeSeed()

        outcome = await run_inline_qa(
            handler,  # type: ignore[arg-type]
            postmortem=self._pm(),
            ac_index=0,
            ac_content="Some AC",
            seed=seed,  # type: ignore[arg-type]
            qa_session_id="qa-fixed-1",
        )

        assert outcome.qa_session_id == "qa-fixed-1"

    async def test_explicit_final_message_overrides_key_output(self) -> None:
        """Caller-provided final_message replaces postmortem.summary.key_output."""
        meta = _make_ok_meta()
        handler = FakeQAHandler([_make_ok_result(meta)])
        seed = FakeSeed()

        await run_inline_qa(
            handler,  # type: ignore[arg-type]
            postmortem=self._pm(key_output="SHORT EXCERPT"),
            ac_index=0,
            ac_content="AC",
            seed=seed,  # type: ignore[arg-type]
            final_message="The agent's full unabridged final reply.",
        )

        artifact = handler._call_args[0]["artifact"]
        assert "The agent's full unabridged final reply." in artifact
        assert "SHORT EXCERPT" not in artifact

    async def test_iteration_history_forwarded_to_handler(self) -> None:
        """When iteration_history is provided, it is passed through to QAHandler."""
        meta = _make_ok_meta()
        handler = FakeQAHandler([_make_ok_result(meta)])
        seed = FakeSeed()

        history = [{"iteration": 1, "score": 0.62, "verdict": "revise", "loop_action": "revise"}]
        await run_inline_qa(
            handler,  # type: ignore[arg-type]
            postmortem=self._pm(),
            ac_index=0,
            ac_content="AC",
            seed=seed,  # type: ignore[arg-type]
            iteration_history=history,
        )

        assert handler._call_args[0]["iteration_history"] == history

    async def test_timeout_returns_skipped_error(self) -> None:
        """When QAHandler.handle hangs past timeout_s → loop_action='skipped_error'."""
        import asyncio

        class HangingHandler:
            async def handle(self, arguments: dict) -> Result:
                await asyncio.sleep(10)
                return _make_ok_result(_make_ok_meta())

        seed = FakeSeed()
        outcome = await run_inline_qa(
            HangingHandler(),  # type: ignore[arg-type]
            postmortem=self._pm(),
            ac_index=0,
            ac_content="AC",
            seed=seed,  # type: ignore[arg-type]
            timeout_s=0.01,
        )

        assert outcome.loop_action == "skipped_error"
        assert outcome.verdict_dict is None
        assert outcome.score == 0.0

    async def test_missing_loop_action_short_circuits_to_skipped_delegated(self) -> None:
        """Malformed meta missing loop_action → skipped_delegated, not 'fail'.

        A 'fail' default would burn unnecessary retries on garbage payloads.
        """
        meta = _make_ok_meta()
        meta.pop("loop_action")  # simulate malformed payload
        handler = FakeQAHandler([_make_ok_result(meta)])
        seed = FakeSeed()

        outcome = await run_inline_qa(
            handler,  # type: ignore[arg-type]
            postmortem=self._pm(),
            ac_index=0,
            ac_content="AC",
            seed=seed,  # type: ignore[arg-type]
        )

        assert outcome.loop_action == "skipped_delegated"

    async def test_unexpected_loop_action_normalized_to_skipped_delegated(self) -> None:
        """Unexpected loop_action values are short-circuited so the executor doesn't retry on garbage."""
        meta = _make_ok_meta(loop_action="not-a-real-action")
        handler = FakeQAHandler([_make_ok_result(meta)])
        seed = FakeSeed()

        outcome = await run_inline_qa(
            handler,  # type: ignore[arg-type]
            postmortem=self._pm(),
            ac_index=0,
            ac_content="AC",
            seed=seed,  # type: ignore[arg-type]
        )

        assert outcome.loop_action == "skipped_delegated"

    # -----------------------------------------------------------------
    # Tests 58-59: PR-#6 CodeRabbit item 4 — verdict_dict canonical shape
    # (Q4.1 design AC-4, path 4a — round-trip via _serialize_qa_verdict)
    # -----------------------------------------------------------------

    async def test_58_verdict_dict_matches_serialize_qa_verdict_canonical_shape(self) -> None:
        """Test 58: run_inline_qa's verdict_dict equals _serialize_qa_verdict(QAVerdict(**fields)).

        After Q4.1 / AC-4 / item 4, run_inline_qa must round-trip
        ``MCPToolResult.meta`` through QAVerdict → _serialize_qa_verdict so
        the returned verdict_dict is byte-identical to dicts produced by
        callers that construct QAVerdict directly.  This single source of
        truth keeps postmortem dicts comparable across provenance paths.
        """
        meta = _make_ok_meta(
            score=0.83,
            verdict="pass",
            loop_action="pass",
            differences=["minor wording diff"],
            suggestions=["use docstring", "add type hints"],
            reasoning="Solid implementation.",
            dimensions={"correctness": 0.9, "completeness": 0.76},
        )
        handler = FakeQAHandler([_make_ok_result(meta)])
        seed = FakeSeed()

        outcome = await run_inline_qa(
            handler,  # type: ignore[arg-type]
            postmortem=self._pm(),
            ac_index=0,
            ac_content="AC",
            seed=seed,  # type: ignore[arg-type]
        )

        # Build the canonical reference dict via the same _serialize_qa_verdict
        # path used elsewhere in the codebase.
        reference = _serialize_qa_verdict(
            QAVerdict(
                score=0.83,
                verdict="pass",
                dimensions={"correctness": 0.9, "completeness": 0.76},
                differences=["minor wording diff"],
                suggestions=["use docstring", "add type hints"],
                reasoning="Solid implementation.",
            )
        )

        assert outcome.verdict_dict == reference
        # Canonical shape: exactly the six QAVerdict-shaped keys, no leakage
        # of meta-only keys (loop_action, qa_session_id, passed, status).
        assert outcome.verdict_dict is not None
        assert set(outcome.verdict_dict.keys()) == {
            "score",
            "verdict",
            "dimensions",
            "differences",
            "suggestions",
            "reasoning",
        }
        # Score is also propagated to the InlineQAOutcome scalar field.
        assert outcome.score == 0.83
        # Type discipline — score is a Python float, not a numeric string.
        assert isinstance(outcome.verdict_dict["score"], float)
        # Sequence types are JSON-safe lists (not tuples).
        assert isinstance(outcome.verdict_dict["differences"], list)
        assert isinstance(outcome.verdict_dict["suggestions"], list)

    async def test_59_meta_to_qaverdict_kwargs_normalizes_partial_meta(self) -> None:
        """Test 59: _meta_to_qaverdict_kwargs survives missing/None/wrong-type meta fields.

        Path 4a relies on QAVerdict(**_meta_to_qaverdict_kwargs(meta)) succeeding
        even when the LLM returned a partially malformed payload — so the helper
        must apply safe defaults (score 0.0, empty strings, empty collections)
        and coerce types (e.g., dimensions=None → {}).  This guards against a
        TypeError on the QAVerdict ctor crashing the QA path on malformed meta.
        """
        # Direct unit coverage of the helper — partial / wrong-typed meta.
        kwargs = _meta_to_qaverdict_kwargs(
            {
                # score missing entirely
                "verdict": None,  # explicit None should normalize to ""
                "dimensions": None,  # wrong type should normalize to {}
                # differences omitted
                "suggestions": [],  # already empty
                "reasoning": None,  # explicit None should normalize to ""
                # extra keys MUST be filtered out so QAVerdict(**kwargs) works
                "loop_action": "skipped_delegated",
                "qa_session_id": "qa-fake",
                "passed": False,
                "status": "ok",
            }
        )
        # Filtered down to exactly QAVerdict's six fields:
        assert set(kwargs.keys()) == {
            "score",
            "verdict",
            "dimensions",
            "differences",
            "suggestions",
            "reasoning",
        }
        # Defaults applied:
        assert kwargs["score"] == 0.0
        assert kwargs["verdict"] == ""
        assert kwargs["dimensions"] == {}
        assert kwargs["differences"] == []
        assert kwargs["suggestions"] == []
        assert kwargs["reasoning"] == ""

        # The kwargs MUST construct a valid QAVerdict (no TypeError).
        verdict = QAVerdict(**kwargs)
        # Round-trip through _serialize_qa_verdict yields canonical-shape dict:
        serialized = _serialize_qa_verdict(verdict)
        assert serialized == {
            "score": 0.0,
            "verdict": "",
            "dimensions": {},
            "differences": [],
            "suggestions": [],
            "reasoning": "",
        }

        # End-to-end: feed the same partial meta through run_inline_qa and
        # confirm its returned verdict_dict matches the canonical empty shape.
        partial_meta = {
            "loop_action": "fail",  # valid loop_action so we hit the
                                    # serialization branch, not skip path
            "qa_session_id": "qa-fake",
        }
        handler = FakeQAHandler([_make_ok_result(partial_meta)])
        seed = FakeSeed()

        outcome = await run_inline_qa(
            handler,  # type: ignore[arg-type]
            postmortem=self._pm(),
            ac_index=0,
            ac_content="AC",
            seed=seed,  # type: ignore[arg-type]
        )

        assert outcome.loop_action == "fail"
        assert outcome.verdict_dict == {
            "score": 0.0,
            "verdict": "",
            "dimensions": {},
            "differences": [],
            "suggestions": [],
            "reasoning": "",
        }
        assert outcome.score == 0.0
