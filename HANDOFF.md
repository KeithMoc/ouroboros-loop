# Handoff Document

> Last Updated: 2026-04-27
> Session: Phase-2 Q2 shipped (PR #4) + dogfood-gap discovered and hotfixed (PR #5)

---

## Goal

Ship serial-compounding Phase 2 incrementally as dogfood runs. Each work item from the brainstorm doc (`docs/brainstorm/serial-compounding-open-questions.md`) becomes one `ooo run --compounding` cycle: design → seed → run → evaluate → ship.

Phase 1.5 already shipped (PR #3, commit `ee53eb8`). Q2 shipped in PR #4, then a real-execution dogfood validation surfaced two bugs that the unit suite missed; PR #5 fixed them.

---

## Current Progress

### ✅ Phase-2 Q2 — Per-AC diff capture (PR #4 merged)

**Squash-merged as `ccfc479` on `KeithMoc/ouroboros-loop:main` at 2026-04-26 08:45:35 UTC.**

What shipped:
- New module `src/ouroboros/orchestrator/diff_capture.py` (~440 lines): `capture_pre_ac_snapshot` + `compute_diff_summary` helpers using `git stash create` boundaries around each AC. Truncated `git diff --stat` populated into `ACPostmortem.diff_summary`.
- `SerialCompoundingExecutor` wired to call the helpers per-AC; parallel mode untouched.
- 25 unit tests in new `tests/unit/orchestrator/test_diff_capture.py` + 5 integration tests in `tests/unit/orchestrator/test_serial_executor.py`.
- Test-fixture cleanup: autouse fixture in `tests/conftest.py` redirects `OUROBOROS_CHAIN_ARTIFACT_DIR` to `tmp_path` per test (was leaking ~280 `chain-*.md` files into `docs/brainstorm/` per CI run before this).
- Design doc: `docs/brainstorm/phase-2-q2-diff-capture-design.md`. Dogfood seed: `seeds/phase-2-q2-diff-capture.yaml`.

### ✅ Phase-2 Q2.1 — Dogfood-gap hotfix (PR #5)

**Branch `fix/q2-diff-capture-and-render` on `KeithMoc/ouroboros-loop`.**

A real `--compounding` validation run on 2026-04-26 (`orch_d90a52690b15`) produced a chain artifact with all four ACs marked `[pass]` but **zero `diff_summary` matches** despite the inner orch worktree having ten real commits. Root cause: two coupled bugs.

1. **Capture-time** — `git stash create` exits 0 with empty stdout on a clean tree. The orchestrator commits each (sub-)AC's work for traceability, leaving clean trees at AC boundaries → the entire capture pipeline short-circuited (`pre_sha = None` → `compute_diff_summary` returned `""`). PR #5 falls back to `git rev-parse HEAD` (also a valid tree-ish for `git diff --stat`) when stash returns empty. Pre AND post snapshots both fall back. Failure paths preserved: both stash and HEAD failing → `None`.
2. **Render-time** — `_render_chain_as_markdown` (in `serial_executor.py`) iterated serialized postmortem entries but never read the `diff_summary` field. Even with capture working, the markdown artifact would still hide the data. PR #5 emits it as a fenced code block under each AC's bullet list when non-empty (with dynamic-fence selection so pathological file names containing backticks can't prematurely close the block).

Tests added: 4 capture-side regression tests (HEAD fallback on truly clean tree, no-HEAD repo, committed-only, mixed committed+uncommitted) + 2 renderer tests (emits/omits, plus dynamic fence) + 1 end-to-end integration test `test_diff_summary_flows_end_to_end_with_commit_per_ac` that drives `execute_serial` with a real commit-per-AC pattern and asserts the rendered artifact contains the stat. The e2e test is the regression-net that would have caught both bugs in CI.

Refactor: `_resolve_head`, `capture_pre_ac_snapshot`, `_capture_post_snapshot`, and `compute_diff_summary` now share a `_run_git(args, *, phase, cmd_name, fallback=None)` helper that consolidates the four-way error handling (FileNotFoundError, TimeoutExpired, OSError, non-zero exit).

Test status: **5559 passed, 2 skipped** post-PR #5.

### Decisions made during this cycle

| # | Decision | Why |
|---|---|---|
| Scope | Q2 only this cycle (Q4 + prompt caching deferred) | First non-self-referential dogfood |
| Boundary | `git stash create` with HEAD fallback (PR #5) | Pure stash misses commit-per-AC workflow; HEAD fallback covers it without stash-list pollution |
| Format | `git diff --stat` + 20-file / 4 KB caps | Human-readable, defends against generated-file blowups |
| Failures | Empty `diff_summary` + structured log on every git error | Best-effort, never a run-blocker |
| API | `file_cap`/`char_budget=None` consults env, explicit int hard-overrides | Hard-override semantics fixed via CodeRabbit review |
| Filter | Index-based (not set-based) file-cap truncation | Byte-identical rows can't slip past the cap |
| Tight budget | Hard-cap at `char_budget` even when overhead alone busts it | Contract preserved unconditionally |
| Render fence (PR #5) | Dynamic backtick run + 1, default ```` ``` ```` | Pathological file names containing backticks cannot prematurely close the fenced block |

### CodeRabbit review timeline

**PR #4** — 3 review passes, all addressed:

1. Pass 1 (`d2b6cf6`) — 1 actionable: `char_budget` cap not enforced when overhead alone busts budget. Fixed in `9672cbb` with 3 regression tests.
2. Pass 2 (`9672cbb`) — clean.
3. Pass 3 (`e96e29a`) — 2 nitpicks: magic-default env-override coupling + set-based duplicate filter. Fixed with 7 regression tests.
4. Pass 4 (`e96e29a` re-review) — clean. Human merge followed.

**PR #5** — initial review feedback (multiple findings) addressed in a follow-up commit on `fix/q2-diff-capture-and-render`: dynamic-fence selection (TDD: failing test first), explicit `monkeypatch.setenv` in the e2e test, fence-structure assertions in the renderer test, and the `_run_git` consolidation refactor.

Reviews replied to inline (`gh api .../comments/<id>/replies`) for inline comments and via `gh pr comment` for review-level findings.

---

## Important Files (Phase-2 Q2 + Q2.1)

```text
docs/brainstorm/serial-compounding-open-questions.md   # Master Q-list, decisions log
docs/brainstorm/phase-2-q2-diff-capture-design.md      # PR #4 design
docs/guides/serial-compounding.md                      # Living guide (Phase-1.5 + 2 entries)
seeds/phase-2-q2-diff-capture.yaml                     # The seed that drove the PR #4 run
seeds/phase-1.5-dogfood*.yaml                          # Prior cycle's seeds (kept for reference)

src/ouroboros/orchestrator/diff_capture.py             # Q2 core; PR #5 added _run_git + HEAD fallback
src/ouroboros/orchestrator/serial_executor.py          # Wiring at lines ~1300/1370/1580; PR #5 added diff_summary block in _render_chain_as_markdown
src/ouroboros/orchestrator/level_context.py            # ACPostmortem.diff_summary field
tests/unit/orchestrator/test_diff_capture.py           # 29 unit tests (4 added in PR #5)
tests/unit/orchestrator/test_serial_executor.py        # 133 unit tests (3 added in PR #5)
tests/conftest.py                                      # Autouse chain-artifact-dir fixture
```

---

## What Worked

- **Brainstorm-with-skill → seed → ooo run pattern.** Spending a few clarifying questions to lock the four design decisions (boundary, format, caps, failure mode) before writing the seed paid off — the dogfood agent had ~370 lines of LOC + 17 tests shipped in ~20 min and the eval came back APPROVED at 0.88 with no rework on the implementation itself.
- **Dogfood-as-validation.** The `--compounding` run that surfaced the two PR #5 bugs is the strongest argument for keeping a real-execution validation step in every dogfood cycle. Unit tests covered the data layer; only the live run exercised the orchestrator's commit-per-AC workflow against the renderer's actual output.
- **Eating CodeRabbit's review findings via the loop.** Every actionable finding across PR #4 and #5 has been real (the char_budget overhead bug, the magic-default coupling, the set-based filter, the static-fence collision, the e2e test self-containment). The fixes were small.
- **The autouse `OUROBOROS_CHAIN_ARTIFACT_DIR` redirect.** Discovered ~280 leaked artifacts in `docs/brainstorm/` from prior test runs; the autouse fixture in `tests/conftest.py` makes future leaks impossible.
- **Saving project memory for the prompt-caching constraint.** The user runs Claude Code on a subscription, which blocks the prompt-caching adapter rewrite indefinitely. Memory at `prompt_caching_blocked.md` ensures it doesn't get re-proposed.

## What Didn't Work / Open Gaps

- **Q2's `git stash create`-only boundary mismatched the orchestrator's commit-per-AC workflow.** Discovered post-PR #4 via the dogfood validation run; both the capture-time short-circuit and the renderer's silent omission of `diff_summary` shipped in `ccfc479`. Fixed in PR #5 with the HEAD fallback and the `Diff summary` fenced block, plus an end-to-end integration test (`test_diff_summary_flows_end_to_end_with_commit_per_ac`) that closes the unit-test gap. **Lesson**: per-AC capture features must be designed against the orchestrator's actual commit pattern, not just the working tree state.
- **The MCP `ouroboros_execute_seed` tool doesn't expose a `mode` parameter.** The seed's `metadata.execution_mode_required: "compounding"` is not used by the runner. The PR #4 dogfood run executed in **parallel** mode, not compounding — which is why the rolling-postmortem-chain bugs were not seen during the cycle. Q2.1's e2e test now exercises the compounding path directly in unit space, so this MCP gap is no longer load-bearing for diff-capture validation, but the runner-level fix (honoring `execution_mode_required`) is still worth doing for future cycles.
- **Inline review-level nitpicks aren't reachable via the per-comment reply API.** CodeRabbit puts review-level findings in the review body, not as inline comments. Had to use `gh pr comment` (issue-level comment) instead of `gh api .../comments/<id>/replies`.

---

## Next Steps

### Phase-2 Q4 — Inline QA (next dogfood cycle)

Per the brainstorm doc and decisions log:
- Wire `QAHandler` (`mcp/tools/qa.py:397`) inline at the existing `_build_postmortem_from_result` call sites in `serial_executor.py:1156` / `:1328`.
- Add `--inline-qa` CLI flag (default off — roughly doubles model calls).
- Add separate `--max-qa-retries` counter (default 1) so QA-failure retries don't share the stall-retry budget.
- Estimated ~250 LOC.
- Suggested ordering: **wire QA → add `--inline-qa` flag → add `--max-qa-retries`** as separate ACs in one seed, OR a single AC if the wiring is small enough.
- **Watch the workflow-assumption gotcha**: when wiring QA per-AC, design against the orchestrator's commit-per-AC pattern (same lesson as Q2.1).

Same workflow:
1. `/superpowers:brainstorming continue with Q4 inline QA via ooo proper workflow`
2. Lock 3-4 design decisions (where exactly to call QA, what to do on REVISE, retry-counter semantics)
3. Write spec + seed at `docs/brainstorm/phase-2-q4-inline-qa-design.md` and `seeds/phase-2-q4-inline-qa.yaml`
4. `ooo run workflow seeds/phase-2-q4-inline-qa.yaml --compounding`
5. `ooo evaluate <session_id>`
6. Squash-merge to `KeithMoc/ouroboros-loop:main`

### Deferred (not blocked)

- **Q5 — `ooo evolve` integration**: end-of-run hint only (option B from brainstorm). Cheap; can land any time.
- **Phase-2 prompt caching**: blocked by Claude Code subscription runtime. See memory `prompt_caching_blocked.md`. Reopen only if the runtime constraint changes.
- **MCP `execute_seed` mode honoring**: cosmetic — once fixed, MCP-driven dogfood runs would use the right executor by default.

---

## Repo State at Handoff Time

- Branch: `fix/q2-diff-capture-and-render` (PR #5 open against `KeithMoc/ouroboros-loop:main`).
- `main` at `f665c06` (`chore: ignore .worktrees/`); will fast-forward to `1f58098` + the review-response amendment commits when PR #5 merges.
- Active worktree: `.worktrees/q2-fix` for the hotfix branch.
- Untracked: `.claude/scheduled_tasks.lock` (transient, ignore).

---

## Verification Commands

```bash
# Tests
uv run pytest tests/unit/orchestrator/test_diff_capture.py -q       # 29 passed
uv run pytest tests/unit/orchestrator/test_serial_executor.py -q    # 133 passed
uv run pytest tests/unit/ -q                                         # 5559 passed, 2 skipped

# Lint
uv run ruff check src/ouroboros/orchestrator/diff_capture.py
uv run ruff check src/ouroboros/orchestrator/serial_executor.py

# Repo state
git log --oneline -5
git status --short
```

---

*Phase-2 Q2 shipped clean (PR #4); the dogfood-gap finding spawned PR #5 with HEAD-aware capture, dynamic-fence rendering, the missing e2e regression test, and the `_run_git` consolidation. Next dogfood cycle: Q4 inline QA — apply the workflow-assumption lesson up front.*
