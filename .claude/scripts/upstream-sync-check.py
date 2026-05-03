#!/usr/bin/env python3
"""SessionStart hook: notify Claude when origin/main is behind upstream/main.

Local-dev only — wired in `.claude/settings.json`, NOT in the plugin's
`hooks/hooks.json`. End users of the ouroboros plugin should never see this.

Behavior:
- Reads SessionStart hook payload from stdin (JSON).
- Only fires on session source `startup` and `resume` (skip `clear`/`compact`
  to avoid re-prompting in the same session).
- Skips silently when `upstream` remote is missing, when offline, or when
  origin/main is already up to date.
- Throttles to once per 6h via a cache file so repeated session starts
  don't spam the user.
- On a real signal, prints additionalContext via the Claude Code hook JSON
  contract so Claude sees: "upstream is N ahead — invoke /upstream-sync skill".

Cache file: `.claude/.upstream-sync-cache.json` (gitignored).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

THROTTLE_SECONDS = 6 * 60 * 60  # 6h
ALLOWED_SOURCES = {"startup", "resume"}


def _run(cmd: list[str], cwd: Path) -> tuple[int, str]:
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode, (result.stdout or "").strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return 1, ""


def _project_dir() -> Path:
    explicit = os.environ.get("CLAUDE_PROJECT_DIR")
    if explicit:
        return Path(explicit)
    return Path.cwd()


def _read_payload() -> dict:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _emit_silent() -> None:
    # Successful no-op — say nothing on stdout so Claude's context stays clean.
    sys.exit(0)


def _emit_context(message: str) -> None:
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": message,
        }
    }
    print(json.dumps(payload))
    sys.exit(0)


def _cache_path(project: Path) -> Path:
    return project / ".claude" / ".upstream-sync-cache.json"


def _throttled(cache: Path) -> bool:
    if not cache.exists():
        return False
    try:
        data = json.loads(cache.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    last = data.get("last_run_epoch")
    if not isinstance(last, (int, float)):
        return False
    return (time.time() - last) < THROTTLE_SECONDS


def _stamp(cache: Path, behind: int) -> None:
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps(
            {
                "last_run_epoch": int(time.time()),
                "behind": behind,
            }
        )
    )


def main() -> None:
    payload = _read_payload()
    source = payload.get("source") or payload.get("hookSpecificOutput", {}).get("source")
    if source and source not in ALLOWED_SOURCES:
        _emit_silent()

    project = _project_dir()
    if not (project / ".git").exists():
        _emit_silent()

    # Need an `upstream` remote. If absent, this is not a fork checkout.
    rc, _ = _run(["git", "remote", "get-url", "upstream"], project)
    if rc != 0:
        _emit_silent()

    cache = _cache_path(project)
    if _throttled(cache):
        _emit_silent()

    # Quiet network fetch. Bounded by subprocess timeout.
    _run(["git", "fetch", "--quiet", "upstream"], project)
    _run(["git", "fetch", "--quiet", "origin"], project)

    # How many upstream commits is origin/main missing?
    rc, out = _run(
        ["git", "rev-list", "--count", "origin/main..upstream/main"],
        project,
    )
    if rc != 0 or not out.isdigit():
        _emit_silent()

    behind = int(out)
    _stamp(cache, behind)
    if behind == 0:
        _emit_silent()

    # Surface a sample of incoming commit subjects for the user's first impression.
    _, sample = _run(
        [
            "git",
            "log",
            "--oneline",
            "--no-merges",
            "-5",
            "origin/main..upstream/main",
        ],
        project,
    )
    sample_block = sample if sample else "(no non-merge commits in range)"

    message = (
        f"[upstream-sync] origin/main is {behind} commit(s) behind upstream/main "
        f"(Q00/ouroboros).\n\n"
        f"Recent upstream commits:\n{sample_block}\n\n"
        "Ask the user whether to run the upstream-sync skill now. If yes, "
        "invoke the upstream-sync skill at .claude/skills/upstream-sync/SKILL.md "
        "and follow it. If no, acknowledge once and do not re-prompt this session."
    )
    _emit_context(message)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 — never block session start
        sys.stderr.write(f"upstream-sync-check: {exc}\n")
        sys.exit(0)
