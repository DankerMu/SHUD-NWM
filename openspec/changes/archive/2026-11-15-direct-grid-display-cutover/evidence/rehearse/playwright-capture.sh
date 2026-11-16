#!/usr/bin/env bash
# Screenshot capture runner for the display-cutover rehearsal.
#
# Runs on node-27 in parallel with rehearse.py's SCREENSHOT_WINDOW block.
# Copies the standalone rehearsal Playwright spec into the frontend's
# live-display test lane (which requires the spec filename to be exactly
# `live-display.spec.ts` per `apps/frontend/playwright.live-display.config.ts`
# testMatch pattern), runs Playwright against the live public host, then
# restores the original spec.
#
# Usage (node-27):
#     bash rehearse/playwright-capture.sh
#
# Emits: rehearse/retention-empty-state.png (populated on success).
set -euo pipefail

EVIDENCE_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$EVIDENCE_DIR/../../../../.." && pwd)"
FRONTEND_DIR="$REPO_ROOT/apps/frontend"
LIVE_SPEC_TARGET="$FRONTEND_DIR/e2e/live-display.spec.ts"
LIVE_SPEC_BACKUP="$FRONTEND_DIR/e2e/live-display.spec.ts.rehearsal-backup"
REHEARSAL_SPEC_SRC="$EVIDENCE_DIR/../screenshot/nwm-retention-empty-state.spec.ts"

: "${PLAYWRIGHT_LIVE_BASE_URL:=https://test.nwm.ac.cn}"
: "${PLAYWRIGHT_LIVE_API_BASE_URL:=https://test.nwm.ac.cn}"
export PLAYWRIGHT_LIVE_BASE_URL
export PLAYWRIGHT_LIVE_API_BASE_URL

echo "[playwright-capture] EVIDENCE_DIR=$EVIDENCE_DIR"
echo "[playwright-capture] REPO_ROOT=$REPO_ROOT"
echo "[playwright-capture] LIVE URLs: base=$PLAYWRIGHT_LIVE_BASE_URL api=$PLAYWRIGHT_LIVE_API_BASE_URL"

if [[ ! -f "$REHEARSAL_SPEC_SRC" ]]; then
  echo "[playwright-capture] FATAL: rehearsal spec not found at $REHEARSAL_SPEC_SRC" >&2
  exit 2
fi

cleanup() {
  local rc=$?
  echo "[playwright-capture] cleanup (rc=$rc): restoring original live-display spec"
  if [[ -f "$LIVE_SPEC_BACKUP" ]]; then
    mv -f "$LIVE_SPEC_BACKUP" "$LIVE_SPEC_TARGET"
  fi
  return $rc
}
trap cleanup EXIT

# Backup original live-display.spec.ts and drop in the rehearsal spec.
if [[ -f "$LIVE_SPEC_TARGET" ]]; then
  cp -f "$LIVE_SPEC_TARGET" "$LIVE_SPEC_BACKUP"
fi
cp -f "$REHEARSAL_SPEC_SRC" "$LIVE_SPEC_TARGET"
echo "[playwright-capture] rehearsal spec staged into $LIVE_SPEC_TARGET"

cd "$FRONTEND_DIR"

# Ensure Playwright is available. node-27 has the chromium binary cached at
# ~/.cache/ms-playwright/chromium-1217/... but node_modules may be missing.
if [[ ! -d node_modules/@playwright/test ]]; then
  echo "[playwright-capture] installing frontend dependencies (frozen lockfile)"
  CI=true corepack pnpm install --frozen-lockfile
fi

# Run the rehearsal spec via the live-display lane config.
echo "[playwright-capture] running playwright test (live-display lane)"
corepack pnpm exec playwright test --config playwright.live-display.config.ts

# Playwright emits the screenshot to `retention-empty-state.png` in the
# working directory (see the spec's `page.screenshot({ path: ... })`).
SCREENSHOT_SRC="$FRONTEND_DIR/retention-empty-state.png"
SCREENSHOT_DEST="$EVIDENCE_DIR/retention-empty-state.png"
if [[ -f "$SCREENSHOT_SRC" ]]; then
  mv -f "$SCREENSHOT_SRC" "$SCREENSHOT_DEST"
  echo "[playwright-capture] screenshot -> $SCREENSHOT_DEST"
else
  echo "[playwright-capture] WARNING: screenshot not emitted at $SCREENSHOT_SRC" >&2
fi
