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

## Phase 0 — Scope confirmation

Before any git work, confirm with the user:

1. **Which upstream ref?** Default `upstream/main`. Confirm if the user wants a tag (e.g. `upstream/v0.32.0`) or a different branch.
2. **Target branch?** Default: integrate via a topic branch named `chore/sync-upstream-<version-or-date>`, then PR into `main`. Never merge directly to `main` without a PR.
3. **Time budget.** Ask whether they want the **fast path only** (skip if anything is non-mechanical) or the **full path** (write the issue and stop, or proceed if mechanical).

Only proceed once these three are answered.

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

When uncertain, classify **COMPLEX**. The cost of a wrong "mechanical" call is much higher than the cost of writing an issue.

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

### Step 1 — Open a tracking issue

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
4. Update `docs/handoff/` with a one-paragraph note: version synced, mechanical vs complex, what to watch for next.

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
