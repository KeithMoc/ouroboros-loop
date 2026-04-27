# Phase 2 — Q4: Inline QA in Serial-Compounding Mode (Design)

> Status: **design approved 2026-04-27**, awaiting implementation plan + dogfood run.
> Sibling docs: [`serial-compounding-open-questions.md`](./serial-compounding-open-questions.md), [`phase-2-q2-diff-capture-design.md`](./phase-2-q2-diff-capture-design.md), [`../guides/serial-compounding.md`](../guides/serial-compounding.md).
> Phase-2 Q2 (per-AC diff capture) shipped in `ccfc479` + hotfix `4f0b044`. This is the next dogfood cycle.

## Why this exists

`QAHandler` (`mcp/tools/qa.py:397`) is a production-ready, structured-verdict QA judge already used at end-of-run by `ouroboros run` (`cli/commands/run.py:614-650`). In `--compounding` mode, it is **never called per-AC** — only at the end of the entire run. The compounding chain therefore propagates AC-by-AC postmortems whose only quality signal is "did `_execute_single_ac` return success" — i.e., whether the agent thinks it succeeded.

Q2.1 (PR #5) made this gap concrete: a real dogfood `--compounding` run produced a chain with all four ACs marked `[pass]` while the inner orch worktree had ten real commits — but the chain rendered no `diff_summary` because the capture+render pipeline silently short-circuited. Unit tests didn't catch it; only the live run exposed the divergence between *agent claim* and *workspace reality*.

Inline QA closes the same class of gap with explicit independent verification: each AC's claim is judged against its actual diff, with an LLM verdict (`pass | revise | fail`) recorded in the postmortem chain. Failed/recoverable verdicts trigger a budgeted retry loop with QA feedback injected into the next attempt's prompt.

## Decisions (locked)

| # | Question | Decision | Why |
|---|---|---|---|
| 1 | What does QA see as `artifact`? | **`final_message + diff_summary`** (composed text block) | Final message is the agent's *claim*; `diff_summary` (already populated post-Q2.1) is the cheapest possible *evidence*. Together they catch the "claims success, touched no files" failure class that Q2.1 surfaced. Full `git diff` deferred — token-budget risk + generated-file blowups. |
| 2 | Which verdicts trigger a retry? | **REVISE *or* FAIL** until `--max-qa-retries` exhausted | Score is a continuum; the REVISE/FAIL label is just a threshold. LLM judges are noisy — gating on the label denies retries to recoverable cases. Budget (default 1 → 2 attempts total) is the real cap. |
| 3 | Retry mechanism — prior commits? | **Add-on commits**; `pre_sha` captured **once per AC** outside the retry loop | Matches Q2.1's lesson directly: design *with* the orchestrator's commit-per-AC pattern, not against it. Reverting destroys traceability and goes against the explicit "each (sub-)AC's work is committed for traceability" rule. Squash-merge collapses retry-noise at PR time. |
| 4 | Exhausted retries — hard or soft fail? | **Soft-pass; verdict captured** (always, regardless of `fail_fast`) | Cycle-1 default. A flaky LLM judge mistakenly returning FAIL would otherwise nuke a 20-AC compounding chain. Verdict + suggestions still land in the postmortem so downstream ACs see what was flagged. Follow-up cycle (Q4.1) can flip to `fail_fast`-respecting once judge accuracy is observed. |

**Pre-decided in master brainstorm + handoff:**

- Wire `QAHandler` per-AC at the postmortem-construction site.
- Gate behind `--inline-qa` (default off — roughly doubles model calls per AC).
- Add `--max-qa-retries` (default 1) — separate counter from `MAX_STALL_RETRIES` so QA failures don't share the stall budget.
- Externally-satisfied ACs (`--skip-completed`) skip QA — user explicitly opted those out.

## Mechanism

### Per-AC flow

The current per-AC body in `SerialCompoundingExecutor.execute_serial` (around `serial_executor.py:1330–1391`) is wrapped in a QA-retry loop:

```text
pre_sha = capture_pre_ac_snapshot(workspace_for_diff)   # ONCE per AC, outside loop
qa_verdict_for_feedback = None

for qa_attempt in range(max_qa_retries + 1):
    context_for_attempt = context_section
    if qa_verdict_for_feedback is not None:
        context_for_attempt += _format_qa_feedback_section(
            qa_verdict_for_feedback,
            attempt_number=qa_attempt + 1,
            max_attempts=max_qa_retries + 1,
        )

    result = await self._execute_single_ac(..., context_override=context_for_attempt)
    diff_summary = compute_diff_summary(pre_sha, workspace_for_diff)   # spans pre→HEAD
    postmortem = self._build_postmortem_from_result(result, diff_summary=diff_summary)

    # Skip QA entirely if disabled OR the execution itself failed/stalled —
    # the existing FAILED-path takes over, untouched.
    if not inline_qa_enabled or result.outcome != ACExecutionOutcome.SUCCEEDED:
        break

    inline_qa_outcome = await run_inline_qa(
        qa_handler,
        postmortem=postmortem,
        ac_index=ac_index,
        ac_content=ac_content,
        seed=seed,
        qa_session_id=f"qa-ac{ac_index}-{session_id[:8]}",
    )

    postmortem = postmortem.with_qa(
        verdict=inline_qa_outcome.verdict_dict,
        status=("passed" if inline_qa_outcome.loop_action == "pass" else None),  # finalized below
        attempts=qa_attempt + 1,
    )

    await self._safe_emit_event(create_ac_qa_evaluated_event(...))

    if inline_qa_outcome.loop_action == "pass":
        postmortem = postmortem.with_qa_status("passed")
        break

    qa_verdict_for_feedback = inline_qa_outcome.verdict_dict
    # Continue to next iteration; postmortem from this attempt is replaced

else:
    # Loop exhausted without a PASS — Q4=B soft-pass:
    # keep the last attempt's postmortem, mark exhausted.
    postmortem = postmortem.with_qa_status("exhausted")

# Existing invariant-extraction / chain-append logic continues from here, unchanged.
```

`pre_sha` is captured **exactly once** before the retry loop. `compute_diff_summary` runs **per attempt** so each iteration's postmortem reflects the cumulative state at that point, but the *winning* postmortem (whichever attempt's `break`s the loop, or the last one on exhaustion) sees the full pre→post AC diff per Q3=B's add-on-commits mechanic.

### Skip / bypass paths

| Path | Bypass QA? | Why |
|---|---|---|
| `external_completed[ac_index]` (line 1186) | Yes | User explicitly flagged AC as already satisfied; no execution happened, no claim to judge. |
| `result.outcome != SUCCEEDED` (failed / stalled) | Yes | Existing FAILED-path handles it. QA on a stall result would judge the partial diff out of context. |
| `inline_qa_enabled is False` (default) | Yes | Flag is opt-in. Zero behavior change for runs without `--inline-qa`. |
| Sub-AC decomposition | Parent only | Sub-postmortems are already aggregated into `parent.sub_postmortems`. QA's input is the parent's combined view; running QA per sub-AC would double cost and complicate the verdict-feedback prompt. |
| `--inline-qa` outside `--compounding` | Yes | Parallel mode is out of scope for cycle-1. Logged warning + ignored. |

### Artifact assembly

```python
def _assemble_qa_artifact(result: ACExecutionResult, postmortem: ACPostmortem) -> str:
    final_message = (result.final_message or "").strip() or "(empty)"
    diff_block = postmortem.diff_summary.strip() or "(no changes detected)"
    return (
        "## Agent's final message\n"
        f"{final_message}\n\n"
        "## Diff summary (`git diff --stat`, pre-AC → post-AC)\n"
        f"```\n{diff_block}\n```"
    )
```

The `(empty)` and `(no changes detected)` sentinels are deliberate — those *are* the diagnostic signal QA is meant to catch.

### Quality bar assembly

```python
def _assemble_qa_quality_bar(ac_index: int, ac_content: str, seed_goal: str) -> str:
    return (
        f"Acceptance criterion AC-{ac_index + 1}: {ac_content}\n\n"
        f"Seed goal: {seed_goal}"
    )
```

AC text is primary. `seed.goal` adds context so QA judges the AC against broader intent. **Excluded:** `seed.quality_bar` (used by post-execution QA only — keeps the inline path scoped) and chain invariants (those are the chain's job to propagate, not QA's input).

### Feedback injection (REVISE / FAIL → next attempt)

Appended to the next attempt's `context_override`:

```text
## QA verdict on previous attempt (attempt {attempt_number} of {max_attempts})

Score: {score:.2f} / 1.00 — {verdict_label_upper}

### Differences flagged
- {differences[0]}
- {differences[1]}
...

### Suggested revisions
- {suggestions[0]}
- {suggestions[1]}
...

### Reasoning
{reasoning}

The previous attempt's commits are **kept on the branch**. Build on them — do
NOT revert. Address the differences and suggestions above in a follow-up commit.
```

The last paragraph is essential — it tells the agent the workflow contract (additive commits, not revert-and-redo) per Q3=B.

## Schema additions

### `ACPostmortem` new fields (`level_context.py`)

| Field | Type | Default | Meaning |
|---|---|---|---|
| `qa_verdict` | `dict[str, Any] \| None` | `None` | Serialized last-attempt `QAVerdict` (`score`, `verdict`, `dimensions`, `differences`, `suggestions`, `reasoning`). `None` when QA wasn't run. |
| `qa_status` | `str \| None` | `None` | One of `None` (not run), `"passed"` (PASS verdict on some attempt), `"exhausted"` (budget hit, last verdict still non-pass), `"skipped_delegated"` (opencode-mode plugin-dispatch path returned `delegated_to_subagent`; no in-process verdict available — see Failure modes). |
| `qa_attempts` | `int` | `0` | Number of QA evaluations performed for this AC. `0` when QA wasn't run. |

Round-trip serialization (`serialize_postmortem_chain` / `deserialize_postmortem_chain`) extends to cover the three fields. `to_prompt_text` includes `qa_status` + `qa_verdict.score` in the rendered AC bullet when set, so downstream ACs see the QA story in their compounding context.

### `_render_chain_as_markdown` (markdown artifact)

Per AC, when `qa_status is not None`, append a `### QA` block with the same dynamic-fence selection used in PR #5's `Diff summary` block (defends against backticks in suggestion text):

```text
### QA
- Status: passed (attempt 2 of 2)
- Score: 0.86 / 1.00 — pass
- Suggestions:
  - {suggestions[0]}
  - ...
```

### Event factory (`orchestrator/events.py`)

New: `create_ac_qa_evaluated_event(session_id, execution_id, ac_index, qa_attempt, score, verdict_label, loop_action, passed)` — emitted on every QA call, both pass and non-pass. Mirrors `create_ac_postmortem_captured_event`'s shape and uses the same event-naming convention.

### Outcome semantics

`ACExecutionResult.outcome` stays `SUCCEEDED` even when QA exhaustion hits (per Q4=B soft-pass). The verdict tells the chain reader the QA story; the AC itself is not re-classified. **`ACExecutionOutcome` enum is unchanged** — no new states, no migrations.

## Module structure

New file: `src/ouroboros/orchestrator/inline_qa.py` (~120 LOC). Keeps QA-specific logic isolated so `serial_executor.py` (already 1700+ lines) doesn't grow further.

```
src/ouroboros/orchestrator/inline_qa.py
├── _assemble_qa_artifact(result, postmortem) -> str
├── _assemble_qa_quality_bar(ac_index, ac_content, seed_goal) -> str
├── _format_qa_feedback_section(verdict_dict, attempt_number, max_attempts) -> str
├── _serialize_qa_verdict(verdict: QAVerdict) -> dict[str, Any]
├── @dataclass(frozen=True) InlineQAOutcome
│       loop_action: str  # "pass" | "revise" | "fail"
│       score: float
│       verdict_dict: dict[str, Any]
│       qa_session_id: str
└── async def run_inline_qa(qa_handler, *, postmortem, ac_index, ac_content,
                            seed, qa_session_id) -> InlineQAOutcome
```

`serial_executor.py` imports the helpers + `run_inline_qa`; no QA-specific logic is inlined.

## CLI surface (`cli/commands/run.py`)

```python
inline_qa: Annotated[
    bool,
    typer.Option(
        "--inline-qa",
        help=(
            "Run per-AC QA evaluation in compounding mode. Captures verdict in "
            "the postmortem chain. Roughly doubles model calls. No effect outside "
            "--compounding mode."
        ),
    ),
] = False,
max_qa_retries: Annotated[
    int,
    typer.Option(
        "--max-qa-retries",
        min=0,
        help=(
            "Max retries when QA verdict is REVISE/FAIL (separate from --max-stall-retries). "
            "Default 1 → 2 total QA-judged attempts per AC. Budget-only; QA never blocks "
            "the run on exhaustion (verdict captured in postmortem)."
        ),
    ),
] = 1,
```

- `--inline-qa` outside `--compounding`: warn + ignore.
- `--max-qa-retries` without `--inline-qa`: warn + ignore.
- Both kwargs propagate to `execute_serial(..., inline_qa: bool, max_qa_retries: int)`.

## Testing strategy

### Unit tests (`tests/unit/orchestrator/test_inline_qa.py`, ~15-20 tests)

- Artifact assembly: empty `final_message`; empty `diff_summary`; both empty; multiline final_message
- Quality bar: multiline AC text; special chars in `seed.goal`
- Feedback section: PASS/REVISE/FAIL verdicts; suggestions list with backticks; empty suggestions
- Verdict serialization round-trip into `qa_verdict` dict (including `dimensions` map)
- `InlineQAOutcome` construction from `MCPToolResult.meta`
- `run_inline_qa` happy path, error path (QA handler returns Err), parse-failure path

### Integration tests (`tests/unit/orchestrator/test_serial_executor.py`, +8-10 tests)

- `--inline-qa` off (default): zero QA calls; `qa_*` fields untouched (`None`/`0`)
- `--inline-qa` on, first-attempt PASS: 1 QA call; `qa_status="passed"`; `qa_attempts=1`; no retry
- `--inline-qa` on, REVISE-then-PASS: 2 QA calls; `qa_attempts=2`; `qa_status="passed"`
- `--inline-qa` on, FAIL × (max+1): `qa_attempts=max+1`; `qa_status="exhausted"`; last postmortem retained; chain still advances (Q4=B soft-pass)
- `pre_sha` captured **once** across QA retries (regression guard for Q2.1 lesson)
- Externally-satisfied AC bypass: zero QA calls
- Failed `_execute_single_ac` bypass: zero QA calls (`result.outcome != SUCCEEDED`)
- Sub-AC parent-only: decomposed AC produces 1 QA call on the parent, not N on subs
- `--inline-qa` outside `--compounding`: warning emitted, no QA calls
- Markdown render contains `### QA` block when `qa_verdict` set; omits it when not

### End-to-end test (the Q2.1 lesson — every per-AC feature gets one)

`test_inline_qa_flows_end_to_end_with_commit_per_ac` (in `test_serial_executor.py`):

Drive `execute_serial` with a real-ish commit-per-AC pattern; mock the `QAHandler` to return REVISE-then-PASS. Assert:
- `qa_attempts == 2` in serialized chain
- `pre_sha` SHA was captured exactly once (verify via spy on `capture_pre_ac_snapshot`)
- `diff_summary` spans both attempts' commits (`pre_sha → HEAD`)
- Markdown artifact contains the `### QA` block with verdict score
- Feedback section was injected into the second attempt's `context_override`

This test is the regression-net analogue of `test_diff_summary_flows_end_to_end_with_commit_per_ac` from PR #5.

### Total expected post-cycle test count: ~5660 (current 5635 + ~25 new)

## Dogfood seed shape (`seeds/phase-2-q4-inline-qa.yaml`)

3 ACs:

1. **AC-1** — Add `inline_qa.py` module with helpers + `run_inline_qa`. Add unit tests.
2. **AC-2** — Wire into `serial_executor.execute_serial` per the flow above; add `qa_*` fields to `ACPostmortem`; add CLI flags + propagation; add integration tests.
3. **AC-3** — Add the `### QA` markdown rendering + render test; add the `test_inline_qa_flows_end_to_end_with_commit_per_ac` e2e test; add `create_ac_qa_evaluated_event` factory + event test.

`metadata.execution_mode_required: "compounding"` — currently cosmetic (MCP `execute_seed` doesn't honor it per HANDOFF gap), but recorded so future runner-driven runs pick the right executor automatically. The dogfood run itself drives `execute_serial` via the CLI `--compounding` flag explicitly.

## Failure modes / open risks

- **Flaky LLM judge.** Q4=B soft-pass mitigates; verdicts surfaced in chain so user can audit post-run. If a real run shows judge accuracy >85%, follow-up Q4.1 flips default to `fail_fast`-respecting.
- **`QAHandler` plugin-dispatch path.** `should_dispatch_via_plugin` (in `qa.py`) returns `delegated_to_subagent` for opencode-mode runtimes — `meta` won't carry a verdict in that case. `run_inline_qa` must detect this and either (a) skip with `qa_status="skipped_delegated"` + log, or (b) treat as PASS to avoid breaking opencode users. **Decision for cycle-1: skip with `qa_status="skipped_delegated"`** so user sees the gap explicitly. Add a unit test for this path.
- **Cost.** ~2× model calls per AC when `--inline-qa` on. Default off; the `--max-qa-retries=0` setting collapses to "single QA verdict, no retry" for cost-conscious runs.
- **`qa_verdict` in chain prompt growth.** Each AC adds another potential verdict block to the next AC's compounding context. Existing chain-truncation (Q7) trims oldest first; verdict adds maybe 200-500 chars per AC, well within budget — but worth measuring during dogfood.

## Estimated scope

| Component | LOC (approx) |
|---|---|
| `inline_qa.py` module | ~120 |
| `serial_executor.py` retry-loop wiring | ~60 |
| `ACPostmortem` fields + serialization | ~30 |
| `_render_chain_as_markdown` QA block | ~20 |
| `events.py` `create_ac_qa_evaluated_event` | ~25 |
| CLI flag plumbing | ~30 |
| Unit + integration + e2e tests | ~165 |
| **Total** | **~450** |

Above the brainstorm's original ~250 estimate — accounted for the dedicated module, event factory, opencode-mode skip path, and the e2e regression test that PR #5's lesson forces in.

## Out of scope (deferred)

- Hard-fail mode (Q4 default-flip; reopen as Q4.1 if dogfood shows judge accuracy is high enough).
- Retry matrix (per-verdict retry prompts) — Option C in the master brainstorm; complexity not yet justified.
- Inline QA for parallel mode — out of scope this cycle; would need different feedback-injection plumbing.
- Caching QA verdicts across retries — wasteful since each retry's diff differs.
- Honoring `metadata.execution_mode_required` in MCP `execute_seed` — separate cosmetic gap, tracked in HANDOFF.

---

*Locked 2026-04-27. Implementation plan + seed authored by `superpowers:writing-plans` in the next step. Dogfood run will drive `execute_serial` with `--compounding --inline-qa`.*
