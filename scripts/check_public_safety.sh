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
    '(termux-super-secret|secret-android-key|136\.112\.|8675[0-9]+|TELEGRAM_BOT_TOKEN</key>|AIza[0-9A-Za-z_-]{20,}|[0-9]{8,}:[A-Za-z0-9_-]{20,}|PUSHPLUS_TOKEN[\"'"'"' =:]+[0-9a-f]{32}|sk-ant-(api|oat)[0-9A-Za-z_-]{20,}|sk-proj-[0-9A-Za-z_-]{20,}|sk-ws-[0-9A-Za-z_-]{20,})' \
    -- . ':!*.jpg' ':!*.jpeg' ':!*.png' ':!*.heic' ':!*.heif' \
    ':!scripts/check_public_safety.sh' \
    2>/dev/null | grep -vE 'FAKE_TOKEN|oat01-NEW|EXAMPLE|placeholder' || true
)

if [ -n "$secret_matches" ]; then
  echo "$secret_matches" >&2
  fail "secret-like values found in tracked files"
fi

# HISTORY, not just HEAD. A credential committed once and "scrubbed"
# later is still public forever the moment the repo is — which is exactly
# what happened here: a real Gemini key lived in config.py from the
# initial commit until 3b89473 removed it (found 2026-08-01). This check
# is opt-in (PUBLIC_SAFETY_SCAN_HISTORY=1) because a full-history grep is
# slow; run it before ANY push to a public remote.
if [ "${PUBLIC_SAFETY_SCAN_HISTORY:-}" = "1" ]; then
  # Which history to walk. Default --all (every ref: "is this repo safe
  # to publish?"). The publish script narrows it to the orphan branch it
  # just built, because that branch's history is what actually ships —
  # main's dirty ancestry is deliberately NOT part of it.
  history_refs="${PUBLIC_SAFETY_HISTORY_REFS:---all}"
  history_matches=$(
    git rev-list $history_refs | while read -r commit; do
      # NO -I: the initial commit's __pycache__/*.pyc blobs provably
      # contain the Gemini, Telegram and PushPlus secrets as string
      # constants, and -I (skip binaries) walked straight past them. Any
      # future .pyc, .db or bundled APK would hide a key the same way.
      git grep -l -E \
        '(AIza[0-9A-Za-z_-]{30,}|sk-ant-api[0-9A-Za-z_-]{20,}|sk-ant-oat[0-9A-Za-z_-]{20,}|sk-proj-[0-9A-Za-z_-]{20,}|sk-[A-Za-z0-9]{32,}|[0-9]{9,10}:AA[A-Za-z0-9_-]{30,}|PUSHPLUS_TOKEN[\"'"'"' =:]+[0-9a-f]{32})' \
        "$commit" -- . ':!tests/*' ':!scripts/check_public_safety.sh' \
        2>/dev/null | sed "s|^|$commit |"
    done
  )
  if [ -n "$history_matches" ]; then
    echo "$history_matches" >&2
    fail "secret-like values found in git HISTORY (rewrite required before publishing)"
  fi
  echo "history scan clean"
fi

echo "public safety check passed"
