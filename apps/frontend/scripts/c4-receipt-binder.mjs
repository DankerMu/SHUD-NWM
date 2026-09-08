#!/usr/bin/env node
/**
 * C4 live evidence acceptance binder (#1895).
 *
 * Node-20 stdlib only. Accepts EXACTLY the schema-1.0 PASS terminal.
 *
 *   node apps/frontend/scripts/c4-receipt-binder.mjs \
 *     --receipt "$RECEIPT" \
 *     --frontend-origin "$PLAYWRIGHT_LIVE_BASE_URL" \
 *     --api-origin "$PLAYWRIGHT_LIVE_API_BASE_URL" \
 *     --basin-id "$PLAYWRIGHT_LIVE_C4_BASIN_ID" \
 *     --segment-id "$PLAYWRIGHT_LIVE_C4_SEGMENT_ID" \
 *     --cmd-start "$CMD_START" --cmd-end "$CMD_END"
 */

import { acceptC4Receipt } from './c4-receipt-binder-core.mjs'

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
const result = acceptC4Receipt(args)
if (!result.ok) fail(result.message)
process.stdout.write('BINDER: PASS\n')
