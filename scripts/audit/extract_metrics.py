#!/usr/bin/env python3
"""Extract per-run metrics from Ouroboros's global event store.

The orchestrator (cli/commands/run.py:524) writes all events to a hardcoded
``~/.ouroboros/ouroboros.db`` regardless of the OUROBOROS_EVENT_STORE_PATH
env var. The audit harness can't divert that, so this script extracts
metrics for a specific session by filtering on session_id (parsed from the
session.log).

Usage:
    extract_metrics.py <session_log> <wall_seconds> <exit_code> <out_metrics.json>

Reads:
    - <session_log>  — orchestrator stdout/stderr; we grep session_id from it
    - ~/.ouroboros/ouroboros.db (default) or $OUROBOROS_GLOBAL_EVENT_STORE if set

Writes:
    - <out_metrics.json> — { session_id, ac_count, tokens_total, cost_usd,
                             tool_calls_count, messages_count,
                             wall_seconds_total, exit_code }

Best-effort: missing values fall back to 0/None.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from pathlib import Path

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[mGKHFJ]")
SESSION_ID_RE = re.compile(r"session_id=(orch_[0-9a-f]+)")
AC_LIFECYCLE_TYPES = ("orchestrator.task.started", "orchestrator.task.completed")
PROGRESS_TYPE = "workflow.progress.updated"
SESSION_STARTED_TYPE = "orchestrator.session.started"


def _global_db_path() -> Path:
    override = os.environ.get("OUROBOROS_GLOBAL_EVENT_STORE")
    if override:
        return Path(override)
    return Path.home() / ".ouroboros" / "ouroboros.db"


def _parse_session_id(log_path: Path) -> str | None:
    """Grep the session.log for the orchestrator's session_id."""
    if not log_path.exists():
        return None
    raw = log_path.read_text(encoding="utf-8", errors="ignore")
    text = ANSI_ESCAPE_RE.sub("", raw)
    matches = SESSION_ID_RE.findall(text)
    return matches[0] if matches else None


def _query_session_events(db: Path, session_id: str) -> list[dict]:
    """Return all events for the given session_id, sorted by created_at."""
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT event_type, payload
              FROM events
             WHERE aggregate_id = ?
                OR payload LIKE ?
             ORDER BY timestamp ASC
            """,
            (session_id, f'%"{session_id}"%'),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    out = []
    for event_type, raw_payload in rows:
        try:
            payload = json.loads(raw_payload) if isinstance(raw_payload, str) else json.loads(raw_payload.decode())
        except (json.JSONDecodeError, AttributeError):
            payload = {}
        out.append({"event_type": event_type, "payload": payload})
    return out


def extract(session_log: Path, wall: int, exit_code: int) -> dict:
    metrics = {
        "session_id": None,
        "ac_count": 0,
        "tokens_total": 0,
        "cost_usd": 0.0,
        "tool_calls_count": 0,
        "messages_count": 0,
        "wall_seconds_total": float(wall),
        "exit_code": exit_code,
    }

    session_id = _parse_session_id(session_log)
    metrics["session_id"] = session_id
    if not session_id:
        metrics["_extract_error"] = "no session_id found in session log"
        return metrics

    db = _global_db_path()
    events = _query_session_events(db, session_id)
    if not events:
        metrics["_extract_error"] = f"no events for session {session_id} in {db}"
        return metrics

    # Two event shapes depending on mode:
    #   parallel mode    → workflow.progress.updated (cumulative AC + tool counts)
    #   compounding mode → orchestrator.progress.updated (per-message; no cumulative
    #                      counts) + execution.ac.postmortem.captured (one per AC)
    # Strategy: prefer workflow-level cumulative counts when present, else
    # derive from per-event tallies that work for both modes.
    workflow_progress = [e for e in events if e["event_type"] == PROGRESS_TYPE]
    if workflow_progress:
        last = workflow_progress[-1]["payload"]
        metrics["ac_count"] = int(last.get("completed_count") or 0)
        metrics["tokens_total"] = int(last.get("estimated_tokens") or 0)
        metrics["cost_usd"] = float(last.get("estimated_cost_usd") or 0.0)
        metrics["tool_calls_count"] = int(last.get("tool_calls_count") or 0)
        metrics["messages_count"] = int(last.get("messages_count") or 0)

    # Fall-back / fill gaps from per-event tallies (works in both modes).
    if metrics["ac_count"] == 0:
        postmortems = sum(1 for e in events if e["event_type"] == "execution.ac.postmortem.captured")
        task_completed = sum(1 for e in events if e["event_type"] == "orchestrator.task.completed")
        metrics["ac_count"] = max(postmortems, task_completed)
    if metrics["tool_calls_count"] == 0:
        metrics["tool_calls_count"] = sum(1 for e in events if e["event_type"] == "orchestrator.tool.called")
    if metrics["messages_count"] == 0:
        metrics["messages_count"] = sum(
            1
            for e in events
            if e["event_type"] in ("orchestrator.progress.updated", "workflow.progress.updated")
        )

    if metrics["tokens_total"] == 0:
        # estimated_tokens is 0 when the runtime adapter (e.g. Claude
        # CLI subprocess) does not report token counts back to the
        # orchestrator. Use tool_calls + messages as cost proxies.
        metrics["_note_tokens"] = (
            "estimated_tokens=0 in event store; runtime did not report "
            "token usage. Use tool_calls_count and messages_count as "
            "comparative cost proxies."
        )

    return metrics


def main() -> int:
    if len(sys.argv) != 5:
        print("usage: extract_metrics.py <session_log> <wall> <exit_code> <out.json>", file=sys.stderr)
        return 2
    session_log = Path(sys.argv[1])
    wall = int(sys.argv[2])
    exit_code = int(sys.argv[3])
    out_path = Path(sys.argv[4])

    metrics = extract(session_log, wall, exit_code)
    out_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")

    sid = metrics.get("session_id") or "?"
    print(
        f"[metrics] session={sid} ac_count={metrics['ac_count']} "
        f"tokens={metrics['tokens_total']} cost=${metrics['cost_usd']:.2f} "
        f"tools={metrics['tool_calls_count']} wall={wall}s exit={exit_code}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
