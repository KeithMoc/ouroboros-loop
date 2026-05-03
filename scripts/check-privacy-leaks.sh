#!/usr/bin/env bash
# Privacy-leak guard — pre-commit hook.
#
# Scans STAGED diff additions for personal-info patterns that should not ship
# to a public repo. Operates on `git diff --cached` so it only fires on lines
# being newly added in this commit; it does NOT re-flag legacy content from
# upstream or earlier commits.
#
# What it blocks:
#   - Real home-dir paths under /home/<user>/ or /Users/<user>/
#     (test conventions /home/user/, /home/runner/, /home/u/ are allowed)
#   - The maintainer's personal email
#   - The local-workspace path marker _WORKSPACES_
#   - Claude Code's username-bearing project-slug pattern (.claude/projects/-home-)
#
# What it does NOT block:
#   - The fork's GitHub URL slug (KeithMoc/ouroboros-loop) — that becomes public
#     when the repo flips public, so it isn't a privacy leak.
#   - Generic test fixtures using literal "user" or "runner" usernames.
#
# To bypass for a single emergency commit (RARELY appropriate — this hook
# exists because past leaks shipped):
#   git commit --no-verify
# But prefer to fix the leak.
#
# Tunable allowlist: edit ALLOWED_HOME_USERS and ALLOWED_FILES below.

set -euo pipefail

# Path patterns we deliberately allow (known test/CI conventions).
ALLOWED_HOME_USERS_REGEX='^(user|users|runner|u|root|admin|test|example)$'

# Files exempt from scanning. The hook itself contains the very patterns it
# searches for, so scanning itself would be a false-positive engine.
ALLOWED_FILES=(
  "scripts/check-privacy-leaks.sh"
)

# Build the staged-diff stream. -U0 keeps the noise low (no surrounding
# context lines that would create false positives from inherited code).
DIFF=$(git diff --cached --no-color --no-ext-diff -U0 -- 2>/dev/null || true)
if [ -z "$DIFF" ]; then
  exit 0  # Nothing staged. Nothing to do.
fi

# We also need to know which file each + line came from. Walk the diff
# state-machine style.

current_file=""
exempt=0
hits=0

# Patterns to scan for on added (+) lines.
EMAIL_RE='keithmoc\.dev@gmail\.com'
WORKSPACE_RE='_WORKSPACES_'
PROJECT_SLUG_RE='\.claude/projects/-home-[a-zA-Z0-9_]+'

# Print a finding once we see one.
report() {
  local kind="$1"
  local file="$2"
  local line="$3"
  printf '  [%s] %s:\n    %s\n\n' "$kind" "$file" "$line" >&2
}

while IFS= read -r line; do
  case "$line" in
    "diff --git "*)
      # New file section. Extract the b/ side path.
      current_file="${line#*b/}"
      exempt=0
      for f in "${ALLOWED_FILES[@]}"; do
        if [ "$current_file" = "$f" ]; then
          exempt=1
          break
        fi
      done
      ;;
    "+++ "*|"--- "*|"@@ "*|"+++"*|"---"*)
      : # diff metadata — not a content line.
      ;;
    "+"*)
      [ "$exempt" -eq 1 ] && continue
      added="${line#+}"

      # 1) Real home-dir path like /home/keith/  or  /Users/keith/
      if printf '%s' "$added" | grep -qE '/(home|Users)/[A-Za-z0-9_-]+/'; then
        # Extract the username portion to compare against the allowlist.
        users=$(printf '%s' "$added" | grep -oE '/(home|Users)/[A-Za-z0-9_-]+/' | sed -E 's|^/(home\|Users)/||; s|/$||' || true)
        for u in $users; do
          # The grep above can spit "home/keith" because of escaping. Strip again.
          u="${u#home/}"
          u="${u#Users/}"
          if ! printf '%s' "$u" | grep -qE "$ALLOWED_HOME_USERS_REGEX"; then
            report "home-path" "$current_file" "$added"
            hits=$((hits + 1))
            break
          fi
        done
      fi

      # 2) Personal email.
      if printf '%s' "$added" | grep -qE "$EMAIL_RE"; then
        report "personal-email" "$current_file" "$added"
        hits=$((hits + 1))
      fi

      # 3) Local workspace marker.
      if printf '%s' "$added" | grep -q "$WORKSPACE_RE"; then
        report "workspace-marker" "$current_file" "$added"
        hits=$((hits + 1))
      fi

      # 4) Claude Code project-slug with embedded home-dir.
      if printf '%s' "$added" | grep -qE "$PROJECT_SLUG_RE"; then
        report "claude-project-slug" "$current_file" "$added"
        hits=$((hits + 1))
      fi
      ;;
    *)
      : # Context or removed line.
      ;;
  esac
done <<< "$DIFF"

if [ "$hits" -gt 0 ]; then
  cat >&2 <<'EOF'

──────────────────────────────────────────────────────────────────────────
PRIVACY LEAK GUARD: blocking commit.

The diff contains content that looks like personal/local-machine info.
Patterns that should never ship to a public repo:

  • /home/<your-username>/ or /Users/<your-username>/ paths
  • Personal email addresses
  • Local workspace markers (e.g. _WORKSPACES_)
  • Claude Code project slugs that embed your home-dir name

Fixes:

  1) Move local-only content to a gitignored path:
       docs/local/, *.local.md, *.private.md
  2) Replace literal home-dir paths with placeholders like
       ~/.claude/projects/<this-project>/...
  3) If a hit is a genuine false positive, edit
       scripts/check-privacy-leaks.sh
     to extend ALLOWED_HOME_USERS_REGEX or ALLOWED_FILES, then commit.

To bypass once (RARELY OK — past leaks shipped because of this):
  git commit --no-verify
──────────────────────────────────────────────────────────────────────────
EOF
  exit 1
fi

exit 0
