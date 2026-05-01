"""Unit tests for the QA-verdict audit-diff sidecar writer.

Exercises :func:`ouroboros.orchestrator.serial_executor._append_qa_audit_diff`
— the AC-3 helper that records every overwritten QA verdict in a unified-diff
sidecar file paired with the chain-markdown artifact.

The five tests cover the full surface area of the helper required by the
Q4.1 design's ``audit_trail_preservation`` and ``graceful_degradation``
principles:

  Test 45 — first-replay file creation, header + ``### Replay 1`` section.
  Test 46 — cumulative second-replay append, prior section preserved,
            ``post-replay-1`` lineage label on the second hunk.
  Test 47 — ``None`` verdict serialized as the literal JSON ``null`` so the
            diff stays well-formed when a phase-1 sentinel had no prior
            verdict to overwrite.
  Test 48 — filesystem write failure is swallowed: helper returns ``None``,
            never raises, emits ``serial_executor.qa_audit_diff.write_failed``
            warning so the run continues uninterrupted.
  Test 49 — patch round-trip: extracting +/- lines from the recorded diff
            recovers the original and replay verdict JSON byte-for-byte,
            proving the audit record is genuinely reversible.

[[INVARIANT: _append_qa_audit_diff is best-effort — write failures never raise]]
[[INVARIANT: _append_qa_audit_diff appends a new '### Replay N' section per call, preserving prior sections]]
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from ouroboros.orchestrator.serial_executor import _append_qa_audit_diff


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _expected_path(artifact_dir: Path, session_id: str, ac_index: int) -> Path:
    """Mirror the helper's filename convention for assertions."""
    return artifact_dir / f"chain-{session_id}-ac{ac_index}-qa.original.diff"


def _reconstruct_from_unified_diff(diff_text: str) -> tuple[str, str]:
    """Rebuild original-side and replay-side text from a unified-diff block.

    Works for inputs small enough to fit in a single contiguous hunk (no gap
    between hunks).  Sufficient for the round-trip test below where the
    verdict JSON is ~10 lines.

    Returns:
        Tuple of ``(original_text, replay_text)`` reconstructed by
        partitioning lines on the unified-diff prefix.
    """
    original_chunks: list[str] = []
    replay_chunks: list[str] = []
    for line in diff_text.splitlines(keepends=True):
        # File / hunk headers are not part of either side's content.
        if line.startswith("--- ") or line.startswith("+++ "):
            continue
        if line.startswith("@@"):
            continue
        if line.startswith("-"):
            original_chunks.append(line[1:])
        elif line.startswith("+"):
            replay_chunks.append(line[1:])
        elif line.startswith(" "):
            # Context line — present in both sides.
            original_chunks.append(line[1:])
            replay_chunks.append(line[1:])
        else:
            # Should not occur for valid unified-diff bodies.
            continue
    return "".join(original_chunks), "".join(replay_chunks)


def _extract_diff_block(file_text: str, replay_number: int) -> str:
    """Slice out the unified-diff body of a specific ``### Replay N`` section.

    Returns the substring starting at the ``---`` line of the requested
    replay and ending just before the next ``### Replay`` header (or EOF).
    """
    header = f"### Replay {replay_number} "
    start = file_text.index(header)
    # Skip past the section header line + blank separator.
    body_start = file_text.index("\n", start) + 1
    # Skip an optional blank line right after the header.
    if file_text[body_start:body_start + 1] == "\n":
        body_start += 1
    next_header_match = re.search(
        r"^### Replay \d+ ", file_text[body_start:], re.MULTILINE
    )
    if next_header_match is None:
        body_end = len(file_text)
    else:
        body_end = body_start + next_header_match.start()
    return file_text[body_start:body_end]


# ---------------------------------------------------------------------------
# Test 45 — first-replay file creation
# ---------------------------------------------------------------------------


def test_45_first_replay_creates_file_with_header_and_section(
    tmp_path: Path,
) -> None:
    """First call creates the sidecar file with header + ``### Replay 1``.

    Verifies:
      * Returned :class:`pathlib.Path` matches the documented
        ``chain-<session>-ac<N>-qa.original.diff`` filename convention.
      * File contents open with the markdown header naming the session and
        AC index — supports human grep / triage when CI uploads the artifact.
      * Exactly one ``### Replay`` header is present (this is replay #1).
      * The unified-diff body uses ``original`` as the from-file label
        (lineage marker; replay 2+ would use ``post-replay-1``).
    """
    session_id = "sess-test45"
    ac_index = 3
    original = {"verdict": "pass", "score": 0.95, "reason": "All checks ok"}
    replay = {"verdict": "fail", "score": 0.40, "reason": "Test broke"}

    written_path = _append_qa_audit_diff(
        session_id=session_id,
        ac_index=ac_index,
        original_verdict_dict=original,
        replay_verdict_dict=replay,
        artifact_dir=str(tmp_path),
        timestamp=datetime(2026, 4, 29, 12, 30, 0, tzinfo=UTC),
    )

    expected = _expected_path(tmp_path, session_id, ac_index)
    assert written_path == expected, (
        "Helper must return the exact sidecar path so callers can log / "
        "surface it in event payloads."
    )
    assert expected.exists(), "Sidecar file must exist after a successful call."

    text = expected.read_text(encoding="utf-8")
    assert (
        f"# QA verdict audit diff — session {session_id}, AC {ac_index}"
        in text
    ), "First-time write must emit the markdown header line."
    assert text.count("### Replay ") == 1, (
        "Exactly one Replay section after a single call."
    )
    assert "### Replay 1 (2026-04-29 12:30:00 UTC)" in text, (
        "Replay header must include the supplied timestamp in UTC format."
    )
    # First replay's lineage label is "original".
    assert "--- original" in text
    assert "+++ replay" in text


# ---------------------------------------------------------------------------
# Test 46 — cumulative second-replay append
# ---------------------------------------------------------------------------


def test_46_second_replay_appends_without_overwriting(tmp_path: Path) -> None:
    """Second call appends ``### Replay 2`` while preserving Replay 1.

    Verifies the cumulative-history requirement from the design doc:
      * Both Replay 1 and Replay 2 sections coexist in the file.
      * Replay 2's lineage label is ``post-replay-1`` (not ``original``),
        making the chain of overwrites unambiguous.
      * The first replay's diff body remains byte-identical to the
        single-call result — appending must never rewrite history.
    """
    session_id = "sess-test46"
    ac_index = 1
    v1 = {"verdict": "pass", "score": 0.9}
    v2 = {"verdict": "revise", "score": 0.5}
    v3 = {"verdict": "fail", "score": 0.1}

    # First replay (v1 → v2).
    path1 = _append_qa_audit_diff(
        session_id=session_id,
        ac_index=ac_index,
        original_verdict_dict=v1,
        replay_verdict_dict=v2,
        artifact_dir=str(tmp_path),
        timestamp=datetime(2026, 4, 29, 10, 0, 0, tzinfo=UTC),
    )
    text_after_first = path1.read_text(encoding="utf-8") if path1 else ""
    assert path1 is not None
    first_replay_block = _extract_diff_block(text_after_first, replay_number=1)

    # Second replay (v2 → v3).  Must append without rewriting Replay 1.
    path2 = _append_qa_audit_diff(
        session_id=session_id,
        ac_index=ac_index,
        original_verdict_dict=v2,
        replay_verdict_dict=v3,
        artifact_dir=str(tmp_path),
        timestamp=datetime(2026, 4, 29, 11, 0, 0, tzinfo=UTC),
    )
    assert path2 == path1, "Both calls must target the same sidecar path."
    text_after_second = path2.read_text(encoding="utf-8")

    # Both sections present.
    assert text_after_second.count("### Replay ") == 2
    assert "### Replay 1 (2026-04-29 10:00:00 UTC)" in text_after_second
    assert "### Replay 2 (2026-04-29 11:00:00 UTC)" in text_after_second

    # Replay 1's body is preserved verbatim.
    preserved_first_block = _extract_diff_block(
        text_after_second, replay_number=1
    )
    assert preserved_first_block == first_replay_block, (
        "Append must never rewrite earlier Replay sections."
    )

    # Replay 2 uses post-replay-1 lineage label.
    second_block = _extract_diff_block(text_after_second, replay_number=2)
    assert "--- post-replay-1" in second_block, (
        "Replay 2's from-label must be 'post-replay-1' to chain the "
        "overwrite lineage."
    )
    assert "+++ replay" in second_block


# ---------------------------------------------------------------------------
# Test 47 — None-verdict rendering
# ---------------------------------------------------------------------------


def test_47_none_verdict_renders_as_json_null(tmp_path: Path) -> None:
    """A ``None`` verdict serializes as the literal JSON value ``null``.

    Phase-1 sentinel writes carry no prior verdict (``qa_verdict=None``).
    When the replay overwrites that state, the helper must still produce a
    well-formed unified diff — the ``-null`` line on the original side
    represents the absent prior verdict.
    """
    session_id = "sess-test47"
    ac_index = 0
    replay_verdict = {"verdict": "pass", "score": 1.0}

    written_path = _append_qa_audit_diff(
        session_id=session_id,
        ac_index=ac_index,
        original_verdict_dict=None,
        replay_verdict_dict=replay_verdict,
        artifact_dir=str(tmp_path),
        timestamp=datetime(2026, 4, 29, 9, 0, 0, tzinfo=UTC),
    )
    assert written_path is not None
    text = written_path.read_text(encoding="utf-8")

    # The original side must include a `-null` removal line (with the
    # leading `-` it appears as `-null` in the diff body).
    diff_body = _extract_diff_block(text, replay_number=1)
    assert "-null" in diff_body, (
        "None original verdict must serialize as `-null` on the diff's "
        "from-file side."
    )

    # And the inverse case: None on the replay side renders as `+null`.
    written_path2 = _append_qa_audit_diff(
        session_id=session_id,
        ac_index=ac_index + 1,  # different file to avoid Replay 2 numbering
        original_verdict_dict={"verdict": "pass"},
        replay_verdict_dict=None,
        artifact_dir=str(tmp_path),
        timestamp=datetime(2026, 4, 29, 9, 5, 0, tzinfo=UTC),
    )
    assert written_path2 is not None
    text2 = written_path2.read_text(encoding="utf-8")
    diff_body2 = _extract_diff_block(text2, replay_number=1)
    assert "+null" in diff_body2, (
        "None replay verdict must serialize as `+null` on the diff's "
        "to-file side."
    )

    # And the both-None corner case must not raise — the diff body simply
    # records "no textual difference" since two `null` JSON strings are
    # identical.
    written_path3 = _append_qa_audit_diff(
        session_id=session_id,
        ac_index=ac_index + 2,
        original_verdict_dict=None,
        replay_verdict_dict=None,
        artifact_dir=str(tmp_path),
        timestamp=datetime(2026, 4, 29, 9, 10, 0, tzinfo=UTC),
    )
    assert written_path3 is not None
    text3 = written_path3.read_text(encoding="utf-8")
    assert "(no textual difference)" in text3


# ---------------------------------------------------------------------------
# Test 48 — write-failure warning (no raise)
# ---------------------------------------------------------------------------


def test_48_write_failure_logs_warning_and_returns_none(
    tmp_path: Path,
) -> None:
    """A filesystem error must be swallowed, logged, and yield ``None``.

    The design treats audit-diff writes as a recovery aide, not a
    correctness boundary.  An unwritable artifact directory or a transient
    OS error must NOT abort the run — the helper degrades to a logged
    warning under ``serial_executor.qa_audit_diff.write_failed``.

    Implementation: we point the helper at an ``artifact_dir`` whose path
    is occupied by a regular file.  ``Path.mkdir(parents=True,
    exist_ok=True)`` raises :class:`FileExistsError` in that situation
    because the existing entry is not a directory — exactly the kind of
    OS error the helper must absorb.
    """
    # Create a regular file at the path we'll pass as ``artifact_dir``.
    blocking_file = tmp_path / "not-a-directory"
    blocking_file.write_text("blocker", encoding="utf-8")

    with patch("ouroboros.orchestrator.serial_executor.log") as mock_log:
        result = _append_qa_audit_diff(
            session_id="sess-test48",
            ac_index=0,
            original_verdict_dict={"a": 1},
            replay_verdict_dict={"a": 2},
            artifact_dir=str(blocking_file),
        )

    assert result is None, (
        "Write failure must yield None — the helper is best-effort and "
        "callers ignore the return value when audit-recording fails."
    )
    # Verify the warning was emitted with the documented event name.
    assert mock_log.warning.called, (
        "Write failure path must log under "
        "'serial_executor.qa_audit_diff.write_failed'."
    )
    warning_event = mock_log.warning.call_args[0][0]
    assert warning_event == "serial_executor.qa_audit_diff.write_failed"
    warning_kwargs = mock_log.warning.call_args.kwargs
    assert warning_kwargs["session_id"] == "sess-test48"
    assert warning_kwargs["ac_index"] == 0
    # The exception text is captured so triage isn't blind.
    assert "error" in warning_kwargs
    assert warning_kwargs["error"]  # non-empty string


# ---------------------------------------------------------------------------
# Test 49 — patch round-trip
# ---------------------------------------------------------------------------


def test_49_diff_round_trips_to_input_verdicts(tmp_path: Path) -> None:
    """Extracting ``+``/``-`` lines from the recorded diff recovers both verdicts.

    The audit-diff is the audit record — if it isn't reversible, the trail
    is broken.  This test proves the recorded unified diff genuinely lets
    a reader recover both the original and the replay verdict JSON
    byte-for-byte.

    To keep the unified diff a single contiguous hunk (so the simple
    ``+``/``-``/` `` partitioning recovers the full files), the two
    verdicts are kept small and share most keys with the default
    ``n=3`` context width.
    """
    session_id = "sess-test49"
    ac_index = 7
    original = {
        "verdict": "pass",
        "score": 0.85,
        "reason": "Looks good",
        "issues": [],
    }
    replay = {
        "verdict": "revise",
        "score": 0.55,
        "reason": "Edge case missing",
        "issues": ["off-by-one in loop bound"],
    }

    written_path = _append_qa_audit_diff(
        session_id=session_id,
        ac_index=ac_index,
        original_verdict_dict=original,
        replay_verdict_dict=replay,
        artifact_dir=str(tmp_path),
        timestamp=datetime(2026, 4, 29, 8, 0, 0, tzinfo=UTC),
    )
    assert written_path is not None
    diff_body = _extract_diff_block(
        written_path.read_text(encoding="utf-8"), replay_number=1
    )

    recovered_original_text, recovered_replay_text = (
        _reconstruct_from_unified_diff(diff_body)
    )

    # The helper serializes with ``json.dumps(..., indent=2, sort_keys=True,
    # default=str)``.  Reproduce the exact serialization for comparison.
    expected_original_text = (
        json.dumps(original, indent=2, sort_keys=True, default=str) + "\n"
    )
    expected_replay_text = (
        json.dumps(replay, indent=2, sort_keys=True, default=str) + "\n"
    )

    assert recovered_original_text == expected_original_text, (
        "Round-trip from the recorded diff must reproduce the original "
        "verdict's JSON serialization byte-for-byte."
    )
    assert recovered_replay_text == expected_replay_text, (
        "Round-trip from the recorded diff must reproduce the replay "
        "verdict's JSON serialization byte-for-byte."
    )

    # And the recovered JSON parses back to the original Python dicts.
    assert json.loads(recovered_original_text) == original
    assert json.loads(recovered_replay_text) == replay
