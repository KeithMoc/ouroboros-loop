#!/usr/bin/env python3
"""Score a set of parking-lot-audit runs.

Walks examples/parking-lot-audit/runs/<mode>-r<i>/ for each completed run,
extracts a fixed set of metrics, and writes:

    examples/parking-lot-audit/REPORT.auto.md — auto-generated data tables
    (REPORT.md itself is curated and NEVER overwritten by this script)
    examples/parking-lot-audit/scores.json    — machine-readable sidecar

Metrics extracted per run:
    - ac_count           : number of ACs the orchestrator attempted
    - tokens_total       : sum of prompt+completion tokens across ACs
    - wall_seconds_total : end-to-end orchestrator wall time
    - schema_drift       : count of AC-1 schema field names MISSING from
                           output/parking/cli.py, tui.py, git_probe.py,
                           handoff_export.py
    - cli_drift          : count of CLI subcommand/flag names declared in
                           AC-2 (per the seed) MISSING from tui.py /
                           handoff_export.py invocations
    - invariant_count    : count of [[INVARIANT: ...]] tags emitted across
                           the run's postmortem chain or commit messages
    - tests_passed       : pytest -q exit / passed count for output/tests/
    - smoke_ok           : 1 if `parking add` + `parking list` exits 0,
                           else 0

The two "drift" metrics are the headline audit signals: compounding-mode
runs should score zero (or near-zero) on both, while parallel-mode runs
should drift on at least 1-2 names per replay.

Usage:
    python scripts/audit/score_parking_lot.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import mean

REPO_ROOT = Path(__file__).resolve().parents[2]
AUDIT_ROOT = REPO_ROOT / "examples" / "parking-lot-audit"
RUNS_ROOT = AUDIT_ROOT / "runs"
REPORT_PATH = AUDIT_ROOT / "REPORT.auto.md"
SCORES_PATH = AUDIT_ROOT / "scores.json"

SCHEMA_FIELDS = (
    "entry_id",
    "project",
    "worktree_path",
    "branch",
    "head_sha",
    "dirty",
    "parked_at",
    "tag",
    "note",
    "resumed_at",
)

CLI_SURFACE = (
    "parking add",
    "parking list",
    "parking resume",
    "parking done",
    "parking tag",
    "--worktree-path",
    "--project",
    "--tag",
    "--note",
    "--pending",
)

INVARIANT_RE = re.compile(r"\[\[INVARIANT:\s*([^\]]+?)\s*\]\]")


@dataclass
class RunScore:
    run_id: str
    mode: str
    replay: int
    ac_count: int = 0
    tokens_total: int = 0
    wall_seconds_total: float = 0.0
    schema_drift: int = 0
    cli_drift: int = 0
    invariant_count: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    smoke_ok: int = 0
    notes: list[str] = field(default_factory=list)


def _read_text_safely(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeDecodeError):
        return ""


def _gather_source(run_dir: Path) -> str:
    """Concatenate all generated source files for static checks."""
    output_dir = run_dir / "output"
    if not output_dir.exists():
        return ""
    blobs: list[str] = []
    for py_file in sorted(output_dir.rglob("*.py")):
        blobs.append(_read_text_safely(py_file))
    return "\n".join(blobs)


def _score_drift(haystack: str, needles: tuple[str, ...]) -> tuple[int, list[str]]:
    """Return (count_missing, missing_list)."""
    missing = [n for n in needles if n not in haystack]
    return len(missing), missing


def _count_invariants(run_dir: Path) -> int:
    """Count [[INVARIANT: ...]] tags in postmortem chain / commit messages /
    output sources."""
    blobs: list[str] = []
    chain_dir = run_dir / "docs" / "brainstorm"
    if chain_dir.exists():
        for f in chain_dir.glob("chain-*.md"):
            blobs.append(_read_text_safely(f))
    commits_log = run_dir / "commits.log"
    if commits_log.exists():
        blobs.append(_read_text_safely(commits_log))
    blobs.append(_gather_source(run_dir))
    return sum(len(INVARIANT_RE.findall(b)) for b in blobs)


def _read_metrics_json(run_dir: Path) -> dict:
    """Read tokens/wall-time captured by harness (if present).

    The harness writes metrics.json with keys:
        ac_count, tokens_total, wall_seconds_total, session_id
    """
    metrics_path = run_dir / "metrics.json"
    if not metrics_path.exists():
        return {}
    try:
        return json.loads(metrics_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _run_pytest(run_dir: Path) -> tuple[int, int]:
    """Returns (passed, failed). Both 0 if pytest unable to run."""
    tests_dir = run_dir / "output" / "tests"
    if not tests_dir.exists():
        return (0, 0)
    cmd = ["uv", "run", "--frozen", "pytest", str(tests_dir), "-q", "--tb=no", "-p", "no:cacheprovider"]
    try:
        proc = subprocess.run(
            cmd,
            cwd=run_dir,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return (0, 0)
    out = proc.stdout + proc.stderr
    m_pass = re.search(r"(\d+)\s+passed", out)
    m_fail = re.search(r"(\d+)\s+failed", out)
    return (int(m_pass.group(1)) if m_pass else 0, int(m_fail.group(1)) if m_fail else 0)


def _smoke_test(run_dir: Path) -> int:
    """Run `parking add` + `parking list` non-interactively. Returns 1 on success."""
    cli_path = run_dir / "output" / "parking" / "cli.py"
    if not cli_path.exists():
        return 0
    env_db = run_dir / "smoke.db"
    cmd_add = [
        "uv",
        "run",
        "--frozen",
        "python",
        "-m",
        "parking",
        "add",
        "--project",
        "audit",
        "--worktree-path",
        str(run_dir),
        "--tag",
        "smoke",
        "--note",
        "audit smoke",
    ]
    cmd_list = ["uv", "run", "--frozen", "python", "-m", "parking", "list"]
    try:
        env = {"PARKING_DB": str(env_db), "PYTHONPATH": str(run_dir / "output")}
        add_proc = subprocess.run(
            cmd_add, cwd=run_dir, capture_output=True, timeout=60, env={**env, "PATH": ""}, check=False
        )
        if add_proc.returncode != 0:
            return 0
        list_proc = subprocess.run(
            cmd_list, cwd=run_dir, capture_output=True, timeout=60, env={**env, "PATH": ""}, check=False
        )
        return 1 if list_proc.returncode == 0 else 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0


def score_run(run_dir: Path) -> RunScore:
    name = run_dir.name  # e.g. "compounding-r2"
    m = re.match(r"(parallel|compounding)-r(\d+)$", name)
    if not m:
        raise ValueError(f"unexpected run dir name: {name}")
    mode, replay = m.group(1), int(m.group(2))
    score = RunScore(run_id=name, mode=mode, replay=replay)

    metrics = _read_metrics_json(run_dir)
    score.ac_count = int(metrics.get("ac_count") or 0)
    score.tokens_total = int(metrics.get("tokens_total") or 0)
    score.wall_seconds_total = float(metrics.get("wall_seconds_total") or 0.0)

    src = _gather_source(run_dir)
    schema_missing_count, schema_missing = _score_drift(src, SCHEMA_FIELDS)
    cli_missing_count, cli_missing = _score_drift(src, CLI_SURFACE)
    score.schema_drift = schema_missing_count
    score.cli_drift = cli_missing_count
    if schema_missing:
        score.notes.append(f"schema fields missing: {schema_missing}")
    if cli_missing:
        score.notes.append(f"cli surface missing: {cli_missing}")

    score.invariant_count = _count_invariants(run_dir)

    passed, failed = _run_pytest(run_dir)
    score.tests_passed = passed
    score.tests_failed = failed

    score.smoke_ok = _smoke_test(run_dir)
    return score


def aggregate(scores: list[RunScore], mode: str) -> dict[str, float]:
    subset = [s for s in scores if s.mode == mode]
    if not subset:
        return {}
    return {
        "n": len(subset),
        "ac_count_mean": mean(s.ac_count for s in subset),
        "tokens_total_mean": mean(s.tokens_total for s in subset),
        "wall_seconds_total_mean": mean(s.wall_seconds_total for s in subset),
        "schema_drift_mean": mean(s.schema_drift for s in subset),
        "cli_drift_mean": mean(s.cli_drift for s in subset),
        "invariant_count_mean": mean(s.invariant_count for s in subset),
        "tests_passed_mean": mean(s.tests_passed for s in subset),
        "tests_failed_mean": mean(s.tests_failed for s in subset),
        "smoke_ok_rate": mean(s.smoke_ok for s in subset),
    }


def render_report(scores: list[RunScore], agg_par: dict, agg_comp: dict) -> str:
    lines: list[str] = []
    lines.append("# Parking-Lot Audit — Compounding vs Parallel\n")
    lines.append(
        "Controlled experiment measuring cross-AC coherence in two execution\n"
        "modes of the Ouroboros orchestrator on a 5-AC TUI/CLI project where\n"
        "each AC depends on schema/surface decisions made by earlier ACs.\n"
    )
    lines.append("")
    lines.append("## Summary (means across replays)\n")
    lines.append("| Metric | Parallel | Compounding | Δ (compounding − parallel) |")
    lines.append("|--------|---------:|------------:|---------------------------:|")
    keys_lower_better = {"schema_drift_mean", "cli_drift_mean", "tests_failed_mean"}
    for k in (
        "n",
        "ac_count_mean",
        "tokens_total_mean",
        "wall_seconds_total_mean",
        "schema_drift_mean",
        "cli_drift_mean",
        "invariant_count_mean",
        "tests_passed_mean",
        "tests_failed_mean",
        "smoke_ok_rate",
    ):
        p = agg_par.get(k, float("nan"))
        c = agg_comp.get(k, float("nan"))
        try:
            delta = c - p
        except TypeError:
            delta = float("nan")
        marker = ""
        if k in keys_lower_better:
            marker = " ✅" if delta < 0 else (" ❌" if delta > 0 else "")
        elif k in {"invariant_count_mean", "tests_passed_mean", "smoke_ok_rate"}:
            marker = " ✅" if delta > 0 else (" ❌" if delta < 0 else "")
        lines.append(f"| {k} | {p:.2f} | {c:.2f} | {delta:+.2f}{marker} |")
    lines.append("")
    lines.append("## Per-run scores\n")
    lines.append("| Run | AC count | Tokens | Wall (s) | Schema drift | CLI drift | Invariants | Tests pass/fail | Smoke |")
    lines.append("|-----|---------:|-------:|---------:|-------------:|----------:|-----------:|----------------:|------:|")
    for s in sorted(scores, key=lambda r: (r.mode, r.replay)):
        lines.append(
            f"| {s.run_id} | {s.ac_count} | {s.tokens_total} | {s.wall_seconds_total:.0f} | "
            f"{s.schema_drift} | {s.cli_drift} | {s.invariant_count} | "
            f"{s.tests_passed}/{s.tests_failed} | {'✅' if s.smoke_ok else '❌'} |"
        )
    lines.append("")
    lines.append("## Notes\n")
    for s in sorted(scores, key=lambda r: (r.mode, r.replay)):
        if s.notes:
            lines.append(f"- **{s.run_id}**: " + "; ".join(s.notes))
    lines.append("")
    lines.append("## Methodology\n")
    lines.append(
        "- Same seed (`examples/parking-lot-audit/seed.yaml`) fed to both modes.\n"
        "- N=3 replays per mode (or as available).\n"
        "- Static drift checks count missing schema field names / CLI flags in\n"
        "  the generated source. Compounding propagates AC-1's schema to ACs 2-5;\n"
        "  parallel does not.\n"
        "- Pytest run via `uv run --frozen pytest output/tests/ -q` per run.\n"
        "- Smoke test runs `parking add` + `parking list` against an isolated\n"
        "  $PARKING_DB and asserts both exit 0.\n"
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    if not RUNS_ROOT.exists():
        print(f"no runs found at {RUNS_ROOT}", file=sys.stderr)
        return 1
    run_dirs = sorted(d for d in RUNS_ROOT.iterdir() if d.is_dir())
    if not run_dirs:
        print(f"no runs found at {RUNS_ROOT}", file=sys.stderr)
        return 1
    scores: list[RunScore] = []
    for d in run_dirs:
        try:
            scores.append(score_run(d))
        except ValueError as e:
            print(f"skipping {d}: {e}", file=sys.stderr)
    agg_par = aggregate(scores, "parallel")
    agg_comp = aggregate(scores, "compounding")
    report = render_report(scores, agg_par, agg_comp)
    REPORT_PATH.write_text(report, encoding="utf-8")
    SCORES_PATH.write_text(
        json.dumps(
            {
                "runs": [asdict(s) for s in scores],
                "aggregates": {"parallel": agg_par, "compounding": agg_comp},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"wrote {REPORT_PATH}")
    print(f"wrote {SCORES_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
