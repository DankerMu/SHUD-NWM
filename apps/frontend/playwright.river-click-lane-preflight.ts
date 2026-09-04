/**
 * Bounded preflight half of the browser-side no-mock river-click P95 lane
 * (#1970): shared failure helpers, the bounded Response reader, and the exact
 * 3-request identity resolution. Pure logic with an injected minimal Playwright
 * surface so it is unit-testable in vitest without a browser.
 *
 * Series evidence uses request method/URL identity, response status and network
 * completion only — never series bodies or headers and never raw request
 * URL/query text.
 */

import {
  RIVER_CLICK_FAILURE_MESSAGE_MAX_BYTES,
  RIVER_CLICK_PER_SAMPLE_DEADLINE_MS,
  RIVER_CLICK_PREFLIGHT_MAX_BODY_BYTES,
  RIVER_CLICK_WHOLE_RUN_DEADLINE_MS,
} from './src/lib/riverClickEvidence/constants'
import type { RiverClickConfig } from './src/lib/riverClickEvidence/config'
import { createRiverClickDeadline, withRiverClickDeadline, type RiverClickDeadline } from './src/lib/riverClickEvidence/deadline'
import {
  classifyRiverClickPreflightResponse,
  normalizeRiverClickEnvelope,
  parseRiverClickPreflightProduct,
  parseRiverClickPreflightSegment,
  type RiverClickPreflightResponseSource,
  type RiverClickProductSourceIdentity,
} from './src/lib/riverClickEvidence/preflight'
import type { RiverClickProductRequestIdentity } from './src/lib/riverClickEvidence/requestMatching'
import type {
  RiverClickFeatureIdentity,
  RiverClickFailure,
  RiverClickProductIdentity,
} from './src/lib/riverClickEvidence/receipt'

export interface RiverClickLaneBrowserRequest {
  method(): string
  url(): string
}

export interface RiverClickLaneBrowserResponse {
  url(): string
  status(): number
  finished(): Promise<unknown>
  /** Correlation back to the originating Request; absent on synthetic seams. */
  request?: () => RiverClickLaneBrowserRequest
  /** Fallback correlation by method; absent on synthetic seams. */
  method?(): string
}

/**
 * Minimal disposable page-side object reference (JSHandle in real Playwright:
 * `dispose(): Promise<void>` is the exact Playwright JSHandle surface the
 * attempt needs). The attempt creates TWO handles per attempt and disposes both
 * on every terminal; disposal failure must never mask the attempt terminal.
 */
export interface RiverClickJsHandle {
  dispose(): Promise<void>
}

export interface RiverClickLanePageSurface {
  goto(url: string): Promise<unknown>
  addInitScript(script: string | (() => void)): Promise<unknown>
  waitForTimeout(ms: number): Promise<unknown>
  evaluate<T>(fn: string | ((...args: unknown[]) => T | Promise<T>), ...args: unknown[]): Promise<T>
  /** Retain a page-side object reference across separate evaluates (JSHandle in
   *  real Playwright; the attempt uses it to prove the exact pre-dispatch hook
   *  and m11-map-surface node survive through the scoped close and the quiet
   *  interval). Every created handle MUST be disposed by the attempt. */
  evaluateHandle<T>(fn: string | ((...args: unknown[]) => T), ...args: unknown[]): Promise<RiverClickJsHandle>
  on(event: 'request' | 'response' | 'requestfailed', listener: (...args: never[]) => void): unknown
  off(event: 'request' | 'response' | 'requestfailed', listener: (...args: never[]) => void): unknown
  requests(): unknown
}

export interface RiverClickLaneEnv {
  config: RiverClickConfig
  page: RiverClickLanePageSurface
}

export interface RiverClickLaneIdentity {
  requestedFeature: RiverClickFeatureIdentity
  /** No rendered feature here: provenance begins at the first hook dispatch. */
  gfs: RiverClickProductIdentity
  ifs: RiverClickProductIdentity
  preflightGfs: RiverClickProductRequestIdentity
  preflightIfs: RiverClickProductRequestIdentity
  bbox: [[number, number], [number, number]]
  anchor: [number, number]
}

export type RiverClickFailureShape = RiverClickFailure

export const TIMEOUT_SENTINEL = Symbol('river-click-deadline-expired')

export function timeoutValue<T>(): T {
  return TIMEOUT_SENTINEL as unknown as T
}

/**
 * UTF-8 byte-safe truncation to RIVER_CLICK_FAILURE_MESSAGE_MAX_BYTES. Plain
 * code-point slicing is NOT byte-safe (480 surrogate pairs = 1920 UTF-8 bytes),
 * so truncation walks code points and stops before adding one that would
 * overflow the byte ceiling, then appends a fixed suffix.
 */
export function boundedMessage(message: string): string {
  try {
    const text = String(message)
    const max = RIVER_CLICK_FAILURE_MESSAGE_MAX_BYTES
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
    return 'river-click lane failure'
  }
}

export function failureOf(
  code: RiverClickFailureShape['code'],
  stage: RiverClickFailureShape['stage'],
  message: string,
  sampleIndex: number | null = null,
  gfsStatus: number | null = null,
  ifsStatus: number | null = null,
): RiverClickFailureShape {
  return { code, stage, sampleIndex, gfsStatus, ifsStatus, message: boundedMessage(message) }
}

export function simpleFailure(
  code: RiverClickFailureShape['code'],
  stage: RiverClickFailureShape['stage'],
  message: string,
  sampleIndex: number | null = null,
  gfsStatus: number | null = null,
  ifsStatus: number | null = null,
): { ok: false; failure: RiverClickFailureShape } {
  return { ok: false, failure: failureOf(code, stage, message, sampleIndex, gfsStatus, ifsStatus) }
}

function productIdentityFromPreflight(product: RiverClickProductSourceIdentity): RiverClickProductIdentity {
  return {
    sourceId: product.source_id,
    basinId: product.basin_id,
    basinVersionId: product.basin_version_id,
    riverNetworkVersionId: product.river_network_version_id,
    runId: product.run_id,
    modelId: product.model_id,
    cycleTime: product.cycle_time,
    scenario: product.scenario,
  }
}

/**
 * Bounded streaming reader over Response.body.getReader(). A null body means
 * ZERO bytes (never an unbounded arrayBuffer fallback). At most maxBytes+1
 * bytes are ever retained; exactly maxBytes+1 retained means overflow=true.
 * An abort signal cancels/errors the reader so no read hangs.
 */
export async function readResponseBounded(
  response: Response,
  maxBytes: number,
  options: { signal?: AbortSignal } = {},
): Promise<{ bytes: Uint8Array; overflow: boolean }> {
  const body = response.body
  if (body === null) {
    return { bytes: new Uint8Array(0), overflow: false }
  }
  const reader = body.getReader()
  const chunks: Uint8Array[] = []
  let total = 0
  let overflow = false
  const onAbort = () => {
    try {
      void reader.cancel().catch(() => undefined)
    } catch {
      // already released
    }
  }
  options.signal?.addEventListener('abort', onAbort, { once: true })
  try {
    for (;;) {
      let result: ReadableStreamReadResult<Uint8Array>
      try {
        result = await reader.read()
      } catch (error) {
        // An ABORT is a bounded timeout (the caller cancels the reader and
        // classifies against the deadline). Any OTHER stream error must fail
        // the read: accepting already-collected bytes could turn a truncated
        // body into a complete-looking valid preflight response.
        if (options.signal?.aborted) break
        throw new Error('preflight response body stream failed')
      }
      const { done, value } = result
      if (done) break
      if (total + value.byteLength > maxBytes + 1) {
        const keep = Math.max(0, maxBytes + 1 - total)
        chunks.push(value.subarray(0, keep).slice())
        total += keep
        overflow = true
        break
      }
      chunks.push(value)
      total += value.byteLength
    }
    if (total > maxBytes) overflow = true
  } finally {
    options.signal?.removeEventListener('abort', onAbort)
    try {
      await reader.cancel()
    } catch {
      // ignore cancel errors; classification uses the retained bytes
    }
  }
  const merged = new Uint8Array(total)
  let offset = 0
  for (const part of chunks) {
    merged.set(part, offset)
    offset += part.byteLength
  }
  return { bytes: merged, overflow }
}

function responseAdapter(response: Response, signal: AbortSignal | undefined): RiverClickPreflightResponseSource {
  return {
    url: () => response.url,
    status: () => response.status,
    headerValue: async (name: string) => response.headers.get(name),
    readBounded: async (maxBytes: number) => {
      const { bytes } = await readResponseBounded(response, maxBytes, { signal })
      return bytes
    },
  }
}

async function fetchPreflight(
  url: string,
  fetchImpl: (url: string, init: RequestInit) => Promise<Response>,
  deadline: RiverClickDeadline,
): Promise<{ ok: true; payload: Record<string, unknown> } | { ok: false; failure: RiverClickFailureShape }> {
  const controller = new AbortController()
  const abortOnExpiry = () => {
    try {
      controller.abort()
    } catch {
      // abort is best-effort
    }
  }
  let response: Response
  try {
    response = await withRiverClickDeadline(
      fetchImpl(url, {
        credentials: 'omit',
        headers: { 'Accept-Encoding': 'identity' },
        redirect: 'manual',
        signal: controller.signal,
      }),
      deadline,
      () => {
        abortOnExpiry()
        return timeoutValue()
      },
    )
  } catch {
    if (deadline.expired()) {
      return simpleFailure('WHOLE_RUN_TIMEOUT', 'preflight', 'whole-run deadline exceeded during preflight')
    }
    return simpleFailure('PREFLIGHT_HTTP_ERROR', 'preflight', 'preflight request failed')
  }
  if ((response as unknown) === TIMEOUT_SENTINEL) {
    return simpleFailure('WHOLE_RUN_TIMEOUT', 'preflight', 'whole-run deadline exceeded during preflight')
  }
  // Manual redirect semantics: the resolved response URL must be the exact
  // requested URL (no redirect was followed, no credential-bearing URL).
  let resolvedUrl: string
  try {
    resolvedUrl = response.url
  } catch {
    resolvedUrl = ''
  }
  if (resolvedUrl !== '' && resolvedUrl !== url) {
    try {
      const resolved = new URL(resolvedUrl)
      const requested = new URL(url)
      if (resolved.origin !== requested.origin) {
        return simpleFailure('PREFLIGHT_HTTP_ERROR', 'preflight', 'preflight response resolved to a foreign origin')
      }
      if (resolved.href !== requested.href) {
        return simpleFailure('PREFLIGHT_RESPONSE_INVALID', 'preflight', 'preflight response URL differs from the requested URL')
      }
    } catch {
      return simpleFailure('PREFLIGHT_RESPONSE_INVALID', 'preflight', 'preflight response URL is malformed')
    }
  }
  if (!response.ok) {
    return simpleFailure('PREFLIGHT_HTTP_ERROR', 'preflight', `preflight status ${response.status}`)
  }
  let classified
  try {
    classified = await withRiverClickDeadline(
      classifyRiverClickPreflightResponse(responseAdapter(response, controller.signal)),
      deadline,
      () => {
        // BODY classification expiry must abort the shared controller too, so
        // the bounded reader cancels and no reader/timer stays pending.
        abortOnExpiry()
        return timeoutValue()
      },
    )
  } catch {
    if (deadline.expired()) {
      return simpleFailure('WHOLE_RUN_TIMEOUT', 'preflight', 'whole-run deadline exceeded during preflight read')
    }
    return simpleFailure('PREFLIGHT_RESPONSE_INVALID', 'preflight', 'preflight response is unreadable')
  }
  if ((classified as unknown) === TIMEOUT_SENTINEL) {
    return simpleFailure('WHOLE_RUN_TIMEOUT', 'preflight', 'whole-run deadline exceeded during preflight read')
  }
  if (!classified.ok) {
    return simpleFailure('PREFLIGHT_RESPONSE_INVALID', 'preflight', `preflight body invalid: ${classified.message}`)
  }
  const data = normalizeRiverClickEnvelope(classified.payload)
  if (data === null) {
    return simpleFailure('PREFLIGHT_RESPONSE_INVALID', 'preflight', 'preflight envelope is not {status:"ok",data:{...}}')
  }
  return { ok: true, payload: data }
}

/**
 * Bounded preflight owner: one identity-only GFS + IFS latest-product and one
 * exact segment detail. No redirects; credentials omitted; Accept-Encoding
 * identity; bounded bytes; one monotonic deadline from before the first fetch.
 */
export async function resolveRiverClickIdentity(
  config: RiverClickConfig,
  fetchImpl: (url: string, init: RequestInit) => Promise<Response>,
  deadline: RiverClickDeadline = createRiverClickDeadline(RIVER_CLICK_WHOLE_RUN_DEADLINE_MS),
): Promise<{ ok: true; identity: RiverClickLaneIdentity } | { ok: false; failure: RiverClickFailureShape }> {
  const base = `${config.apiOrigin}/api/v1`
  const gfsUrl = `${base}/mvp/qhh/latest-product?source=GFS&identity_only=true&basin_id=${encodeURIComponent(config.basinId)}`
  const ifsUrl = `${base}/mvp/qhh/latest-product?source=IFS&identity_only=true&basin_id=${encodeURIComponent(config.basinId)}`

  const gfsFetch = await fetchPreflight(gfsUrl, fetchImpl, deadline)
  if (!gfsFetch.ok) return { ok: false, failure: gfsFetch.failure }
  const ifsFetch = await fetchPreflight(ifsUrl, fetchImpl, deadline)
  if (!ifsFetch.ok) return { ok: false, failure: ifsFetch.failure }

  const gfs = parseRiverClickPreflightProduct(gfsFetch.payload, 'GFS', config.basinId)
  if (!gfs.ok) return simpleFailure('PRODUCT_UNAVAILABLE', 'preflight', 'GFS product identity is unavailable')
  const ifs = parseRiverClickPreflightProduct(ifsFetch.payload, 'IFS', config.basinId)
  if (!ifs.ok) return simpleFailure('PRODUCT_UNAVAILABLE', 'preflight', 'IFS product identity is unavailable')

  if (gfs.product.basin_version_id !== ifs.product.basin_version_id) {
    return simpleFailure('IDENTITY_MISMATCH', 'preflight', 'GFS/IFS basin_version_id differ')
  }
  if (gfs.product.river_network_version_id !== ifs.product.river_network_version_id) {
    return simpleFailure('IDENTITY_MISMATCH', 'preflight', 'GFS/IFS river_network_version_id differ')
  }

  const detailUrl = `${base}/basin-versions/${encodeURIComponent(gfs.product.basin_version_id)}/river-segments/${encodeURIComponent(config.segmentId)}?river_network_version_id=${encodeURIComponent(gfs.product.river_network_version_id)}`
  const detailFetch = await fetchPreflight(detailUrl, fetchImpl, deadline)
  if (!detailFetch.ok) return { ok: false, failure: detailFetch.failure }

  const segment = parseRiverClickPreflightSegment(
    detailFetch.payload,
    config.segmentId,
    gfs.product.river_network_version_id,
    gfs.product.basin_version_id,
  )
  if (!segment.ok) {
    return simpleFailure('SEGMENT_GEOMETRY_INVALID', 'preflight', 'segment geometry is invalid')
  }

  const requestedFeature: RiverClickFeatureIdentity = {
    basinId: config.basinId,
    riverSegmentId: config.segmentId,
    basinVersionId: gfs.product.basin_version_id,
    riverNetworkVersionId: gfs.product.river_network_version_id,
  }
  return {
    ok: true,
    identity: {
      requestedFeature,
      // No rendered feature here: provenance begins at the first hook dispatch.
      gfs: productIdentityFromPreflight(gfs.product),
      ifs: productIdentityFromPreflight(ifs.product),
      preflightGfs: {
        source: 'GFS',
        scenario: gfs.product.scenario,
        runId: gfs.product.run_id,
        modelId: gfs.product.model_id,
        issueTime: gfs.product.cycle_time,
        riverNetworkVersionId: gfs.product.river_network_version_id,
      },
      preflightIfs: {
        source: 'IFS',
        scenario: ifs.product.scenario,
        runId: ifs.product.run_id,
        modelId: ifs.product.model_id,
        issueTime: ifs.product.cycle_time,
        riverNetworkVersionId: ifs.product.river_network_version_id,
      },
      bbox: segment.segment.bbox,
      anchor: segment.segment.anchor,
    },
  }
}

/** Default whole-run deadline used by single-call preflight tests. */
export function defaultWholeRunDeadline(now: () => number = () => performance.now()): RiverClickDeadline {
  return createRiverClickDeadline(RIVER_CLICK_WHOLE_RUN_DEADLINE_MS, now)
}

/** Keep the per-sample deadline constant importable for the orchestrator. */
export { RIVER_CLICK_PER_SAMPLE_DEADLINE_MS }
export type { RiverClickConfig }
