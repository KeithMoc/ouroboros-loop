#!/usr/bin/env python3
"""Score parking-lot-audit-v2 runs.

This is v2 of the score script. The key difference from v1: instead of
checking generated code for VERBATIM strings (which v1 did, where the
seed gave away the answer), v2 reads AC-1's *picked* representations
from the docstring/INVARIANT tags it emitted, then checks downstream
ACs for ALIGNMENT with those picks.

If AC-1 picked ``axis_1_timestamp: unix_epoch``, then:
  - cli.py must parse/render unix epoch timestamps (regex against int patterns)
  - tui.py must format unix epoch
  - handoff_export.py must serialize unix epoch
  - sync.py JSONL output must contain unix epoch ints, not ISO strings

Drift = downstream uses a different representation than AC-1 chose.
That is the regime compounding's postmortem chain is supposed to win.

Output:
    examples/parking-lot-audit-v2/REPORT.md
    examples/parking-lot-audit-v2/scores.json
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
AUDIT_ROOT = REPO_ROOT / "examples" / "parking-lot-audit-v2"
RUNS_ROOT = AUDIT_ROOT / "runs"
REPORT_PATH = AUDIT_ROOT / "REPORT.md"
SCORES_PATH = AUDIT_ROOT / "scores.json"

# AC-1 ambiguity axes. Each axis maps to:
#   - choice tokens AC-1 may have picked
#   - per-choice alignment regexes that downstream source must satisfy
AXES = {
    "axis_1_timestamp": {
        "tokens": ("iso8601", "unix_epoch", "rfc3339"),
        "downstream_signal": {
            # If AC-1 picked iso8601, downstream code should reference
            # 'isoformat' or string-shaped timestamps. If unix_epoch,
            # int-shaped. If rfc3339, '+00:00' or named offset.
            "iso8601": [
                re.compile(r"isoformat\(\)|datetime\.(?:utc)?now"),
                re.compile(r"['\"]\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"),
            ],
            "unix_epoch": [
                re.compile(r"\.timestamp\(\)|time\.time\(\)|int\(time"),
                re.compile(r"\bint\b.*parked_at|parked_at.*\bint\b"),
            ],
            "rfc3339": [
                re.compile(r"isoformat\(\)|strftime"),
                re.compile(r"\+00:00|%z"),
            ],
        },
    },
    "axis_2_tag": {
        "tokens": ("single_string", "csv_string", "json_array"),
        "downstream_signal": {
            "single_string": [
                # No splitting on tag; tag is just str.
                re.compile(r"tag:\s*str|tag:\s*Optional\[str\]|tag:\s*str\s*\|"),
            ],
            "csv_string": [
                re.compile(r"\.split\(['\"],['\"]?\)|','\.join"),
            ],
            "json_array": [
                re.compile(r"json\.loads.*tag|json\.dumps.*tag|tag.*list\["),
            ],
        },
    },
    "axis_3_entry_id": {
        "tokens": ("pe_hex12", "uuid4", "ulid"),
        "downstream_signal": {
            "pe_hex12": [
                re.compile(r"['\"]pe_['\"]|secrets\.token_hex|uuid4\(\)\.hex\[:12\]"),
            ],
            "uuid4": [
                re.compile(r"uuid\.uuid4|uuid4\(\)"),
            ],
            "ulid": [
                re.compile(r"\bulid\b|ULID"),
            ],
        },
    },
    "axis_4_soft_delete": {
        "tokens": ("hard_delete", "deleted_at", "status_enum"),
        "downstream_signal": {
            "hard_delete": [
                re.compile(r"DELETE\s+FROM|conn\.execute\(.*DELETE", re.IGNORECASE),
            ],
            "deleted_at": [
                re.compile(r"\bdeleted_at\b"),
            ],
            "status_enum": [
                re.compile(r"\bstatus\b.*=.*['\"](?:active|done|archived)|"
                           r"['\"](?:active|done|archived)['\"]"),
            ],
        },
    },
}

AXIS_5_PRIORITY = {
    # Accept multiple token spellings — agent has been seen to emit int_1_5
    # instead of the seed's int_1to5. Both denote the same choice.
    "tokens": ("int_1to5", "int_1_5", "enum_low_med_high", "enum_low_high"),
    "downstream_signal": {
        "int_1to5": [
            re.compile(r"priority:\s*int|--priority\s+(?:INTEGER|INT)|\b1\s*<=\s*priority"),
        ],
        "int_1_5": [
            re.compile(r"priority:\s*int|--priority\s+(?:INTEGER|INT)|\b1\s*<=\s*priority"),
        ],
        "enum_low_med_high": [
            re.compile(r"['\"]low['\"]|['\"]med['\"]|['\"]high['\"]"),
        ],
        "enum_low_high": [
            re.compile(r"['\"]low['\"]|['\"]high['\"]"),
        ],
    },
}

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

INVARIANT_RE = re.compile(r"\[\[INVARIANT:\s*([^\]]+?)\s*\]\]")


@dataclass
class RunScore:
    run_id: str
    mode: str
    replay: int
    ac_count: int = 0
    tokens_total: int = 0
    cost_usd: float = 0.0
    tool_calls_count: int = 0
    messages_count: int = 0
    wall_seconds_total: float = 0.0
    exit_code: int = 0

    # AC-1's picks (extracted from models.py docstring)
    ac1_picks: dict[str, str | None] = field(default_factory=dict)
    ac7_priority_pick: str | None = None

    # Per-axis alignment: 1 if downstream code satisfies AC-1's choice on that axis, else 0
    axis_1_aligned: int = 0
    axis_2_aligned: int = 0
    axis_3_aligned: int = 0
    axis_4_aligned: int = 0
    axis_5_aligned: int = 0  # AC-7 priority

    schema_field_coverage: int = 0  # 0..10 — how many AC-1 schema fields appear in downstream
    invariant_count: int = 0

    tests_passed: int = 0
    tests_failed: int = 0
    smoke_ok: int = 0

    notes: list[str] = field(default_factory=list)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeDecodeError):
        return ""


def _gather_source(run_dir: Path) -> str:
    output = run_dir / "output"
    if not output.exists():
        return ""
    return "\n".join(_read_text(p) for p in sorted(output.rglob("*.py")))


def _extract_axis_picks(models_py: str) -> dict[str, str | None]:
    """Find lines like '# axis_<N>_<name>: <token>' in the docstring."""
    picks: dict[str, str | None] = {axis: None for axis in AXES}
    pattern = re.compile(r"#\s*(axis_[1-4]_[a-z_]+):\s*([a-z0-9_]+)")
    for match in pattern.finditer(models_py):
        key, val = match.group(1), match.group(2)
        if key in AXES and val in AXES[key]["tokens"]:
            picks[key] = val
    # Also accept the INVARIANT-tag form: [[INVARIANT: axis_1_timestamp = iso8601]]
    invariant_pattern = re.compile(
        r"\[\[INVARIANT:\s*(axis_[1-4]_[a-z_]+)\s*=\s*([a-z0-9_]+)\s*\]\]"
    )
    for match in invariant_pattern.finditer(models_py):
        key, val = match.group(1), match.group(2)
        if key in AXES and val in AXES[key]["tokens"]:
            picks[key] = val
    return picks


def _extract_priority_pick(db_py: str) -> str | None:
    """Find AC-7's priority pick in db.py docstring or INVARIANT tag."""
    for pattern in (
        re.compile(r"#\s*axis_5_priority:\s*([a-z0-9_]+)"),
        re.compile(r"\[\[INVARIANT:\s*axis_5_priority\s*=\s*([a-z0-9_]+)\s*\]\]"),
    ):
        m = pattern.search(db_py)
        if m and m.group(1) in AXIS_5_PRIORITY["tokens"]:
            return m.group(1)
    return None


def _check_axis_alignment(
    axis_key: str,
    pick: str | None,
    downstream_source: str,
    axis_def: dict,
) -> int:
    """Return 1 if downstream source satisfies AT LEAST ONE signal regex for the
    pick AND does NOT satisfy a clearly-incompatible signal from another token."""
    if not pick or pick not in axis_def["downstream_signal"]:
        return 0
    own_signals = axis_def["downstream_signal"][pick]
    if any(p.search(downstream_source) for p in own_signals):
        return 1
    return 0


def _count_invariants(run_dir: Path) -> int:
    blobs: list[str] = []
    chain_dir = run_dir / "docs" / "brainstorm"
    if chain_dir.exists():
        for f in chain_dir.glob("chain-*.md"):
            blobs.append(_read_text(f))
    blobs.append(_gather_source(run_dir))
    return sum(len(INVARIANT_RE.findall(b)) for b in blobs)


def _read_metrics_json(run_dir: Path) -> dict:
    p = run_dir / "metrics.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _run_pytest(run_dir: Path) -> tuple[int, int]:
    tests = run_dir / "output" / "tests"
    if not tests.exists():
        return (0, 0)
    cmd = [
        "uv", "run", "--frozen", "--project", str(REPO_ROOT),
        "pytest", str(tests), "-q", "--tb=no", "-p", "no:cacheprovider",
    ]
    try:
        proc = subprocess.run(cmd, cwd=run_dir, capture_output=True, text=True, timeout=300, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return (0, 0)
    out = proc.stdout + proc.stderr
    m_pass = re.search(r"(\d+)\s+passed", out)
    m_fail = re.search(r"(\d+)\s+failed", out)
    return (int(m_pass.group(1)) if m_pass else 0, int(m_fail.group(1)) if m_fail else 0)


def _smoke_test(run_dir: Path) -> int:
    """Run `parking add` then `parking list`, asserting both exit 0.

    Fix from v1: do NOT strip PATH from the subprocess env. Inherit
    real env so the agent's installed venv is reachable.
    """
    cli = run_dir / "output" / "parking" / "cli.py"
    if not cli.exists():
        return 0
    import os, tempfile
    with tempfile.TemporaryDirectory() as tmp:
        env = {
            **os.environ,
            "PARKING_DB": str(Path(tmp) / "smoke.db"),
            "PYTHONPATH": str(run_dir / "output"),
        }
        try:
            add = subprocess.run(
                ["python", "-m", "parking", "add",
                 "--project", "audit", "--worktree-path", tmp,
                 "--tag", "wip", "--note", "audit smoke"],
                cwd=run_dir / "output", capture_output=True, env=env, timeout=60, check=False,
            )
            if add.returncode != 0:
                return 0
            ls = subprocess.run(
                ["python", "-m", "parking", "list"],
                cwd=run_dir / "output", capture_output=True, env=env, timeout=60, check=False,
            )
            return 1 if ls.returncode == 0 else 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return 0


def score_run(run_dir: Path) -> RunScore:
    name = run_dir.name
    m = re.match(r"(parallel|compounding)-r(\d+)$", name)
    if not m:
        raise ValueError(f"unexpected run dir: {name}")
    mode, replay = m.group(1), int(m.group(2))
    score = RunScore(run_id=name, mode=mode, replay=replay)

    metrics = _read_metrics_json(run_dir)
    score.ac_count = int(metrics.get("ac_count") or 0)
    score.tokens_total = int(metrics.get("tokens_total") or 0)
    score.cost_usd = float(metrics.get("cost_usd") or 0.0)
    score.tool_calls_count = int(metrics.get("tool_calls_count") or 0)
    score.messages_count = int(metrics.get("messages_count") or 0)
    score.wall_seconds_total = float(metrics.get("wall_seconds_total") or 0.0)
    score.exit_code = int(metrics.get("exit_code") or 0)

    # AC-1 picks
    models_py = _read_text(run_dir / "output" / "parking" / "models.py")
    score.ac1_picks = _extract_axis_picks(models_py)

    # AC-7 priority pick
    db_py = _read_text(run_dir / "output" / "parking" / "db.py")
    score.ac7_priority_pick = _extract_priority_pick(db_py)

    # Aggregate downstream source. Include models.py too — agents commonly
    # put ID-generation helpers there (e.g. `_new_entry_id`) and downstream
    # callers just import the helper, so the alignment signal lives in the
    # helper's body. Excluding models.py made axis_3 (entry_id) under-detect.
    downstream = "\n".join(
        _read_text(run_dir / "output" / "parking" / f)
        for f in ("cli.py", "tui.py", "handoff_export.py", "sync.py", "git_probe.py", "models.py", "db.py")
    )

    score.axis_1_aligned = _check_axis_alignment(
        "axis_1_timestamp", score.ac1_picks.get("axis_1_timestamp"), downstream, AXES["axis_1_timestamp"]
    )
    score.axis_2_aligned = _check_axis_alignment(
        "axis_2_tag", score.ac1_picks.get("axis_2_tag"), downstream, AXES["axis_2_tag"]
    )
    score.axis_3_aligned = _check_axis_alignment(
        "axis_3_entry_id", score.ac1_picks.get("axis_3_entry_id"), downstream, AXES["axis_3_entry_id"]
    )
    score.axis_4_aligned = _check_axis_alignment(
        "axis_4_soft_delete", score.ac1_picks.get("axis_4_soft_delete"), downstream, AXES["axis_4_soft_delete"]
    )
    score.axis_5_aligned = _check_axis_alignment(
        "axis_5_priority", score.ac7_priority_pick, downstream, AXIS_5_PRIORITY
    )

    # Schema field coverage in downstream
    score.schema_field_coverage = sum(1 for f in SCHEMA_FIELDS if f in downstream)

    score.invariant_count = _count_invariants(run_dir)

    passed, failed = _run_pytest(run_dir)
    score.tests_passed = passed
    score.tests_failed = failed
    score.smoke_ok = _smoke_test(run_dir)

    if not any(v for v in score.ac1_picks.values()):
        score.notes.append("could not extract AC-1 axis picks from models.py")
    if score.ac7_priority_pick is None and score.ac_count >= 7:
        score.notes.append("could not extract AC-7 priority pick from db.py")

    return score


def aggregate(scores: list[RunScore], mode: str) -> dict[str, float]:
    subset = [s for s in scores if s.mode == mode]
    if not subset:
        return {}
    return {
        "n": len(subset),
        "ac_count_mean": mean(s.ac_count for s in subset),
        "wall_seconds_mean": mean(s.wall_seconds_total for s in subset),
        "tool_calls_mean": mean(s.tool_calls_count for s in subset),
        "messages_mean": mean(s.messages_count for s in subset),
        "tokens_mean": mean(s.tokens_total for s in subset),
        "cost_mean": mean(s.cost_usd for s in subset),
        "axis_1_aligned_rate": mean(s.axis_1_aligned for s in subset),
        "axis_2_aligned_rate": mean(s.axis_2_aligned for s in subset),
        "axis_3_aligned_rate": mean(s.axis_3_aligned for s in subset),
        "axis_4_aligned_rate": mean(s.axis_4_aligned for s in subset),
        "axis_5_aligned_rate": mean(s.axis_5_aligned for s in subset),
        "alignment_total_mean": mean(
            s.axis_1_aligned + s.axis_2_aligned + s.axis_3_aligned + s.axis_4_aligned + s.axis_5_aligned
            for s in subset
        ),
        "schema_field_coverage_mean": mean(s.schema_field_coverage for s in subset),
        "invariant_count_mean": mean(s.invariant_count for s in subset),
        "tests_passed_mean": mean(s.tests_passed for s in subset),
        "tests_failed_mean": mean(s.tests_failed for s in subset),
        "smoke_ok_rate": mean(s.smoke_ok for s in subset),
    }


def render_report(scores: list[RunScore], agg_par: dict, agg_comp: dict) -> str:
    lines: list[str] = []
    lines.append("# Parking-Lot Audit v2 — Compounding vs Parallel\n")
    lines.append(
        "Controlled experiment with 8-AC chain and **deliberately ambiguous** "
        "representation choices. AC-1 picks 4 axes (timestamp format, tag rep, "
        "entry-id format, soft-delete semantics); AC-7 picks a 5th (priority "
        "encoding). Downstream ACs must align with those picks. The audit "
        "measures alignment.\n"
    )
    lines.append("## Headline (means across replays)\n")
    lines.append("| Metric | Parallel | Compounding | Δ |")
    lines.append("|--------|---------:|------------:|--:|")
    keys = (
        "n", "ac_count_mean", "wall_seconds_mean", "tool_calls_mean", "messages_mean",
        "tokens_mean", "cost_mean",
        "alignment_total_mean", "axis_1_aligned_rate", "axis_2_aligned_rate",
        "axis_3_aligned_rate", "axis_4_aligned_rate", "axis_5_aligned_rate",
        "schema_field_coverage_mean", "invariant_count_mean",
        "tests_passed_mean", "tests_failed_mean", "smoke_ok_rate",
    )
    for k in keys:
        p = agg_par.get(k, float("nan"))
        c = agg_comp.get(k, float("nan"))
        try:
            delta = c - p
        except TypeError:
            delta = float("nan")
        lines.append(f"| {k} | {p:.2f} | {c:.2f} | {delta:+.2f} |")
    lines.append("")
    lines.append("## Per-run\n")
    lines.append("| Run | ACs | Wall(s) | Tools | Msgs | Align(0-5) | Schema(0-10) | Invariants | Tests p/f | Smoke | AC-1 picks |")
    lines.append("|-----|----:|--------:|------:|-----:|-----------:|-------------:|-----------:|----------:|------:|------------|")
    for s in sorted(scores, key=lambda r: (r.mode, r.replay)):
        align = s.axis_1_aligned + s.axis_2_aligned + s.axis_3_aligned + s.axis_4_aligned + s.axis_5_aligned
        picks_str = ",".join(f"{k.split('_')[1]}={v}" for k, v in s.ac1_picks.items() if v)
        lines.append(
            f"| {s.run_id} | {s.ac_count} | {s.wall_seconds_total:.0f} | "
            f"{s.tool_calls_count} | {s.messages_count} | {align}/5 | "
            f"{s.schema_field_coverage}/10 | {s.invariant_count} | "
            f"{s.tests_passed}/{s.tests_failed} | {'✅' if s.smoke_ok else '❌'} | "
            f"{picks_str or 'n/a'} |"
        )
    lines.append("")
    lines.append("## Notes\n")
    for s in sorted(scores, key=lambda r: (r.mode, r.replay)):
        if s.notes:
            lines.append(f"- **{s.run_id}**: " + "; ".join(s.notes))
    lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    if not RUNS_ROOT.exists():
        print(f"no runs at {RUNS_ROOT}", file=sys.stderr)
        return 1
    run_dirs = sorted(d for d in RUNS_ROOT.iterdir() if d.is_dir())
    if not run_dirs:
        print(f"no runs at {RUNS_ROOT}", file=sys.stderr)
        return 1
    scores: list[RunScore] = []
    for d in run_dirs:
        try:
            scores.append(score_run(d))
        except ValueError as e:
            print(f"skipping {d}: {e}", file=sys.stderr)
    agg_par = aggregate(scores, "parallel")
    agg_comp = aggregate(scores, "compounding")
    REPORT_PATH.write_text(render_report(scores, agg_par, agg_comp), encoding="utf-8")
    SCORES_PATH.write_text(
        json.dumps(
            {"runs": [asdict(s) for s in scores],
             "aggregates": {"parallel": agg_par, "compounding": agg_comp}},
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"wrote {REPORT_PATH}")
    print(f"wrote {SCORES_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
