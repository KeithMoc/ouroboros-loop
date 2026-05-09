#!/usr/bin/env bash
# Parking-Lot Audit v2 Bench Harness — full-clone isolation, N=5 default
#
# Same isolation pattern as v1 (full git clone per run into /tmp/), but:
#   - points at examples/parking-lot-audit-v2/seed.yaml
#   - uses scripts/audit/extract_metrics.py for real-schema metrics
#     (parses session_id from session.log, queries ~/.ouroboros/ouroboros.db
#     filtered by aggregate_id, derives ac_count + tool/msg counts)
#   - default N=5 (vs v1's 3)
#   - clone root /tmp/parking-lot-audit-v2-clones/ to keep v1 leftovers separate
#   - artifacts at examples/parking-lot-audit-v2/runs/<mode>-r<i>/
#   - scoring via scripts/audit/score_parking_lot_v2.py (AST-derived
#     alignment-by-axis, not verbatim-string match)
#
# Usage:
#   scripts/audit/run_parking_lot_bench_v2.sh [N] [MODES]
#
# Examples:
#   scripts/audit/run_parking_lot_bench_v2.sh                # N=5, both modes
#   scripts/audit/run_parking_lot_bench_v2.sh 1              # pilot N=1, both modes
#   scripts/audit/run_parking_lot_bench_v2.sh 1 compounding  # single-mode pilot

set -euo pipefail

REPO_ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
AUDIT_ROOT="$REPO_ROOT/examples/parking-lot-audit-v2"
SEED_FILE="$AUDIT_ROOT/seed.yaml"
RUNS_ROOT="$AUDIT_ROOT/runs"
CLONES_ROOT="/tmp/parking-lot-audit-v2-clones"
EXTRACT_SCRIPT="$REPO_ROOT/scripts/audit/extract_metrics.py"
SCORE_SCRIPT="$REPO_ROOT/scripts/audit/score_parking_lot_v2.py"

N="${1:-5}"
MODES="${2:-parallel compounding}"

if [[ ! -f "$SEED_FILE" ]]; then
  echo "[bench-v2] seed file not found: $SEED_FILE" >&2
  exit 1
fi
if ! git -C "$REPO_ROOT" ls-files --error-unmatch "$SEED_FILE" >/dev/null 2>&1; then
  echo "[bench-v2] seed not tracked: $SEED_FILE — commit it first" >&2
  exit 4
fi

mkdir -p "$RUNS_ROOT" "$CLONES_ROOT"

run_one() {
  local mode="$1" replay="$2"
  local run_id="${mode}-r${replay}"
  local run_dir="$RUNS_ROOT/$run_id"
  local clone_dir="$CLONES_ROOT/$run_id"

  if [[ -d "$run_dir/output" ]]; then
    echo "[bench-v2] $run_id artifacts captured — skipping (delete to re-run)"
    return 0
  fi
  mkdir -p "$run_dir"
  rm -rf "$clone_dir"

  echo "[bench-v2] === $run_id ==="
  git clone --local --no-hardlinks --quiet "$REPO_ROOT" "$clone_dir"
  cp "$SEED_FILE" "$clone_dir/seed.yaml"

  local mode_flag=""
  if [[ "$mode" == "compounding" ]]; then
    mode_flag="--compounding"
  fi

  local started_at
  started_at=$(date +%s)

  pushd "$clone_dir" >/dev/null
  set +e
  uv run --frozen --project "$REPO_ROOT" ouroboros run workflow "$clone_dir/seed.yaml" \
    $mode_flag --runtime claude --max-decomposition-depth 0 \
    > "$clone_dir/session.log" 2>&1
  local exit_code=$?
  set -e
  popd >/dev/null

  local finished_at
  finished_at=$(date +%s)
  local wall=$((finished_at - started_at))

  echo "[bench-v2]   exit=$exit_code wall=${wall}s"

  # Capture artifacts.
  if [[ -d "$clone_dir/output" ]]; then
    rm -rf "$run_dir/output"
    cp -r "$clone_dir/output" "$run_dir/output"
  fi
  if [[ -f "$clone_dir/session.log" ]]; then
    cp "$clone_dir/session.log" "$run_dir/session.log"
  fi
  git -C "$clone_dir" log --all --oneline -100 > "$run_dir/commits.log" 2>/dev/null || true

  # Real metrics via extract script.
  python3 "$EXTRACT_SCRIPT" "$run_dir/session.log" "$wall" "$exit_code" "$run_dir/metrics.json" \
    || echo "[bench-v2]   metrics extraction failed (non-fatal)"

  rm -rf "$clone_dir"

  if [[ $exit_code -ne 0 ]]; then
    echo "[bench-v2]   ⚠ orchestrator exited $exit_code"
  fi
}

echo "[bench-v2] N=$N MODES=$MODES"
echo "[bench-v2] clones: $CLONES_ROOT (ephemeral)"
echo "[bench-v2] artifacts: $RUNS_ROOT (kept)"
for mode in $MODES; do
  if [[ "$mode" != "parallel" && "$mode" != "compounding" ]]; then
    echo "[bench-v2] unknown mode: $mode" >&2
    exit 2
  fi
  for ((i=1; i<=N; i++)); do
    run_one "$mode" "$i"
  done
done

echo "[bench-v2] all runs complete; scoring..."
python3 "$SCORE_SCRIPT"
echo "[bench-v2] DONE"
