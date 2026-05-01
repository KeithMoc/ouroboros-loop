# Phase 2 — Q4.1: Hardening Cycle (Design)

> Status: **design approved 2026-04-28**, awaiting `ooo interview` per-AC sharpening before seed-write.
> Sibling docs: [`serial-compounding-open-questions.md`](./serial-compounding-open-questions.md), [`phase-2-q4-inline-qa-design.md`](./phase-2-q4-inline-qa-design.md), [`phase-2-q2-diff-capture-design.md`](./phase-2-q2-diff-capture-design.md), [`../guides/serial-compounding.md`](../guides/serial-compounding.md).
> Phase-2 Q4 (per-AC inline QA) shipped in `ce5ad1e` (PR #6, 2026-04-28). This is the next dogfood cycle.

## Why this exists

Q4 shipped inline QA per AC, but a P0 recon for the next cycle surfaced four open questions that were either misframed in `HANDOFF.md` or invisible until the codebase was audited at file:line level:

1. **Q4 × decomposition** — sub-ACs run sequentially within a parent AC, but Q4's QA gate sits at the parent level. The behavior is locked in by an explicit test (`test_inline_qa_decomposed_ac_parent_only`), so it was a deliberate design choice — not an unintended gap. The cycle needs to **document** the choice rather than change it, and acknowledge the inline-retry semantics on parent QA fail (vs. a deferred end-of-run sweep, deferred to Q4.2).
2. **MCP `execution_mode_required` is silently dropped** today — `SeedMetadata` (`core/seed.py:136`) is `frozen=True` with default `extra="ignore"`, so the field appears in 4 seed YAML files but is discarded during deserialization. This is the silent-defaults-to-parallel gap that hid Q2.1's bug class. The MCP `ouroboros_execute_seed` handler also defaults to `"parallel"` when neither caller arg nor seed metadata is present — which conflicts with the project's center of gravity (compounding is the better-information mode).
3. **QA-crash resume semantics are undesigned.** The Q4 checkpoint write at `serial_executor.py:1765` runs *after* the QA loop completes. A crash *during* QA (Ctrl+C is the common case, OOM/system death the rare one) leaves no checkpoint, so on resume the AC re-runs *from scratch* — agent and all — even though the agent's work was already committed. The agent then sees its prior attempt's commits as pre-existing state, and behavior is non-deterministic.
4. **Cycle-1 Q4 deferred four CodeRabbit-flagged items** in PR #6's body. Plus there is no end-of-run cost / QA / mode-conflict surface — `completion_summary` carries only `final_message`, `messages_processed`, and `_task_summary()`. The Run Summary surface needs to land alongside the deferred items so the cycle leaves a clean "where do I see what happened?" story.

This cycle bundles the four items into a hardening cycle that closes the workflow-assumption gotcha class (the Q2.1 / Q4×decomposition lesson) without expanding scope into a new feature surface.

## Decisions (locked)

| # | Question | Decision | Why |
|---|---|---|---|
| 1 | AC-1: per-sub-AC QA or parent-only? | **Parent-only + document the substrate reason** | Sub-ACs are agent-derived from parent content; they have no independent acceptance criteria; parent QA already judges the sum-of-sub-AC outcome via the cumulative diff. Per-sub-AC QA would either rubber-stamp or noise-fire. |
| 1b | AC-1: retry strategy on parent QA fail when decomposed? | **Inline retry of the whole parent (Q4 cycle-1 default)** | Re-decompose-and-rerun matches the existing per-AC retry loop. Deferred end-of-run sweep mode (`--qa-mode=defer`) is a real alternative but warrants its own cycle (Q4.2) — it adds a queue, sweep dispatcher, and ordering rules that compound with `--max-qa-retries` in confusing ways. |
| 2 | AC-2: caller-vs-seed-metadata conflict policy? | **Caller wins, log warning + emit structured `mode_conflict` event** | Surfaces conflicts without breaking callers (the Q2.1 lesson — silent mode-mismatches are the worst class of gap). Pairs with AC-4's Run Summary surface so users see the conflict count at run end. |
| 2b | AC-2: transition strategy from parallel-default to compounding-default? | **Soft-flip with one-time deprecation warning** | Compounding is the project's center of gravity (better-information mode) and the right global default. A one-time per-session warning gives external MCP callers a release cycle to annotate seeds or pass `mode` explicitly without breaking them today. |
| 3 | AC-3: process-death-during-QA recovery model? | **QA-pending sentinel** | `qa_status="pending"` extends the existing finite-state field (clean schema fit). Phase-1 checkpoint preserves the agent's already-committed work; on resume, only QA replays against the same `final_message + diff_summary` artifact. Idempotent. Avoids re-paying agent cost for a QA-only crash — the "Ctrl+C mid-QA is common" reality. |
| 4 | AC-4: cost / QA / mode-conflict observability surface? | **Extend `completion_summary` dict + new "Run Summary" panel printed below the existing completion panel** | Single end-of-run UX surface for cost, QA stats, invariant counts, and AC-2's conflict count. Panel only prints when there's something to show — non-compounding, non-inline-QA runs are byte-identical to today. Data flows through `completion_summary` so it lands in event store + session repo, not just console. |
| 4a | AC-4: `run_inline_qa` serialization unify approach? | **Round-trip via `_serialize_qa_verdict`** (with normalizer fallback at implementation if dataclass schema can't absorb meta keys) | Single source of truth for verdict shape. Implementation-time fallback to `_normalize_qa_meta(meta) -> dict` is acceptable if the dataclass's `extra="forbid"` rejects meta keys. |

**Pre-decided in master brainstorm + handoff:**
- Bundle the four items into one cycle (not four separate ones) — the deferred CodeRabbit items are too small to justify their own cycles, AC-1 is documentation-only, AC-2 is small, AC-3 is the only substantial code change.
- `ooo interview` runs after this design lock and before seed-write, per the agreed three-pass procedure being trialled on this cycle.

## Mechanism

### AC-1 — Parent-only QA + design rationale documentation

**Code-flow change:** none.

**Documentation change:** add a docstring block immediately above the QA call at `serial_executor.py:~1516` (the `if not inline_qa or result.outcome != ACExecutionOutcome.SUCCEEDED: break` line) explaining:
1. QA fires at the parent AC level only — sub-ACs created by `_try_decompose_ac` are not independently QA'd.
2. Substrate reason: sub-ACs are agent-derived from the parent's content; the seed provides acceptance criteria only at the parent AC level. A sub-AC QA judge would have no crisp pass/fail bar.
3. Sub-AC work flows into the parent's diff (via the commit-per-AC pattern) and into the parent's invariants (via the Q3 sub-postmortem flatten path). The parent's QA verdict therefore judges the sum-of-sub-AC outcome.
4. Inline-retry on parent QA fail re-decomposes the whole parent — Q4 cycle-1 default. Deferred end-of-run sweep mode is tracked as Q4.2.

**Test surface:** rename `test_inline_qa_decomposed_ac_parent_only` → `test_inline_qa_parent_only_is_intentional` and add a docstring referencing this design doc, so the next reader doesn't read the test as a "TODO: extend to sub-ACs."

**LOC:** ~10 (docstring + comment + test rename).

### AC-2 — MCP `execution_mode_required` honoring + soft-flip default

**Schema change** (`core/seed.py:136`):

```python
class SeedMetadata(BaseModel, frozen=True):
    seed_id: str = Field(default_factory=lambda: f"seed_{uuid4().hex[:12]}")
    version: str = Field(default="1.0.0")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ambiguity_score: float = Field(default=0.15, ge=0.0, le=1.0)
    interview_id: str | None = Field(default=None)
    parent_seed_id: str | None = Field(default=None)
    execution_mode_required: Literal["parallel", "compounding"] | None = Field(
        default=None,
        description="Mode the seed expects. None = no preference.",
    )
```

Old YAML files (without the field) hydrate as `None` — backward-compatible. The 4 existing seed YAMLs that already have `execution_mode_required: "compounding"` start being honored.

**Handler logic** (`mcp/tools/execution_handlers.py`):

> **P1 finding C — handler ordering:** today the handler reads `mode` at line 286, *before* `seed = Seed.from_dict(seed_dict)` at line 348. AC-2's design depends on `seed.metadata.execution_mode_required` being available at the time mode is resolved. **The mode resolution block must move below the seed-parsing line** (after line 349). The early-return on invalid mode therefore also moves later — must come before any execution dispatch but after seed validation.

```python
# (already exists at line ~348:)
seed_dict = yaml.safe_load(seed_content)
seed = Seed.from_dict(seed_dict)

# NEW: mode resolution AFTER seed parsing
caller_mode = arguments.get("mode")  # may be None
seed_mode = seed.metadata.execution_mode_required  # may be None

if caller_mode is not None:
    execution_mode = caller_mode
    if seed_mode is not None and caller_mode != seed_mode:
        log.warning(
            "mcp.execute_seed.mode_override",
            caller=caller_mode,
            seed=seed_mode,
            session_id=session_id,
        )
        await event_store.append(
            create_mode_conflict_event(
                session_id=session_id,
                caller_mode=caller_mode,
                seed_mode=seed_mode,
                seed_id=seed.metadata.seed_id,  # interview-derived: groups conflicts across sessions
                timestamp=datetime.now(UTC),    # interview-derived: explicit, not row-derived
            )
        )
        # P1-derived: increment session-scoped counter for AC-4 Run Summary
        _mode_conflict_counter[session_id] = _mode_conflict_counter.get(session_id, 0) + 1
elif seed_mode is not None:
    execution_mode = seed_mode
else:
    execution_mode = "compounding"  # NEW global default
    if not _SOFT_FLIP_WARNED_THIS_SESSION:
        log.warning(
            "mcp.execute_seed.default_flipped",
            message=(
                "No mode specified — defaulting to 'compounding' "
                "(was 'parallel' before vX.Y). "
                "Set mode='parallel' or metadata.execution_mode_required='parallel' "
                "to silence this warning."
            ),
        )
        _SOFT_FLIP_WARNED_THIS_SESSION = True

if execution_mode not in {"parallel", "compounding"}:
    return Result.err(MCPToolError(...))
```

The soft-flip flag is a module-level boolean, reset only on process restart — gives one warning per session per process. The mode-conflict counter is a separate module-level dict keyed by `session_id` — read by AC-4 at run end (no event-store query needed; cheaper and fewer dependencies).

**Failure mode:** never blocks the call. Warnings best-effort; event-store write failure caught and logged.

**Test surface** (new file or extend existing `test_execution_handlers.py`):
- Unit: 5 conflict cases — caller-only, seed-only, both-agree, both-disagree, neither.
- Unit: deprecation warning fires once per session.
- Integration: seed YAML round-trips `execution_mode_required` through `Seed.parse(...)`; old YAMLs without the field hydrate with `None`.

**LOC:** ~50 (schema 2, handler 12, soft-flip flag + warn 8, conflict event factory 8, tests ~20).

### AC-3 — QA-pending sentinel resume

**Schema extension** (`level_context.py`, `ACPostmortem`):

```python
# qa_status enum extended:
qa_status: str | None = None
# enum: None | "pending" | "passed" | "exhausted" | "skipped_delegated" | "skipped_error"
#               ^^^^^^^^^ NEW (Q4.1 / AC-3)

# NEW field (P1 finding A): ACPostmortem doesn't currently carry final_message,
# but the QA-replay branch needs it to call run_inline_qa(... final_message=...).
final_message: str | None = None
```

Forward-compatible: old serialized chains hydrate via existing `d.get(...)` defaults for both fields. The `final_message` field is populated at `serial_executor.py` post-`_execute_single_ac` from `result.final_message`.

**Write sequence in `serial_executor.py`:**

```text
[existing]
  result = await self._execute_single_ac(...)
  postmortem = self._build_postmortem_from_result(result, diff_summary=diff_summary)

[NEW — phase-1 checkpoint write, AC-3]
  if inline_qa and result.outcome == ACExecutionOutcome.SUCCEEDED and self._checkpoint_store:
      pending_postmortem = postmortem.with_qa_status("pending")
      pending_chain = chain.append(pending_postmortem)
      _write_compounding_checkpoint(
          store=self._checkpoint_store,
          seed_id=seed.metadata.seed_id,
          session_id=session_id,
          ac_index=ac_index,
          chain=pending_chain,
      )

[existing — QA loop]
  if not inline_qa or result.outcome != ACExecutionOutcome.SUCCEEDED:
      break
  inline_qa_outcome = await run_inline_qa(...)
  postmortem = ...with terminal qa_status...

[existing — phase-2 checkpoint write at line 1765]
  # now an UPDATE in place — overwrites the pending checkpoint with the
  # terminal-state chain. Same site, same call, no API change.
```

**Resume path** (`runner.py` `--resume` rehydration):

```text
last_pm = chain.entries[-1] if chain.entries else None
if last_pm is not None and last_pm.qa_status == "pending":
    # Phase-1 checkpoint without phase-2 write — agent succeeded but QA died.
    # Skip the agent; replay QA only against the persisted artifact.
    inline_qa_outcome = await run_inline_qa(
        qa_handler,
        postmortem=last_pm,
        ac_index=last_pm_index,
        ac_content=ac_at_index_text,
        seed=seed,
        qa_session_id=f"qa-replay-ac{last_pm_index}-{session_id[:8]}",
        final_message=last_pm.summary.final_message,
        iteration_history=None,  # fresh attempt
    )
    finalized_pm = _finalize_pm_from_qa_outcome(last_pm, inline_qa_outcome)
    chain = chain.replace_last(finalized_pm)
    _write_compounding_checkpoint(... chain ...)
    # then continue with next AC as usual
```

**Edge cases:**
- **QA-replay itself dies again** → next resume sees `pending` again → re-replays. Idempotent: artifact is fixed.
- **`--inline-qa` was off when *original* run started** → `qa_status` never set to pending → resume path is unchanged.
- **`--inline-qa` was on for original run but off on resume** *(P1-derived edge case)* → resume sees `qa_status="pending"` but `inline_qa=False`. **Decided semantic:** skip QA replay, mark `qa_status=None`, treat AC as terminal, log a warning (`"resume.qa_replay_skipped: --inline-qa not set on resume; AC marked terminal without QA verdict"`). User opted out of QA on this run — respect it. ~5 LOC.
- **Decomposed AC** → phase-1 write happens after parent's `_execute_single_ac` returns; sub-postmortems are already part of the postmortem at that point. Same flow.
- **Checkpoint store unavailable during phase-1 write** → log warning, continue without sentinel (degrades to status quo / pre-Q4.1 behavior — agent re-runs on resume).
- **Checkpoint overwrite semantics** *(P1 finding B — verified)* → `CheckpointStore.save()` writes by `seed_id` only (`persistence/checkpoint.py:356`), `load(seed_id)` reads. So phase-2 update IS overwrite-in-place — no API change needed. The phase-1 and phase-2 writes go through the same `_write_compounding_checkpoint` call site, just with different `qa_status` baked into the chain.

**QA-replay event-stream + audit-diff sidecar** *(interview-derived):*

The QA-replay branch invokes `run_inline_qa` again, which emits a new `ac_qa_evaluated` event under the **same** `qa_session_id` namespace as the original attempt (`f"qa-ac{ac_index}-{session_id[:8]}"`) — the original verdict event is silently superseded in the live event stream. Audit trail is preserved out-of-band via a sidecar diff file:

- **Path:** `<OUROBOROS_CHAIN_ARTIFACT_DIR>/chain-<session_id>-ac<N>-qa.original.diff` (paired with the existing `chain-<session>.md` artifact; covered by the autouse pytest fixture).
- **Format:** unified git-diff (via Python's `difflib.unified_diff`) showing original verdict_dict → replay verdict_dict, JSON-serialized for stable line-by-line comparison.
- **Multi-replay behavior:** when a session crashes mid-QA two or more times, each successful replay **appends** a new `### Replay N (timestamp)` section to the same diff file. Full audit history preserved — every overwrite recorded.

Example multi-replay layout:
```
### Replay 1 (2026-04-28 14:22)
--- original
+++ replay
@@ -1,3 +1,3 @@
 verdict: pass → revise
 score: 0.85 → 0.62
 ...

### Replay 2 (2026-04-28 14:31)
--- original (post-replay-1)
+++ replay
@@ ...
```

**LOC delta:** ~25 LOC for the diff render + append-write logic. AC-3 LOC budget bumps from ~100 to ~125.

**Test surface:**
- Integration: simulate phase-1-checkpoint-then-crash → resume reads `pending` → QA replays → terminal status persisted → next AC proceeds.
- Unit: resume path branches on `qa_status == "pending"`.
- Unit: phase-1 checkpoint contains the pending postmortem.
- Unit: idempotent re-replay (two crashes in a row).

**LOC:** ~125 (sentinel write 15, resume detection + replay branch 35, schema enum 1, `final_message` field 5, audit-diff render + append 25, helpers + finalization 10, tests ~35).

### AC-4 — Deferred CodeRabbit items + Run Summary observability

**Item 1** (`run.py`: `ctx.get_parameter_source` for `--max-qa-retries`):

Today's warning at `run.py:889-893` fires whenever `max_qa_retries != 1 and not inline_qa` — including when `max_qa_retries=1` is the default and the user passed nothing. Fix: only warn on *explicit* user override.

```python
if (
    ctx.get_parameter_source("max_qa_retries") == ParameterSource.COMMANDLINE
    and max_qa_retries != 1
    and not inline_qa
):
    log.warning(...)
```

~10 LOC.

**Item 2** (`runner.py`: always forward `max_qa_retries`):

Move `parallel_kwargs["max_qa_retries"] = max_qa_retries` outside the `if inline_qa:` block at `runner.py:1810`. The two flags are independent. ~1 LOC.

**Item 3** (`serial_executor.py`: render suggestions outside fence):

Currently suggestions at `serial_executor.py:400-404` are wrapped in a fenced code block, preventing Markdown bullet rendering. Restructure so each suggestion renders as `- {sug}` outside the fence. ~5 LOC.

**Item 4** (`inline_qa.py`: unify `run_inline_qa` serialization through `_serialize_qa_verdict`):

```python
# Today (inline_qa.py:~165):
verdict_dict = meta  # raw dict from MCPToolResult.meta

# After (4a — round-trip via serializer):
verdict = QAVerdict(**_meta_to_qaverdict_kwargs(meta))
verdict_dict = _serialize_qa_verdict(verdict)

# Fallback (4b — if QAVerdict can't absorb meta keys):
verdict_dict = _normalize_qa_meta(meta)
```

Implementation chooses 4a if the dataclass schema cleanly absorbs meta keys; 4b otherwise. Either way, `run_inline_qa` returns a verdict_dict with the same shape `_serialize_qa_verdict` produces — single source of truth at the call-site interface. ~30 LOC + tests.

**Run Summary panel + `completion_summary` extension** (`runner.py:2091-2114`):

```python
# Aggregate QA stats from chain
qa_calls = sum(pm.qa_attempts for pm in chain.entries)
qa_verdicts = {
    "passed": sum(1 for pm in chain.entries if pm.qa_status == "passed"),
    "exhausted": sum(1 for pm in chain.entries if pm.qa_status == "exhausted"),
    "skipped_error": sum(1 for pm in chain.entries if pm.qa_status == "skipped_error"),
    "skipped_delegated": sum(1 for pm in chain.entries if pm.qa_status == "skipped_delegated"),
}
invariants_above_threshold = sum(
    1 for pm in chain.entries
    for inv in pm.invariants_established
    if not inv.is_contradicted and inv.reliability >= _get_min_reliability()
)
# Mode conflicts read from session-scoped counter (P1 finding E):
# EventStore has no count() method — use a module-level counter incremented at
# the AC-2 conflict-warning site and drained here. Avoids an end-of-run query
# and keeps the dependency surface small.
mode_conflicts = _mode_conflict_counter.pop(session_id, 0)

completion_summary = {
    "final_message": final_message[:500],
    "messages_processed": messages_processed,
    "cost_usd": progress_data.get("estimated_cost_usd", 0.0),
    "qa_calls": qa_calls,
    "qa_verdicts": qa_verdicts,
    "invariants_above_threshold": invariants_above_threshold,
    "mode_conflicts": mode_conflicts,
    **self._task_summary(),
}

# Existing completion panel (unchanged)
self._console.print(Panel(Text(final_message[:1000], style="green"), ...))

# NEW: Run Summary panel — print when compounding mode OR --inline-qa OR any non-zero stat.
# Interview-derived: ALWAYS show all 5 rows (consistent panel shape, easier grep/parse).
panel_visible = (
    is_compounding_mode
    or inline_qa
    or any([cost_usd > 0, qa_calls > 0, mode_conflicts > 0, invariants_above_threshold > 0])
)
if panel_visible:
    summary_lines = [
        f"Cost:                ${cost_usd:.2f} ({len(chain.entries)} ACs)",
        f"QA calls:            {qa_calls} across {len(chain.entries)} ACs",
        f"QA verdicts:         {qa_verdicts['passed']} passed, "
        f"{qa_verdicts['exhausted']} exhausted, "
        f"{qa_verdicts['skipped_error']} errors",
        f"Invariants captured: {invariants_above_threshold} above threshold "
        f"{_get_min_reliability()}",
        f"Mode conflicts:      {mode_conflicts}",  # always shown, zero-explicit
    ]
    self._console.print(
        Panel(
            Text("\n".join(summary_lines), style="cyan"),
            title="[cyan]Run Summary[/cyan]",
            border_style="cyan",
        )
    )
```

**Failure mode:** panel print failure never aborts; `completion_summary` fields default to safe values (0 cost, empty distribution).

**Test surface:**
- Unit: `_serialize_qa_verdict` round-trip via meta (item 4).
- Unit: `--max-qa-retries` warning fires only on explicit override (item 1).
- Unit: completion_summary dict contains all 5 new keys.
- Unit: Run Summary panel render under each visibility condition (cost only, QA only, conflicts only, all-zero → no panel).
- Integration: 2-AC compounding cycle with `--inline-qa` produces a `completion_summary` with non-zero qa_calls + cost.

**LOC:** ~170 total.

## Implementation order

| AC | Scope | LOC | Dependencies |
|---|---|---|---|
| **1** | AC-1 — docstring + comment + test rename | ~10 | None |
| **2** | AC-2 — schema, handler, soft-flip warn, conflict event | ~50 | None |
| **3** | AC-3 — phase-1 checkpoint, resume QA-replay branch, schema enum, audit-diff sidecar | ~125 | Independent of AC-2 |
| **4** | AC-4 — items 1–4 + completion_summary + Run Summary panel (always-show-5) | ~170 | Consumes AC-2's conflict event, AC-3's pending state visibility (via qa_status field) |

Total: ~355 LOC across ~10 files. (Was ~330 pre-interview; bumped by AC-3 audit-diff sidecar derived from the interview.)

## Cross-AC schema deltas

| File | Change | Type |
|---|---|---|
| `core/seed.py:136` (`SeedMetadata`) | Add `execution_mode_required: Literal["parallel", "compounding"] \| None = None` | Additive, backward-compat |
| `level_context.py` (`ACPostmortem.qa_status`) | Extend enum with `"pending"` | Additive, backward-compat |
| `level_context.py` (`ACPostmortem.final_message`) *(P1-derived)* | New field `final_message: str \| None = None` — needed by AC-3 QA-replay path | Additive, backward-compat |
| `completion_summary` dict | Add `cost_usd`, `qa_calls`, `qa_verdicts`, `invariants_above_threshold`, `mode_conflicts` | Additive |
| `events.py` | New factory `create_mode_conflict_event` | New |
| `mcp/tools/execution_handlers.py` | Module-level `_mode_conflict_counter: dict[str, int]` and `_SOFT_FLIP_WARNED_THIS_SESSION: bool` | New |

## Dogfood success criteria

For `ooo evaluate <session_id>`:

| Criterion | How we check |
|---|---|
| AC-1 docstring at QA call site references the substrate reason and inline-retry semantics | `git diff` shows the addition; manual read |
| AC-1 test renamed and includes design-rationale docstring | `git diff` shows the rename + docstring |
| AC-2 conflict warning fires when caller passes `mode="parallel"` against a `compounding` seed | Manual test via MCP tool call; conflict event in event store |
| AC-2 soft-flip deprecation warning fires exactly once per session | Inspect log output of two MCP calls in one session |
| AC-2 old seed YAML (without metadata field) hydrates with `execution_mode_required=None` | Unit test passes |
| AC-3 simulated Ctrl+C between phase-1 and phase-2 checkpoint → `ooo run --resume` → QA replays → terminal status persisted | Integration test passes |
| AC-3 idempotent re-replay (crash twice in a row) → terminal state still reached | Integration test passes |
| AC-4 Run Summary panel renders during the dogfood run itself with non-zero cost + qa_calls | Manual review of dogfood-run console output |
| AC-4 `completion_summary` contains all 5 new keys in the persisted session record | `ouroboros_query_events` after run end |
| Existing 5699-test suite stays green; new tests land per AC | `uv run pytest tests/unit -q` |

## Failure modes / edge cases

- **Checkpoint store down during AC-3 phase-1 write** → log warning, skip the sentinel, fall through to status-quo behavior (agent re-runs on resume). Best-effort; the sentinel is a recovery optimization, not a correctness boundary.
- **AC-2 event-store write failure for conflict event** → warning still emits; user still sees the surface. Conflict count in Run Summary may under-report; `await event_store.append` failures are already caught by `_safe_emit_event`-style wrappers in the codebase.
- **AC-4 cost data missing from `progress_data`** → default to `0.0`. Run Summary panel still renders if any other condition (QA calls, conflicts, invariants) is non-zero.
- **AC-3 resume-replay produces a different verdict than the original would have** → acceptable; QA judges are nondeterministic by design. The verdict is approximately stable because the artifact (`final_message + diff_summary`) is fixed.
- **Soft-flip warning suppression flag (`_SOFT_FLIP_WARNED_THIS_SESSION`) racy under concurrent MCP calls** → benign race; worst case the warning prints twice. Not a correctness issue.

## Deferred to later cycles

- **Q4.2 — End-of-run QA sweep mode** (`--qa-mode=defer`). AC-by-AC soft-pass on QA fail, accumulate failed-QA verdicts in a queue, dispatch a sweep at end of run. Real ~250 LOC mode change with queue persistence, sweep dispatcher, ordering rules. Worth its own design + dogfood cycle.
- **Q5 — `ooo evolve` integration**: end-of-run hint only (option B from master brainstorm). Cheap; can land any time.
- **Phase-2 prompt caching**: blocked by Claude Code subscription runtime; see memory `prompt_caching_blocked.md`.
- **Pre-v0.30 checkpoint migration test** (HANDOFF gap #6): low priority unless someone surfaces a real broken resume.

## References

- Master brainstorm Q-list: [`serial-compounding-open-questions.md`](./serial-compounding-open-questions.md)
- Q4 design (preceding cycle): [`phase-2-q4-inline-qa-design.md`](./phase-2-q4-inline-qa-design.md)
- Q2 design + Q2.1 hotfix (workflow-assumption gotcha lesson): [`phase-2-q2-diff-capture-design.md`](./phase-2-q2-diff-capture-design.md)
- HANDOFF (cycle status): [`../../HANDOFF.md`](../../HANDOFF.md)
- Q4.1 P0 recon: this conversation's recon block (the four material shifts) — to be lifted into commit message of the design-doc commit for traceability.

## P0/P1/P2 procedure note

This is the first cycle trialling the three-pass pre-brainstorm/interview procedure:

- **P0 (recon)** ran before brainstorming — surfaced 4 material shifts vs. master brainstorm-doc:
  1. AC-1 already-decided-and-tested → rescope to docs.
  2. AC-2 needs Pydantic schema extension (LOC estimate bumped 30 → 50).
  3. AC-3's actual design choice is QA-crash agent re-run handling, not checkpoint placement (Explore agent's initial claim was wrong — verified via direct file read, the kind of P2-style check P0 borrowed).
  4. AC-4 item 4 has the only architectural choice; rest mechanical.
- **P1 (edge-case mining)** ran after the initial spec commit. Surfaced 5 findings, 3 of which required spec adjustments:
  1. `final_message` is not on `ACPostmortem` today → AC-3 also extends the schema with `final_message: str | None`.
  2. `CheckpointStore` is single-row-per-seed_id with overwrite-in-place semantics → AC-3's two-phase-write model fits cleanly with no API change (good news, no spec delta).
  3. MCP handler reads `mode` *before* parsing the seed → AC-2's mode-resolution block must be relocated below the seed-parse line.
  4. No existing `qa_status` branch in resume path → AC-3 adds a new control-flow branch (already in the LOC budget).
  5. `EventStore` has no `count()` method → AC-4's `mode_conflicts` source flips to a session-scoped module-level counter incremented at the AC-2 conflict-warning site (cheaper, fewer dependencies).
- **P2 (test-surface verification)** to run during the interview, per AC.

Procedure success will be evaluated post-cycle: did the three-pass approach surface gotchas worth its ~60-min overhead? Running tally:
- P0 alone justified itself (3 of 4 ACs rescoped before brainstorming).
- P1 then caught the AC-2 ordering gotcha and the missing `ACPostmortem.final_message` field — both would have surfaced as failed integration tests during seed execution otherwise.
- The interview (Path B fallback — MCP `ouroboros_interview` errored with `No module named 'ouroboros.events.interview'`, packaging gap between local repo and installed `ouroboros-ai`) added the AC-3 audit-diff sidecar design (genuinely creative — preserves verdict-supersede audit trail through replays), the AC-4 always-show-5-row panel decision, and the AC-2 conflict-event payload shape. These are sharper than what brainstorming alone produced.
