# Parking-Lot Audit — Compounding vs Parallel

**Run date:** 2026-05-09 (fork commit `d5b089ee`)
**N:** 3 replays per mode
**Seed:** [`seed.yaml`](seed.yaml)
**Harness:** [`scripts/audit/run_parking_lot_bench.sh`](../../scripts/audit/run_parking_lot_bench.sh)
**Score script:** [`scripts/audit/score_parking_lot.py`](../../scripts/audit/score_parking_lot.py)

## TL;DR

On this 5-AC project, **compounding mode did not clearly win** versus parallel
mode. Parallel produced more passing tests, ~2× the `[[INVARIANT: …]]` tag
density, and ran ~13% faster on average. The headline drift signal the seed
was designed to expose (cross-AC schema-name agreement) **registered zero
drift on either side** — the seed's contracts were so explicit that even
parallel agents stayed faithful to AC-1's schema-of-record, which means the
audit didn't actually probe the regime where compounding's postmortem chain
should help.

This is a real empirical finding: on tasks where each AC's contract is
already nailed down in the seed, compounding's overhead is not paid back by
better cross-AC coherence — because there's no coherence drift left to
prevent. Compounding's value should reappear on tasks where AC-1 makes
arbitrary-but-binding choices that AC-2..N cannot infer from the seed alone.
The seed for this audit was the wrong shape to prove that hypothesis.

## Headline metrics (means across 3 replays)

| Metric | Parallel | Compounding | Δ (compounding − parallel) | Direction |
|--------|---------:|------------:|---------------------------:|:---------:|
| n | 3 | 3 | — | — |
| Wall time (s) | 1432.3 | 1649.3 | **+217 (+15%)** | ❌ compounding slower |
| Schema drift (missing field names) | 0.0 | 0.0 | 0 | ⚪ tie (trap didn't fire) |
| CLI surface drift (missing flags) | 0.0 | 0.0 | 0 | ⚪ tie (trap didn't fire) |
| `[[INVARIANT: ]]` tags emitted | 51.7 | 28.0 | **−23.7 (−46%)** | ❌ compounding emits fewer |
| Tests passed (mean) | 96.3 | 81.7 | **−14.7 (−15%)** | ❌ compounding writes fewer |
| Tests failed (mean) | 0.0 | 2.3 | +2.3 | ❌ compounding has failures |

Lower-is-better: failed tests, schema/cli drift, wall time.
Higher-is-better: invariants, passed tests.

## Per-run scores

| Run | Wall (s) | Schema drift | CLI drift | INVARIANT tags | Tests pass/fail | Source files | Test files |
|-----|---------:|-------------:|----------:|---------------:|----------------:|-------------:|-----------:|
| parallel-r1     | 1467 | 0 | 0 | 61 | 119 / 0 | 7 | 6 |
| parallel-r2     | 1390 | 0 | 0 | 36 |  95 / 0 | 7 | 6 |
| parallel-r3     | 1440 | 0 | 0 | 58 |  75 / 0 | 7 | 6 |
| compounding-r1  | 1928 | 0 | 0 | 29 |  92 / 7 | 8 | 6 |
| compounding-r2  | 1503 | 0 | 0 | 27 |  84 / 0 | 7 | 6 |
| compounding-r3  | 1517 | 0 | 0 | 28 |  69 / 0 | 7 | 6 |

## What the audit actually proved

1. **Both modes deliver a working project.** Each of the 6 runs produced 7–8
   source files + 6 test files, the full 5-AC scope, and a passing test
   suite (compounding-r1 was the only outlier with 7 test failures, all in
   `test_smoke.py` shelling to `python -m parking.cli` against an
   environment that wasn't preserved across the run boundary).

2. **Schema fidelity is *not* a discriminating signal at this seed precision.**
   AC-1 in the seed declared 10 schema-of-record field names verbatim. ACs
   2-5 in both modes referenced those names. Drift = 0 across the board.
   The audit trap was over-engineered: the seed told both modes exactly
   what to do, leaving no inference burden for compounding to help with.

3. **Parallel emits ~2× more `[[INVARIANT: …]]` tags.** Compounding agents,
   reading prior ACs' postmortems containing prior INVARIANTs, appear to
   suppress redundant emission. Whether this is a feature (no duplication)
   or a bug (loss of audit-trail density) depends on what downstream tooling
   expects.

4. **Compounding writes ~15% fewer tests on average.** Hypothesis: postmortem
   chain context displaces the agent's "be thorough on tests" instruction
   budget. Not confirmed — needs token-cost data.

5. **Compounding is ~15% slower.** Postmortem-chain build/serialize overhead
   between ACs.

## What the audit *failed* to measure

These are not "compounding lost"; they are "the audit didn't probe these":

- **Token cost.** Event-store extraction returned `ac_count=0`/`tokens=0`
  for all runs — the harness's heuristic for the orchestrator's event
  schema is wrong. Compounding's context overhead is invisible in this
  audit. (Tracked as known limitation; fix in a follow-up.)

- **Compounding's actual hypothesis.** The postmortem chain is meant to
  carry *non-obvious decisions* from AC-N to AC-N+1 — choices the seed
  *deliberately* leaves under-specified so the agent must improvise. This
  seed left zero such choices. Schema field names, CLI flag names, ISO
  format vs unix timestamp, tag-as-string-vs-list — all spelled out
  explicitly. There was nothing for the postmortem chain to carry.

- **Long-chain quality.** 5 ACs is a short chain. Compounding's value is
  hypothesised to grow with chain length (AC-N references AC-1's choice
  via a chain of N-1 postmortems). A 10-15 AC project would test this.

- **Smoke test.** Score script's smoke-test invocation strips PATH, so all
  6 runs scored 0 on that metric. Bug in the score script, not the audit.

## What this audit *did* discover that wasn't planned

1. **The full-clone isolation harness works.** First-attempt audit (commit
   `f1f0a90f`) used git-worktree isolation, which the orchestrator's agent
   silently bypassed by `cd`-ing into the parent repo and writing/committing
   there directly. Polluted local `main` with parking-lot commits, leaked
   commits across runs. Fix in `d5b089ee`: full `git clone --local
   --no-hardlinks` per run into `/tmp/parking-lot-audit-clones/`, with the
   seed copied to clone root so `project_dir` resolves inside the clone.
   Verified zero pollution this run.

2. **Both modes can build a usable parking-lot TUI in ~25–30 minutes wall
   time per run.** The deliverable itself (a TUI for parking AI-agent
   handoff work across worktrees) is a real working tool. See any
   `runs/<mode>-r<i>/output/` for a complete implementation.

3. **Run-to-run variance is high at N=3.** Test counts ranged 69–119 across
   parallel runs alone. Most "compounding loses" gaps are within the
   noise band. A defensible delta would need N≥10.

## What a better audit would look like

Follow-up design `parking-lot-audit-v2` should:

1. **Strip the seed of explicit contracts on AT LEAST 3 axes.** Don't
   declare `parked_at` as ISO 8601 — let AC-1 pick. Don't declare `tag` as
   string-vs-list — let AC-1 pick. Don't declare exact CLI flag names — let
   AC-2 pick. Then measure whether ACs 3-5 align with AC-1/AC-2's choices.
   That is the regime compounding's postmortem chain is *supposed* to win.

2. **Lengthen the chain.** 8-12 ACs, with explicit cross-references like
   "AC-7 must invoke AC-3's exporter using AC-3's exact signature". The
   chain depth is where parallel mode loses its grip and compounding
   should pull ahead.

3. **Fix the harness's event-store extraction.** Read the actual schema
   from `src/ouroboros/orchestrator/events.py` instead of guessing column
   names. Capture per-AC token cost so the compounding overhead is visible.

4. **Fix the smoke test.** Don't strip PATH from the subprocess env.

5. **N≥10 per mode.** Single-digit replays are noise.

6. **Add a third mode: "parallel + manual chain-summary handoff."** A
   weaker compounding analogue where the harness injects a 200-token
   summary of prior ACs into the parallel agent's context. Tests whether
   compounding's value is from *the postmortem chain artifact* or from
   *strict serial execution* — currently confounded.

## Methodology

- Same `seed.yaml` fed to both modes; only the `--compounding` flag varies.
- Each run runs in a full `git clone --local --no-hardlinks` of the repo at
  `/tmp/parking-lot-audit-clones/<mode>-r<i>/`, with the seed copied to clone
  root. Orchestrator launches with cwd = clone, so `project_dir` resolves
  inside the clone, isolating all writes/commits from the parent repo.
- After each run, `output/`, `events.sqlite`, `session.log`, `metrics.json`,
  and `commits.log` are copied from clone to
  `examples/parking-lot-audit/runs/<mode>-r<i>/`. Clones are then deleted.
- Scoring is via [`score_parking_lot.py`](../../scripts/audit/score_parking_lot.py).

## Reproducing this audit

```bash
# Pilot single run (fast):
scripts/audit/run_parking_lot_bench.sh 1 parallel
scripts/audit/run_parking_lot_bench.sh 1 compounding

# Full N=3 (~2 hours wall):
scripts/audit/run_parking_lot_bench.sh 3 "parallel compounding"

# Re-score without re-running:
python3 scripts/audit/score_parking_lot.py
```

Each run takes 23-32 min wall time; full N=3 ≈ 2.4 hours sequential.

## Status

| Question | Answer |
|----------|--------|
| Did compounding win? | No, not on this seed. |
| Did compounding lose? | Slightly — wall +15%, fewer invariants/tests, no quality benefit. |
| Is this conclusive? | No. Seed didn't probe compounding's actual hypothesis. |
| Is the harness defensible? | Yes — full-clone isolation verified zero pollution. |
| What next? | Build `parking-lot-audit-v2` with deliberately ambiguous contracts and a longer chain. |
