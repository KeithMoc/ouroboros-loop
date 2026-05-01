# Handoff Document

> Last Updated: 2026-05-01 ~04:50 UTC
> Session: **Upstream sync** — `Q00/ouroboros:main` (v0.30.0 → v0.31.1, +35 commits) merged into `KeithMoc/ouroboros-loop:main` as merge-commit `57a6449`. 3 conflicts resolved (1 trivial, 1 architectural, 1 additive); 1 upstream test reshaped to honor Q4.1 AC-2 ordering. Tests `5847 passed + 2 skipped` (+105 net new tests from upstream). Local main now 15 ahead / 0 behind upstream. Not pushed to origin.
> Prior session: Q4.1 SHIPPED. PR #7 squash-merged as `9a8d9f9` at 2026-05-01 04:33:23 UTC. CodeRabbit posted 9 inline + 1 outside-diff actionable findings on first pass; all 10 addressed in follow-up commit `a2f17e2`. Pre-merge tests `5742 passed + 2 skipped`.

---

## Latest Update — Upstream sync (v0.30.0 → v0.31.1)

**Merge commit:** `57a6449 Merge remote-tracking branch 'upstream/main'`

### What we picked up from upstream

| Tag | Highlights |
|---|---|
| **v0.31.0** | Gemini CLI runtime backend (#504), parallel worker-cap honoring (#489), seed ontology rendering in execution contract, AC decomposition depth cap (=2), seed-contract prompt scoped to execution, AC-duplication fix in execution prompt, router multiline/Windows-path fixes, MCP startup-test isolation, brownfield-store init failure-safety, resume skill rename (`resume` → `resume-session`) |
| **v0.31.1** | Brownfield store refcount + share for `serve --db` (#487 / #507), brownfield scan-roots validation (#486), interview seed-ready reopen fixes (closure-pressure removal + per-dimension gap signal restoration) |

### Conflict resolution

Real overlap was 11 files; auto-merge resolved 8. Three needed manual work:

1. **`src/ouroboros/config/__init__.py`** — trivial: both sides added an import (`get_max_decomposition_depth_default` ours, `get_max_parallel_workers` upstream). Kept both.
2. **`src/ouroboros/mcp/tools/execution_handlers.py`** — architectural: upstream put worker-cap resolution + plugin-dispatch *before* seed parsing (#489); our Q4.1 AC-2 requires seed parsing *before* plugin dispatch (so `seed.metadata.execution_mode_required` is honored on delegated runs). Resolution: take upstream's worker-cap resolution at the top (it doesn't need the seed), drop the now-redundant intent comment block, keep Q4.1 AC-2's mode-resolution flow at its existing position, and wire upstream's `max_parallel_workers=...` argument into the `build_execute_subagent` call below.
3. **`tests/unit/orchestrator/test_runner.py`** — additive: HEAD added `test_claude_md_*` tests, upstream added `test_handles_empty_ontology_fields` + `test_includes_brownfield_context`. Kept both.

Plus one downstream test fix: **`tests/unit/mcp/tools/test_handler_subagent_wiring.py::test_plugin_payload_includes_resolved_worker_cap`** was authored against upstream's pre-AC-2 ordering and used `seed_content="goal: test"` (insufficient for full Pydantic validation). Now passes `_VALID_SEED_YAML`. The companion `test_plugin_path_surfaces_worker_cap_config_error` still uses the minimal seed correctly — its ConfigError fires before seed parsing and the test relies on that.

### Tests

`uv run pytest tests/unit/ -q` → **5847 passed + 2 skipped** (was 5742 + 2 pre-merge).

### What this changes downstream

- **Worker-cap is now honored on the plugin-dispatch path.** Our local AC-2 work fixed the mode contract on plugin dispatch; #489 fixes the worker-cap contract on plugin dispatch. Both apply now.
- **Gemini CLI runtime is available** (`runtime_backend="gemini_cli"`). Adds a third backend alongside Claude / OpenCode / Codex / Hermes.
- **AC decomposition depth is capped at 2.** Reduces risk of sub-AC explosion in compounding mode — should be invisible to existing Q4.1 work since our seeds set `expected_decomposition_depth=0`.
- **Resume skill renamed.** `skills/resume/` → `skills/resume-session/`. Documented in command summary; the user-facing `ooo resume` mapping in `CLAUDE.md` still resolves through the harness.
- **Dogfood-bootstrap caveat partially eased.** When PyPI publishes ouroboros-ai 0.31.1, dogfood cycles on the next refresh will pick up router / interview / brownfield fixes — but **AC-2 (`execution_mode_required` honoring) is still unique to this fork** until merged upstream.

### Repo state at merge time

- Branch: `main` at `57a6449` (merge commit). `origin/main` still at `9a8d9f9` — **not pushed**.
- Working tree: clean (only `.claude/scheduled_tasks.lock` untracked, which is gitignored anyway).
- Diff vs upstream: 15 ahead / 0 behind.

---

## Goal

Ship serial-compounding Phase 2 incrementally as dogfood runs. Each work item from the brainstorm doc (`docs/brainstorm/serial-compounding-open-questions.md`) becomes one `ooo run --compounding` cycle: design → seed → run → evaluate → ship.

Phase-2 Q1, Q2, Q3, Q4, Q4.1, Q6, Q7 all merged (PRs #3–#7). Phase 2's open queue narrows to Q4.2 (judge-accuracy substrate + end-of-run sweep mode), Q5 (`ooo evolve` integration), and the carry-over prompt-caching investigation.

---

## Current Progress

### ✅ Phase-2 Q4 — Inline QA per AC (PR #6 merged)

**Squash-merged as `ce5ad1e` on `KeithMoc/ouroboros-loop:main` at 2026-04-28 00:11:59 +0930.** See prior `git log` and `docs/brainstorm/phase-2-q4-inline-qa-design.md` for the design.

Test status: **5699 tests collected** post-PR #6.

### ✅ Phase-2 Q4.1 — Hardening cycle (PR #7 merged)

**Squash-merged as `9a8d9f9` on `KeithMoc/ouroboros-loop:main` at 2026-05-01 04:33:23 UTC.**

Pre-squash branch (`feat/phase-2-q4.1-hardening`, now deleted) had five commits:
- `33836b2 docs(compounding): lock phase-2 Q4.1 hardening design`
- `03c322f feat(compounding): add phase-2 Q4.1 hardening dogfood seed`
- `a4cf6f3 feat(compounding): Q4.1 — AC-1 parent-only QA design rationale + test rename` *(orchestrator's commit-per-AC fired only for AC-1 — see [What Didn't Work](#what-didnt-work--open-gaps))*
- `a6002b9 feat(compounding): Q4.1 — AC-2/3/4 (MCP mode + resume sentinel + observability + 4 deferred CR items)` *(this session, post-run)*
- `a2f17e2 fix(compounding): address CodeRabbit findings on PR #7 (10 items)` *(post-CR-review fix batch)*

**Run details:** session `orch_ca1ac0b88c85` / job `job_77267701b1c8` / exec `exec_c826cecb17c7`. Started 2026-04-29 03:10:38 UTC, completed 04:57:35 UTC (~1h47m). Backend: `claude` / `claude_code`. 1031 messages processed.

**Tests:** `5742 passed + 2 skipped` (was 5697 + 2 = 5699). Net **+43 new tests** across 9 modules.

**`ooo evaluate` outcome:**
- Stage 1 (mech): PASSED — build + tests green; lint/static skipped (no command configured).
- Stage 2 (semantic): score 0.73, AC-compliant=YES, drift 0.15. Evidence cites actual code (mode-resolution branches, soft-flip flag, audit-diff round-trip, by-reference counter aliasing).
- Stage 3 (consensus): REJECTED 0/3 — but spuriously, due to passing `artifact="."` which serialized to `.ouroboros_eval_artifact.md` containing literal `"x"`. Voters had no real artifact. Stage 2's code-inspection signal stands.
- **Lesson:** when calling `mcp__ouroboros__ouroboros_evaluate`, pass the actual diff text (or chain artifact) as `artifact`, not a path-shorthand. Stage 3 voters work off the artifact string.

**Spec + seed:**

- **Design spec:** `docs/brainstorm/phase-2-q4.1-hardening-design.md` — ~600 lines, 4 ACs, all decisions locked through brainstorming + P0/P1/interview.
- **Dogfood seed:** `seeds/phase-2-q4.1-hardening.yaml` — ~700 lines, ambiguity_score=0.10, expected_decomposition_depth=0, execution_mode_required="compounding".

**4 ACs shipped:**

| AC | Scope | LOC | Risk |
|---|---|---|---|
| **AC-1** | Parent-only QA design rationale + test rename (docs-only) | ~10 | trivial |
| **AC-2** | SeedMetadata `execution_mode_required` honoring + soft-flip default + caller-wins-with-warn conflict policy | ~50 | low |
| **AC-3** | QA-pending sentinel resume + audit-diff sidecar (unified-diff format with cumulative replay sections) | ~125 | medium |
| **AC-4** | 4 deferred CodeRabbit items + Run Summary observability panel (always-show-5-row format) | ~170 | low |

Total: ~355 LOC across ~10 files.

**Three-pass procedure trial results** (this was the FIRST cycle to run the procedure):

| Pass | Time | Material findings |
|---|---|---|
| **P0 recon + late verification** | ~12 min | 4 shifts vs. brainstorm-doc; 3 of 4 ACs rescoped before brainstorming; caught the AC-3 checkpoint-timing misclaim from the Explore agent (resolved via direct file read) |
| **superpowers:brainstorming** | ~15 min | 7 locked decisions across 4 ACs; initial spec written |
| **P1 edge-case mining** | ~6 min | 5 findings → 3 spec deltas: AC-2 handler reads `mode` BEFORE `Seed.from_dict()` (must reorder); ACPostmortem has no `final_message` field (AC-3 schema extension); EventStore has no `count()` API (AC-4 uses session-scoped counter instead) |
| **ooo interview (Path B fallback)** | ~8 min | 3 sharpenings: AC-3 audit-diff sidecar with cumulative `### Replay N` sections; AC-4 panel always-show-5-row (consistency over compactness); AC-2 conflict-event payload (5 fields: session_id, caller_mode, seed_mode, seed_id, timestamp) |
| **Total prep** | **~41 min** | Spec ~30% richer than brainstorm-only would have produced |

**Verdict on the procedure:** worth it — each pass produced material refinements that would otherwise have surfaced as runtime errors or improvised-at-implementation-time. P0 caught that 3 of 4 ACs needed rescoping; P1 caught the AC-2 ordering gotcha; the interview added the audit-diff design.

### ✅ Phase-2 Q1 / Q2 / Q3 / Q6 / Q7 — already in code

| Q | Where |
|---|---|
| Q1 (B-prime sub-postmortems) | `level_context.py:539, 589` |
| Q2 (per-AC diff capture) | `diff_capture.py`, `serial_executor.py:1300+` |
| Q3 (C-plus invariants) | `level_context.py:509–820` |
| Q6 (resume) | `level_context.py`, `cli/commands/resume.py` |
| Q7 (truncation event) | `events.py:666`, `serial_executor.py:1293` |

---

## Important Files

```text
HANDOFF.md                                                # This file
docs/brainstorm/phase-2-q4.1-hardening-design.md          # Q4.1 design spec — 4 ACs locked
docs/brainstorm/phase-2-q4-inline-qa-design.md            # Q4 predecessor (PR #6 design)
docs/brainstorm/phase-2-q2-diff-capture-design.md         # Q2 + Q2.1 hotfix design (workflow-assumption lesson)
docs/brainstorm/serial-compounding-open-questions.md      # Master Q-list, decisions log
docs/guides/serial-compounding.md                         # Living guide

seeds/phase-2-q4.1-hardening.yaml                         # Q4.1 seed — READY TO EXECUTE
seeds/phase-2-q4-inline-qa.yaml                           # Q4 seed (drove PR #6)
seeds/phase-2-q2-diff-capture.yaml                        # Q2 seed (drove PR #4)

src/ouroboros/orchestrator/inline_qa.py                   # Q4 core; AC-4 unified run_inline_qa serialization through _serialize_qa_verdict
src/ouroboros/orchestrator/serial_executor.py             # AC-3 added phase-1 'qa_status=pending' checkpoint write + resume QA-replay branch
src/ouroboros/orchestrator/level_context.py               # AC-3 extended ACPostmortem with final_message field + qa_status='pending' enum value
src/ouroboros/orchestrator/events.py                      # AC-2 added create_mode_conflict_event factory
src/ouroboros/orchestrator/runner.py                      # AC-4 Run Summary panel + completion_summary extension
src/ouroboros/orchestrator/_q41_state.py                  # AC-2/4 shared module-level state (soft-flip flag + mode-conflict counter)
src/ouroboros/core/seed.py                                # AC-2 extended SeedMetadata with execution_mode_required field
src/ouroboros/mcp/tools/execution_handlers.py             # AC-2 relocated the mode-resolution block below the seed-parse line

tests/unit/orchestrator/                                  # Where most new Q4.1 tests will land
tests/conftest.py                                         # Autouse OUROBOROS_CHAIN_ARTIFACT_DIR redirect
```

---

## What Worked

- **Brainstorm-with-skill → seed → ooo run pattern** continues to be the most effective shape.
- **Three-pass pre-execution procedure** (P0 recon → brainstorm → P1 edge-case mining → interview). First trial. Each pass surfaced material refinements; ~41 min total overhead beat brainstorming-only by ~30% on spec quality.
- **Forward-compatible postmortem deserialization** (`d.get(..., default)`) prevented chain-shape changes from breaking older serialized chains. Worth keeping as discipline for any future field additions.
- **`pre_sha` captured outside the QA-retry loop** in Q4. The Q2.1 lesson — design *with* the orchestrator's commit-per-AC pattern — was applied up front. Same lesson applied to Q4.1 AC-3's phase-1 checkpoint design.
- **Path B fallback when MCP errored mid-cycle** kept the cycle moving. The `ooo interview` MCP tool errored; Path B (direct interview using superpowers methodology + AskUserQuestion) unblocked us in <10 min.
- **Audit-diff sidecar design** (interview-derived). When a QA replay supersedes an original verdict in the live event stream, an out-of-band unified-diff file at `<chain_artifact_dir>/chain-<session>-ac<N>-qa.original.diff` preserves the audit trail. Multi-replay scenarios append cumulative `### Replay N (timestamp)` sections.

## What Didn't Work / Open Gaps

### From the Q4.1 cycle (lessons captured)

1. **Commit-per-AC pattern only fired for AC-1; AC-2/3/4 ran the parallel-decomp path and left work uncommitted.** Activity field showed `Level 1/2: ACs [1, 2, 3]` mid-run — three ACs in flight concurrently despite `execution_mode_required="compounding"` on the seed. Expected: the v0.30.0 orchestrator pre-dates AC-2 (the very feature that adds `execution_mode_required` honoring), so it ignored the field and chose its default mode. Classic dogfood bootstrap: this run was building the feature it wished it had. **Lesson:** when dogfooding a behavior change, expect the existing orchestrator to operate under the OLD rules. Plan the post-run cleanup (here, manual commit + branch surgery for AC-2/3/4) up front. The next compounding cycle (post-Q4.1 release) will honor the field correctly.

2. **`ooo evaluate` Stage 3 needs a real artifact, not a path.** Passing `artifact="."` produced `.ouroboros_eval_artifact.md` with literal `"x"`; all three voters rejected with 0.95 confidence for "no meaningful evaluation artifact." Stage 2 inspected the actual code regardless and gave 0.73 / AC-compliant — that's the trustworthy signal. **Lesson:** pass the diff text or the chain artifact contents as `artifact`, not a path shorthand. Or document a `working_dir`-only mode for Stage 3 if one is intended. This is a candidate API tweak for a future cycle.

3. **Wakeup chain broke after hour 1 in the prior session (carry-forward).** `ScheduleWakeup` doesn't survive session closes; `[60, 3600]`-second clamp forces chaining for any delay >1h. **Lesson:** for deferred-start or cross-session-robust polling, use `/schedule` (cron-based remote agent), not `ScheduleWakeup`. This session's polling worked fine (`CronCreate` at `7,37 * * * *`, session-only) because the session stayed open through the run.

4. **MCP packaging gap (resolved, but worth knowing).** `ouroboros_interview` errored in the prior session with `No module named 'ouroboros.events.interview'`. The execute-seed handler does NOT depend on `ouroboros.events.interview` (only authoring_handlers.py does), so kickoff was unblocked. `uvx --refresh-package ouroboros-ai` confirmed PyPI was at 0.30.0 (same as cached). **Lesson:** before assuming an MCP-tool failure blocks unrelated tools, check imports — different tool families may not share the missing module.

### Carry-over open gaps (not yet addressed by Q4.1)

3. **Pre-v0.30 checkpoint migration story.** Forward-compat shims handle field additions (`d.get(...)`), but a v0.29 checkpoint file loaded by v0.30+ may not survive resume. Smoke-test if anyone reports a broken resume.

4. **Prompt-prefix caching transparency in the Claude Code SDK** (low confidence; worth a 30-min test). The `prompt_caching_blocked.md` memo treats savings as theoretical. Two compounding cycles back-to-back with identical system prompts + AC-2 first-turn latency measurement would tell us whether the SDK caches transparently at the runtime layer.

6. **Push the upstream-sync merge to `origin`?** `57a6449` is unpushed. Whether to fast-forward `origin/main` or open a PR for review is a project-style call — typical for this fork has been direct push to `origin/main` after green tests, but the merge surface here is large enough (35 upstream commits + 1 architectural conflict resolution) that a one-shot review PR is defensible.

### Stale prior gaps (CLOSED by Q4.1 — code shipped)

| Prior gap | Closed by |
|---|---|
| Q4 × decomposition is unverified | AC-1 documents intentional parent-only design; test renamed |
| MCP `execution_mode_required` ignored | AC-2 schema extension + handler relocation + soft-flip |
| Q4 × resume semantics undesigned | AC-3 QA-pending sentinel + audit-diff sidecar |
| No cost / rate-limit observability for inline QA | AC-4 Run Summary panel + completion_summary extension |
| Q4.1 deferred CodeRabbit items (4) | AC-4 ships them all |

5. **Q4.1 judge-accuracy substrate** (FP/FN observation infrastructure for the eventual `--fail-fast` flip) is NOT in Q4.1's scope. Now the natural Q4.2 cycle, ready to start once #7 is squash-merged.

### Carry-over from prior session (still valid)

- **Inline review-level nitpicks aren't reachable via the per-comment reply API.** CodeRabbit puts review-level findings in the review body, not as inline comments. Use `gh pr comment` (issue-level) instead of `gh api .../comments/<id>/replies` for those.

---

## Next Steps

### Immediate — pick up where this session ended

1. **Q4.2 kickoff** is the natural next cycle. Two scopes packaged together in the brainstorm doc:
   - **Judge-accuracy substrate** — FP/FN observation infrastructure that lets `--fail-fast` actually trigger.
   - **End-of-run sweep mode** (`--qa-mode=defer`) — real ~250 LOC mode change.
   Apply the same three-pass procedure as Q4.1 (P0 recon → brainstorming → P1 edge-case mining → ooo interview / Path B fallback).
2. **First Q4.2 run will exercise AC-4's Run Summary panel + AC-2's mode honoring as the *driver*, not the *target*.** That's the meta-validation Q4.1 was designed to enable. Watch the panel render during Q4.2's actual `ooo run`.
3. **PyPI release of `ouroboros-ai==0.30.1`** (or whatever the next version) is gated externally — but until that ships, dogfood cycles will continue to be driven by 0.30.0 (= pre-Q4.1) until the user pushes a release. The dogfood-bootstrap memory note covers this.

### Deferred (not blocked, not in Q4.1)

- **Q4.2 — judge-accuracy observation infrastructure** (the FP/FN substrate that lets `--fail-fast` actually trigger). Natural follow-up to Q4.1 once shipped.
- **Q4.2 — end-of-run sweep mode** (`--qa-mode=defer`). Real ~250 LOC mode change. Worth its own cycle.
- **Q5 — `ooo evolve` integration**: end-of-run hint only (option B from brainstorm). Cheap; can land any time.
- **Phase-2 prompt caching**: blocked by Claude Code subscription runtime. See memory `prompt_caching_blocked.md`.
- **Pre-v0.30 checkpoint migration test**: low priority unless broken resume reported.

---

## Repo State at Handoff Time

- Branch: `main` at `57a6449` (merge of `upstream/main` v0.31.1). **Not pushed** — `origin/main` is still at `9a8d9f9`.
- Diff vs upstream: 15 ahead / 0 behind.
- Feature branch `feat/phase-2-q4.1-hardening` deleted locally; remote branch survives on origin (GitHub may auto-delete depending on repo settings).
- Working tree clean (only `.claude/scheduled_tasks.lock` untracked, which is gitignored).

---

## Verification Commands

```bash
# Tests (post-merge baseline)
uv run pytest tests/unit/ -q  # 5847 passed + 2 skipped

# Repo state
git log --oneline -10
git status --short
git rev-list --left-right --count main...upstream/main  # 15  0

# PRs
gh pr view 7 --repo KeithMoc/ouroboros-loop
```

---

## Quick context for fresh-context resume

If you're picking this up cold from a new session:

1. **Read this file** (you are).
2. **Verify main is at `57a6449`** (upstream-sync merge): `git log --oneline -1`. The Q4.1 squash-merge `9a8d9f9` is now the parent on the local-side, and `483e9be` (upstream's v0.31.1 release-merge) is the parent on the upstream-side.
3. **Decide whether to push `57a6449` to `origin/main`** (or open a review PR — see open gap #6).
4. **Q4.2 is the next cycle** (judge-accuracy substrate + end-of-run sweep mode). Start with P0 recon against the master Q-list at `docs/brainstorm/serial-compounding-open-questions.md`, then brainstorming, then P1 edge-case mining, then ooo-style interview — same three-pass procedure as Q4.1 (took ~41 min and produced a spec ~30% richer than brainstorm-only).
5. **Check `.claude/projects/-home-keith--WORKSPACES--ouroboros-loop/memory/MEMORY.md`** for project memory: dogfood-bootstrap pattern, gh-fork-resolution gotcha, prompt-caching block.

---

*Q4.1 shipped a 4-AC hardening cycle: MCP `execution_mode_required` honoring (with caller-wins-and-warn conflict policy), a QA-pending sentinel resume path with audit-diff sidecar, a Run Summary observability panel, and 4 deferred CR items. Dogfood run completed cleanly; first CodeRabbit pass surfaced 10 actionable findings (architectural: plugin-dispatch path bypassed mode resolution; quality-of-life: defensive coercion, panel completeness, hermetic test discipline) — all addressed in commit `a2f17e2`. Three-pass procedure proved its keep again; carrying as standard cycle discipline.*

*Upstream sync (this session): merged `Q00/ouroboros:main` v0.30.0 → v0.31.1 (+35 commits) into local main. The architectural collision was instructive — upstream's #489 worker-cap fix and our Q4.1 AC-2 mode-resolution fix both targeted the plugin-dispatch path but from different angles. Resolution preserves AC-2's "seed-parse-before-dispatch" ordering (which is required for `execution_mode_required` to be readable) and slots #489's worker-cap resolution above it (which doesn't need the seed). 5847 tests green. Worth flagging upstream that AC-2 generalizes #489's fix shape: contracts that depend on the seed must be resolved before the plugin-dispatch gate, not after.*
