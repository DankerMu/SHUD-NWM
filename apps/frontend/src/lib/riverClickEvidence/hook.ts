import {
  HOOK_QUERY_LIMIT,
  HOOK_QUERY_SIZE_PX,
  HOOK_READY_TIMEOUT_MS,
  RIVER_CLICK_HOOK_CODES,
  RIVER_CLICK_M11_IDENTIFIER_PATTERN,
  RIVER_CLICK_PER_MAP_DEADLINE_MS,
  isRiverClickHookIdentity,
  type RiverClickHookCode,
} from './constants'
import { createRiverClickDeadline, type RiverClickDeadline } from './deadline'

export { HOOK_QUERY_LIMIT, HOOK_QUERY_SIZE_PX, HOOK_READY_TIMEOUT_MS }

/** One canvas pointer-down as the capture listener observes it. */
export interface RiverClickPointerEventLike {
  isTrusted: boolean
  timeStamp: number
  clientX: number
  clientY: number
  target: unknown
}

/**
 * Minimal map-canvas surface: the viewport rect (client-coordinate
 * conversion) and capture-phase pointer-down listener registration. The real
 * value is the native `<canvas>` element itself, so identity comparison with
 * `document.elementFromPoint` is exact.
 */
export interface RiverClickHookCanvas {
  getBoundingClientRect(): { left: number; top: number }
  addEventListener(type: 'pointerdown', listener: (event: RiverClickPointerEventLike) => void, options: { capture: true }): void
  removeEventListener(type: 'pointerdown', listener: (event: RiverClickPointerEventLike) => void, options: { capture: true }): void
}

/** Minimal MapLibre map surface consumed by the river-click hook. */
export interface RiverClickHookMap {
  loaded(): boolean
  isStyleLoaded(): boolean
  fitBounds(bounds: [[number, number], [number, number]], options: { padding: number; duration: number; maxZoom: number }): unknown
  project(coord: [number, number]): { x: number; y: number }
  queryRenderedFeatures(
    geometry: { x: number; y: number } | [{ x: number; y: number }, { x: number; y: number }],
    options: { layers: string[] },
  ): unknown[]
  getLayer(id: string): unknown
  getCanvas(): RiverClickHookCanvas
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

/** Internal locate result: the matched identity plus the viewport client point. */
export interface RiverClickHookLocateOutput {
  normalized: RiverClickNormalizedFeatureIdentity
  clientX: number
  clientY: number
  /** The canvas the point was checked against (internal; never exposed on the global). */
  canvas: RiverClickHookCanvas
}

export type RiverClickHookLocateResult =
  | { ok: true; output: RiverClickHookLocateOutput }
  | { ok: false; code: RiverClickHookCode; message: string }

/** What a product map click at a point would act on (the shared resolver's answer). */
export interface RiverClickProductClickTarget {
  kind: string
  feature: RiverClickRenderedFeature
}

/**
 * Read-only view of the product click path at a point: the interactive layer
 * ids the `<Map interactiveLayerIds>` prop currently receives, and the
 * product's own click-target resolution (the pure walk shared with
 * `handleM11MapClick`). Both are read through refs kept current each render.
 */
export interface RiverClickProductClickContext {
  getInteractiveLayerIds(): readonly string[]
  resolveClickTarget(features: RiverClickRenderedFeature[]): RiverClickProductClickTarget | null
}

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

function validConfiguredIdentity(value: string): boolean {
  return RIVER_CLICK_M11_IDENTIFIER_PATTERN.test(value)
}

export function validateRiverClickSelectionInput(input: RiverClickHookSelectionInput): string | null {
  if (!Array.isArray(input.bbox) || input.bbox.length !== 2 || !validBbox(input.bbox)) return 'invalid bbox'
  if (!Array.isArray(input.anchor) || input.anchor.length !== 2 || !validAnchor(input.anchor)) return 'invalid anchor'
  if (!validConfiguredIdentity(input.basinId)) return 'invalid basinId'
  if (!validConfiguredIdentity(input.riverSegmentId)) return 'invalid riverSegmentId'
  if (!isRiverClickHookIdentity(input.basinVersionId)) return 'invalid basinVersionId'
  if (!isRiverClickHookIdentity(input.riverNetworkVersionId)) return 'invalid riverNetworkVersionId'
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

function sameIdentity(a: RiverClickNormalizedFeatureIdentity, input: RiverClickHookSelectionInput): boolean {
  return (
    a.basinId === input.basinId &&
    a.riverSegmentId === input.riverSegmentId &&
    a.basinVersionId === input.basinVersionId &&
    a.riverNetworkVersionId === input.riverNetworkVersionId
  )
}

/**
 * Pure locate core: require the current renderable discharge overlay and a
 * loaded map, then fit the bbox (padding 48, duration 0, maxZoom 14), wait at
 * most the ONE shared deadline for the post-fit `idle` event (idle implies
 * rendered/settled) plus loaded/style-loaded, project the anchor, query the
 * 16-by-16 CSS pixel box around it in the registered overlay hit layer only,
 * refuse more than 64 total results, and require exactly one feature whose
 * layer id equals the exact queried hit layer and whose basin/segment/network
 * identities match.
 *
 * It then proves a REAL click at the anchor reaches that river through the
 * product path: the viewport client point (canvas rect + projected point)
 * must hit the map canvas itself (`elementFromPoint`), and the product's own
 * click-target resolution, fed exactly what react-map-gl would hand a click
 * there (`queryRenderedFeatures(point, {layers: interactive ids that exist})`),
 * must select the overlay hit-layer feature with the pinned identity. Either
 * failing is HOOK_POINT_OCCLUDED. It never dispatches anything.
 */
export async function locateRenderedRiverFeature({
  input,
  map,
  getOverlayHitLayerId,
  productClick,
  elementFromPoint,
  now,
  deadlineMs,
}: {
  input: RiverClickHookSelectionInput
  map: RiverClickHookMap
  getOverlayHitLayerId: () => string | null
  productClick: RiverClickProductClickContext
  elementFromPoint: (x: number, y: number) => unknown
  now: () => number
  deadlineMs: number
}): Promise<RiverClickHookLocateResult> {
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
    return normalized !== null && sameIdentity(normalized, input)
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

  // Viewport client point of the anchor: the canvas rect origin plus the
  // canvas-relative projected point.
  let canvas: RiverClickHookCanvas
  let clientX: number
  let clientY: number
  try {
    canvas = map.getCanvas()
    const rect = canvas.getBoundingClientRect()
    clientX = rect.left + projected.x
    clientY = rect.top + projected.y
  } catch {
    return { ok: false, code: 'HOOK_QUERY_FAILED', message: 'river-click hook canvas client point is unavailable' }
  }
  if (!Number.isFinite(clientX) || !Number.isFinite(clientY)) {
    return { ok: false, code: 'HOOK_QUERY_FAILED', message: 'river-click hook canvas client point is not finite' }
  }

  // DOM check: a real click there must land on the map canvas, not on a
  // control, switcher, marker or status overlay above it.
  let topElement: unknown
  try {
    topElement = elementFromPoint(clientX, clientY)
  } catch {
    topElement = null
  }
  if (topElement !== canvas) {
    return { ok: false, code: 'HOOK_POINT_OCCLUDED', message: 'river-click hook located point is covered by another element' }
  }

  // Product-hit check: exactly what react-map-gl hands the click handler for
  // this point (interactive ids filtered by map.getLayer, so an absent layer id
  // never raises a map ErrorEvent), resolved by the product's own walk.
  let interactiveFeatures: unknown
  try {
    const layers = productClick.getInteractiveLayerIds().filter((id) => {
      try {
        return Boolean(map.getLayer(id))
      } catch {
        return false
      }
    })
    interactiveFeatures = map.queryRenderedFeatures({ x: projected.x, y: projected.y }, { layers })
  } catch {
    return { ok: false, code: 'HOOK_QUERY_FAILED', message: 'river-click hook interactive feature query failed' }
  }
  if (!Array.isArray(interactiveFeatures)) {
    return { ok: false, code: 'HOOK_QUERY_FAILED', message: 'river-click hook interactive feature query did not return an array' }
  }
  let target: RiverClickProductClickTarget | null
  try {
    target = productClick.resolveClickTarget(
      interactiveFeatures.filter((item): item is RiverClickRenderedFeature => item !== null && typeof item === 'object'),
    )
  } catch {
    target = null
  }
  const targetIdentity = target?.kind === 'overlay' && target.feature.layer?.id === hitLayerId
    ? normalizeRiverClickFeatureIdentity(target.feature)
    : null
  if (targetIdentity === null || !sameIdentity(targetIdentity, input)) {
    return { ok: false, code: 'HOOK_POINT_OCCLUDED', message: 'river-click hook located point resolves to another product click target' }
  }

  return { ok: true, output: { normalized, clientX, clientY, canvas } }
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
 * Locate controller. A null map ref is WAITED for under the same ONE
 * 15,000-ms absolute budget that also covers overlay/load readiness, fit, and
 * post-fit idle — never an immediate HOOK_MAP_UNAVAILABLE, never
 * budget-per-fact. It never exposes a map ref, generic query method, or
 * mutation surface.
 */
export function createRiverClickHookController({
  getMap,
  getOverlayHitLayerId,
  productClick,
  elementFromPoint,
  now,
  locate,
}: {
  getMap: () => RiverClickHookMap | null
  getOverlayHitLayerId: () => string | null
  productClick: RiverClickProductClickContext
  elementFromPoint: (x: number, y: number) => unknown
  now: () => number
  locate: typeof locateRenderedRiverFeature
}): { locateRenderedRiver: (input: RiverClickHookSelectionInput) => Promise<RiverClickHookLocateOutput> } {
  return {
    async locateRenderedRiver(input: RiverClickHookSelectionInput): Promise<RiverClickHookLocateOutput> {
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
      // locate core must receive the REMAINING budget (never a fresh 15s).
      const remainingMs = Math.max(0, deadline.remaining())
      return locate({
        input: { ...input, bbox: paddedBbox },
        map,
        getOverlayHitLayerId,
        productClick,
        elementFromPoint,
        now,
        deadlineMs: remainingMs,
      }).then((result) => {
        if (!result.ok) return Promise.reject({ code: result.code, message: result.message })
        return result.output
      })
    },
  }
}

/** A trusted pointer-down on the map canvas: the sample's t0 source. */
export interface RiverClickPointerCapture {
  timeStamp: number
  clientX: number
  clientY: number
  isTrusted: true
}

export type RiverClickPointerCaptureResult = RiverClickPointerCapture | { error: RiverClickHookCode }

/** Observation cap: two events already decide "duplicate"; the rest only count. */
const POINTER_EVENT_RECORD_LIMIT = 8

/**
 * Pointer-down capture on the map canvas. `arm()` clears any previous capture
 * and installs ONE capture-phase `pointerdown` listener on the current canvas
 * (or, when the map/canvas does not exist yet, records the arm so `attach()`
 * installs it once `locateRenderedRiver` has the canvas). The listener
 * observes EVERY pointer-down targeting the canvas, trusted or not, keeping
 * only `isTrusted`, `timeStamp` and the client point. `take()` removes the
 * listener and classifies: exactly one trusted event -> the capture; none or
 * never armed -> HOOK_POINTER_MISSING; more than one or any untrusted ->
 * HOOK_POINTER_INVALID. `dispose()` removes any listener (mount cleanup).
 */
export function createRiverClickPointerCapture({
  getCanvas,
}: {
  getCanvas: () => RiverClickHookCanvas | null
}): {
  arm: () => void
  attach: (canvas: RiverClickHookCanvas) => void
  take: () => RiverClickPointerCaptureResult
  dispose: () => void
} {
  let armed = false
  let attached: RiverClickHookCanvas | null = null
  let count = 0
  let recorded: Array<{ isTrusted: boolean; timeStamp: number; clientX: number; clientY: number }> = []
  const listener = (event: RiverClickPointerEventLike) => {
    if (attached === null || event.target !== attached) return
    count += 1
    if (recorded.length < POINTER_EVENT_RECORD_LIMIT) {
      recorded.push({ isTrusted: event.isTrusted === true, timeStamp: event.timeStamp, clientX: event.clientX, clientY: event.clientY })
    }
  }
  const detach = () => {
    if (attached === null) return
    const canvas = attached
    attached = null
    try {
      canvas.removeEventListener('pointerdown', listener, { capture: true })
    } catch {
      // listener removal must never mask the terminal state
    }
  }
  const attach = (canvas: RiverClickHookCanvas) => {
    if (!armed || attached === canvas) return
    detach()
    try {
      canvas.addEventListener('pointerdown', listener, { capture: true })
      attached = canvas
    } catch {
      attached = null
    }
  }
  return {
    arm() {
      detach()
      armed = true
      count = 0
      recorded = []
      let canvas: RiverClickHookCanvas | null = null
      try {
        canvas = getCanvas()
      } catch {
        canvas = null
      }
      if (canvas !== null) attach(canvas)
    },
    attach,
    take(): RiverClickPointerCaptureResult {
      const wasArmed = armed
      detach()
      armed = false
      const events = recorded
      const total = count
      recorded = []
      count = 0
      if (!wasArmed || total === 0) return { error: 'HOOK_POINTER_MISSING' }
      if (total > 1 || events.some((event) => !event.isTrusted)) return { error: 'HOOK_POINTER_INVALID' }
      const [event] = events
      if (!Number.isFinite(event.timeStamp) || event.timeStamp < 0 || !Number.isFinite(event.clientX) || !Number.isFinite(event.clientY)) {
        return { error: 'HOOK_POINTER_INVALID' }
      }
      return { timeStamp: event.timeStamp, clientX: event.clientX, clientY: event.clientY, isTrusted: true }
    },
    dispose() {
      detach()
      armed = false
      count = 0
      recorded = []
    },
  }
}

function isClosedHookCode(value: unknown): value is RiverClickHookCode {
  return typeof value === 'string' && (RIVER_CLICK_HOOK_CODES as readonly string[]).includes(value)
}

export interface RiverClickResolvedIdentity {
  basinId: string
  riverSegmentId: string
  basinVersionId: string
  riverNetworkVersionId: string
  clientX: number
  clientY: number
}

/** The exact gated global: three methods, nothing else. */
export interface RiverClickEvidenceHook {
  locateRenderedRiver: (input: RiverClickHookSelectionInput) => Promise<RiverClickResolvedIdentity>
  armPointerCapture: () => void
  takePointerCapture: () => RiverClickPointerCaptureResult
}

/**
 * Build the exact gated global `{locateRenderedRiver, armPointerCapture,
 * takePointerCapture}`. `locateRenderedRiver` resolves only the four
 * normalized identities plus the finite viewport point; it takes no product
 * callback and there is no code path from this object into the product click
 * handler — only the map's own click event, produced by a real pointer at
 * that point, reaches it.
 *
 * Fail-closed rules: every rejection code must be inside
 * RIVER_CLICK_HOOK_CODES or is redacted to HOOK_QUERY_FAILED; rejection
 * messages are fixed/redacted strings.
 */
export function createRiverClickEvidenceHook({
  controller,
  pointerCapture,
}: {
  controller: { locateRenderedRiver: (input: RiverClickHookSelectionInput) => Promise<RiverClickHookLocateOutput> }
  pointerCapture: ReturnType<typeof createRiverClickPointerCapture>
}): RiverClickEvidenceHook {
  return {
    locateRenderedRiver(input: RiverClickHookSelectionInput): Promise<RiverClickResolvedIdentity> {
      return controller.locateRenderedRiver(input).then(
        (output) => {
          if (!Number.isFinite(output.clientX) || !Number.isFinite(output.clientY)) {
            return Promise.reject({ code: 'HOOK_QUERY_FAILED', message: 'river-click hook locate failed' })
          }
          // An arm issued before the map/canvas existed attaches now, before
          // the caller can click the returned point.
          pointerCapture.attach(output.canvas)
          return {
            basinId: output.normalized.basinId,
            riverSegmentId: output.normalized.riverSegmentId,
            basinVersionId: output.normalized.basinVersionId,
            riverNetworkVersionId: output.normalized.riverNetworkVersionId,
            clientX: output.clientX,
            clientY: output.clientY,
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
          return Promise.reject({ code, message: 'river-click hook locate failed' })
        },
      )
    },
    armPointerCapture(): void {
      pointerCapture.arm()
    },
    takePointerCapture(): RiverClickPointerCaptureResult {
      return pointerCapture.take()
    },
  }
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
 * gated river-click hook needs. It binds ONLY the narrow read/fit/query/idle/
 * layer-existence/canvas methods and closes over the native map object, so
 * every delegated call keeps the native map's own `this`. `project` returns
 * exactly {x,y}; `queryRenderedFeatures` is treated as an array (the actual
 * rendered features are passed through UNMODIFIED — the hook never
 * synthesizes or mutates them); `getCanvas` returns the native canvas element
 * itself (identity matters for the DOM check and the pointer listener).
 * Returns null for a non-object or incomplete native map (a null map ref is a
 * transient readiness state the controller waits on, never an unavailable map).
 */
export function adaptRiverClickHookMap(native: unknown): RiverClickHookMap | null {
  if (native === null || native === undefined || typeof native !== 'object') return null
  const map = native as {
    loaded?: () => boolean
    isStyleLoaded?: () => boolean
    fitBounds?: (bounds: unknown, options?: unknown) => unknown
    project?: (coord: [number, number]) => { x: number; y: number }
    queryRenderedFeatures?: (...args: unknown[]) => unknown
    getLayer?: (id: string) => unknown
    getCanvas?: () => RiverClickHookCanvas
    once?: (event: string, callback: () => void) => unknown
    off?: (event: string, callback: () => void) => unknown
  }
  if (
    typeof map.loaded !== 'function' ||
    typeof map.isStyleLoaded !== 'function' ||
    typeof map.fitBounds !== 'function' ||
    typeof map.project !== 'function' ||
    typeof map.queryRenderedFeatures !== 'function' ||
    typeof map.getLayer !== 'function' ||
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
    queryRenderedFeatures: (geometry, options) => map.queryRenderedFeatures!(geometry, options) as unknown[],
    getLayer: (id) => map.getLayer!(id),
    getCanvas: () => map.getCanvas!(),
    once: (event, callback) => map.once!(event, callback),
    off: (event, callback) => map.off?.(event, callback),
  }
}
