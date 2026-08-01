#!/usr/bin/env bash
# Publish the CURRENT TREE to the public remote as a SINGLE clean commit.
#
# Why an orphan commit instead of pushing main: main's history contains
# three real credentials from the early commits (a Gemini key, the
# Telegram bot token, a PushPlus token — in source, launchd plists, log
# files and compiled .pyc blobs). "Scrub" commits removed them from the
# working tree, but git keeps every prior version, and history is what
# publication exposes. The public remote therefore carries a DISJOINT
# history that starts at the already-clean present.
#
# Consequence to respect forever: NEVER `git push public main`. This
# script is the only sanctioned path.
set -euo pipefail
cd "$(dirname "$0")/.."

REMOTE="${PUBLIC_REMOTE:-public}"
VERSION="$(grep '^version:' app/pubspec.yaml | awk '{print $2}' | cut -d+ -f1)"
SRC_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
TMP_BRANCH="public-release-$(date +%Y%m%d-%H%M%S)"

if [ -n "$(git status --porcelain)" ]; then
  echo "working tree is dirty — commit or stash first" >&2
  exit 1
fi

echo "==> Safety checks on the tree being published"
bash scripts/check_public_safety.sh

echo "==> Building the single-commit tree on $TMP_BRANCH"
git checkout -q --orphan "$TMP_BRANCH"
git add -A
git commit -q -m "CalorieTracker v$VERSION

Personal calorie tracking from food photos: a Telegram bot + Flask API,
and a Flutter app (iOS + Android) that runs the same pipeline on-device.

Published with a fresh history on purpose — see scripts/publish_public.sh."

echo "==> Verifying the NEW history has no secrets"
PUBLIC_SAFETY_SCAN_HISTORY=1 bash scripts/check_public_safety.sh

echo "==> Pushing to $REMOTE/main"
git push --force "$REMOTE" "$TMP_BRANCH:main"

git checkout -q "$SRC_BRANCH"
git branch -D "$TMP_BRANCH" >/dev/null
echo "published v$VERSION to $REMOTE/main (single commit, clean history)"
