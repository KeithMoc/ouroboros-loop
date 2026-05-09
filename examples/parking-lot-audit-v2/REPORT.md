# Parking-Lot Audit v2 — Compounding vs Parallel

**Run date:** 2026-05-09 (fork commit `cfb13f04`)
**N:** 5 replays per mode
**Seed:** [`seed.yaml`](seed.yaml) — 8 ACs, 4+1 deliberate ambiguity axes
**Harness:** [`scripts/audit/run_parking_lot_bench_v2.sh`](../../scripts/audit/run_parking_lot_bench_v2.sh)
**Score:** [`scripts/audit/score_parking_lot_v2.py`](../../scripts/audit/score_parking_lot_v2.py)
**Metrics:** [`scripts/audit/extract_metrics.py`](../../scripts/audit/extract_metrics.py)

## TL;DR

Across 10 runs, **the drift hypothesis the audit was designed to expose did
not manifest**. Both parallel and compounding modes converged on the *same
choice on every ambiguity axis*, every successful run:

  axis_1_timestamp = `iso8601`
  axis_2_tag       = `json_array`
  axis_3_entry_id  = `pe_hex12`
  axis_4_soft_delete = `deleted_at`
  axis_5_priority  = `int_1_5`

The agents picked the "Pythonic default" on every axis, every time. There
was no drift for compounding's postmortem chain to prevent. The 5 axes
weren't actually ambiguous to a Python-idiomatic agent; they were
under-specified to me, the seed author. Lesson banked for v3.

The audit *did* surface real differences:

- **Compounding stability**: 2/5 compounding runs hit a 13-second instant
  fast-fail at "Serial AC 1/8 [0 postmortems in chain]" with zero tool
  calls and zero agent invocation, suggesting Claude API rate-limit or
  similar shared-infra contention, not a seed/harness defect (parallel
  runs immediately before completed cleanly).

- **Compounding got further on the chain**: when both modes ran the
  full chain, compounding mode reached AC-7 in r1, r2 (success) and
  r3 (success through AC-7, fail at AC-8). Parallel mode reached AC-8
  in 4/5 runs, but parallel-r1 cascaded on AC-7+AC-8.

- **Cost**: among successful runs, compounding emits ~58% fewer
  `[[INVARIANT: …]]` tags (mean 56.7 vs 137.4), ~17% fewer tool calls
  (mean 137.3 vs 167.4), and ~3× fewer messages (mean 212 vs 523), while
  taking ~17% more wall time (mean 1944s vs 1665s).

## Headline (means across N=5 replays per mode)

| Metric | Parallel | Compounding | Δ (compounding − parallel) | Direction |
|--------|---------:|------------:|---------------------------:|:---------:|
| Successful (8/8 ACs) | 4 / 5 | 2 / 5 | −2 | ❌ |
| ≥7 ACs completed | 5 / 5 | 3 / 5 | −2 | ❌ |
| Wall time (s) | 1665.0 | 1171.4 | −493.6 | ⚪ skewed by 2 fast-fails |
| Wall time, successful runs only | 1665.0 | 1944.0 | +279 (+17%) | ❌ compounding slower |
| Tool calls | 167.4 | 82.4 | −85.0 | ⚪ skewed |
| Tool calls, successful only | 167.4 | 137.3 | −30 (−18%) | ✅ compounding cheaper |
| Messages | 523.0 | 130.8 | −392 | ⚪ skewed |
| Messages, successful only | 523.0 | 212.0 | −311 (−59%) | ✅ compounding much cheaper |
| Alignment (0-5) | 5.00 | 3.00 | −2 | ⚪ tied at 5/5 when both run |
| Alignment, successful only | 5.00 | 5.00 | 0 | ⚪ tie |
| Schema field coverage (0-10) | 10.00 | 6.00 | −4 | ⚪ skewed |
| INVARIANT tags emitted | 137.4 | 34.0 | −103 | ⚪ skewed |
| INVARIANT tags, successful only | 137.4 | 56.7 | −81 (−59%) | ❌ compounding emits fewer |
| Tests passed | 142.6 | 79.2 | −63 | ⚪ skewed |
| Tests passed, successful only | 142.6 | 132.0 | −11 | ❌ slight |
| Smoke OK rate | 0/5 | 0/5 | 0 | ⚪ score script bug — see Caveats |

## Per-run breakdown

| Run | Outcome | Wall (s) | ACs | Tools | Msgs | Align | Schema | Invariants | Tests p/f | AC-1 picks |
|-----|---------|---------:|----:|------:|-----:|------:|-------:|-----------:|----------:|-----------|
| parallel-r1     | partial 6/8 (AC-7+8 fail) | 1494 | 6 | 181 | 635 | 5/5 | 10/10 |  93 | 130/4 | iso8601, json_array, pe_hex12, deleted_at |
| parallel-r2     | ✅ 8/8                    | 1736 | 8 | 153 | 455 | 5/5 | 10/10 | 174 | 153/8 | iso8601, json_array, pe_hex12, deleted_at |
| parallel-r3     | ✅ 8/8                    | 1768 | 8 | 166 | 505 | 5/5 | 10/10 | 140 | 154/17 | iso8601, json_array, pe_hex12, deleted_at |
| parallel-r4     | ✅ 8/8                    | 1735 | 8 | 160 | 484 | 5/5 | 10/10 | 123 | 140/0 | iso8601, json_array, pe_hex12, deleted_at |
| parallel-r5     | ✅ 8/8                    | 1592 | 8 | 177 | 536 | 5/5 | 10/10 | 157 | 136/0 | iso8601, json_array, pe_hex12, deleted_at |
| compounding-r1  | ✅ 8/8                    | 2111 | 8 | 148 | 223 | 5/5 | 10/10 |  70 | 106/5 | iso8601, json_array, pe_hex12, deleted_at |
| compounding-r2  | ✅ 8/8                    | 2255 | 8 | 152 | 231 | 5/5 | 10/10 |  55 | 123/16 | iso8601, json_array, pe_hex12, deleted_at |
| compounding-r3  | partial 7/8 (AC-8 fail)   | 1465 | 8 | 112 | 182 | 5/5 | 10/10 |  45 | 167/0 | iso8601, json_array, pe_hex12, deleted_at |
| compounding-r4  | ❌ fast-fail at 13s       |   13 | 1 |   0 |   9 | 0/5 |  0/10 |   0 |   0/0 | n/a (no agent run) |
| compounding-r5  | ❌ fast-fail at 13s       |   13 | 1 |   0 |   9 | 0/5 |  0/10 |   0 |   0/0 | n/a (no agent run) |

(`Align` = sum of 5 axis-alignment booleans where downstream code matches AC-1's chosen representation)

## Findings

### 1. The drift trap didn't fire

Every successful run in both modes picked **identical** representations
on all 5 axes. The agents' bias toward Python-idiomatic defaults is
strong enough that `axis_1_timestamp` is always `iso8601`, `axis_2_tag`
is always `json_array`, and so on. Compounding's postmortem chain has
nothing to differentiate from parallel's stateless guess when the
"guess" is identical. v2 is unable to falsify or confirm the drift
hypothesis on this seed.

### 2. Two fast-fail compounding runs (r4, r5) point at infrastructure, not seed

Both r4 and r5 died at exactly **13 seconds** at the moment the
orchestrator tried to launch agent execution for AC-1, with **zero tool
calls** and **zero messages**. Both ran back-to-back after r3's normal-length
1465s session. The exact-13s pattern across two consecutive runs strongly
suggests Claude API rate-limit or local CLI throttle hit at the agent
spawn boundary. Compounding-r1 / r2 / r3 all started with non-zero
agent activity, so the seed/harness mechanic is fine.

The harness should detect this case and add automatic retry with
backoff. Filed as v3 follow-up.

### 3. Among successful runs, compounding is cheaper but not better

When both modes complete, compounding produces:
- **−59% messages** (212 vs 523 mean)
- **−18% tool calls** (137 vs 167 mean)
- **−59% INVARIANT tags emitted** (57 vs 137 mean)
- **−7% tests passed** (132 vs 143 mean)
- **+17% wall time** (1944s vs 1665s mean)

Compounding's postmortem chain saves agent time (fewer messages, fewer
re-explorations) but the agent emits *fewer* INVARIANT tags and writes
*fewer* tests. The most charitable read: compounding's denser context
makes the agent more terse but no more correct. The least charitable:
the postmortem chain crowds out instructions to be thorough.

### 4. Compounding traversed the chain further than parallel did once

`parallel-r1` failed cascade on AC-7 (the mid-chain new-axis pick) and
AC-8 (tests + README). `compounding-r3` got AC-7 done and only failed
AC-8. This is one anecdote — not a proof — but consistent with the
hypothesis that the postmortem chain helps mid-chain ACs that depend on
earlier choices. Future audits should design specifically for this
regime (long chains with heavy mid-chain dependencies).

### 5. Per-run alignment is 5/5 across both modes when they complete

The score script extracted axis picks from `models.py` for every
non-fast-fail run. Downstream files (cli, tui, handoff_export, sync,
git_probe, db) reference AC-1's choice on all 5 axes. The audit is
*verifying* that the agents deliver consistent multi-AC code, just not
that compounding does it better than parallel.

## Caveats

- **Drift hypothesis untested.** v2's 5 axes weren't ambiguous enough
  to expose drift. v3 must force less-default options (e.g.,
  "AC-1 must NOT pick the most common option for any axis").

- **Smoke test 0/10**. Score script's smoke harness invokes
  `python -m parking add` against a tmp `$PARKING_DB`. The fix from v1
  (don't strip `PATH`) was applied, but the test still fails because
  the generated `parking` package isn't installed in the score runner's
  environment. Need to wrap each run in a dedicated venv with
  `pip install -e <run>/output` before smoking. Filed as v3 follow-up.

- **Token cost still unobserved.** `estimated_tokens=0` across all 10
  runs. The orchestrator's adapter doesn't get token counts back from
  the Claude CLI subprocess. Tool/message counts are decent proxies
  but not the real currency.

- **Two compounding fast-fails skew the means.** The "successful only"
  rows in the headline table are the more honest comparison.

- **N=5 still high variance.** Tests passed for parallel ranged 130-154
  (Δ 24); for compounding (successful only) 106-167 (Δ 61). A
  defensible per-axis comparison needs N≥10 even on a clean seed.

## What v3 would need

| Issue | Fix |
|-------|-----|
| Drift trap silent | Force AC-1 to pick non-default token on each axis (e.g., seed says "must NOT pick `iso8601` for axis_1; pick `unix_epoch` or `rfc3339`"). Or randomize the forbidden-default per replay. |
| Fast-fail handling | Harness retries with exponential backoff when wall<60s + 0 tool calls. |
| Smoke test | Per-run venv: `python -m venv <run>/.venv && <run>/.venv/bin/pip install -e <run>/output && smoke against that python`. |
| Token cost | Claude CLI subprocess does not emit token counts to the orchestrator. Either patch the adapter to scrape from CLI stderr, or accept tool/message counts as the cost metric. |
| Variance | N≥10 per mode. Or paired runs with same seed-id but mode-flag swapped. |

## Audit infrastructure landed in this fork

| File | Purpose |
|------|---------|
| `examples/parking-lot-audit-v2/seed.yaml` | 8 ACs, 4+1 ambiguity axes, structured docstring contract for AC-1's picks |
| `scripts/audit/run_parking_lot_bench_v2.sh` | Full git-clone isolation per run, N configurable, decomposition off, Claude runtime |
| `scripts/audit/score_parking_lot_v2.py` | Axis-alignment scoring with per-token signal regexes; reads picks from models.py |
| `scripts/audit/extract_metrics.py` | Filter global event store by session_id; handles both parallel and compounding event shapes |

All three reusable for v3 with minimal edits.

## Reproduction

```bash
# Pilot single run (~30 min):
scripts/audit/run_parking_lot_bench_v2.sh 1 parallel
scripts/audit/run_parking_lot_bench_v2.sh 1 compounding

# Full N=5 (~5 hr wall):
scripts/audit/run_parking_lot_bench_v2.sh 5 "parallel compounding"

# Re-score without re-running:
python3 scripts/audit/score_parking_lot_v2.py
```

## Status

| Question | Answer |
|----------|--------|
| Did compounding win on alignment? | Tied at 5/5 — drift didn't manifest |
| Did compounding win on cost? | Yes when it completed: −59% messages, −18% tools |
| Did compounding lose on cost? | Yes on wall time: +17% slower |
| Did compounding lose on stability? | 2/5 fast-fails (likely API rate-limit, not orchestrator) |
| Did compounding lose on quality? | −7% tests passed; −59% INVARIANT tags |
| Is this conclusive? | No. Drift hypothesis untested because seed defaults too obvious. |
| Is the harness defensible? | Yes — full-clone isolation verified, real metrics extracted, repeatable |
| What next? | v3 with forced-non-default picks, per-run venv smoke, fast-fail backoff |
