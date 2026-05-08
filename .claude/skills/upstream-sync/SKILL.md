---
name: upstream-sync
description: Audit, classify, and merge new commits from the Q00/ouroboros upstream into this fork without breaking compounding work. Use this skill whenever the user says yes to the SessionStart "upstream is N commits ahead" prompt, says any of "sync upstream", "merge upstream", "pull from upstream", "rebase on upstream", "Q00/ouroboros has new commits", "we're behind upstream", or asks about syncing the fork. Also use when the SessionStart hook injects a `[upstream-sync]` notice. The skill enforces a compounding audit (worth merging? does it conflict with our local work?), risk classification (mechanical vs complex), and the correct merge strategy (merge commit, never squash).
---

# Upstream Sync — Audit, Classify, Merge

## Why this skill exists

This repo is a **fork** of `Q00/ouroboros` with our own compounding work layered on top. We want upstream's improvements but we cannot afford to:

- **Lose our local diff.** Our compounding features (Phase-2 work, MCP changes, dogfood seeds) must survive every sync.
- **Squash-merge upstream PRs.** Squashing rewrites all upstream commit hashes, making GitHub permanently report the fork as "N commits behind" even when content is identical. Always use a **real merge commit** for upstream syncs.
- **Sleepwalk through complex merges.** A complex merge is a feature. It deserves an issue, a branch, tests, and review — not a 2 AM `git merge --strategy=ours`.

The hook fires on session start when `upstream/main` has new commits. The user gets to opt in. If they opt in, you run this procedure.

## Inputs you should expect

The SessionStart hook injects something like:

```
[upstream-sync] origin/main is N commits behind upstream/main (Q00/ouroboros).
Run the upstream-sync skill to audit and merge if the user agrees.
```

If the user says "yes / let's do it / sync it / proceed", you start at **Phase 0**. If they say "not now", acknowledge and stop — do NOT keep nagging in the same session.

## Autopilot mode

If the user says "autopilot", "run it end-to-end", "do the whole thing", or similar, the flow becomes non-interactive — but the **safety classifications still stand**. Autopilot does NOT mean:

- Bypass the MECHANICAL/COMPLEX gate.
- Power through a complex merge inside the session-prompt window.
- Skip the tracking issue.

Autopilot DOES mean:

- Pick the obvious defaults at Phase 0 (latest `upstream/main`, named `chore/sync-upstream-<latest-tag>` for mechanical or `feat/upstream-sync-<latest-tag>` for complex, "full path").
- Drive every phase without pausing for confirmation.
- Capture lessons learned at the end and feed them back into this skill.

The classification gate is non-negotiable. If autopilot lands on COMPLEX, the autopilot succeeds by **filing the tracking artifact and stopping**, not by forcing the merge.

## Phase 0 — Scope confirmation

Before any git work, confirm with the user (skip if autopilot — use defaults below):

1. **Which upstream ref?** Default `upstream/main`. Confirm if the user wants a tag (e.g. `upstream/v0.32.0`) or a different branch.
2. **Target branch?** Default: integrate via a topic branch named `chore/sync-upstream-<version-or-date>`, then PR into `main`. Never merge directly to `main` without a PR.
3. **Time budget.** Ask whether they want the **fast path only** (skip if anything is non-mechanical) or the **full path** (write the issue and stop, or proceed if mechanical).

Only proceed once these three are answered (or autopilot defaults apply).

### Sniff test before audit

Before running Phase 1, check `git rev-list --count origin/main..upstream/main`. **Heuristic: N > 50 commits = expect COMPLEX**. The probability that 50+ upstream commits leave our hot paths untouched is essentially zero on this fork. You can still run the full audit, but anchor your expectation at COMPLEX from the start.

## Phase 1 — Audit

```bash
git fetch upstream --prune --tags
git fetch origin --prune
```

Collect the facts:

```bash
# What's coming in
git log --oneline --no-merges origin/main..upstream/main
git log --oneline --merges     origin/main..upstream/main

# Files touched by upstream
git diff --stat origin/main...upstream/main

# Files touched by us since the divergence point
MERGE_BASE=$(git merge-base origin/main upstream/main)
git diff --stat "$MERGE_BASE"...origin/main
```

Produce a **short audit report** in chat with these sections:

- **New upstream commits** — count + grouped themes (releases, fixes, features). Don't paste 36 hashes; cluster them.
- **Upstream-touched files** — top 10 by churn.
- **Our-touched files since divergence** — top 10 by churn.
- **Overlap set** — files that BOTH sides touched. This is the conflict-likely zone.

## Phase 2 — Compounding analysis

For each non-trivial upstream cluster, answer:

1. **Does it advance our compounding direction?** Look at our `docs/handoff/`, recent project memory, and the seeds in `seeds/`. If upstream is fixing the same surface we're hardening, it usually compounds. If upstream is going in a different direction (e.g. removing something we just hardened), flag it.
2. **Does any commit *contradict* a local invariant?** Examples: upstream renames a function we depend on, upstream removes a CLI flag we rely on, upstream changes a contract we've extended.
3. **Is anything safe to *cherry-pick out*?** If only 5 of 36 commits are valuable and the rest are churn, propose cherry-picking instead of full merge. Get user buy-in before deviating from full merge.

This phase is conversational — propose a take, let the user steer.

## Phase 3 — Risk classification

Classify the merge as **MECHANICAL** or **COMPLEX** using these criteria:

**Always run the trial merge on a throwaway probe branch**, not on `main` and not on the future feat/ branch:

```bash
git checkout -b probe/trial-merge-<version> main
git merge --no-commit --no-ff upstream/<ref>
# inspect conflicts
git merge --abort
git checkout main
git branch -D probe/trial-merge-<version>
```

This keeps the audit non-destructive and leaves your real branches untouched. (Learned the hard way: aborting a merge on `main` or a feat/ branch makes the next state confusing.)

### MECHANICAL — proceed to fast path

All of:

- `git merge --no-commit --no-ff upstream/<ref>` produces **zero conflicts**, OR conflicts only in:
  - Lockfiles (`uv.lock`, `package-lock.json`, `bun.lock`) — regenerate, don't hand-edit.
  - `CHANGELOG.md` / `VERSION` — keep both, ours below upstream's section.
  - Pure additions to disjoint files.
- No upstream commit touches a file we've materially changed in the same hunk.
- No upstream commit removes/renames a public symbol our code references.
- Test suite is green after the trial merge.

### COMPLEX — proceed to slow path

Any one of:

- Real conflicts in code we've extended (interview engine, seed pipeline, MCP server, agents).
- Upstream removes/renames something our local diff calls.
- Upstream introduces an architectural shift (new orchestrator backend, new IPC contract, breaking config change).
- Test suite fails after trial merge for non-trivial reasons.
- The audit surfaced "we should think about this" anywhere.

#### Hot-path watchlist (shortcut to COMPLEX)

Any conflict in these files is COMPLEX by default — they carry our compounding work and any disturbance there needs design review, not hunk-fixing:

- `src/ouroboros/orchestrator/parallel_executor.py` (Q4 inline-QA `context_override`, Q4.1 `Complexity` adaptive routing)
- `src/ouroboros/orchestrator/runner.py` (Q4.1 Run Summary panel, recoverable-failure flow)
- `src/ouroboros/orchestrator/serial_executor.py` (Phase-1.5 dogfood postmortem chain)
- `src/ouroboros/orchestrator/diff_capture.py` / `inline_qa.py` / `level_context.py` (Phase-2 Q2/Q4 features — entirely fork-local)
- `src/ouroboros/mcp/tools/{evolution,execution}_handlers.py` (worker-cap + mode-resolution ordering — Q4.1 AC-2)
- `src/ouroboros/config/{loader,models,__init__}.py` when upstream renames helpers (`get_skip_qa_default`, `get_parallel_default`, `get_max_decomposition_depth_default`) — every consumer needs migration

When uncertain, classify **COMPLEX**. The cost of a wrong "mechanical" call is much higher than the cost of writing a tracking doc.

Always abort the trial merge before moving on:

```bash
git merge --abort   # if the trial left an in-progress merge
```

## Phase 4A — Mechanical path

```bash
# Fresh topic branch off origin/main
git checkout main && git pull --ff-only origin main
BRANCH="chore/sync-upstream-$(git describe --tags --abbrev=0 upstream/main 2>/dev/null || date +%Y-%m-%d)"
git checkout -b "$BRANCH"

# Real merge commit — NEVER squash, NEVER fast-forward
git merge --no-ff upstream/main -m "chore(upstream): sync Q00/ouroboros $(git describe --tags --abbrev=0 upstream/main 2>/dev/null) (+$(git rev-list --count origin/main..upstream/main) commits)"
```

Resolve any allowed mechanical conflicts (lockfile / changelog only). For lockfiles, regenerate with the project's tool (e.g. `uv lock`), don't hand-merge.

Verify:

```bash
# Whatever the project's pre-push gate is. Look at .pre-commit-config.yaml / CI
# At minimum:
just test 2>/dev/null || pytest -q || true
```

Push and open a PR:

```bash
git push -u origin "$BRANCH"
gh pr create --repo KeithMoc/ouroboros-loop --base main --head "$BRANCH" \
  --title "chore(upstream): sync Q00/ouroboros $(git describe --tags --abbrev=0 upstream/main 2>/dev/null) (+N commits)" \
  --body "$(cat <<'EOF'
## Summary
Mechanical sync of upstream Q00/ouroboros. No semantic conflicts; only lockfile / changelog adjustments.

## Risk
LOW — classified MECHANICAL by the upstream-sync skill (see audit below).

## Audit
<paste the Phase 1 + Phase 2 summary here>

## Merge strategy
**Use "Create a merge commit"** on this PR. Squash will break upstream commit-hash reachability and cause GitHub to permanently report the fork as behind.
EOF
)"
```

Tell the user the PR URL and **explicitly remind them**: click "Create a merge commit", not "Squash and merge".

After the PR merges, locally:

```bash
git checkout main
git pull --ff-only origin main
git fetch upstream
git rev-list --left-right --count upstream/main...origin/main
# Expect 0 left, N right. If GitHub still shows behind, see post-merge troubleshooting.
```

## Phase 4B — Complex path

The merge is a feature now. Don't merge it tonight.

### Step 1 — Open a tracking artifact

**Try GitHub issue first:**

```bash
gh issue create --repo KeithMoc/ouroboros-loop \
  --title "chore(upstream): sync Q00/ouroboros <version> — complex merge" \
  --label "upstream-sync,needs-design" \
  --body "$(cat <<'EOF'
## Upstream delta
<commits / files / themes from Phase 1>

## Why this is complex
<concrete reasons from Phase 3 — name the files, the conflicting symbols, the architectural shift>

## Local invariants at risk
<list local features that could regress — Phase-2 compounding, MCP contract, agent definitions>

## Proposed resolution approach
<your plan: order of integration, which conflicts go which way, what tests need to be added>

## Acceptance criteria
- [ ] Trial merge completes with all conflicts resolved deliberately (not auto)
- [ ] Local test suite is green
- [ ] No regression in <name the compounding features at risk>
- [ ] PR uses **merge commit**, not squash
- [ ] Post-merge: `git rev-list --left-right --count upstream/main...origin/main` shows 0 behind
EOF
)"
```

**If `gh issue create` fails with `the '<repo>' repository has disabled issues`** (this fork has issues disabled by design — strategic notes go local), fall back to a local tracking doc under the gitignored `docs/local/` tree:

```bash
mkdir -p docs/local/upstream-sync-<version>
# Write the same body content to docs/local/upstream-sync-<version>/TRACKING.md
```

The doc is gitignored under the `docs/local/` rule (commit `3be8ee5e`) so it stays on the maintainer's machine. Still update `docs/local/HANDOFF.md` (also gitignored) with a one-paragraph status entry pointing to the TRACKING doc, so the next session inherits the parked-work state via the `dx:handoff` convention.

### Step 2 — Treat as a feature branch

- Branch: `feat/upstream-sync-<version>` (the `feat/` prefix is intentional — it's feature-grade work).
- One commit per logical conflict resolution. Don't rush to squash; we want to be able to bisect.
- The final merge of the feature branch into `main` is a **merge commit**, like Phase 4A.

### Step 3 — Stop and hand off

After the issue is filed and the branch is created with the trial merge in progress (or aborted, depending on user preference), **stop**. Tell the user:

> Issue #N filed. The complex merge is now a feature task. Resume it when you have the time budget — it should not be done in the SessionStart prompt window.

Don't try to power through a complex merge inside the session-start interaction. That's how regressions ship.

## Phase 5 — Verification & memory hygiene

Whether mechanical or complex, after the merge lands on `origin/main`:

1. Run the full project test suite locally.
2. Check `git rev-list --left-right --count upstream/main...origin/main`. Left should be 0 (or just the commits upstream landed in the last hour).
3. If GitHub UI still says "N behind" after a successful merge-commit-based PR, the fix is the same as last time: ensure local `main` has the real merge, then `git push origin main --force-with-lease`. This only happens if the PR was accidentally squashed.
4. Update `docs/local/HANDOFF.md` with a one-paragraph note: version synced, mechanical vs complex, what to watch for next. (The public `HANDOFF.md` stays byte-identical to upstream per the privacy convention — fork-strategic notes go local.)

If the merge was parked as COMPLEX, Phase 5 still runs but only the verification of *parked* state:
- `feat/upstream-sync-<version>` branch exists locally
- `docs/local/upstream-sync-<version>/TRACKING.md` exists with conflict inventory
- `docs/local/HANDOFF.md` updated with parked-status entry
- `git rev-list --count upstream/main...origin/main` still shows the original delta (the merge has not landed yet)

## Anti-patterns — do not do these

- **Squash-merge an upstream sync PR.** Permanently breaks GitHub's "behind" indicator and forces future force-pushes.
- **`git merge --strategy=ours`.** Hides upstream changes instead of reconciling them. Almost always wrong.
- **Merging upstream directly into `main` without a PR.** No review, no CI, no audit trail.
- **Calling a merge "mechanical" because the trial merge had no conflicts.** Conflict-free is necessary, not sufficient. Also check that no symbol we use was renamed or removed.
- **Trying to do a complex merge inside the SessionStart prompt window.** Park it as an issue and come back when you have hours, not minutes.

## Quick reference — commands

```bash
# Audit
git fetch upstream --prune --tags && git fetch origin --prune
git log --oneline --no-merges origin/main..upstream/main
git diff --stat origin/main...upstream/main

# Trial merge (always abort after audit)
git merge --no-commit --no-ff upstream/main
git merge --abort

# Real merge (mechanical path)
git checkout -b chore/sync-upstream-<v>
git merge --no-ff upstream/main -m "chore(upstream): sync ..."
git push -u origin HEAD

# Verify post-merge
git fetch upstream && git rev-list --left-right --count upstream/main...origin/main
```

## Lessons learned (rolling appendix)

Each completed run of this skill should add a one-bullet entry below if the run surfaced a generalizable lesson. Don't add per-merge debugging — only patterns the *next* sync should expect.

- **2026-05-08 (v0.36.0, MECHANICAL, merge `94925cb2`, PR #17, 22 commits):** Two patterns worth banking:

  1. **`gh pr merge --auto --merge` returns success but `autoMergeRequest` polls `null` — and it still merges.** Observed on this sync (and v0.35.0 sync per memory entry 5536). The CLI exits 0, the `autoMergeRequest` query returns `null` immediately after, but GitHub still queues + lands the PR with a real merge commit (verified `git cat-file -p <merge>` shows two parents). The skill currently warns "auto-merge silently failed → manual click required" — that over-corrects. **Wait ~30–60s + check `gh pr view <N> --json mergeCommit,state` before falling back to manual.** If `state=MERGED` and `mergeCommit.oid` is non-empty, auto-merge worked even though `autoMergeRequest` reads null.

  2. **Hermes-style additive overlap on watchlist files is MECHANICAL, not COMPLEX.** Single overlap this round was `mcp/tools/execution_handlers.py` (watchlist hot-path) — upstream added `_resolve_dispatch_cwd_result` cwd validator + `startup_output_timeout_seconds=0`/`stdout_idle_timeout_seconds=0` Hermes opt-out kwargs. Both slotted next to our Q4.1 `_q41_state` mode-resolution block at the top of the file in disjoint zones; auto-merge resolved cleanly with both sides preserved. **The watchlist's "any conflict on hot-path = COMPLEX" rule should keep its narrow reading from the v0.33.0 lesson: only contradictions (rename, removal, semantic shift to a symbol we depend on) qualify. Sibling helpers added next to our extension auto-merge fine.** Verify post-merge with `grep -nE "_q41_state|<our-symbol>"` to confirm coexistence.

  Total time: ~10 minutes including probe trial, suite verify (7510 pass, ralph flake 5th occurrence — auto-skipped per the v0.34.0 lesson), PR open, auto-merge land.

- **2026-05-07 (v0.34.0 + v0.35.0, MECHANICAL, merge `43673353`, PR #13, 42 commits):** Fourth sync in a row with the **exact same `config/__init__.py` `__all__` alphabetical conflict** (this round: `get_copilot_cli_path` colliding with our `get_enable_decomposition_default`/`get_executor_model`/`get_executor_tier` block). Recurring enough to call it: **whenever upstream adds a new backend, expect both an `.env.example` runtime stanza conflict and an `__all__` insertion conflict — both purely additive, both MECHANICAL.** Resolution recipe is now mechanical: alphabetical insert in `__all__`, expand the runtime value list in `.env.example`. The pre-merge audit can predict these conflicts from the upstream commit themes alone (`feat(providers): support hermes interview driver` + `feat(copilot): add first-class Copilot CLI runtime` ⇒ both conflicts incoming). One pattern worth banking: **the test_ralph_handler flake is now a 3-sync recurring upstream signature — log it once per session and ignore subsequent occurrences during the same merge.** Flake re-confirmed on this sync (failed in full-suite run, 5/5 pass in isolation in 0.3s). Total time end-to-end: ~12 minutes including the test-suite verify on probe + PR creation. Mostly autopilot-driven.

  Also worth banking: **the probe-then-fresh-chore dance is overkill for clean mechanical syncs.** Skill currently mandates `probe/trial-merge-<v>` → abort → fresh `chore/sync-upstream-<v>` → re-resolve. For deterministic additive conflicts (the `__all__` + `.env.example` recurring pair), the resolutions are byte-identical across both branches; redoing them costs ~2 minutes and adds no safety. Cheaper alternative: commit on probe with `--no-verify` (just to verify test suite), then if MECHANICAL, `git branch -m probe/trial-merge-<v> chore/sync-upstream-<v>` and re-amend with hooks. **Did not change the skill this round — preserving the discipline for the COMPLEX path is more valuable than saving 2 minutes — but flagging for future review.**

- **2026-05-06 (post-v0.33.0 wave 2, MECHANICAL after reclassification, merge `40f77381`, PR #12, 56 commits):** Originally classified COMPLEX in autopilot based on `tests/unit/mcp/tools/test_ralph_handler.py::test_ralph_handler_returns_job_id_and_completes_loop` hanging to 30s timeout post-merge. **The failure was a load-induced upstream flake**, not a merge regression — reproduces on a pure `upstream/main` checkout (~1 in 5 full-file runs), present even though upstream commit `8fbc3853` already extended polling from 5s → 30s. Two patterns to bank: **(1) when a single test fails post-merge, run it 5+ times alone *and* run it 3+ times against pure upstream/main before classifying COMPLEX. The 0.4s-vs-30s gap I noted in the previous lesson is real, but only when reproducible** — flakes can mimic that gap on a single run. **(2) "Upstream removed code we extended" alone is not COMPLEX if auto-merge keeps our additions and the full test suite still passes.** This sync stripped 229 lines from `execution_handlers.py` (Q4.1 AC-2 `mode` parameter + `_q41_state` import + caller-wins gate) and 98 lines from `evolution_handlers.py` (helper-default indirection + strict truthy parsing). Auto-merge preserved our local diff in both files and 6727/6727 unit tests passed. Coexistence > conflict. The earlier lesson's framing — "auto-merge produces a Frankenstein when upstream removes the surface our code attaches to" — is too pessimistic; check whether the Frankenstein actually breaks anything before parking. Total time: ~15 min from autopilot park → reclassify → suite verify → PR open.

  Also worth banking: **abort + re-merge is cheaper than bisecting cross-file interactions when the per-file isolation tests pass.** I spent ~5 minutes overlaying upstream-vs-merged versions of `evolution_handlers.py` and `execution_handlers.py` to bisect the hang. Both passed in isolation. Both passed when combined. Whole-file run failed. The signal that ended the bisect was running the failing test in isolation 5 times (all passed) — at which point the flake hypothesis became cheaper than continuing to chase a non-existent merge interaction.

- **2026-05-05 (post-v0.33.0, MECHANICAL, merge commit `e2d4467a`, PR #11, 43 commits):** Third sync of the day, riding the clean v0.33.0 baseline. Two real conflicts on the trial merge — both **additive-only in sorted/disjoint zones**: (a) `.env.example` "Orchestrator / Runtime" stanza where ours and upstream each rewrote the comment block from a different angle (kept both stanzas — `OUROBOROS_AGENT_RUNTIME` primary, `OUROBOROS_RUNTIME` documented as upstream alias), (b) `src/ouroboros/config/__init__.py` `__all__` where each side appended a new export name in the same alphabetical zone. Pattern to bank: **the watchlist's "any conflict in a hot-path file = COMPLEX by default" rule should be read narrowly — purely additive conflicts in sorted symbol exports or disjoint comment stanzas qualify as MECHANICAL, even when the file is on the watchlist.** What pushes COMPLEX is upstream renaming/removing a symbol we depend on, not upstream adding a new sibling next to ours. Verify by running `grep -nE "^def "` for our local helpers post-auto-merge before promoting.

  Also worth banking: **`config/__init__.py` `__all__` conflict is now a recurring pattern across three syncs in a row.** Both forks of the project keep adding new `get_*` helpers — alphabetical merge resolution + `git diff --name-only --diff-filter=U` to confirm only this file conflicted is the standard recipe. Took ~15 minutes end-to-end including 1m33s for full unit suite (6687 tests).

- **2026-05-05 (v0.32.0 → v0.33.0, MECHANICAL, merge `4ce56e47`, 31 commits):** Back-to-back sync immediately after the v0.32.0 complex merge landed. Trial merge on `probe/trial-merge-v0.33.0` produced **zero conflicts** across the 5 overlap files (`config/{__init__,loader,models}.py`, `codex_cli_runtime.py`, `test_models.py`); test suite green on the trial state. Pattern worth banking: **once you've paid the cost of a complex sync, the next mechanical sync rides the clean baseline cheaply — don't wait, chain them.** Took ~10 minutes including PR creation, CodeRabbit, auto-merge, verification.
- **2026-05-05 (v0.31.1 → v0.32.0, COMPLEX resolved end-to-end, merge `2d2d2dc9`, 142 commits):** Three breakage flavors surfaced during the actual merge that pre-merge audit did not predict, and that future complex-merge resolution should expect:
  1. **Test stubs become brittle to additive kwargs.** Upstream-introduced unit tests carry their own minimal `_StubRuntime.execute_task` definitions that don't accept newer kwargs added by our local work (e.g., Q4.1 added `model=` to the adapter). Fix per-stub with `**_kwargs` defensive padding — not a real signature change. Expect at least one such fix per major upstream sync.
  2. **Plugin-distribution invariants collide with fork-local hooks.** Upstream may add regression tests on `.claude/settings.json` enforcing plugin-distribution shape (must use `${CLAUDE_PLUGIN_ROOT}`, must have `python3 ... || python ...` fallback, etc.). Fork-local hooks that worked in our dev workspace (using `${CLAUDE_PROJECT_DIR}` and pointing at `.claude/scripts/`) will break those tests. Standard fix: move fork-only hooks to `.claude/settings.local.json` (gitignored), keep the tracked `settings.json` byte-identical to upstream.
  3. **Upstream moves during the sync window.** v0.33.0 was cut ~2 hours after we started this merge. The post-sync `git rev-list --left-right --count upstream/main...origin/main` showed `31 22` instead of `0 22` — but every commit reachable from `v0.32.0` IS in `origin/main`, so the v0.32.0 sync is correct. Verify with `git rev-list --count v<target-tag>..origin/main` (should be > 0) and `git rev-list --count origin/main..v<target-tag>` (should be 0). The "left-right behind" count may be non-zero against the live `upstream/main` tip simply because upstream is active. Don't chase it during the same session.
- **2026-05-04 (v0.31.1 → v0.32.0, parked COMPLEX at audit time, 142 commits):** Trial merge on a `probe/` branch (not on `main`, not on `feat/`) keeps abort state out of the real branches. Issues are disabled on the fork — `gh issue create` fails with `the '<repo>' repository has disabled issues`, so tracking falls back to `docs/local/<topic>/TRACKING.md`. The `auto` workflow stack upstream is moving fast and replaced our `parent_ac_index/sub_ac_index/context_override` with a unified `ExecutionNodeIdentity`; expect more such object-of-related-things refactors in future syncs and treat them as design tasks, not hunk fixes.
- **2026-05-01 (v0.30.0 → v0.31.1, mechanical-with-care, 35 commits):** Trial-merge "no semantic conflict" was misleading — upstream reordered worker-cap resolution before seed parsing, which contradicted our Q4.1 AC-2 ordering. Always check ordering of operations, not just file-level conflicts.
