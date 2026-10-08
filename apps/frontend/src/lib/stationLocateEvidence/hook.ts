/**
 * Gated, read-only station locate hook (openspec mobile-responsive-display
 * task 1.4). It is a separate global from the river-click hook, which it does
 * not change; it only reuses that module's narrow map adapter type, deadline
 * and ownership predicate. Relative imports only: nothing here may pull in
 * `@/…` or jsx. The station layer id and the product click-target resolution
 * are injected by `M11MapLibreSurface`.
 */
import { createRiverClickDeadline, type RiverClickDeadline } from '../riverClickEvidence/deadline'
import {
  deleteRiverClickHookIfOwned,
  padRiverClickBbox,
  type RiverClickHookMap,
  type RiverClickProductClickContext,
  type RiverClickRenderedFeature,
} from '../riverClickEvidence/hook'

/** The window property the hook is installed on. */
export const STATION_LOCATE_GLOBAL = '__nhmsStationLocateEvidence'

/** One budget for map readiness, the camera move and the settle after it. */
export const STATION_LOCATE_DEADLINE_MS = 15_000

/** Largest accepted `stationId` (UTF-8 bytes). */
export const STATION_LOCATE_ID_MAX_BYTES = 256

/** Closed set of rejection codes. */
export const STATION_LOCATE_HOOK_CODES = [
  /** `stationId` is not a bounded non-empty string, or `lngLat` is not a finite WGS84 `[lng, lat]`. */
  'STATION_HOOK_INVALID_INPUT',
  /** The product is not showing the station layer (switch off, or on with no features). */
  'STATION_HOOK_LAYER_OFF',
  /** The map, its style, the station layer on the map, or the settle after the camera move did not arrive in time. */
  'STATION_HOOK_MAP_TIMEOUT',
  /** The map threw or answered with something unusable. */
  'STATION_HOOK_QUERY_FAILED',
  /** No rendered station feature with that `station_id` lies at the point. */
  'STATION_HOOK_NOT_RENDERED',
  /** A product click at the point would expand a station cluster instead of opening a station. */
  'STATION_HOOK_CLUSTERED',
  /** A DOM element covers the point, or a product click there would act on something else. */
  'STATION_HOOK_POINT_OCCLUDED',
] as const
export type StationLocateHookCode = (typeof STATION_LOCATE_HOOK_CODES)[number]

const MESSAGES: Record<StationLocateHookCode, string> = {
  STATION_HOOK_INVALID_INPUT: 'station-locate hook input is invalid',
  STATION_HOOK_LAYER_OFF: 'station-locate hook: the station layer is not shown',
  STATION_HOOK_MAP_TIMEOUT: 'station-locate hook: the map or its station layer did not become ready within the bounded deadline',
  STATION_HOOK_QUERY_FAILED: 'station-locate hook: the map query failed',
  STATION_HOOK_NOT_RENDERED: 'station-locate hook: no rendered station with that id lies at the point',
  STATION_HOOK_CLUSTERED: 'station-locate hook: the point renders a station cluster, not a single station',
  STATION_HOOK_POINT_OCCLUDED: 'station-locate hook: the located point is covered or resolves to another click target',
}

export interface StationLocateInput {
  stationId: string
  /** `[lng, lat]`, WGS84. */
  lngLat: [number, number]
}

/** Everything a caller gets back: the station identity and the viewport point (whole CSS px). */
export interface StationLocateResult {
  stationId: string
  clientX: number
  clientY: number
}

/** The exact gated global: one method, nothing else. */
export interface StationLocateEvidenceHook {
  locateRenderedStation: (input: StationLocateInput) => Promise<StationLocateResult>
}

export interface StationLocateHookDeps {
  /** The narrow map view (`adaptRiverClickHookMap`); null while the map does not exist yet. */
  getMap: () => RiverClickHookMap | null
  /** Id of the single-station layer while the product shows the station layer, else null. */
  getStationPointLayerId: () => string | null
  /** The product's interactive layer ids and its own click-target resolution. */
  productClick: RiverClickProductClickContext
  elementFromPoint: (x: number, y: number) => unknown
  now: () => number
  deadlineMs?: number
}

class StationLocateFailure {
  constructor(readonly code: StationLocateHookCode) {}
}

function fail(code: StationLocateHookCode): never {
  throw new StationLocateFailure(code)
}

function validInput(input: unknown): input is StationLocateInput {
  if (input === null || typeof input !== 'object') return false
  const { stationId, lngLat } = input as { stationId?: unknown; lngLat?: unknown }
  if (typeof stationId !== 'string' || stationId.length === 0) return false
  if (new TextEncoder().encode(stationId).byteLength > STATION_LOCATE_ID_MAX_BYTES) return false
  if (!Array.isArray(lngLat) || lngLat.length !== 2) return false
  const [lng, lat] = lngLat as unknown[]
  if (typeof lng !== 'number' || typeof lat !== 'number' || !Number.isFinite(lng) || !Number.isFinite(lat)) return false
  return lng >= -180 && lng <= 180 && lat >= -90 && lat <= 90
}

function pause(deadline: RiverClickDeadline): Promise<void> {
  // No unref(): a pending timer keeps the caller's promise resolvable.
  return new Promise((resolve) => setTimeout(resolve, Math.min(50, Math.max(1, deadline.remaining()))))
}

/**
 * Poll until the map exists, is loaded with its style, and carries the station
 * point layer. The switch going off meanwhile is STATION_HOOK_LAYER_OFF; the
 * deadline passing is STATION_HOOK_MAP_TIMEOUT.
 */
async function waitForStationLayerOnMap(deps: StationLocateHookDeps, deadline: RiverClickDeadline) {
  for (;;) {
    const layerId = deps.getStationPointLayerId()
    if (layerId === null) fail('STATION_HOOK_LAYER_OFF')
    const map = deps.getMap()
    let ready = false
    try {
      ready = map !== null && map.loaded() && map.isStyleLoaded() && Boolean(map.getLayer(layerId))
    } catch {
      ready = false
    }
    if (map !== null && ready) return { map, layerId }
    if (deadline.expired()) fail('STATION_HOOK_MAP_TIMEOUT')
    await pause(deadline)
  }
}

/** One `idle` after the camera move (idle implies rendered and settled), within the remaining budget. */
function waitForIdle(map: RiverClickHookMap, deadline: RiverClickDeadline): Promise<boolean> {
  return new Promise((resolve) => {
    let settled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const onIdle = () => finish(true)
    function finish(done: boolean) {
      if (settled) return
      settled = true
      if (timer !== undefined) clearTimeout(timer)
      try {
        map.off?.('idle', onIdle)
      } catch {
        // listener removal must never mask the outcome
      }
      resolve(done)
    }
    const remaining = deadline.remaining()
    if (remaining <= 0) {
      finish(false)
      return
    }
    timer = setTimeout(() => finish(false), remaining)
    try {
      map.once('idle', onIdle)
    } catch {
      finish(false)
    }
  })
}

function stationIdOf(feature: RiverClickRenderedFeature | null | undefined): string | null {
  const value = feature?.properties?.station_id
  return typeof value === 'string' && value.length > 0 ? value : null
}

async function locate(deps: StationLocateHookDeps, input: unknown): Promise<StationLocateResult> {
  if (!validInput(input)) fail('STATION_HOOK_INVALID_INPUT')
  const { stationId } = input
  const lngLat: [number, number] = [input.lngLat[0], input.lngLat[1]]
  // Layer off is answered at once: callers wait for the layer before calling.
  if (deps.getStationPointLayerId() === null) fail('STATION_HOOK_LAYER_OFF')

  const deadline = createRiverClickDeadline(deps.deadlineMs ?? STATION_LOCATE_DEADLINE_MS, deps.now, deps.now())
  const { map, layerId } = await waitForStationLayerOnMap(deps, deadline)

  // Camera: centre on the station at the fit's maxZoom (a point bbox padded by
  // the documented epsilon), the same fit options as the river hook. That zoom
  // is above every station cluster maxZoom, so a lone station is not clustered.
  map.fitBounds(padRiverClickBbox([lngLat, lngLat]), { padding: 48, duration: 0, maxZoom: 14 })
  const idle = await waitForIdle(map, deadline)
  if (!idle || !map.loaded() || !map.isStyleLoaded()) fail('STATION_HOOK_MAP_TIMEOUT')

  // Viewport point: canvas rect origin + projected point, rounded to the whole
  // CSS px a real pointer hits. The query below runs at that same pixel.
  const projected = map.project(lngLat)
  const canvas = map.getCanvas()
  const rect = canvas.getBoundingClientRect()
  const clientX = Math.round(rect.left + projected.x)
  const clientY = Math.round(rect.top + projected.y)
  const canvasX = clientX - rect.left
  const canvasY = clientY - rect.top
  if (![clientX, clientY, canvasX, canvasY].every(Number.isFinite)) fail('STATION_HOOK_QUERY_FAILED')

  // DOM: a real click there must land on the map canvas itself.
  if (deps.elementFromPoint(clientX, clientY) !== canvas) fail('STATION_HOOK_POINT_OCCLUDED')

  // Product path: what react-map-gl hands the click handler for this point
  // (interactive ids that exist on the map; the point is an array, a plain
  // {x, y} would be read as options and query the whole viewport), resolved by
  // the product's own walk.
  const layers = deps.productClick.getInteractiveLayerIds().filter((id) => Boolean(map.getLayer(id)))
  const rendered: unknown = map.queryRenderedFeatures([canvasX, canvasY], { layers })
  if (!Array.isArray(rendered)) fail('STATION_HOOK_QUERY_FAILED')
  const features = rendered.filter((item): item is RiverClickRenderedFeature => item !== null && typeof item === 'object')
  const target = deps.productClick.resolveClickTarget(features)
  if (target?.kind === 'station-cluster') fail('STATION_HOOK_CLUSTERED')
  if (!features.some((feature) => feature.layer?.id === layerId && stationIdOf(feature) === stationId)) {
    fail('STATION_HOOK_NOT_RENDERED')
  }
  if (target?.kind !== 'station' || stationIdOf(target.feature) !== stationId) fail('STATION_HOOK_POINT_OCCLUDED')

  return { stationId, clientX, clientY }
}

/**
 * Build the gated global `{locateRenderedStation}`. It takes no product
 * callback and holds no path into the product click handler: it moves the
 * camera, reads what is rendered at the station's point, and resolves the
 * station id with the viewport point. Rejections are `{code, message}` with a
 * code from STATION_LOCATE_HOOK_CODES and a fixed message; anything
 * unexpected is STATION_HOOK_QUERY_FAILED.
 */
export function createStationLocateEvidenceHook(deps: StationLocateHookDeps): StationLocateEvidenceHook {
  return {
    locateRenderedStation(input: StationLocateInput): Promise<StationLocateResult> {
      return locate(deps, input).catch((error: unknown) => {
        const code = error instanceof StationLocateFailure ? error.code : 'STATION_HOOK_QUERY_FAILED'
        return Promise.reject({ code, message: MESSAGES[code] })
      })
    },
  }
}

// Monotonic token source and the token of the currently installed hook.
let stationLocateHookGeneration = 0
let stationLocateInstalledGeneration: number | null = null

/**
 * Put `hook` on `target` and return its cleanup. The cleanup deletes the
 * global only while both the object and the installed generation token are
 * still this install's, so a stale cleanup can never delete a newer instance.
 */
export function installStationLocateEvidenceHook(target: Record<string, unknown>, hook: StationLocateEvidenceHook): () => void {
  const generation = stationLocateHookGeneration
  stationLocateHookGeneration += 1
  target[STATION_LOCATE_GLOBAL] = hook
  stationLocateInstalledGeneration = generation
  return () => {
    if (deleteRiverClickHookIfOwned(target[STATION_LOCATE_GLOBAL], hook, generation, stationLocateInstalledGeneration)) {
      delete target[STATION_LOCATE_GLOBAL]
      stationLocateInstalledGeneration = null
    }
  }
}
