#!/usr/bin/env bash
set -euo pipefail

fail() {
  echo "security check failed: $*" >&2
  exit 1
}

tracked_forbidden=$(
  git ls-files \
    '.env' '.env.*' \
    'logs/*' 'reports/*' \
    '*.db' '*.sqlite3' '*.pyc' '*__pycache__*' \
    'dietary_profile.txt' \
    '*.shortcut' '*.wflow' \
    2>/dev/null | grep -v -x '.env.example' || true
)

if [ -n "$tracked_forbidden" ]; then
  echo "$tracked_forbidden" >&2
  fail "runtime, personal, or generated files are tracked"
fi

secret_matches=$(
  git grep -n -I -E \
    '(termux-super-secret|secret-android-key|136\.112\.|8675[0-9]+|TELEGRAM_BOT_TOKEN</key>|AIza[0-9A-Za-z_-]{20,}|[0-9]{8,}:[A-Za-z0-9_-]{20,})' \
    -- . ':!*.jpg' ':!*.jpeg' ':!*.png' ':!*.heic' ':!*.heif' \
    ':!scripts/check_public_safety.sh' \
    2>/dev/null || true
)

if [ -n "$secret_matches" ]; then
  echo "$secret_matches" >&2
  fail "secret-like values found in tracked files"
fi

echo "public safety check passed"
