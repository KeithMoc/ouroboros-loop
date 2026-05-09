#!/usr/bin/env bash
# Parking-Lot Audit Bench Harness — full-clone isolation
#
# Drives the Ouroboros orchestrator through the parking-lot seed in two modes
# (parallel, compounding), N replays each, with each run isolated in its own
# **full local git clone** under /tmp/parking-lot-audit-clones/. After all runs
# finish, invokes the score script.
#
# Why full-clone isolation (vs git worktree, vs cwd switch):
#   The orchestrator's agent freely runs `cd <abs-path>` and resolves
#   project_dir from seed-file location. Only true filesystem separation
#   stops cross-run contamination. Each clone has its own .git, its own
#   working tree, and lives at a path the agent has no prior knowledge of.
#
# Usage:
#   scripts/audit/run_parking_lot_bench.sh [N] [MODES]
#
# Args:
#   N      — replay count per mode. Default 3.
#   MODES  — space-separated. Default "parallel compounding".
#
# Per-run isolation:
#   /tmp/parking-lot-audit-clones/<mode>-r<i>/   (full git clone, deleted after)
#   ├── seed.yaml             — copied from this repo, lives at clone root
#   ├── output/               — orchestrator generates project here
#   ├── events.sqlite         — per-run event store
#   └── session.log           — orchestrator stdout+stderr
#
# Captured to repo (tracked dir, but per-run artifacts are gitignored):
#   examples/parking-lot-audit/runs/<mode>-r<i>/
#     output/                  — generated source
#     events.sqlite            — copy of per-run event store
#     session.log              — orchestrator log
#     metrics.json             — { ac_count, tokens_total, wall_seconds_total, session_id, exit_code }
#     commits.log              — git log from the per-run clone
#
# Final:
#   examples/parking-lot-audit/REPORT.md
#   examples/parking-lot-audit/scores.json

set -euo pipefail

REPO_ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
AUDIT_ROOT="$REPO_ROOT/examples/parking-lot-audit"
SEED_FILE="$AUDIT_ROOT/seed.yaml"
RUNS_ROOT="$AUDIT_ROOT/runs"
CLONES_ROOT="/tmp/parking-lot-audit-clones"
SCORE_SCRIPT="$REPO_ROOT/scripts/audit/score_parking_lot.py"

N="${1:-3}"
MODES="${2:-parallel compounding}"

if [[ ! -f "$SEED_FILE" ]]; then
  echo "[bench] seed file not found: $SEED_FILE" >&2
  exit 1
fi

if ! git -C "$REPO_ROOT" ls-files --error-unmatch "$SEED_FILE" >/dev/null 2>&1; then
  echo "[bench] seed file not tracked: $SEED_FILE" >&2
  echo "[bench] git add + commit it before running the bench." >&2
  exit 4
fi

mkdir -p "$RUNS_ROOT" "$CLONES_ROOT"

run_one() {
  local mode="$1" replay="$2"
  local run_id="${mode}-r${replay}"
  local run_dir="$RUNS_ROOT/$run_id"
  local clone_dir="$CLONES_ROOT/$run_id"

  if [[ -d "$run_dir/output" ]]; then
    echo "[bench] $run_id artifacts already captured — skipping (delete to re-run)"
    return 0
  fi

  mkdir -p "$run_dir"

  # Tear down any stale clone from a prior crashed run.
  rm -rf "$clone_dir"

  echo "[bench] === $run_id ==="
  git clone --local --no-hardlinks --quiet "$REPO_ROOT" "$clone_dir"
  echo "[bench]   clone=$clone_dir"

  # Place the seed at the clone root so the orchestrator resolves project_dir
  # to the clone, not to any path the agent might know about in the parent repo.
  cp "$SEED_FILE" "$clone_dir/seed.yaml"

  local mode_flag=""
  if [[ "$mode" == "compounding" ]]; then
    mode_flag="--compounding"
  fi

  local event_store="$clone_dir/events.sqlite"

  local started_at
  started_at=$(date +%s)

  pushd "$clone_dir" >/dev/null
  set +e
  OUROBOROS_EVENT_STORE_PATH="$event_store" \
  uv run --frozen --project "$REPO_ROOT" ouroboros run workflow "$clone_dir/seed.yaml" \
    $mode_flag --runtime claude \
    > "$clone_dir/session.log" 2>&1
  local exit_code=$?
  set -e
  popd >/dev/null

  local finished_at
  finished_at=$(date +%s)
  local wall=$((finished_at - started_at))

  echo "[bench]   exit=$exit_code wall=${wall}s"

  # Capture artifacts from clone → run_dir.
  if [[ -d "$clone_dir/output" ]]; then
    rm -rf "$run_dir/output"
    cp -r "$clone_dir/output" "$run_dir/output"
  fi
  if [[ -f "$event_store" ]]; then
    cp "$event_store" "$run_dir/events.sqlite"
  fi
  if [[ -f "$clone_dir/session.log" ]]; then
    cp "$clone_dir/session.log" "$run_dir/session.log"
  fi
  git -C "$clone_dir" log --all --oneline -100 > "$run_dir/commits.log" 2>/dev/null || true

  # Extract metrics from the event store. Best-effort — the orchestrator's
  # event schema may vary. Drift metrics from generated source are the
  # primary audit signal regardless.
  python3 - "$run_dir/events.sqlite" "$run_dir/metrics.json" "$wall" "$exit_code" <<'PY' || echo "[bench]   metrics extraction failed (non-fatal)"
import json, sqlite3, sys
from pathlib import Path

event_store, out_path, wall, exit_code = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
metrics = {
    "ac_count": 0,
    "tokens_total": 0,
    "wall_seconds_total": float(wall),
    "exit_code": exit_code,
    "session_id": None,
}
try:
    conn = sqlite3.connect(event_store)
    cur = conn.cursor()
    tables = {row[0] for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "events" in tables:
        cur.execute("PRAGMA table_info(events)")
        cols = [c[1] for c in cur.fetchall()]
        # Be permissive about which column holds the JSON payload.
        json_col = next((c for c in ("data", "payload", "body") if c in cols), None)
        if json_col:
            cur.execute(f"SELECT {json_col} FROM events")
            for (raw,) in cur.fetchall():
                try:
                    payload = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
                except Exception:
                    continue
                etype = (payload.get("event_type") or payload.get("type") or "").lower()
                if "ac.started" in etype or "ac_started" in etype:
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
print(f"[bench]   metrics: ac_count={metrics['ac_count']} tokens={metrics['tokens_total']} wall={wall}s exit={exit_code}")
PY

  # Drop the clone — artifacts already captured.
  rm -rf "$clone_dir"

  if [[ $exit_code -ne 0 ]]; then
    echo "[bench]   ⚠ orchestrator exited $exit_code (artifacts captured; score script will surface failures)"
  fi
}

echo "[bench] N=$N MODES=$MODES"
echo "[bench] clones: $CLONES_ROOT (ephemeral)"
echo "[bench] artifacts: $RUNS_ROOT (kept)"
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
