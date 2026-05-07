# Serial Compounding Execution — Design Notes

> Status: phase 1, 1.5, and phase-2 milestones M5/M7 all shipped.
> Phase-2 prompt caching remains deferred (adapter migration blocker).
> This guide captures the brainstorming, audit findings, and design decisions
> behind the compounding-execution loop, plus the remaining deferred work.

## TL;DR

A new **`mode="compounding"`** execution path runs acceptance criteria strictly
one at a time. Each AC carries forward a rolling **postmortem chain** — a
curated record of what every prior AC did, touched, and established — into its
prompt. Fresh SDK session per AC for focus; shared system prefix (including a
pinned `CLAUDE.md` snapshot) for eventual prompt-cache affinity; fail-fast on
unrecoverable errors, matching "atomic" semantics.

Parallel mode is untouched — the implementation is strictly additive.

```bash
ouroboros run workflow seed.yaml --compounding
```

```jsonc
// MCP
{ "tool": "ouroboros_execute_seed", "arguments": { "seed_content": "...", "mode": "compounding" } }
```

---

## Why this exists

The gap from the original brainstorm:

> Each AC is largely a cold start with only the seed + a short level summary.
> There's no concept of an "AC postmortem" artifact written by AC-N, read by AC-(N+1).
> CLAUDE.md isn't explicitly included — only picked up by Claude Code's own auto-load,
> which doesn't apply to fresh SDK sessions.
> The immutable seed + ontology are shared by re-serialization into every prompt,
> not via cache or a shared system block.

What the parallel executor gives you today is per-**level** context (a
summary of all ACs in level N, passed to level N+1). That is not compounding:
it's level-scoped, it's coarse, and it doesn't carry gotchas from failed
retries, QA signal, or invariants established. Each AC within a level still
starts cold on its siblings.

Compounding engineering — AC-N explicitly building on AC-(N-1)'s diff + trace
+ invariants + failures — is a different primitive. This track adds it without
changing the parallel path.

---

## How to use it

### CLI

```bash
# New in phase 1
ouroboros run workflow seed.yaml --compounding

# Legacy flags still work, mean what they meant before
ouroboros run workflow seed.yaml                 # parallel (default)
ouroboros run workflow seed.yaml --sequential    # parallel executor disabled
                                                 # (one-big-session degenerate path)

# Mutually exclusive with --sequential
ouroboros run workflow seed.yaml --compounding --sequential   # ERROR, exit 1
```

### MCP

```json
{
  "tool": "ouroboros_execute_seed",
  "arguments": {
    "seed_path": "seed.yaml",
    "mode": "compounding"
  }
}
```

Omitting `mode` is identical to `"parallel"` — backward compatible.

### Env overrides for postmortem rendering

| Variable | Default | Effect |
|---|---|---|
| `OUROBOROS_POSTMORTEM_FULL_K` | `3` | Number of most-recent postmortems rendered in full form; older render as one-line digests |
| `OUROBOROS_POSTMORTEM_TOKEN_BUDGET` | `8000` | Approximate token budget for the chain section; oldest digests drop first under pressure, full forms + cumulative invariants always preserved |

---

## What we found in the current codebase (audit)

Done before writing any code, with file:line refs. Four of these changed the plan.

| # | Finding | Implication |
|---|---|---|
| 1 | `runner.py:1563` — when `parallel=False`, the fall-through path sends **the whole seed's ACs in one `execute_task()` call**, not a per-AC loop. | Existing `--sequential` flag is not compounding and never was. The primitive didn't exist in either mode. |
| 2 | `events/base.py:47-59` — `sanitize_event_data_for_persistence` strips only `raw_*` / `subscribed_*` MCP wrapper keys. `tool_input` is **preserved**. | Files touched by Write/Edit ARE reconstructible from events. Initial exploration agent was wrong about this; verified via code read. |
| 3 | `level_context.py:175-195` — `ACContextSummary` already carries `files_modified, tools_used, key_output, public_api` derived deterministically from events. | The postmortem primitive is ~70% already there; composition over this beats greenfield extraction. |
| 4 | `claude_code_adapter.py` uses the subprocess-based `claude_agent_sdk`, not raw `anthropic`. System prompt is passed as a flat string via `ClaudeAgentOptions.system_prompt`. No `cache_control` surface exposed. | Prompt caching with ephemeral breakpoints is a deeper refactor than a flag flip — defer to phase 2. |
| 5 | `parallel_executor.py:2111-3300` — `_execute_single_ac` is ~1150 lines, with runtime-handle memoization, stall-timeout cancel scopes, recovery-discontinuity events, executor-model routing, etc. | Extracting it into a shared module in phase 1 would be the single biggest regression risk. Subclass-first approach dominates. |
| 6 | `parallel_executor.py:2898` — `_execute_atomic_ac` (not `_execute_single_ac`) is where the prompt is actually assembled. Level context is injected at `2946` via `build_context_prompt(level_contexts)`. | The minimal injection hook is one kwarg on `_execute_atomic_ac`, threaded through from `_execute_single_ac`. |
| 7 | `checkpoint.py:28-75` — `CheckpointData.state: dict[str, Any]` is schema-flexible. | AC-granular checkpoint can piggyback on the existing shape without migration. |
| 8 | `mcp/tools/qa.py:84-93` — `QAVerdict(score, verdict, dimensions, differences, suggestions, reasoning)` is structured and already used by the MCP QA tool, but **not** called inline from the executor. | Inline-QA-for-compounding is a simple wiring job (M7), not a design problem. |
| 9 | `core/seed.py:155-252` — `Seed` is Pydantic `frozen=True`. Ontology is a field on Seed. | Safe to treat as an immutable input; no drift guards needed. |
| 10 | CLAUDE.md is never read by any Ouroboros code in the prompt-assembly path. Only Claude CLI's auto-load handles it — which doesn't apply to fresh SDK sessions. | Need explicit read-and-pin, done once at run start, to avoid mid-run drift. |

---

## Design decisions (and why)

### Composition, not inheritance, for `ACPostmortem`

`ACPostmortem` **contains** an `ACContextSummary` (`level_context.py`) rather
than extending it. The parallel executor still builds `ACContextSummary` and
serializes it into checkpoints. Extending that class with postmortem-only
fields (gotchas, qa_suggestions, invariants) would inflate every parallel
checkpoint and leak semantics across modes. Composition keeps the parallel
path byte-identical.

### Subclass, don't extract

`SerialCompoundingExecutor(ParallelACExecutor)` reuses `_execute_single_ac` /
`_execute_atomic_ac` via a single new kwarg: `context_override: str | None`.
Default `None` → behavior is byte-identical to pre-change. Set by the serial
executor → the rolling-chain string replaces `build_context_prompt(level_contexts)`.
All 44 parallel-executor tests stay green. Total surface touched in
`parallel_executor.py`: three signature additions + one conditional.

The Plan agent proposed extracting `_execute_single_ac` into a shared module.
That extraction is ~1150 lines and touches runtime-handle memoization keys,
stall-timeout cancel scopes, executor-model routing, and recovery event
emission. The phase-1 cost is not justified; revisit only if a third executor
variant shows up.

### Fail-fast by default, flag for fail-forward

`fail_fast=True` on `execute_serial`. The user's original framing —
"atomically run, each compound over each other" — implies all-or-nothing
semantics. When an AC fails after retries, the loop halts and records the
remainder as `blocked`. The postmortem chain up to that point is preserved
and returned, so the user can review and resume (once M6 ships).

`fail_fast=False` keeps going with a failed postmortem visible to downstream
ACs. Useful for "best-effort sweep" runs. Not exposed on the CLI yet — add
`--no-fail-fast` when a use case demands it.

### CLAUDE.md pinned once per run

`build_system_prompt(include_claude_md=True, workspace_root=...)` reads
CLAUDE.md once, bounds it at 10KB, and prepends a
`## Project Guidance (CLAUDE.md)` section. Snapshot-once semantics are
important: if the user edits CLAUDE.md mid-run, later ACs should still see
the same content as earlier ACs (prompt-cache stability + reproducibility).
Default `include_claude_md=False` so parallel mode stays byte-identical —
guarded by `test_claude_md_disabled_by_default_preserves_prompt`.

### Events, not just logs

`execution.ac.postmortem.captured` is a first-class event type, keyed on
`ac_id` + `retry_attempt`. Two reasons:
1. Dual-source resume: checkpoint (fast path) falls back to event replay.
2. TUI + observability consumers can subscribe without special-casing.

The factory reuses `serialize_postmortem_chain` so a single postmortem and a
chain share one serialization path — no divergent encoders.

### `mode` parameter at every layer

CLI (`--compounding`), MCP (`mode: "parallel"|"compounding"`), runner
(`execute_seed(mode=...)`). The legacy `parallel: bool` kwarg is preserved
and interpreted as mode derivation when `mode=None`, so every internal
caller keeps working.

### Prompt caching explicitly deferred

The ~$-saving move (structured system blocks with `cache_control: ephemeral`
breakpoints on CLAUDE.md + seed + ontology) requires migrating the Claude
Code adapter off the subprocess `claude_agent_sdk` onto raw `anthropic`, or
waiting for the Agent SDK to expose structured system blocks. Neither is
phase-1 work. The serial executor is architected to benefit from it later
without further refactor — the system_prompt is built once per run and
reused across ACs.

---

## What shipped

| Commit | Scope | Files |
|---|---|---|
| `3deed1b` | M1 Primitives | `level_context.py` (+312), `test_level_context.py` (+22 tests) |
| `570d9e8` | M3 + M4 + M8 | `serial_executor.py` (new), `events.py` (+factory), `runner.py` (CLAUDE.md kwargs), `parallel_executor.py` (context_override plumbing), tests (+15) |
| `933a816` | M9 | `run.py` (`--compounding`), `execution_handlers.py` (`mode` param), `runner.py` (dispatch), tests (+6) |
| `79a3d5b` | chore | `.gitignore` for `.ouroboros_eval_artifact.md` |

**Test status:** 4918 unit tests pass, 2 skipped (pre-existing).

**Invariants held:**
- Parallel mode prompt is byte-identical to pre-change when `mode != "compounding"` and `include_claude_md=False`.
- `context_override=None` preserves every parallel-mode behavior.
- `ACPostmortem` round-trips via `serialize/deserialize_postmortem_chain` with tolerant field handling.
- Postmortem chain rendering is deterministic given the same inputs.
- Cumulative invariants are deduplicated in insertion order.
- Under token-budget pressure, full forms and invariants are never dropped.

---

## Status of phase-1.5 / phase-2 milestones

| Milestone | Status | Where it shipped |
|---|---|---|
| M5 per-AC diff capture | ✅ Shipped | `src/ouroboros/orchestrator/diff_capture.py` — `git stash create` boundary, 5s timeout, fallback to `git rev-parse HEAD` on clean tree, empty-on-error invariant. SerialCompoundingExecutor-only so parallel mode stays byte-identical. |
| M6 AC-granular checkpoint/resume | ✅ Shipped (Q6.2) | `serial_executor.py` — `CompoundingCheckpointState.last_completed_ac_index`, partial sub-AC checkpoint, `--compounding --resume <id>` with `_validate_compounding_resume_not_fresh_seed` guard. |
| M7 inline QA + retry-with-QA-feedback | ✅ Shipped | `src/ouroboros/orchestrator/inline_qa.py` — `run_inline_qa` wires `QAHandler` after each AC. CLI: `--inline-qa` and `--max-qa-retries` (default 1). |
| Invariants extraction (Q3, C-plus) | ✅ Shipped | `level_context.py` — `_INVARIANT_TAG_RE` parses `[[INVARIANT: ...]]` tags, `Invariant` dataclass, reliability gate (`POSTMORTEM_DEFAULT_INVARIANT_MIN_RELIABILITY=0.7`, env `OUROBOROS_INVARIANT_MIN_RELIABILITY`), Haiku verifier. |
| Q7 budget-overflow event | ✅ Shipped | `events.py` — `create_postmortem_chain_truncated_event` (`execution.postmortem_chain.truncated`), emitted alongside `log.warning`. |
| Phase 2 prompt caching | ⏸ Still deferred | Blocked on Claude Code adapter migration off subprocess `claude_agent_sdk` to raw `anthropic` (or SDK exposing structured system blocks with `cache_control`). The serial executor builds `system_prompt` once per run so it benefits without further refactor when unblocked. |

---

## Tradeoffs we took (honest list)

1. **Subclass over extract.** Keeps `_execute_single_ac` in one place. Downside: `SerialCompoundingExecutor` inherits `ParallelACExecutor`'s vocabulary — naming is slightly awkward ("compounding IS-A parallel executor"). Rename later if a cleaner hierarchy emerges.
2. ~~**Empty `diff_summary` in phase 1.**~~ Resolved by M5 — `diff_capture.py` populates `diff_summary` via `git stash create` boundaries with `git diff --stat`. Empty-on-error invariant means the run is never blocked by capture failures.
3. ~~**Empty `invariants_established` in phase 1.**~~ Resolved by Q3 — agents tag `[[INVARIANT: …]]` in their trace, a Haiku verifier scores reliability (default ≥0.7), and `PostmortemChain.merge_invariants` accumulates / contradicts / re-declares across the chain.
4. **No CLI flag for fail-forward yet.** `--compounding` is fail-fast. Programmatic callers can pass `fail_fast=False`; CLI users can't. Add if a case surfaces.
5. **No cost warning in CLI for long compounding runs.** A 20-AC seed with K=3 full forms will send large prompts. Not a phase-1 problem but worth a `--estimate-tokens` flag down the line.
6. **Token budget heuristic is 4 chars/token.** Crude but deterministic. Real tokenization would require importing the model's tokenizer; not worth it for a guard rail.
7. **Legacy `--sequential` kept with old semantics.** Users relying on "disable the parallel executor" aren't broken. Docstring nudges them toward `--compounding` without forcing migration.

---

## Open questions

Extracted to a dedicated brainstorming doc — each question is ground-truthed with
file:line references, options priced in LOC, and a priority/sequencing matrix:

**[docs/brainstorm/serial-compounding-open-questions.md](../brainstorm/serial-compounding-open-questions.md)**

Summary of decisions (full reasoning in the brainstorm doc):

| # | Question | Decision | Phase |
|---|---|---|---|
| Q1 | Sub-postmortems handling | **B-prime** — flatten in prompt, preserve structure in serialized state | 1.5 (AC-2 of dogfood) |
| Q2 | Diff capture backend | Defer — event-based `files_modified` covers 70% | 2 |
| Q3 | `invariants_established` extraction | **C-plus** — tag `[[INVARIANT: ...]]` + Haiku verifier + reliability score | 1.5 (AC-3 of dogfood) |
| Q4 | Inline QA + retry semantics | Defer to M7 (inline QA not wired yet) | 2 |
| Q5 | `ooo evolve` integration | End-of-run hint only; defer auto-trigger | Defer |
| Q6 | Resume + chain | **B then C** split — Q6.1 end-of-run artifact, Q6.2 per-AC checkpoint + agent-adjudicated resume | 1.5 (AC-1 + AC-4 of dogfood) |
| Q7 | Budget-overflow event | **Bundle into Q6.2** | 1.5 |

**Phase-1.5 plan:** 4 ACs, ~460 LOC, executed as a compounding dogfood run in order **Q6.1 → Q1 → Q3 → Q6.2+Q7**. See the brainstorm doc's "Execution plan" section for AC scopes and success criteria.

---

## References

- Core primitives: `src/ouroboros/orchestrator/level_context.py`
- Serial executor: `src/ouroboros/orchestrator/serial_executor.py`
- Prompt-build + dispatch: `src/ouroboros/orchestrator/runner.py` (search `include_claude_md`, `mode`)
- Context-override hook: `src/ouroboros/orchestrator/parallel_executor.py` (search `context_override`)
- Event factory: `src/ouroboros/orchestrator/events.py` (`create_ac_postmortem_captured_event`)
- CLI flag: `src/ouroboros/cli/commands/run.py` (search `compounding`)
- MCP parameter: `src/ouroboros/mcp/tools/execution_handlers.py` (search `"mode"`)
- Tests: `tests/unit/orchestrator/test_level_context.py`, `tests/unit/orchestrator/test_serial_executor.py`, `tests/unit/orchestrator/test_events.py`, `tests/unit/cli/test_run_compounding.py`, `tests/unit/mcp/tools/test_definitions.py`
