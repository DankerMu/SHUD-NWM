import { afterEach, describe, expect, it, vi } from 'vitest'

import { resolveM11ClickTarget } from '@/components/map/m11MapInteractions'
import type { M11RegisteredOverlay } from '@/components/map/m11MapBuilders'
import {
  M11_BASIN_FILL_LAYER_ID,
  MET_STATION_CLUSTER_LAYER_ID,
  MET_STATION_POINT_LAYER_ID,
} from '@/components/map/m11MapPrimitives'
import type { RiverClickHookMap, RiverClickRenderedFeature } from '@/lib/riverClickEvidence/hook'

import {
  STATION_LOCATE_GLOBAL,
  STATION_LOCATE_HOOK_CODES,
  createStationLocateEvidenceHook,
  installStationLocateEvidenceHook,
  type StationLocateEvidenceHook,
} from '../hook'

const HIT = 'm11-discharge-line-hit'
const OVERLAY = {
  layerId: 'discharge',
  sourceId: 'm11-discharge-source',
  sourceKey: 'k',
  layer: { id: 'm11-discharge-line' },
  source: { type: 'vector', tiles: [], sourceLayer: 'hydro', minzoom: 0, maxzoom: 14, metadata: {} },
} as unknown as M11RegisteredOverlay

const STATION_ID = 'st-0001'
const LNGLAT: [number, number] = [100.5, 30.5]
const INTERACTIVE = [MET_STATION_POINT_LAYER_ID, MET_STATION_CLUSTER_LAYER_ID, M11_BASIN_FILL_LAYER_ID, HIT]

function stationFeature(stationId = STATION_ID) {
  return {
    id: stationId,
    layer: { id: MET_STATION_POINT_LAYER_ID },
    geometry: { type: 'Point', coordinates: LNGLAT },
    properties: { station_id: stationId, station_name: 'Station', basin_id: 'basins_qhh' },
  }
}

function layerFeature(layerId: string) {
  return { layer: { id: layerId }, geometry: { type: 'Point', coordinates: LNGLAT }, properties: { marker: layerId } }
}

interface Harness {
  hook: StationLocateEvidenceHook
  map: RiverClickHookMap & { fitBounds: ReturnType<typeof vi.fn>; queryRenderedFeatures: ReturnType<typeof vi.fn> }
  canvas: object
  state: { map: RiverClickHookMap | null; showStationLayer: boolean; loaded: boolean; topElement: unknown }
  resolveClickTarget: ReturnType<typeof vi.fn>
}

/**
 * The hook under test wired the way `M11MapLibreSurface` wires it: the real
 * product resolver `resolveM11ClickTarget`, a map stub that honours MapLibre's
 * "only an array is geometry" rule, and a canvas whose rect origin is (10, 20).
 */
function harness(options: {
  pointFeatures?: unknown[] | (() => unknown)
  layers?: string[]
  deadlineMs?: number
  idle?: boolean
} = {}): Harness {
  const canvas = { getBoundingClientRect: () => ({ left: 10, top: 20 }), addEventListener: vi.fn(), removeEventListener: vi.fn() }
  const layers = new Set(options.layers ?? INTERACTIVE)
  const state: Harness['state'] = { map: null, showStationLayer: true, loaded: true, topElement: canvas }
  const map = {
    loaded: () => state.loaded,
    isStyleLoaded: () => state.loaded,
    fitBounds: vi.fn(),
    // project(100.5, 30.5) = (45.4, 45.4): a fractional canvas point, so rounding is observable.
    project: (coord: [number, number]) => ({ x: 40.4 + (coord[0] - 100) * 10, y: 40.4 + (coord[1] - 30) * 10 }),
    queryRenderedFeatures: vi.fn((geometry: unknown) => {
      // A non-array first argument is read by MapLibre as options: whole-viewport query.
      if (!Array.isArray(geometry)) return [stationFeature('st-VIEWPORT-OTHER')]
      const features = options.pointFeatures ?? [stationFeature()]
      return typeof features === 'function' ? features() : features
    }),
    getLayer: (id: string) => (layers.has(id) ? { id } : undefined),
    getCanvas: () => canvas,
    once: (_event: string, callback: () => void) => {
      if (options.idle !== false) queueMicrotask(callback)
    },
    off: vi.fn(),
  } as unknown as Harness['map']
  state.map = map
  const resolveClickTarget = vi.fn((features: RiverClickRenderedFeature[]) =>
    resolveM11ClickTarget({ features, showStationLayer: state.showStationLayer, renderableOverlay: OVERLAY }),
  )
  const hook = createStationLocateEvidenceHook({
    getMap: () => state.map,
    getStationPointLayerId: () => (state.showStationLayer ? MET_STATION_POINT_LAYER_ID : null),
    productClick: { getInteractiveLayerIds: () => INTERACTIVE, resolveClickTarget },
    elementFromPoint: () => state.topElement,
    now: () => performance.now(),
    deadlineMs: options.deadlineMs,
  })
  return { hook, map, canvas, state, resolveClickTarget }
}

/** A rejection is exactly `{code, message}`: a stable code and no point. */
async function expectFailure(promise: Promise<unknown>, code: (typeof STATION_LOCATE_HOOK_CODES)[number]) {
  const failure = await promise.then(
    (value) => ({ resolved: value }),
    (error: unknown) => error,
  )
  expect(failure).toEqual({ code, message: expect.any(String) })
  expect(Object.keys(failure as object).sort()).toEqual(['code', 'message'])
  expect((failure as { message: string }).message.length).toBeLessThanOrEqual(160)
}

describe('station-locate hook: surface', () => {
  it('is an object with exactly one own enumerable property, the method locateRenderedStation', () => {
    const { hook } = harness()
    expect(Object.keys(hook)).toEqual(['locateRenderedStation'])
    expect(typeof hook.locateRenderedStation).toBe('function')
    for (const forbidden of ['map', 'getMap', 'query', 'queryRenderedFeatures', 'dispatch', 'onOverlayClick', 'armPointerCapture']) {
      expect(hook).not.toHaveProperty(forbidden)
    }
  })

  it('has a closed set of stable error codes', () => {
    expect([...STATION_LOCATE_HOOK_CODES]).toEqual([
      'STATION_HOOK_INVALID_INPUT',
      'STATION_HOOK_LAYER_OFF',
      'STATION_HOOK_MAP_TIMEOUT',
      'STATION_HOOK_QUERY_FAILED',
      'STATION_HOOK_NOT_RENDERED',
      'STATION_HOOK_CLUSTERED',
      'STATION_HOOK_POINT_OCCLUDED',
    ])
    expect(STATION_LOCATE_GLOBAL).toBe('__nhmsStationLocateEvidence')
  })
})

describe('station-locate hook: locating a rendered station', () => {
  it('moves the camera to the point and resolves only the station id and the rounded viewport point', async () => {
    const { hook, map } = harness()
    const result = await hook.locateRenderedStation({ stationId: STATION_ID, lngLat: LNGLAT })
    // canvas point (45.4, 45.4) + rect origin (10, 20) = (55.4, 65.4) -> whole CSS px.
    expect(result).toEqual({ stationId: STATION_ID, clientX: 55, clientY: 65 })
    expect(Object.keys(result).sort()).toEqual(['clientX', 'clientY', 'stationId'])
    expect(map.fitBounds).toHaveBeenCalledTimes(1)
    const [bounds, fitOptions] = map.fitBounds.mock.calls[0] as [[[number, number], [number, number]], unknown]
    expect(fitOptions).toEqual({ padding: 48, duration: 0, maxZoom: 14 })
    // The fitted bounds are centred on the station and at most a few metres wide.
    expect((bounds[0][0] + bounds[1][0]) / 2).toBeCloseTo(LNGLAT[0], 9)
    expect((bounds[0][1] + bounds[1][1]) / 2).toBeCloseTo(LNGLAT[1], 9)
    expect(bounds[1][0] - bounds[0][0]).toBeLessThan(1e-4)
  })

  it('asks the product click path at that exact rounded point (array geometry) in the interactive layers that exist', async () => {
    const { hook, map, resolveClickTarget } = harness({ layers: [MET_STATION_POINT_LAYER_ID, HIT] })
    await hook.locateRenderedStation({ stationId: STATION_ID, lngLat: LNGLAT })
    // Rounded client point (55, 65) minus the rect origin = canvas (45, 45).
    expect(map.queryRenderedFeatures.mock.calls).toEqual([[[45, 45], { layers: [MET_STATION_POINT_LAYER_ID, HIT] }]])
    expect(resolveClickTarget).toHaveBeenCalledTimes(1)
  })

  it('waits for a map that arrives late instead of failing at once', async () => {
    const { hook, state } = harness()
    const map = state.map
    state.map = null
    setTimeout(() => {
      state.map = map
    }, 30)
    await expect(hook.locateRenderedStation({ stationId: STATION_ID, lngLat: LNGLAT })).resolves.toMatchObject({ stationId: STATION_ID })
  })
})

describe('station-locate hook: every failure has a stable code and no point', () => {
  it('rejects malformed input with STATION_HOOK_INVALID_INPUT before touching the map', async () => {
    const { hook, map } = harness()
    const invalid: unknown[] = [
      undefined,
      null,
      'st-0001',
      {},
      { stationId: STATION_ID },
      { stationId: '', lngLat: LNGLAT },
      { stationId: 42, lngLat: LNGLAT },
      { stationId: 'x'.repeat(257), lngLat: LNGLAT },
      { stationId: STATION_ID, lngLat: [100.5] },
      { stationId: STATION_ID, lngLat: [100.5, 30.5, 1] },
      { stationId: STATION_ID, lngLat: ['100.5', '30.5'] },
      { stationId: STATION_ID, lngLat: [Number.NaN, 30.5] },
      { stationId: STATION_ID, lngLat: [100.5, Number.POSITIVE_INFINITY] },
      { stationId: STATION_ID, lngLat: [181, 30.5] },
      { stationId: STATION_ID, lngLat: [100.5, -91] },
      { stationId: STATION_ID, lngLat: { lng: 100.5, lat: 30.5 } },
    ]
    for (const input of invalid) {
      await expectFailure(hook.locateRenderedStation(input as never), 'STATION_HOOK_INVALID_INPUT')
    }
    expect(map.fitBounds).not.toHaveBeenCalled()
    expect(map.queryRenderedFeatures).not.toHaveBeenCalled()
  })

  it('rejects at once with STATION_HOOK_LAYER_OFF when the station layer is not shown, without moving the camera', async () => {
    const { hook, map, state } = harness()
    state.showStationLayer = false
    const startedAt = performance.now()
    await expectFailure(hook.locateRenderedStation({ stationId: STATION_ID, lngLat: LNGLAT }), 'STATION_HOOK_LAYER_OFF')
    expect(performance.now() - startedAt).toBeLessThan(1_000)
    expect(map.fitBounds).not.toHaveBeenCalled()
  })

  it('rejects with STATION_HOOK_MAP_TIMEOUT when the map never appears within the bounded deadline', async () => {
    const { hook, state } = harness({ deadlineMs: 60 })
    state.map = null
    await expectFailure(hook.locateRenderedStation({ stationId: STATION_ID, lngLat: LNGLAT }), 'STATION_HOOK_MAP_TIMEOUT')
  })

  it('rejects with STATION_HOOK_MAP_TIMEOUT when the map never loads, the station layer never reaches the map, or the move never settles', async () => {
    const notLoaded = harness({ deadlineMs: 60 })
    notLoaded.state.loaded = false
    await expectFailure(notLoaded.hook.locateRenderedStation({ stationId: STATION_ID, lngLat: LNGLAT }), 'STATION_HOOK_MAP_TIMEOUT')
    expect(notLoaded.map.fitBounds).not.toHaveBeenCalled()

    const layerAbsent = harness({ deadlineMs: 60, layers: [HIT] })
    await expectFailure(layerAbsent.hook.locateRenderedStation({ stationId: STATION_ID, lngLat: LNGLAT }), 'STATION_HOOK_MAP_TIMEOUT')
    expect(layerAbsent.map.fitBounds).not.toHaveBeenCalled()

    const neverIdle = harness({ deadlineMs: 60, idle: false })
    await expectFailure(neverIdle.hook.locateRenderedStation({ stationId: STATION_ID, lngLat: LNGLAT }), 'STATION_HOOK_MAP_TIMEOUT')
    expect(neverIdle.map.queryRenderedFeatures).not.toHaveBeenCalled()
  })

  it('rejects an unrendered station id with STATION_HOOK_NOT_RENDERED: nothing there, another station there, or only a river / basin there', async () => {
    for (const pointFeatures of [[], [stationFeature('st-OTHER')], [layerFeature(HIT), layerFeature(M11_BASIN_FILL_LAYER_ID)]]) {
      const { hook } = harness({ pointFeatures })
      await expectFailure(hook.locateRenderedStation({ stationId: STATION_ID, lngLat: LNGLAT }), 'STATION_HOOK_NOT_RENDERED')
    }
  })

  it('does not take a whole-viewport hit for the station: only a feature at the point counts', async () => {
    // The stub answers a non-array geometry with another station; the pinned id is only reachable at the point.
    const { hook } = harness({ pointFeatures: [] })
    await expectFailure(hook.locateRenderedStation({ stationId: 'st-VIEWPORT-OTHER', lngLat: LNGLAT }), 'STATION_HOOK_NOT_RENDERED')
  })

  it('rejects with STATION_HOOK_CLUSTERED when the product click there would expand a cluster', async () => {
    for (const pointFeatures of [[layerFeature(MET_STATION_CLUSTER_LAYER_ID)], [stationFeature(), layerFeature(MET_STATION_CLUSTER_LAYER_ID)]]) {
      const { hook } = harness({ pointFeatures })
      await expectFailure(hook.locateRenderedStation({ stationId: STATION_ID, lngLat: LNGLAT }), 'STATION_HOOK_CLUSTERED')
    }
  })

  it('rejects with STATION_HOOK_POINT_OCCLUDED when a DOM element covers the point', async () => {
    const { hook, state, map } = harness()
    state.topElement = { tagName: 'BUTTON' }
    await expectFailure(hook.locateRenderedStation({ stationId: STATION_ID, lngLat: LNGLAT }), 'STATION_HOOK_POINT_OCCLUDED')
    expect(map.queryRenderedFeatures).not.toHaveBeenCalled()
  })

  it('rejects with STATION_HOOK_POINT_OCCLUDED when another station at the point would win the product click', async () => {
    const { hook } = harness({ pointFeatures: [stationFeature('st-OTHER'), stationFeature()] })
    await expectFailure(hook.locateRenderedStation({ stationId: STATION_ID, lngLat: LNGLAT }), 'STATION_HOOK_POINT_OCCLUDED')
  })

  it('rejects with STATION_HOOK_QUERY_FAILED when the map throws or answers with a non-array', async () => {
    const throwing = harness({
      pointFeatures: () => {
        throw new Error('secret internal detail')
      },
    })
    await expectFailure(throwing.hook.locateRenderedStation({ stationId: STATION_ID, lngLat: LNGLAT }), 'STATION_HOOK_QUERY_FAILED')
    const malformed = harness({ pointFeatures: () => ({ not: 'an array' }) })
    await expectFailure(malformed.hook.locateRenderedStation({ stationId: STATION_ID, lngLat: LNGLAT }), 'STATION_HOOK_QUERY_FAILED')
    const fitThrows = harness()
    fitThrows.map.fitBounds.mockImplementation(() => {
      throw new Error('boom')
    })
    await expectFailure(fitThrows.hook.locateRenderedStation({ stationId: STATION_ID, lngLat: LNGLAT }), 'STATION_HOOK_QUERY_FAILED')
  })

  it('never leaks an internal error message', async () => {
    const { hook } = harness({
      pointFeatures: () => {
        throw new Error('secret internal detail')
      },
    })
    const failure = (await hook.locateRenderedStation({ stationId: STATION_ID, lngLat: LNGLAT }).catch((error: unknown) => error)) as {
      message: string
    }
    expect(failure.message).not.toContain('secret')
  })
})

describe('station-locate hook: read-only', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('issues no request and dispatches no event while locating', async () => {
    const fetchSpy = vi.fn()
    vi.stubGlobal('fetch', fetchSpy)
    const dispatchSpy = vi.spyOn(window, 'dispatchEvent')
    const { hook, canvas } = harness()
    await hook.locateRenderedStation({ stationId: STATION_ID, lngLat: LNGLAT })
    expect(fetchSpy).not.toHaveBeenCalled()
    expect(dispatchSpy).not.toHaveBeenCalled()
    expect((canvas as { addEventListener: ReturnType<typeof vi.fn> }).addEventListener).not.toHaveBeenCalled()
    dispatchSpy.mockRestore()
  })

  it('hands back plain values, never the rendered feature or the map', async () => {
    const feature = stationFeature()
    const { hook, map } = harness({ pointFeatures: [feature] })
    const result = await hook.locateRenderedStation({ stationId: STATION_ID, lngLat: LNGLAT })
    for (const value of Object.values(result)) {
      expect(['string', 'number']).toContain(typeof value)
      expect(value).not.toBe(feature)
      expect(value).not.toBe(map)
    }
  })
})

describe('station-locate hook: install / cleanup ownership', () => {
  it('installs the global and its cleanup removes it', () => {
    const target: Record<string, unknown> = {}
    const { hook } = harness()
    const cleanup = installStationLocateEvidenceHook(target, hook)
    expect(target[STATION_LOCATE_GLOBAL]).toBe(hook)
    cleanup()
    expect(STATION_LOCATE_GLOBAL in target).toBe(false)
  })

  it('a stale cleanup cannot delete a newer instance, and can be called twice safely', () => {
    const target: Record<string, unknown> = {}
    const older = harness().hook
    const newer = harness().hook
    const cleanupOlder = installStationLocateEvidenceHook(target, older)
    const cleanupNewer = installStationLocateEvidenceHook(target, newer)
    expect(target[STATION_LOCATE_GLOBAL]).toBe(newer)
    cleanupOlder()
    expect(target[STATION_LOCATE_GLOBAL]).toBe(newer)
    cleanupNewer()
    expect(STATION_LOCATE_GLOBAL in target).toBe(false)
    cleanupNewer()
    cleanupOlder()
    expect(STATION_LOCATE_GLOBAL in target).toBe(false)
  })

  it('reinstalling after cleanup works, and the earlier cleanup stays inert', () => {
    const target: Record<string, unknown> = {}
    const first = harness().hook
    const cleanupFirst = installStationLocateEvidenceHook(target, first)
    cleanupFirst()
    const second = harness().hook
    const cleanupSecond = installStationLocateEvidenceHook(target, second)
    cleanupFirst()
    expect(target[STATION_LOCATE_GLOBAL]).toBe(second)
    cleanupSecond()
    expect(STATION_LOCATE_GLOBAL in target).toBe(false)
  })

  it('does not delete a foreign object someone else put on the global', () => {
    const target: Record<string, unknown> = {}
    const { hook } = harness()
    const cleanup = installStationLocateEvidenceHook(target, hook)
    const foreign = {}
    target[STATION_LOCATE_GLOBAL] = foreign
    cleanup()
    expect(target[STATION_LOCATE_GLOBAL]).toBe(foreign)
  })
})
