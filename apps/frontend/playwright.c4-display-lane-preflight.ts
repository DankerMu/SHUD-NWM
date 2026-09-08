/**
 * C4 current-identity preflight: reuse the merged #1970 bounded preflight
 * primitives (URL/body/depth/source/version semantics) instead of forking them.
 * C4 records only the closed identity fields; geometry stays out of the receipt.
 */

import type { C4DisplayConfig } from './src/lib/c4DisplayEvidence/config'
import type { C4Failure, C4ProductIdentity, C4RequestedPins } from './src/lib/c4DisplayEvidence/receipt'
import { C4_FAILURE_MESSAGE_MAX_BYTES, C4_WHOLE_RUN_DEADLINE_MS } from './src/lib/c4DisplayEvidence/constants'
import { createRiverClickDeadline, type RiverClickDeadline } from './src/lib/riverClickEvidence/deadline'
import { resolveRiverClickIdentity } from './playwright.river-click-lane-preflight'
import type { RiverClickConfig } from './src/lib/riverClickEvidence/config'
import type { RiverClickFailure } from './src/lib/riverClickEvidence/receipt'

export interface C4LaneIdentity {
  requestedPins: C4RequestedPins
  gfs: C4ProductIdentity
  ifs: C4ProductIdentity
}

export function boundedC4Message(message: string): string {
  try {
    const text = String(message)
    const max = C4_FAILURE_MESSAGE_MAX_BYTES
    const suffix = '... [truncated]'
    const suffixBytes = new TextEncoder().encode(suffix).byteLength
    if (new TextEncoder().encode(text).byteLength <= max) return text
    let out = ''
    let bytes = 0
    for (const ch of text) {
      const chBytes = new TextEncoder().encode(ch).byteLength
      if (bytes + chBytes > max - suffixBytes) break
      out += ch
      bytes += chBytes
    }
    return `${out}${suffix}`
  } catch {
    return 'C4 lane failure'
  }
}

export function c4FailureOf(
  code: C4Failure['code'],
  stage: C4Failure['stage'],
  message: string,
): C4Failure {
  return { code, stage, message: boundedC4Message(message) }
}

export function mapRiverFailure(failure: RiverClickFailure): C4Failure {
  const mapped = failure.code as C4Failure['code']
  const stage = failure.stage === 'preflight' || failure.stage === 'runtime' || failure.stage === 'config'
    ? failure.stage
    : 'preflight'
  const safeMessages: Partial<Record<C4Failure['code'], string>> = {
    CONFIG_INVALID: 'C4 configuration is invalid',
    PREFLIGHT_HTTP_ERROR: 'C4 preflight request failed',
    PREFLIGHT_RESPONSE_INVALID: 'C4 preflight response is invalid',
    PRODUCT_UNAVAILABLE: 'current product is unavailable',
    IDENTITY_MISMATCH: 'current product identity does not match',
    SEGMENT_GEOMETRY_INVALID: 'selected segment geometry is invalid',
    WHOLE_RUN_TIMEOUT: 'whole-run deadline exceeded during preflight',
    INTERNAL_ERROR: 'C4 preflight failed',
  }
  return c4FailureOf(mapped, stage, safeMessages[mapped] ?? 'C4 preflight failed')
}

function asRiverConfig(config: C4DisplayConfig): RiverClickConfig {
  return {
    frontendOrigin: config.frontendOrigin,
    apiOrigin: config.apiOrigin,
    basinId: config.basinId,
    segmentId: config.segmentId,
    receiptPath: config.receiptPath,
  }
}

export async function resolveC4Identity(
  config: C4DisplayConfig,
  fetchImpl: (url: string, init: RequestInit) => Promise<Response>,
  deadline: RiverClickDeadline = createRiverClickDeadline(C4_WHOLE_RUN_DEADLINE_MS),
): Promise<{ ok: true; identity: C4LaneIdentity } | { ok: false; failure: C4Failure }> {
  const resolved = await resolveRiverClickIdentity(asRiverConfig(config), fetchImpl, deadline)
  if (!resolved.ok) return { ok: false, failure: mapRiverFailure(resolved.failure) }
  return {
    ok: true,
    identity: {
      requestedPins: {
        basinId: resolved.identity.requestedFeature.basinId,
        riverSegmentId: resolved.identity.requestedFeature.riverSegmentId,
      },
      gfs: resolved.identity.gfs,
      ifs: resolved.identity.ifs,
    },
  }
}
