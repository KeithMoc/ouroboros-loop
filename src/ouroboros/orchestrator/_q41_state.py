"""Q4.1 module-level state — shared between the MCP execute-seed handler and runner.

This file extracts the small pieces of module-level state introduced in
Q4.1 / AC-2 and consumed in Q4.1 / AC-4 sub-AC 5 (Run Summary observability).
The split exists because

* :mod:`ouroboros.mcp.tools.execution_handlers` already imports
  :class:`ouroboros.orchestrator.runner.OrchestratorRunner`; and
* :mod:`ouroboros.orchestrator.runner` needs to read the same module-level
  ``_mode_conflict_counter`` at end-of-run to render the Run Summary panel.

Importing the runner from the handler **and** the handler from the runner
would create a circular import.  Hoisting the shared state into this
dependency-free module keeps the cycle broken.

Race semantics
~~~~~~~~~~~~~~
Both fields are intentionally simple module-level singletons (no thread
synchronization):

``_SOFT_FLIP_WARNED_THIS_SESSION``
    Process-wide one-time guard for the
    ``"No execution mode specified — defaulting to 'compounding'"`` deprecation
    warning.  Worst-case race under concurrent MCP calls is a duplicate
    one-line warning, not a correctness issue (see
    ``docs/brainstorm/phase-2-q4.1-hardening-design.md``, "Risk register").

``_mode_conflict_counter``
    Session-scoped counter incremented at the AC-2 caller-wins-and-warn
    site whenever a caller-supplied ``mode`` disagrees with
    ``seed.metadata.execution_mode_required``.  AC-4 / sub-AC 5 pops the
    counter at run end via :func:`dict.pop` so a re-run under the same
    session id reads zero unless a new conflict happens.

[[INVARIANT: _q41_state is the canonical home for cross-module Q4.1 singletons]]
[[INVARIANT: _mode_conflict_counter and _SOFT_FLIP_WARNED_THIS_SESSION import-cycle-safe]]
"""

from __future__ import annotations

# Soft-flip deprecation flag — process-wide.  Reset only on process restart.
_SOFT_FLIP_WARNED_THIS_SESSION: bool = False

# Caller-vs-seed mode-conflict counter, keyed by ``session_id``.  Mutating the
# dict in place (``[k] = v`` / ``.pop(k, 0)``) keeps both this module and any
# alias in :mod:`ouroboros.mcp.tools.execution_handlers` pointing at the same
# instance, so test fixtures that ``.clear()`` it via either reference see
# the same effect.
_mode_conflict_counter: dict[str, int] = {}


__all__ = [
    "_SOFT_FLIP_WARNED_THIS_SESSION",
    "_mode_conflict_counter",
]
