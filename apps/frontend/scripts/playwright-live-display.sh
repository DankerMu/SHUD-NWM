#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "--" ]]; then
  shift
fi

export VITE_API_BASE_URL="${PLAYWRIGHT_LIVE_API_BASE_URL:-}"
exec playwright test --config playwright.live-display.config.ts "$@"
