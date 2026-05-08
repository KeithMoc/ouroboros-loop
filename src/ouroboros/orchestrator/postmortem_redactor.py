"""Runtime secret redaction for the compounding postmortem chain.

Compounding-mode postmortems (``ACPostmortem`` in
:mod:`ouroboros.orchestrator.level_context`) carry forward ``key_output``,
``files_modified``, ``tools_used``, and ``gotchas`` from agent trace events
into the next AC's prompt — and are persisted in events / per-AC checkpoints.

Event persistence intentionally preserves ``tool_input`` (see
``sanitize_event_data_for_persistence`` in ``events/base.py``; documented at
audit row #2 of ``docs/guides/serial-compounding.md``) so postmortems can
reconstruct what files an AC touched. That preservation is load-bearing for
diff-capture / replay, but it means a secret an agent printed during execution
(API key in stdout, env var in a tool_input, JSON config with credentials in
``Edit.new_string``) will land in the postmortem's ``key_output`` and from
there into the next AC's system prompt.

This module provides a runtime redactor wired *only* into
``build_postmortem_chain_prompt``. It does not touch event persistence (which
would break diff-capture / replay) or checkpoint storage (which re-renders the
chain from postmortem objects on resume — if the chain redacts, the resumed
prompt does too).

[[INVARIANT: parallel mode prompts are byte-identical pre/post redactor]]

The parallel executor builds its context section via
``build_context_prompt(level_contexts)`` (see ``parallel_executor.py:3135``),
not ``build_postmortem_chain_prompt``. Wiring the redactor into the latter
therefore leaves the parallel-mode prompt untouched.

Detection layers (composed in order):

1. Path allowlist — if any of ``files_modified`` matches a sensitive-path
   glob, the entire ``key_output`` is replaced with ``[redacted: path]``.
2. Pattern set — known-secret regex matches (Stripe, AWS, GitHub PAT, JWT,
   Slack, PEM blocks) are replaced with ``[redacted: pattern:<name>]``.
3. Entropy heuristic (opt-in) — long ``[A-Za-z0-9+/=_-]{32,}`` runs whose
   Shannon entropy exceeds 4.5 bits/char are replaced with
   ``[redacted: entropy]``. Off by default (false-positive cost).

Public API: :func:`redact_for_chain`.

Note on field scope: the redactor is applied to ``key_output`` only.
``files_modified`` is preserved verbatim — the next AC needs the path list
to know what was touched (and the redaction reason ``[redacted: path]`` is
already informative). ``tools_used`` is preserved (high false-positive risk
on tool names, low payoff). ``gotchas`` is freeform agent text that could
contain secrets; the wire site routes each gotcha string through the pattern
+ entropy layers (path-layer suppression of the whole field would be too
aggressive — gotchas are agent-curated lessons, not raw stdout).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from fnmatch import fnmatch
from functools import lru_cache
import math
import os
import re

# --- Defaults -----------------------------------------------------------------

# Default sensitive-path globs. Matched (case-insensitive) against the
# basename of each file in ``files_modified`` and against the full path
# suffix — the latter so ``~/.aws/credentials`` and ``foo/.aws/credentials``
# both hit ``*.aws/credentials``.
_DEFAULT_REDACT_PATHS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "id_rsa*",
    "id_ed25519*",
    "credentials.json",
    "*.kube/config",
    ".aws/credentials",
    ".npmrc",
    ".pypirc",
)

# Pattern set. Order is preserved for deterministic substitution.
# Each entry: (name, compiled_regex). The name appears in the redaction
# marker as ``[redacted: pattern:<name>]``.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Private-key block first — its match spans multiple lines and may
    # contain base64 content the entropy layer would also flag.
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
            r"[\s\S]*?"
            r"-----END [A-Z ]*PRIVATE KEY-----",
        ),
    ),
    ("stripe", re.compile(r"sk_live_[A-Za-z0-9]{24,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github_pat_classic", re.compile(r"ghp_[A-Za-z0-9]{36,}")),
    ("github_pat_fine", re.compile(r"github_pat_[A-Za-z0-9_]{82,}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_=-]+\.eyJ[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+")),
    ("slack", re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}")),
)

# Entropy layer regex: contiguous runs of base64-ish characters length ≥ 32.
_ENTROPY_RUN_RE: re.Pattern[str] = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
# Threshold (bits/char) above which a run is treated as a secret.
_ENTROPY_THRESHOLD: float = 4.5


@dataclass(frozen=True, slots=True)
class _RedactorConfig:
    """Resolved redactor configuration. Immutable; cached at module level."""

    redact_paths: tuple[str, ...]
    entropy_enabled: bool


def _parse_redact_paths(raw: str | None) -> tuple[str, ...]:
    """Resolve the active path-glob set from ``OUROBOROS_POSTMORTEM_REDACT_PATHS``.

    Semantics:

    - Unset / empty → defaults only.
    - Value ``"none"`` (case-insensitive, after strip) → defaults disabled,
      empty set returned.
    - Otherwise → comma-separated globs are appended to the defaults
      (additive; defaults always included unless explicitly disabled).
    """
    if raw is None:
        return _DEFAULT_REDACT_PATHS
    stripped = raw.strip()
    if not stripped:
        return _DEFAULT_REDACT_PATHS
    if stripped.lower() == "none":
        return ()
    extras = tuple(g.strip() for g in stripped.split(",") if g.strip())
    return _DEFAULT_REDACT_PATHS + extras


@lru_cache(maxsize=1)
def _get_config() -> _RedactorConfig:
    """Read env-driven config once; cached.

    Tests that mutate the relevant env vars must call :func:`reset_config_cache`.
    """
    return _RedactorConfig(
        redact_paths=_parse_redact_paths(
            os.environ.get("OUROBOROS_POSTMORTEM_REDACT_PATHS")
        ),
        entropy_enabled=os.environ.get(
            "OUROBOROS_POSTMORTEM_REDACT_ENTROPY", ""
        ).strip()
        == "1",
    )


def reset_config_cache() -> None:
    """Drop the cached :class:`_RedactorConfig`. Test-only hook."""
    _get_config.cache_clear()


def _path_match(files_modified: tuple[str, ...], redact_paths: tuple[str, ...]) -> bool:
    """Return True when any path matches any glob.

    Globs match against both the basename and the full normalized path so
    patterns like ``.aws/credentials`` hit ``~/.aws/credentials`` and
    ``/home/u/.aws/credentials`` alike. Comparison is case-insensitive on
    the glob side because env-file naming conventions vary.
    """
    if not files_modified or not redact_paths:
        return False
    for raw_path in files_modified:
        if not raw_path:
            continue
        path = raw_path.replace("\\", "/")
        basename = path.rsplit("/", 1)[-1]
        path_lc = path.lower()
        basename_lc = basename.lower()
        for glob in redact_paths:
            glob_lc = glob.lower()
            if fnmatch(basename_lc, glob_lc):
                return True
            # Suffix match — handles ``.aws/credentials`` matching
            # ``home/keith/.aws/credentials``.
            if fnmatch(path_lc, glob_lc) or fnmatch(path_lc, "*/" + glob_lc):
                return True
    return False


def _redact_patterns(text: str) -> str:
    """Apply known-secret regex replacements.

    Each match is replaced with ``[redacted: pattern:<name>]``. Patterns are
    applied in declaration order; the private-key block runs first so its
    multi-line match is removed before later layers can fire on its base64
    body.
    """
    if not text:
        return text
    for name, pattern in _PATTERNS:
        text = pattern.sub(f"[redacted: pattern:{name}]", text)
    return text


def _shannon_entropy(s: str) -> float:
    """Shannon entropy in bits/char for a non-empty string.

    Uses ``collections.Counter`` for the symbol distribution. Returns 0.0
    for the empty string (no information).
    """
    if not s:
        return 0.0
    n = len(s)
    counts = Counter(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _redact_entropy(text: str) -> str:
    """Replace high-entropy base64-ish runs with ``[redacted: entropy]``.

    Walks ``[A-Za-z0-9+/=_-]{32,}`` matches; replaces those whose Shannon
    entropy is ≥ :data:`_ENTROPY_THRESHOLD` bits/char. Lower-entropy runs
    (e.g. ``aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa``) are left alone.
    """
    if not text:
        return text

    def _replace(match: re.Match[str]) -> str:
        run = match.group(0)
        if _shannon_entropy(run) >= _ENTROPY_THRESHOLD:
            return "[redacted: entropy]"
        return run

    return _ENTROPY_RUN_RE.sub(_replace, text)


def redact_for_chain(text: str, files_modified: tuple[str, ...]) -> str:
    """Redact secrets from ``text`` before it enters the postmortem chain.

    Pipeline (composable, applied in order):

    1. Path layer — if any of ``files_modified`` matches a sensitive-path
       glob, the *entire* ``text`` is replaced with ``[redacted: path]``.
       This is intentional: when an AC writes to ``.env`` the next AC needs
       to know "you wrote a secret to .env" but must not see the secret.
    2. Pattern layer — known-secret regexes replace each match with
       ``[redacted: pattern:<name>]``.
    3. Entropy layer (opt-in via ``OUROBOROS_POSTMORTEM_REDACT_ENTROPY=1``)
       — high-entropy base64-ish runs replace with ``[redacted: entropy]``.

    Args:
        text: Free-form text to redact (typically a postmortem
            ``key_output`` or ``gotcha`` string).
        files_modified: Paths an AC touched. Used by the path layer only.
            Pass the AC's own ``files_modified`` tuple — passing a
            cross-AC aggregate would suppress more than intended.

    Returns:
        The redacted text. Never raises; empty input returns empty output.

    Pure function. All env-driven config is read via the module-level
    :func:`_get_config` cache; tests that flip env vars should call
    :func:`reset_config_cache` between runs.
    """
    if not text:
        return text

    config = _get_config()

    if _path_match(files_modified, config.redact_paths):
        return "[redacted: path]"

    redacted = _redact_patterns(text)
    if config.entropy_enabled:
        redacted = _redact_entropy(redacted)
    return redacted


__all__ = [
    "redact_for_chain",
    "reset_config_cache",
]
