#!/usr/bin/env bash
# Parking-Lot Audit Bench Harness
#
# Drives the Ouroboros orchestrator through `examples/parking-lot-audit/seed.yaml`
# in two modes (parallel, compounding), N replays each, with each run isolated
# in its own git worktree + branch + event store. After all runs finish,
# invokes the score script.
#
# Usage:
#   scripts/audit/run_parking_lot_bench.sh [N] [MODES]
#
# Args:
#   N      — replay count per mode. Default 3.
#   MODES  — space-separated list. Default "parallel compounding".
#            Use a single mode for a pilot, e.g. "parallel" or "compounding".
#
# Examples:
#   scripts/audit/run_parking_lot_bench.sh                # N=3, both modes
#   scripts/audit/run_parking_lot_bench.sh 1              # N=1, both modes
#   scripts/audit/run_parking_lot_bench.sh 1 compounding  # pilot single mode
#
# Per-run isolation:
#   - Worktree at .audit-worktrees/<mode>-r<i>/  (gitignored)
#   - Branch:    audit/<mode>-r<i>               (deletable after scoring)
#   - Event store at the run dir's events.sqlite (separate per run)
#
# After each run, artifacts are copied from the worktree into:
#   examples/parking-lot-audit/runs/<mode>-r<i>/
#     output/                  — generated source from the orchestrator
#     events.sqlite            — copy of the per-run event store
#     session.log              — orchestrator stdout+stderr
#     metrics.json             — { ac_count, tokens_total, wall_seconds_total, session_id }
#     commits.log              — git log from the per-run worktree
#
# After all runs:
#   examples/parking-lot-audit/REPORT.md
#   examples/parking-lot-audit/scores.json

set -euo pipefail

REPO_ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
AUDIT_ROOT="$REPO_ROOT/examples/parking-lot-audit"
SEED_FILE="$AUDIT_ROOT/seed.yaml"
RUNS_ROOT="$AUDIT_ROOT/runs"
WORKTREES_ROOT="$REPO_ROOT/.audit-worktrees"
SCORE_SCRIPT="$REPO_ROOT/scripts/audit/score_parking_lot.py"

N="${1:-3}"
MODES="${2:-parallel compounding}"

if [[ ! -f "$SEED_FILE" ]]; then
  echo "[bench] seed file not found: $SEED_FILE" >&2
  exit 1
fi

# Confirm seed file + harness are committed — worktree won't see uncommitted files.
if ! git -C "$REPO_ROOT" diff --quiet HEAD -- "$SEED_FILE" 2>/dev/null; then
  echo "[bench] seed file has uncommitted changes — worktrees won't see them. Commit first." >&2
  exit 3
fi
if ! git -C "$REPO_ROOT" ls-files --error-unmatch "$SEED_FILE" >/dev/null 2>&1; then
  echo "[bench] seed file not tracked: $SEED_FILE" >&2
  echo "[bench] git add + commit it before running the bench." >&2
  exit 4
fi

mkdir -p "$RUNS_ROOT" "$WORKTREES_ROOT"

# Resolve the orchestrator entry point. Prefer `uv run ouroboros` from the
# repo root so we exercise the in-tree fork code, not any system install.
ORCH_RUNNER=(uv run --frozen --project "$REPO_ROOT" ouroboros run workflow "$SEED_FILE")

run_one() {
  local mode="$1" replay="$2"
  local run_id="${mode}-r${replay}"
  local run_dir="$RUNS_ROOT/$run_id"
  local wt_dir="$WORKTREES_ROOT/$run_id"
  local branch="audit/${mode}-r${replay}"

  if [[ -d "$run_dir/output" ]]; then
    echo "[bench] $run_id artifacts already captured — skipping (delete to re-run)"
    return 0
  fi

  mkdir -p "$run_dir"

  # Tear down any stale worktree from a prior crashed run.
  if [[ -d "$wt_dir" ]]; then
    git -C "$REPO_ROOT" worktree remove --force "$wt_dir" 2>/dev/null || rm -rf "$wt_dir"
  fi
  if git -C "$REPO_ROOT" show-ref --verify --quiet "refs/heads/$branch"; then
    git -C "$REPO_ROOT" branch -D "$branch" 2>/dev/null || true
  fi

  echo "[bench] === $run_id ==="
  git -C "$REPO_ROOT" worktree add -b "$branch" "$wt_dir" main >/dev/null
  echo "[bench]   worktree=$wt_dir"
  echo "[bench]   branch=$branch"

  local mode_flag=""
  if [[ "$mode" == "compounding" ]]; then
    mode_flag="--compounding"
  fi

  local event_store="$wt_dir/events.sqlite"
  export OUROBOROS_EVENT_STORE_PATH="$event_store"

  local started_at
  started_at=$(date +%s)

  pushd "$wt_dir" >/dev/null
  set +e
  "${ORCH_RUNNER[@]}" $mode_flag --runtime claude \
    > "$wt_dir/session.log" 2>&1
  local exit_code=$?
  set -e
  popd >/dev/null

  local finished_at
  finished_at=$(date +%s)
  local wall=$((finished_at - started_at))

  echo "[bench]   exit=$exit_code wall=${wall}s"

  # Capture artifacts from worktree → run_dir.
  if [[ -d "$wt_dir/output" ]]; then
    rm -rf "$run_dir/output"
    cp -r "$wt_dir/output" "$run_dir/output"
  fi
  if [[ -f "$event_store" ]]; then
    cp "$event_store" "$run_dir/events.sqlite"
  fi
  if [[ -f "$wt_dir/session.log" ]]; then
    cp "$wt_dir/session.log" "$run_dir/session.log"
  fi
  git -C "$wt_dir" log --oneline -50 > "$run_dir/commits.log" 2>/dev/null || true

  # Extract metrics from the event store. Best-effort — schema may vary.
  python3 - "$run_dir/events.sqlite" "$run_dir/metrics.json" "$wall" <<'PY' || echo "[bench]   metrics extraction failed (non-fatal)"
import json, sqlite3, sys
from pathlib import Path

event_store, out_path, wall = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3])
metrics = {"ac_count": 0, "tokens_total": 0, "wall_seconds_total": float(wall), "session_id": None}
try:
    conn = sqlite3.connect(event_store)
    cur = conn.cursor()
    tables = {row[0] for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "events" in tables:
        cur.execute("SELECT data FROM events")
        for (raw,) in cur.fetchall():
            try:
                payload = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
            except Exception:
                continue
            etype = payload.get("event_type") or payload.get("type") or ""
            if "AC_STARTED" in etype.upper() or etype.endswith("ac.started"):
                metrics["ac_count"] += 1
            tok = payload.get("tokens") or payload.get("token_count") or {}
            if isinstance(tok, dict):
                metrics["tokens_total"] += int(tok.get("prompt", 0)) + int(tok.get("completion", 0))
            elif isinstance(tok, int):
                metrics["tokens_total"] += tok
            sid = payload.get("session_id")
            if sid and not metrics["session_id"]:
                metrics["session_id"] = sid
    conn.close()
except Exception as e:
    metrics["_extract_error"] = repr(e)

out_path.write_text(json.dumps(metrics, indent=2))
print(f"[bench]   metrics: ac_count={metrics['ac_count']} tokens={metrics['tokens_total']} wall={wall}s")
PY

  # Tear down worktree but keep branch (so commits stay reviewable).
  git -C "$REPO_ROOT" worktree remove --force "$wt_dir" 2>/dev/null || rm -rf "$wt_dir"

  if [[ $exit_code -ne 0 ]]; then
    echo "[bench]   ⚠ orchestrator exited $exit_code — score script will surface failures"
  fi
}

echo "[bench] N=$N MODES=$MODES"
echo "[bench] worktrees: $WORKTREES_ROOT"
echo "[bench] artifacts: $RUNS_ROOT"
for mode in $MODES; do
  if [[ "$mode" != "parallel" && "$mode" != "compounding" ]]; then
    echo "[bench] unknown mode: $mode" >&2
    exit 2
  fi
  for ((i=1; i<=N; i++)); do
    run_one "$mode" "$i"
  done
done

echo "[bench] all runs complete; scoring..."
python3 "$SCORE_SCRIPT"

echo "[bench] DONE"
echo "[bench] audit branches still exist: git branch --list 'audit/*'"
echo "[bench] to clean: git branch --list 'audit/*' | xargs -r git branch -D"
