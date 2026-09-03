import {
  HOOK_QUERY_LIMIT,
  HOOK_QUERY_SIZE_PX,
  HOOK_READY_TIMEOUT_MS,
  RIVER_CLICK_HOOK_CODES,
  RIVER_CLICK_M11_IDENTIFIER_PATTERN,
  RIVER_CLICK_PER_MAP_DEADLINE_MS,
  type RiverClickHookCode,
} from './constants'
import { createRiverClickDeadline, type RiverClickDeadline } from './deadline'

export { HOOK_QUERY_LIMIT, HOOK_QUERY_SIZE_PX, HOOK_READY_TIMEOUT_MS }

/** Minimal MapLibre map surface consumed by the river-click hook. */
export interface RiverClickHookMap {
  loaded(): boolean
  isStyleLoaded(): boolean
  fitBounds(bounds: [[number, number], [number, number]], options: { padding: number; duration: number; maxZoom: number }): unknown
  project(coord: [number, number]): { x: number; y: number }
  queryRenderedFeatures(box: [{ x: number; y: number }, { x: number; y: number }], options: { layers: string[] }): unknown[]
  getCanvas(): { style: { cursor: string } }
  once(event: string, callback: () => void): unknown
  off?(event: string, callback: () => void): unknown
}

export interface RiverClickRenderedFeature {
  id?: unknown
  layer?: { id?: string }
  geometry?: { type?: string; coordinates?: unknown } | null
  properties?: Record<string, unknown>
}

export interface RiverClickHookSelectionInput {
  bbox: [[number, number], [number, number]]
  anchor: [number, number]
  basinId: string
  riverSegmentId: string
  basinVersionId: string
  riverNetworkVersionId: string
}

export interface RiverClickNormalizedFeatureIdentity {
  basinId: string
  riverSegmentId: string
  basinVersionId: string
  riverNetworkVersionId: string
}

export interface RiverClickHookSelectionOutput {
  feature: RiverClickRenderedFeature
  normalized: RiverClickNormalizedFeatureIdentity
}

export type RiverClickHookSelectionResult =
  | { ok: true; output: RiverClickHookSelectionOutput }
  | { ok: false; code: RiverClickHookCode; message: string }

export function normalizeRiverClickFeatureIdentity(
  feature: RiverClickRenderedFeature,
): RiverClickNormalizedFeatureIdentity | null {
  const geometry = feature.geometry
  if (!geometry || typeof geometry !== 'object' || geometry.type === undefined) return null
  const properties = feature.properties
  if (!properties || typeof properties !== 'object') return null
  const basinId = stringProperty(properties, 'basin_id')
  const basinVersionId = stringProperty(properties, 'basin_version_id')
  const riverNetworkVersionId = stringProperty(properties, 'river_network_version_id')
  const riverSegmentId = stringProperty(properties, 'river_segment_id') ?? stringProperty(properties, 'segment_id')
  const segmentId = stringProperty(properties, 'segment_id')
  if (riverSegmentId === null || basinId === null || basinVersionId === null || riverNetworkVersionId === null) return null
  // When both river_segment_id and segment_id are present they must be equal.
  if (segmentId !== null && segmentId !== riverSegmentId) return null
  return { basinId, riverSegmentId, basinVersionId, riverNetworkVersionId }
}

function stringProperty(properties: Record<string, unknown>, key: string): string | null {
  const value = properties[key]
  return typeof value === 'string' && value.length > 0 ? value : null
}

function finiteCoordinate(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function isFiniteWgs84(lon: number, lat: number): boolean {
  return lon >= -180 && lon <= 180 && lat >= -90 && lat <= 90
}

function validAnchor(anchor: [number, number]): boolean {
  const lon = finiteCoordinate(anchor[0])
  const lat = finiteCoordinate(anchor[1])
  return lon !== null && lat !== null && isFiniteWgs84(lon, lat)
}

/** Smallest documented bounded WGS84 epsilon used to de-degenerate the bbox. */
export const RIVER_CLICK_BBOX_EPSILON_DEG = 1e-6

function clampLon(lon: number): number {
  return Math.min(180, Math.max(-180, lon))
}

function clampLat(lat: number): number {
  return Math.min(90, Math.max(-90, lat))
}

function validBbox(bbox: [[number, number], [number, number]]): boolean {
  const [[minLon, minLat], [maxLon, maxLat]] = bbox
  const minLonN = finiteCoordinate(minLon)
  const minLatN = finiteCoordinate(minLat)
  const maxLonN = finiteCoordinate(maxLon)
  const maxLatN = finiteCoordinate(maxLat)
  if (minLonN === null || minLatN === null || maxLonN === null || maxLatN === null) return false
  if (!isFiniteWgs84(minLonN, minLatN) || !isFiniteWgs84(maxLonN, maxLatN)) return false
  // Ordered and non-degenerate: a point bbox (both dims zero) is rejected; a
  // zero-width or zero-height axis-aligned extent is allowed because the
  // preflight pads such extents with the documented WGS84 epsilon.
  if (minLonN > maxLonN || minLatN > maxLatN) return false
  if (minLonN === maxLonN && minLatN === maxLatN) return false
  return true
}

/**
 * Pad a degenerate (zero-width or zero-height) extent with the smallest
 * documented bounded WGS84 epsilon while still covering all coordinates and
 * clamping at the world edges.
 */
export function padRiverClickBbox(bbox: [[number, number], [number, number]]): [[number, number], [number, number]] {
  const [[minLon, minLat], [maxLon, maxLat]] = bbox
  let outMinLon = minLon
  let outMinLat = minLat
  let outMaxLon = maxLon
  let outMaxLat = maxLat
  if (outMinLon === outMaxLon) {
    outMinLon = clampLon(outMinLon - RIVER_CLICK_BBOX_EPSILON_DEG)
    outMaxLon = clampLon(outMaxLon + RIVER_CLICK_BBOX_EPSILON_DEG)
    if (outMinLon === outMaxLon) {
      // At the ±180 clamp edge, both clamp to the same extreme; extend inward.
      if (maxLon >= 180) {
        outMinLon = 180 - RIVER_CLICK_BBOX_EPSILON_DEG
        outMaxLon = 180
      } else if (minLon <= -180) {
        outMinLon = -180
        outMaxLon = -180 + RIVER_CLICK_BBOX_EPSILON_DEG
      }
    }
  }
  if (outMinLat === outMaxLat) {
    outMinLat = clampLat(outMinLat - RIVER_CLICK_BBOX_EPSILON_DEG)
    outMaxLat = clampLat(outMaxLat + RIVER_CLICK_BBOX_EPSILON_DEG)
    if (outMinLat === outMaxLat) {
      if (maxLat >= 90) {
        outMinLat = 90 - RIVER_CLICK_BBOX_EPSILON_DEG
        outMaxLat = 90
      } else if (minLat <= -90) {
        outMinLat = -90
        outMaxLat = -90 + RIVER_CLICK_BBOX_EPSILON_DEG
      }
    }
  }
  return [[outMinLon, outMinLat], [outMaxLon, outMaxLat]]
}

function validIdentity(value: string): boolean {
  return RIVER_CLICK_M11_IDENTIFIER_PATTERN.test(value)
}

export function validateRiverClickSelectionInput(input: RiverClickHookSelectionInput): string | null {
  if (!Array.isArray(input.bbox) || input.bbox.length !== 2 || !validBbox(input.bbox)) return 'invalid bbox'
  if (!Array.isArray(input.anchor) || input.anchor.length !== 2 || !validAnchor(input.anchor)) return 'invalid anchor'
  if (!validIdentity(input.basinId)) return 'invalid basinId'
  if (!validIdentity(input.riverSegmentId)) return 'invalid riverSegmentId'
  if (!validIdentity(input.basinVersionId)) return 'invalid basinVersionId'
  if (!validIdentity(input.riverNetworkVersionId)) return 'invalid riverNetworkVersionId'
  return null
}

const MAX_QUERY_RESULTS = HOOK_QUERY_LIMIT

/**
 * Bounded wait for map.post-fitIdle (the loaded/rendered/settled signal) plus
 * a loaded/style-loaded verification under one shared deadline. The first
 * `idle` event suffices (idle implies rendered/settled); `render` alone is NOT
 * sufficient (it can precede tile idle). Clears the timer and removes every
 * registered listener exactly once on every terminal path.
 */
function waitForPostFitIdle(
  map: RiverClickHookMap,
  startedAt: number,
  now: () => number,
  deadline: RiverClickDeadline,
): Promise<{ done: boolean }> {
  return new Promise((resolve, reject) => {
    let settled = false
    const registered: Array<[string, () => void]> = []
    let timer: ReturnType<typeof setTimeout> | undefined
    const cleanup = () => {
      if (settled) return
      settled = true
      if (timer !== undefined) clearTimeout(timer)
      for (const [event, callback] of registered) {
        try {
          map.off?.(event, callback)
        } catch {
          // listener removal must never mask the terminal state
        }
      }
    }
    const remaining = deadline.remaining()
    if (remaining <= 0) {
      cleanup()
      resolve({ done: false })
      return
    }
    timer = setTimeout(() => {
      if (settled) return
      cleanup()
      resolve({ done: false })
    }, remaining)
    for (const event of ['idle'] as const) {
      const callback = () => {
        if (settled) return
        cleanup()
        resolve({ done: true })
      }
      registered.push([event, callback])
      try {
        map.once?.(event, callback)
      } catch (error) {
        cleanup()
        reject(error)
        return
      }
    }
  })
}

/**
 * Poll the two readiness facts that can arrive asynchronously: a non-empty
 * renderable discharge hit-layer id and a loaded+style-loaded map. The whole
 * wait spends ONE budget (never budget-per-fact); the fit happens after both
 * are true so the hit layer exists when the anchor is queried.
 */
async function waitForMapDischargeReady(
  map: RiverClickHookMap,
  getOverlayHitLayerId: () => string | null,
  now: () => number,
  deadline: RiverClickDeadline,
): Promise<string | null> {
  for (;;) {
    const layer = getOverlayHitLayerId()
    let loaded = false
    try {
      loaded = map.loaded() && map.isStyleLoaded()
    } catch {
      loaded = false
    }
    if (layer !== null && loaded) return layer
    if (deadline.expired()) return null
    // NO unref(): a pending timer keeps the test promise live and resolvable;
    // an unref'd timer can leave a vitest promise hanging until the suite's
    // own timeout instead of the bounded deadline.
    await new Promise<void>((resolve) => {
      const remaining = deadline.remaining()
      setTimeout(resolve, Math.min(50, Math.max(1, remaining)))
    })
  }
}

/**
 * Pure selection core: require the current renderable discharge overlay and a
 * loaded map, then fit the bbox (padding 48, duration 0, maxZoom 14), wait at
 * most the ONE shared deadline for the post-fit `idle` event (idle implies
 * rendered/settled) plus loaded/style-loaded, project the anchor, query the
 * 16-by-16 CSS pixel box around it in the registered overlay hit layer only,
 * refuse more than 64 total results, and require exactly one feature whose
 * layer id equals the exact queried hit layer and whose basin/segment/network
 * identities match. Returns the unmodified feature (t0 is recorded by the
 * dispatcher later).
 */
export async function selectRenderedRiverFeature({
  input,
  map,
  getOverlayHitLayerId,
  now,
  deadlineMs,
}: {
  input: RiverClickHookSelectionInput
  map: RiverClickHookMap
  getOverlayHitLayerId: () => string | null
  now: () => number
  deadlineMs: number
}): Promise<RiverClickHookSelectionResult> {
  const invalid = validateRiverClickSelectionInput(input)
  if (invalid !== null) {
    return { ok: false, code: 'HOOK_INVALID_INPUT', message: `river-click hook input is invalid: ${invalid}` }
  }

  const startedAt = now()
  const deadline = createRiverClickDeadline(deadlineMs, now, startedAt)

  // ONE budget across wait-for-map/overlay + fit + post-fit idle. The hit layer
  // may legitimately arrive late (async overlay data); the map may not be loaded
  // yet. Spend the budget in a single poll loop, never 15s + 15s.
  const hitLayerId = await waitForMapDischargeReady(map, getOverlayHitLayerId, now, deadline)
  if (hitLayerId === null) {
    if (deadline.expired()) {
      return { ok: false, code: 'HOOK_MAP_TIMEOUT', message: 'river-click hook map/overlay did not become ready within the bounded deadline' }
    }
    return { ok: false, code: 'HOOK_WRONG_LAYER', message: 'river-click hook current overlay is not the discharge product layer' }
  }

  try {
    map.fitBounds(input.bbox, { padding: 48, duration: 0, maxZoom: 14 })
  } catch {
    return { ok: false, code: 'HOOK_QUERY_FAILED', message: 'river-click hook fitBounds failed' }
  }

  // Wait for the post-fit idle event using the REMAINING shared budget, then
  // verify loaded/style-loaded (a bare idle without both is still not ready).
  let ready = false
  try {
    const idle = await waitForPostFitIdle(map, startedAt, now, deadline)
    if (idle.done) {
      ready = map.loaded() && map.isStyleLoaded()
    }
  } catch {
    ready = false
  }
  if (!ready) {
    return { ok: false, code: 'HOOK_MAP_TIMEOUT', message: 'river-click hook map did not become ready within the bounded deadline' }
  }

  const projected = map.project(input.anchor)
  if (!Number.isFinite(projected.x) || !Number.isFinite(projected.y)) {
    return { ok: false, code: 'HOOK_QUERY_FAILED', message: 'river-click hook anchor projection failed' }
  }
  const half = HOOK_QUERY_SIZE_PX / 2
  const queryBox: [{ x: number; y: number }, { x: number; y: number }] = [
    { x: projected.x - half, y: projected.y - half },
    { x: projected.x + half, y: projected.y + half },
  ]

  let rawResults: unknown
  try {
    rawResults = map.queryRenderedFeatures(queryBox, { layers: [hitLayerId] })
  } catch {
    return { ok: false, code: 'HOOK_QUERY_FAILED', message: 'river-click hook rendered feature query failed' }
  }
  // The query MUST return an actual array; malformed output (null, object,
  // undefined) is a closed hook failure, never a silent success.
  if (!Array.isArray(rawResults)) {
    return { ok: false, code: 'HOOK_QUERY_FAILED', message: 'river-click hook rendered feature query did not return an array' }
  }
  const results = rawResults
  if (results.length > MAX_QUERY_RESULTS) {
    return { ok: false, code: 'HOOK_QUERY_LIMIT', message: `river-click hook query returned ${results.length} results, exceeding ${MAX_QUERY_RESULTS}` }
  }

  const matched = results.filter((candidate): candidate is RiverClickRenderedFeature => {
    const feature = candidate as RiverClickRenderedFeature
    if (!feature || typeof feature !== 'object') return false
    // The returned feature MUST have been returned by the exact queried hit layer.
    if ((feature as RiverClickRenderedFeature).layer?.id !== hitLayerId) return false
    const normalized = normalizeRiverClickFeatureIdentity(feature)
    if (normalized === null) return false
    return (
      normalized.basinId === input.basinId &&
      normalized.riverSegmentId === input.riverSegmentId &&
      normalized.basinVersionId === input.basinVersionId &&
      normalized.riverNetworkVersionId === input.riverNetworkVersionId
    )
  })
  if (matched.length !== 1) {
    return {
      ok: false,
      code: 'HOOK_FEATURE_MISMATCH',
      message: `river-click hook rendered discharge feature match count must be exactly 1, got ${matched.length}`,
    }
  }

  const normalized = normalizeRiverClickFeatureIdentity(matched[0])
  if (normalized === null) {
    return { ok: false, code: 'HOOK_FEATURE_MISMATCH', message: 'river-click hook matched feature identity is unreadable' }
  }
  return { ok: true, output: { feature: matched[0], normalized } }
}

/** Poll a nullable map ref under ONE absolute deadline; resolves the first
 *  non-null map or null when the deadline expires. No timer unref (a pending
 *  test promise must stay resolvable). */
function waitForMapRef(
  getMap: () => RiverClickHookMap | null,
  deadline: RiverClickDeadline,
): Promise<RiverClickHookMap | null> {
  return new Promise<RiverClickHookMap | null>((resolve) => {
    const poll = () => {
      const map = getMap()
      if (map !== null) {
        resolve(map)
        return
      }
      if (deadline.expired()) {
        resolve(null)
        return
      }
      setTimeout(poll, Math.min(50, Math.max(1, deadline.remaining())))
    }
    poll()
  })
}

/**
 * Default-off read-only browser test hook controller. Exposes exactly one
 * method, selectRenderedRiver(input). It never exposes a map ref, generic
 * query method, or mutation surface. A null map ref is WAITED for under the
 * same ONE 15,000-ms absolute budget that also covers overlay/load readiness,
 * fit, and post-fit idle — never an immediate HOOK_MAP_UNAVAILABLE, never
 * budget-per-fact.
 */
export function createRiverClickHookController({
  getMap,
  getOverlayHitLayerId,
  now,
  select,
}: {
  getMap: () => RiverClickHookMap | null
  getOverlayHitLayerId: () => string | null
  now: () => number
  select: typeof selectRenderedRiverFeature
}): { selectRenderedRiver: (input: RiverClickHookSelectionInput) => Promise<RiverClickHookSelectionOutput> } {
  return {
    async selectRenderedRiver(input: RiverClickHookSelectionInput): Promise<RiverClickHookSelectionOutput> {
      const invalid = validateRiverClickSelectionInput(input)
      if (invalid !== null) {
        return Promise.reject({
          code: 'HOOK_INVALID_INPUT',
          message: `river-click hook input is invalid: ${invalid}`,
        })
      }
      const startedAt = now()
      const deadline = createRiverClickDeadline(RIVER_CLICK_PER_MAP_DEADLINE_MS, now, startedAt)
      // ONE budget for the map ref + overlay/load readiness + fit + post-fit idle.
      // A null map ref is a transient readiness state, not an unavailable map.
      const map = await waitForMapRef(getMap, deadline)
      if (map === null) {
        return Promise.reject({
          code: 'HOOK_MAP_TIMEOUT',
          message: 'river-click hook map did not become available within the bounded deadline',
        })
      }
      const paddedBbox = padRiverClickBbox(input.bbox)
      // The map-ref wait already consumed part of the ONE absolute budget; the
      // selection core must receive the REMAINING budget (never a fresh 15s).
      const remainingMs = Math.max(0, deadline.remaining())
      return select({
        input: { ...input, bbox: paddedBbox },
        map,
        getOverlayHitLayerId,
        now,
        deadlineMs: remainingMs,
      }).then((result) => {
        if (!result.ok) return Promise.reject({ code: result.code, message: result.message })
        return result.output
      })
    },
  }
}

export interface RiverClickHookDispatch {
  layerId: string
  event: { lngLat: { lng: number; lat: number } }
  feature: RiverClickRenderedFeature
}

function isClosedHookCode(value: unknown): value is RiverClickHookCode {
  return typeof value === 'string' && (RIVER_CLICK_HOOK_CODES as readonly string[]).includes(value)
}

/**
 * Build the exact gated global: selectRenderedRiver(input) resolves only the
 * four normalized identities plus dispatchNowMs. The browser clock is read
 * IMMEDIATELY BEFORE the actual rendered feature enters the existing
 * onOverlayClick — not inside the selection continuation. The product layer id
 * passed to the callback is always "discharge"; the MapLibre hit-layer id is
 * used only for querying and never passed as the product layer id.
 *
 * Fail-closed rules: an absent onOverlayClick rejects (never optional-chains to
 * success); every rejection code must be inside RIVER_CLICK_HOOK_CODES or is
 * redacted to HOOK_QUERY_FAILED; rejection messages are fixed/redacted strings.
 */
export function createRiverClickEvidenceHook({
  onOverlayClick,
  controller,
  now,
}: {
  onOverlayClick?: (dispatch: RiverClickHookDispatch) => void
  controller: { selectRenderedRiver: (input: RiverClickHookSelectionInput) => Promise<RiverClickHookSelectionOutput> }
  now?: () => number
}): { selectRenderedRiver: (input: RiverClickHookSelectionInput) => Promise<RiverClickResolvedIdentity> } {
  const clock = now ?? (() => performance.now())
  if (typeof onOverlayClick !== 'function') {
    // Fail closed: without the real callback the hook can never dispatch an
    // actual selection, so every call rejects with the closed code.
    return {
      selectRenderedRiver(): Promise<RiverClickResolvedIdentity> {
        return Promise.reject({ code: 'HOOK_QUERY_FAILED', message: 'river-click hook callback dispatch failed' })
      },
    }
  }
  return {
    selectRenderedRiver(input: RiverClickHookSelectionInput): Promise<RiverClickResolvedIdentity> {
      return controller.selectRenderedRiver(input).then(
        (output) => {
          // t0 recorded immediately before the feature enters the callback.
          const dispatchNowMs = clock()
          let dispatched = false
          try {
            onOverlayClick({
              layerId: 'discharge',
              event: { lngLat: { lng: input.anchor[0], lat: input.anchor[1] } },
              feature: output.feature,
            })
            dispatched = true
          } catch {
            // Callback errors are closed into a hook failure; never leak the
            // raw error object/message.
          }
          if (!dispatched) {
            return Promise.reject({
              code: 'HOOK_QUERY_FAILED',
              message: 'river-click hook callback dispatch failed',
            })
          }
          return {
            basinId: output.normalized.basinId,
            riverSegmentId: output.normalized.riverSegmentId,
            basinVersionId: output.normalized.basinVersionId,
            riverNetworkVersionId: output.normalized.riverNetworkVersionId,
            dispatchNowMs,
          }
        },
        (error: unknown) => {
          // Closed redaction: only a code inside the closed hook set is
          // propagated verbatim; everything else is the fixed redacted code.
          const code = isClosedHookCode(
            typeof error === 'object' && error !== null && 'code' in error ? (error as { code: unknown }).code : undefined,
          )
            ? (error as { code: RiverClickHookCode }).code
            : 'HOOK_QUERY_FAILED'
          return Promise.reject({ code, message: 'river-click hook selection failed' })
        },
      )
    },
  }
}

export interface RiverClickResolvedIdentity {
  basinId: string
  riverSegmentId: string
  basinVersionId: string
  riverNetworkVersionId: string
  dispatchNowMs: number
}

/**
 * Generation-safe global cleanup: delete the hook global only when both the
 * object identity and the owner generation token still match; a stale cleanup
 * from an older mount can never delete a newer instance. A null current
 * generation (nothing installed/hook already torn down) is never owned: the
 * predicate returns false and no stale cleanup can delete an instance.
 */
export function deleteRiverClickHookIfOwned(
  currentGlobal: unknown,
  ownedHook: unknown,
  ownedGeneration: number,
  currentGeneration: number | null,
): boolean {
  if (currentGeneration === null) return false
  if (currentGlobal === ownedHook && ownedGeneration === currentGeneration) {
    return true
  }
  return false
}

/**
 * Adapter from a native maplibre-gl Map to the narrow RiverClickHookMap the
 * gated river-click hook needs. It binds ONLY the narrow read/fit/query/idle
 * methods and closes over the native map object, so every delegated call keeps
 * the native map's own `this`. Native signatures are normalized explicitly to
 * the RiverClickHookMap contract: `project` returns exactly {x,y}, `queryRenderedFeatures`
 * is treated as an array (the actual rendered features are passed through
 * UNMODIFIED — the hook never synthesizes or mutates them), and listener
 * registration/removal is delegated verbatim. Returns null for a non-object
 * or incomplete native map (a null map ref is a transient readiness state the
 * controller waits on, never an unavailable map).
 */
export function adaptRiverClickHookMap(native: unknown): RiverClickHookMap | null {
  if (native === null || native === undefined || typeof native !== 'object') return null
  const map = native as {
    loaded?: () => boolean
    isStyleLoaded?: () => boolean
    fitBounds?: (bounds: unknown, options?: unknown) => unknown
    project?: (coord: [number, number]) => { x: number; y: number }
    queryRenderedFeatures?: (...args: unknown[]) => unknown
    getCanvas?: () => { style: { cursor: string } }
    once?: (event: string, callback: () => void) => unknown
    off?: (event: string, callback: () => void) => unknown
  }
  if (
    typeof map.loaded !== 'function' ||
    typeof map.isStyleLoaded !== 'function' ||
    typeof map.fitBounds !== 'function' ||
    typeof map.project !== 'function' ||
    typeof map.queryRenderedFeatures !== 'function' ||
    typeof map.getCanvas !== 'function' ||
    typeof map.once !== 'function'
  ) {
    return null
  }
  return {
    loaded: () => map.loaded!(),
    isStyleLoaded: () => map.isStyleLoaded!(),
    fitBounds: (bounds, options) => map.fitBounds!(bounds, options),
    project: (coord) => map.project!(coord),
    queryRenderedFeatures: (box, options) => map.queryRenderedFeatures!(box, options) as unknown[],
    getCanvas: () => map.getCanvas!(),
    once: (event, callback) => map.once!(event, callback),
    off: (event, callback) => map.off?.(event, callback),
  }
}
