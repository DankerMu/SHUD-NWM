#!/usr/bin/env node
/**
 * River-click live evidence acceptance binder (#1970 execution runner #1895).
 *
 * Node-20 stdlib only (no runtime dependency): the node-27 runner and the
 * frontend CI test share this one binder, so a checked-in `.mjs` is simpler
 * than provisioning Python/uv in frontend CI. It accepts EXACTLY the schema-1.0
 * PASS terminal the live spec can publish, and independently recomputes the
 * nearest-rank P95 from the actual sample durations.
 *
 * Usage (set -euo pipefail context; the runbook wraps this in the command
 * bracket so CMD_START/CMD_END/mode/owner/nlink are checked by the shell
 * preamble/first half):
 *
 *   node apps/frontend/scripts/river-click-receipt-binder.mjs \
 *     --receipt "$RECEIPT" \
 *     --frontend-origin "$PLAYWRIGHT_LIVE_BASE_URL" \
 *     --api-origin "$PLAYWRIGHT_LIVE_API_BASE_URL" \
 *     --basin-id "$PLAYWRIGHT_LIVE_RIVER_BASIN_ID" \
 *     --segment-id "$PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID" \
 *     --cmd-start "$CMD_START" --cmd-end "$CMD_END"
 *
 * Failure mode: exit 1 with one bounded `BINDER:` line per violated fact; no
 * file is written or modified. The receipt itself is never trusted from its
 * declared counts — count/P95/identity coherence is recomputed here.
 *
 * Diagnostics are strictly bounded and fixed-shaped: the receipt path, OS
 * error text, configured origins, identities, and receipt values are NEVER
 * echoed. Success prints only `BINDER: PASS (p95_ms <closed number>)` — no
 * path.
 */

import { acceptRiverClickReceipt } from './river-click-receipt-binder-core.mjs'

function fail(message) {
  process.stderr.write(`BINDER: ${message}\n`)
  process.exit(1)
}

function parseArgs(argv) {
  const out = {}
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i]
    if (!arg.startsWith('--')) continue
    const key = arg.slice(2)
    const value = argv[i + 1]
    if (value === undefined || value.startsWith('--')) continue
    out[key] = value
    i += 1
  }
  return out
}

const args = parseArgs(process.argv.slice(2))
const result = acceptRiverClickReceipt(args)
if (!result.ok) fail(result.message)
process.stdout.write(`BINDER: PASS (p95_ms ${result.p95})\n`)
