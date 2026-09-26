import { describe, expect, it, vi } from 'vitest'

import { resolveM11ClickTarget } from '@/components/map/m11MapInteractions'
import type { M11RegisteredOverlay } from '@/components/map/m11MapBuilders'
import {
  M11_BASIN_FILL_LAYER_ID,
  MET_STATION_CLUSTER_LAYER_ID,
  MET_STATION_POINT_LAYER_ID,
} from '@/components/map/m11MapPrimitives'

import {
  RIVER_CLICK_HOOK_CODES,
  RIVER_CLICK_PER_MAP_DEADLINE_MS,
} from '../constants'
import {
  adaptRiverClickHookMap,
  createRiverClickEvidenceHook,
  createRiverClickHookController,
  createRiverClickPointerCapture,
  deleteRiverClickHookIfOwned,
  locateRenderedRiverFeature,
  type RiverClickHookCanvas,
  type RiverClickHookMap,
  type RiverClickHookSelectionInput,
  type RiverClickPointerEventLike,
  type RiverClickProductClickContext,
  type RiverClickRenderedFeature,
} from '../hook'

const HIT = 'm11-discharge-line-hit'
const OVERLAY = {
  layerId: 'discharge',
  sourceId: 'm11-discharge-source',
  sourceKey: 'k',
  layer: { id: 'm11-discharge-line' },
  source: { type: 'vector', tiles: [], sourceLayer: 'hydro', minzoom: 0, maxzoom: 14, metadata: {} },
} as unknown as M11RegisteredOverlay

function makeFeature(overrides: Record<string, unknown> = {}) {
  return {
    id: 'feature-1',
    layer: { id: HIT },
    geometry: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
    properties: {
      basin_id: 'basins_qhh',
      river_segment_id: 'seg-001',
      segment_id: 'seg-001',
      basin_version_id: 'bv-001',
      river_network_version_id: 'rn-001',
    },
    ...overrides,
  }
}

function layerFeature(layerId: string) {
  return { layer: { id: layerId }, geometry: { type: 'Point', coordinates: [101, 31] }, properties: { marker: layerId } }
}

function selectionInput(overrides: Partial<RiverClickHookSelectionInput> = {}): RiverClickHookSelectionInput {
  return {
    bbox: [[100, 30], [102, 32]],
    anchor: [101, 31],
    basinId: 'basins_qhh',
    riverSegmentId: 'seg-001',
    basinVersionId: 'bv-001',
    riverNetworkVersionId: 'rn-001',
    ...overrides,
  }
}

type FakeCanvas = RiverClickHookCanvas & {
  listeners: Set<(event: RiverClickPointerEventLike) => void>
  fire: (event: Partial<RiverClickPointerEventLike>) => void
}

/** Canvas stand-in whose listeners the test can drive with trusted/untrusted events. */
function makeCanvas(rect = { left: 100, top: 50 }): FakeCanvas {
  const listeners = new Set<(event: RiverClickPointerEventLike) => void>()
  const canvas: FakeCanvas = {
    listeners,
    getBoundingClientRect: () => rect,
    addEventListener: vi.fn((_type: string, listener: (event: RiverClickPointerEventLike) => void) => {
      listeners.add(listener)
    }),
    removeEventListener: vi.fn((_type: string, listener: (event: RiverClickPointerEventLike) => void) => {
      listeners.delete(listener)
    }),
    fire: (event) => {
      for (const listener of [...listeners]) {
        listener({ isTrusted: true, timeStamp: 1234.5, clientX: 140, clientY: 90, target: canvas, ...event })
      }
    },
  }
  return canvas
}

/**
 * Map stand-in: the 16px box query (array geometry) returns `boxResults`; the
 * point query the product click path would issue returns `pointResults(layers)`.
 * An absent layer passed to queryRenderedFeatures throws, like MapLibre's
 * ErrorEvent for an unknown layer id.
 */
function makeMap(options: {
  boxResults?: unknown
  pointResults?: (layers: string[]) => unknown[]
  existingLayers?: string[]
  canvas?: FakeCanvas
  overrides?: Record<string, unknown>
} = {}) {
  const canvas = options.canvas ?? makeCanvas()
  const existing = new Set(options.existingLayers ?? [HIT, M11_BASIN_FILL_LAYER_ID, MET_STATION_POINT_LAYER_ID, MET_STATION_CLUSTER_LAYER_ID])
  const map = {
    loaded: () => true,
    isStyleLoaded: () => true,
    fitBounds: vi.fn(),
    project: vi.fn(() => ({ x: 40, y: 40 })),
    queryRenderedFeatures: vi.fn((geometry: unknown, query: { layers: string[] }) => {
      if (Array.isArray(geometry)) return options.boxResults ?? [makeFeature()]
      for (const layer of query.layers) {
        if (!existing.has(layer)) throw new Error(`layer ${layer} does not exist`)
      }
      return options.pointResults ? options.pointResults(query.layers) : [makeFeature()]
    }),
    getLayer: vi.fn((id: string) => (existing.has(id) ? { id } : undefined)),
    getCanvas: vi.fn(() => canvas),
    once: vi.fn((_event: string, callback: () => void) => {
      queueMicrotask(callback)
    }),
    off: vi.fn(),
    ...options.overrides,
  }
  return { map: map as typeof map & RiverClickHookMap, canvas }
}

function productClick(options: { showStationLayer?: boolean; interactive?: string[] } = {}): RiverClickProductClickContext {
  return {
    getInteractiveLayerIds: () => options.interactive ?? [M11_BASIN_FILL_LAYER_ID, HIT],
    resolveClickTarget: (features: RiverClickRenderedFeature[]) =>
      resolveM11ClickTarget({ features, showStationLayer: options.showStationLayer ?? false, renderableOverlay: OVERLAY }),
  }
}

function locate(
  map: RiverClickHookMap,
  extra: { productClick?: RiverClickProductClickContext; elementFromPoint?: (x: number, y: number) => unknown; input?: RiverClickHookSelectionInput } = {},
) {
  const canvas = map.getCanvas()
  return locateRenderedRiverFeature({
    input: extra.input ?? selectionInput(),
    map,
    getOverlayHitLayerId: () => HIT,
    productClick: extra.productClick ?? productClick(),
    elementFromPoint: extra.elementFromPoint ?? (() => canvas),
    now: () => 0,
    deadlineMs: RIVER_CLICK_PER_MAP_DEADLINE_MS,
  })
}

function makeHook(map: RiverClickHookMap | null, extra: { productClick?: RiverClickProductClickContext; elementFromPoint?: (x: number, y: number) => unknown; getMap?: () => RiverClickHookMap | null } = {}) {
  const getMap = extra.getMap ?? (() => map)
  const controller = createRiverClickHookController({
    getMap,
    getOverlayHitLayerId: () => HIT,
    productClick: extra.productClick ?? productClick(),
    elementFromPoint: extra.elementFromPoint ?? (() => getMap()?.getCanvas() ?? null),
    now: () => 0,
    locate: locateRenderedRiverFeature,
  })
  const pointerCapture = createRiverClickPointerCapture({ getCanvas: () => getMap()?.getCanvas() ?? null })
  return { hook: createRiverClickEvidenceHook({ controller, pointerCapture }), pointerCapture }
}

describe('river-click hook post-fit idle gating', () => {
  it('requires the post-fit idle event itself; no render-only event is registered or sufficient', async () => {
    vi.useFakeTimers()
    try {
      const callbacks = new Map<string, () => void>()
      const { map } = makeMap({
        overrides: {
          once: vi.fn((event: string, callback: () => void) => {
            callbacks.set(event, callback)
          }),
          off: vi.fn((event: string, callback: () => void) => {
            if (callbacks.get(event) === callback) callbacks.delete(event)
          }),
        },
      })
      const promise = locate(map)
      await Promise.resolve()
      expect(callbacks.has('render')).toBe(false)
      expect(callbacks.has('idle')).toBe(true)
      await vi.advanceTimersByTimeAsync(RIVER_CLICK_PER_MAP_DEADLINE_MS + 1)
      const result = await promise
      expect(result.ok).toBe(false)
      if (!result.ok) expect(result.code).toBe('HOOK_MAP_TIMEOUT')
    } finally {
      vi.useRealTimers()
    }
  })

  it('accepts a post-fit idle event and verifies loaded/style-loaded under the one deadline', async () => {
    vi.useFakeTimers()
    try {
      const callbacks = new Map<string, () => void>()
      const { map } = makeMap({
        overrides: {
          once: vi.fn((event: string, callback: () => void) => {
            callbacks.set(event, callback)
          }),
          off: vi.fn((event: string, callback: () => void) => {
            if (callbacks.get(event) === callback) callbacks.delete(event)
          }),
        },
      })
      const promise = locate(map)
      await Promise.resolve()
      const idle = callbacks.get('idle')
      expect(idle).toBeTypeOf('function')
      idle!()
      const result = await promise
      expect(result.ok).toBe(true)
      expect(map.off).toHaveBeenCalledTimes(1)
      expect(callbacks.size).toBe(0)
    } finally {
      vi.useRealTimers()
    }
  })

  it('removes listeners and the timer exactly once on the timeout path', async () => {
    vi.useFakeTimers()
    try {
      const callbacks = new Map<string, () => void>()
      const offSpy = vi.fn((event: string, callback: () => void) => {
        if (callbacks.get(event) === callback) callbacks.delete(event)
      })
      const { map } = makeMap({
        overrides: {
          once: vi.fn((event: string, callback: () => void) => {
            callbacks.set(event, callback)
          }),
          off: offSpy,
        },
      })
      const promise = locate(map)
      await Promise.resolve()
      expect(callbacks.get('idle')).toBeTypeOf('function')
      await vi.advanceTimersByTimeAsync(RIVER_CLICK_PER_MAP_DEADLINE_MS + 1)
      const result = await promise
      expect(result.ok).toBe(false)
      if (!result.ok) expect(result.code).toBe('HOOK_MAP_TIMEOUT')
      expect(offSpy).toHaveBeenCalledTimes(1)
      expect(callbacks.size).toBe(0)
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('river-click locate core (bounded query, exact match)', () => {
  it('rejects a queryRenderedFeatures output that is NOT an array as a closed HOOK_QUERY_FAILED', async () => {
    const { map } = makeMap({ boxResults: { not: 'an array' } })
    const result = await locate(map)
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.code).toBe('HOOK_QUERY_FAILED')
  })

  it('uses exact fit options, a 16x16 projected box, and only the current hit layer', async () => {
    const { map } = makeMap()
    const result = await locate(map)
    expect(result.ok).toBe(true)
    expect(map.fitBounds).toHaveBeenCalledWith([[100, 30], [102, 32]], { padding: 48, duration: 0, maxZoom: 14 })
    expect(map.queryRenderedFeatures).toHaveBeenCalledWith([{ x: 32, y: 32 }, { x: 48, y: 48 }], { layers: [HIT] })
  })

  it('rejects 65 total query results as HOOK_QUERY_LIMIT', async () => {
    const extras = Array.from({ length: 65 }, (_, index) => makeFeature({ id: `feature-${index}` }))
    const { map } = makeMap({ boxResults: extras })
    const result = await locate(map)
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.code).toBe('HOOK_QUERY_LIMIT')
    const { hook } = makeHook(map)
    await expect(hook.locateRenderedRiver(selectionInput())).rejects.toMatchObject({ code: 'HOOK_QUERY_LIMIT' })
  })

  it('accepts 64 results with exactly one matching actual feature', async () => {
    const others = Array.from({ length: 63 }, (_, index) => makeFeature({
      id: `other-${index}`,
      properties: { ...makeFeature().properties, river_segment_id: `other-${index}`, segment_id: `other-${index}` },
    }))
    const { map } = makeMap({ boxResults: [...others, makeFeature()] })
    const result = await locate(map)
    expect(result.ok).toBe(true)
    if (!result.ok) throw new Error('64-with-one-match must succeed')
    expect(result.output.normalized.riverSegmentId).toBe('seg-001')
  })

  it('rejects two matching features, a wrong hit layer, zero match, and drifted identities as HOOK_FEATURE_MISMATCH', async () => {
    const cases: unknown[][] = [
      [makeFeature(), makeFeature({ id: 'feature-2' })],
      [makeFeature({ layer: { id: 'm11-other-hit' } })],
      [],
      [makeFeature({ properties: { ...makeFeature().properties, basin_version_id: 'bv-DRIFT' } })],
    ]
    for (const boxResults of cases) {
      const { map } = makeMap({ boxResults })
      const result = await locate(map)
      expect(result.ok).toBe(false)
      if (!result.ok) expect(result.code).toBe('HOOK_FEATURE_MISMATCH')
      const { hook } = makeHook(map)
      await expect(hook.locateRenderedRiver(selectionInput())).rejects.toMatchObject({ code: 'HOOK_FEATURE_MISMATCH' })
    }
  })

  it('accepts API-derived 97-character and 256-byte version identities and rejects over-long/empty ones', async () => {
    const run = async (basinVersionId: string, riverNetworkVersionId: string) => {
      const feature = makeFeature({
        properties: { ...makeFeature().properties, basin_version_id: basinVersionId, river_network_version_id: riverNetworkVersionId },
      })
      const { map } = makeMap({ boxResults: [feature], pointResults: () => [feature] })
      const { hook } = makeHook(map)
      return hook.locateRenderedRiver(selectionInput({ basinVersionId, riverNetworkVersionId }))
        .then((output) => ({ ok: true as const, output }))
        .catch((error: unknown) => ({ ok: false as const, error }))
    }
    const accepted97 = await run('v'.repeat(97), 'v'.repeat(97))
    expect(accepted97.ok).toBe(true)
    if (accepted97.ok) expect(accepted97.output.basinVersionId).toBe('v'.repeat(97))
    expect((await run('w'.repeat(256), 'w'.repeat(256))).ok).toBe(true)
    expect((await run('é'.repeat(128), 'é'.repeat(128))).ok).toBe(true)
    const rejectedOver = await run('x'.repeat(257), 'x'.repeat(257))
    expect(rejectedOver.ok).toBe(false)
    if (!rejectedOver.ok) expect((rejectedOver.error as { code: string }).code).toBe('HOOK_INVALID_INPUT')
    const rejectedEmpty = await run('', '')
    expect(rejectedEmpty.ok).toBe(false)
    if (!rejectedEmpty.ok) expect((rejectedEmpty.error as { code: string }).code).toBe('HOOK_INVALID_INPUT')
  })

  it('no longer takes a waitForReady parameter', () => {
    const source = (locateRenderedRiverFeature as unknown as () => unknown).toString()
    expect(source).not.toContain('waitForReady')
  })
})

describe('river-click locate: the located point must reach the river through the product click path', () => {
  it('resolves the four identities plus the canvas-rect client point, and the product point query uses only existing interactive layers', async () => {
    const canvas = makeCanvas({ left: 100, top: 50 })
    const { map } = makeMap({ canvas })
    const elementFromPoint = vi.fn(() => canvas)
    const { hook } = makeHook(map, { elementFromPoint })
    const located = await hook.locateRenderedRiver(selectionInput())
    expect(located).toEqual({
      basinId: 'basins_qhh',
      riverSegmentId: 'seg-001',
      basinVersionId: 'bv-001',
      riverNetworkVersionId: 'rn-001',
      clientX: 140,
      clientY: 90,
    })
    expect(elementFromPoint).toHaveBeenCalledWith(140, 90)
    expect(map.queryRenderedFeatures).toHaveBeenCalledWith({ x: 40, y: 40 }, { layers: [M11_BASIN_FILL_LAYER_ID, HIT] })
  })

  it('filters an absent interactive layer id through map.getLayer so the point query raises no map error', async () => {
    const { map } = makeMap({ existingLayers: [HIT] })
    const result = await locate(map, {
      productClick: productClick({ interactive: [MET_STATION_POINT_LAYER_ID, MET_STATION_CLUSTER_LAYER_ID, M11_BASIN_FILL_LAYER_ID, HIT] }),
    })
    expect(result.ok).toBe(true)
    expect(map.getLayer).toHaveBeenCalledWith(M11_BASIN_FILL_LAYER_ID)
    expect(map.queryRenderedFeatures).toHaveBeenCalledWith({ x: 40, y: 40 }, { layers: [HIT] })
  })

  it('rejects HOOK_POINT_OCCLUDED when a DOM element covers the point (a real click would hit that control)', async () => {
    const { map } = makeMap()
    const control = { tagName: 'BUTTON' }
    const result = await locate(map, { elementFromPoint: () => control })
    expect(result).toMatchObject({ ok: false, code: 'HOOK_POINT_OCCLUDED' })
    const nothing = await locate(map, { elementFromPoint: () => null })
    expect(nothing).toMatchObject({ ok: false, code: 'HOOK_POINT_OCCLUDED' })
  })

  it('rejects HOOK_POINT_OCCLUDED when the product walk would pick a station cluster, a station, basin fill, or a different river', async () => {
    const river = makeFeature()
    const otherRiver = makeFeature({ properties: { ...makeFeature().properties, river_segment_id: 'seg-OTHER', segment_id: 'seg-OTHER' } })
    const stationsShown = productClick({ showStationLayer: true, interactive: [MET_STATION_POINT_LAYER_ID, MET_STATION_CLUSTER_LAYER_ID, M11_BASIN_FILL_LAYER_ID, HIT] })
    const cases: Array<{ label: string; point: unknown[]; product: RiverClickProductClickContext }> = [
      { label: 'cluster', point: [river, layerFeature(MET_STATION_CLUSTER_LAYER_ID)], product: stationsShown },
      { label: 'station', point: [river, layerFeature(MET_STATION_POINT_LAYER_ID)], product: stationsShown },
      { label: 'basin fill only', point: [layerFeature(M11_BASIN_FILL_LAYER_ID)], product: productClick() },
      { label: 'another river first', point: [otherRiver, river], product: productClick() },
      { label: 'nothing interactive', point: [], product: productClick() },
    ]
    for (const { label, point, product } of cases) {
      const { map } = makeMap({ boxResults: [river], pointResults: () => point })
      const result = await locate(map, { productClick: product })
      expect(result, label).toMatchObject({ ok: false, code: 'HOOK_POINT_OCCLUDED' })
    }
    // The same station features are ignored when stations are not shown (product parity).
    const { map } = makeMap({ boxResults: [river], pointResults: () => [layerFeature(MET_STATION_POINT_LAYER_ID), river] })
    expect((await locate(map, { productClick: productClick({ showStationLayer: false }) })).ok).toBe(true)
  })

  it('never reaches a product selection callback: the hook takes none and a planted spy is never called', async () => {
    const onOverlayClick = vi.fn()
    const { map } = makeMap()
    const controller = createRiverClickHookController({
      getMap: () => map,
      getOverlayHitLayerId: () => HIT,
      productClick: productClick(),
      elementFromPoint: () => map.getCanvas(),
      now: () => 0,
      locate: locateRenderedRiverFeature,
    })
    const pointerCapture = createRiverClickPointerCapture({ getCanvas: () => map.getCanvas() })
    // A caller trying to hand the factory a callback: it is not part of the contract and is ignored.
    const hook = createRiverClickEvidenceHook({ controller, pointerCapture, onOverlayClick } as never)
    expect(Object.keys(hook).sort()).toEqual(['armPointerCapture', 'locateRenderedRiver', 'takePointerCapture'])
    hook.armPointerCapture()
    await hook.locateRenderedRiver(selectionInput())
    ;(map.getCanvas() as FakeCanvas).fire({})
    hook.takePointerCapture()
    expect(onOverlayClick).not.toHaveBeenCalled()
    expect(createRiverClickEvidenceHook.length).toBe(1)
  })

  it('redacts a rejection whose code is outside the closed hook code set to HOOK_QUERY_FAILED', async () => {
    const controller = {
      locateRenderedRiver: vi.fn(async () => {
        throw { code: 'EVIL_INJECTED_CODE', message: 'attacker text' }
      }),
    }
    const hook = createRiverClickEvidenceHook({ controller: controller as never, pointerCapture: createRiverClickPointerCapture({ getCanvas: () => null }) })
    await expect(hook.locateRenderedRiver(selectionInput())).rejects.toEqual({ code: 'HOOK_QUERY_FAILED', message: 'river-click hook locate failed' })
  })

  it('propagates every closed hook code verbatim, including the new occlusion and pointer codes', async () => {
    expect(RIVER_CLICK_HOOK_CODES).toEqual(expect.arrayContaining(['HOOK_POINT_OCCLUDED', 'HOOK_POINTER_MISSING', 'HOOK_POINTER_INVALID']))
    for (const code of RIVER_CLICK_HOOK_CODES) {
      const controller = { locateRenderedRiver: vi.fn(async () => { throw { code, message: 'raw detail' } }) }
      const hook = createRiverClickEvidenceHook({ controller: controller as never, pointerCapture: createRiverClickPointerCapture({ getCanvas: () => null }) })
      await expect(hook.locateRenderedRiver(selectionInput())).rejects.toEqual({ code, message: 'river-click hook locate failed' })
    }
  })
})

describe('river-click trusted pointer capture', () => {
  it('returns the single trusted pointer-down timeStamp and client point', () => {
    const canvas = makeCanvas()
    const capture = createRiverClickPointerCapture({ getCanvas: () => canvas })
    capture.arm()
    expect(canvas.addEventListener).toHaveBeenCalledWith('pointerdown', expect.any(Function), { capture: true })
    canvas.fire({ timeStamp: 812.25, clientX: 141, clientY: 89 })
    expect(capture.take()).toEqual({ timeStamp: 812.25, clientX: 141, clientY: 89, isTrusted: true })
    // take removed the listener: later events are not observed.
    expect(canvas.listeners.size).toBe(0)
  })

  it('rejects an untrusted synthetic dispatchEvent on a real canvas element as HOOK_POINTER_INVALID', () => {
    const canvas = document.createElement('canvas')
    document.body.appendChild(canvas)
    try {
      const capture = createRiverClickPointerCapture({ getCanvas: () => canvas as unknown as RiverClickHookCanvas })
      capture.arm()
      canvas.dispatchEvent(new Event('pointerdown', { bubbles: true }))
      expect(capture.take()).toEqual({ error: 'HOOK_POINTER_INVALID' })
    } finally {
      canvas.remove()
    }
  })

  it('rejects a trusted event accompanied by an untrusted one, and a duplicate trusted pointer-down, as HOOK_POINTER_INVALID', () => {
    const canvas = makeCanvas()
    const capture = createRiverClickPointerCapture({ getCanvas: () => canvas })
    capture.arm()
    canvas.fire({})
    canvas.fire({ isTrusted: false })
    expect(capture.take()).toEqual({ error: 'HOOK_POINTER_INVALID' })
    capture.arm()
    canvas.fire({})
    canvas.fire({ timeStamp: 1300 })
    expect(capture.take()).toEqual({ error: 'HOOK_POINTER_INVALID' })
  })

  it('returns HOOK_POINTER_MISSING when nothing arrived or the capture was never armed', () => {
    const canvas = makeCanvas()
    const capture = createRiverClickPointerCapture({ getCanvas: () => canvas })
    expect(capture.take()).toEqual({ error: 'HOOK_POINTER_MISSING' })
    canvas.fire({})
    expect(capture.take()).toEqual({ error: 'HOOK_POINTER_MISSING' })
    capture.arm()
    expect(capture.take()).toEqual({ error: 'HOOK_POINTER_MISSING' })
    // One take consumes the arm: a second take is unarmed again.
    capture.arm()
    canvas.fire({})
    expect(capture.take()).toMatchObject({ isTrusted: true })
    expect(capture.take()).toEqual({ error: 'HOOK_POINTER_MISSING' })
  })

  it('ignores pointer-downs whose target is not the canvas and clears a previous capture on re-arm', () => {
    const canvas = makeCanvas()
    const capture = createRiverClickPointerCapture({ getCanvas: () => canvas })
    capture.arm()
    canvas.fire({ target: { tagName: 'BUTTON' } })
    canvas.fire({})
    capture.arm()
    expect(canvas.listeners.size).toBe(1)
    canvas.fire({ timeStamp: 99 })
    expect(capture.take()).toEqual({ timeStamp: 99, clientX: 140, clientY: 90, isTrusted: true })
  })

  it('an arm issued before the map exists attaches when locate resolves, so the following click is observed', async () => {
    let current: RiverClickHookMap | null = null
    const { map, canvas } = makeMap()
    const { hook } = makeHook(null, { getMap: () => current })
    hook.armPointerCapture()
    expect(canvas.listeners.size).toBe(0)
    setTimeout(() => {
      current = map
    }, 5)
    const located = await hook.locateRenderedRiver(selectionInput())
    expect(canvas.listeners.size).toBe(1)
    canvas.fire({ timeStamp: 2000, clientX: located.clientX, clientY: located.clientY })
    expect(hook.takePointerCapture()).toEqual({ timeStamp: 2000, clientX: 140, clientY: 90, isTrusted: true })
  })

  it('dispose removes the armed listener (mount cleanup)', () => {
    const canvas = makeCanvas()
    const capture = createRiverClickPointerCapture({ getCanvas: () => canvas })
    capture.arm()
    expect(canvas.listeners.size).toBe(1)
    capture.dispose()
    expect(canvas.listeners.size).toBe(0)
    expect(capture.take()).toEqual({ error: 'HOOK_POINTER_MISSING' })
  })
})

describe('river-click hook ownership', () => {
  it('deleteRiverClickHookIfOwned returns false for a NULL current generation (nothing installed), preserving the identity+generation invariant', () => {
    const hook = { marker: 'hook' }
    expect(deleteRiverClickHookIfOwned(hook, hook, 7, null)).toBe(false)
    expect(deleteRiverClickHookIfOwned(hook, hook, 7, 7)).toBe(true)
    expect(deleteRiverClickHookIfOwned(hook, { marker: 'other' }, 7, 7)).toBe(false)
    expect(deleteRiverClickHookIfOwned(hook, hook, 6, 7)).toBe(false)
  })
})

describe('river-click native map adapter (maplibre-gl Map -> RiverClickHookMap)', () => {
  it('delegates the narrow read/fit/query/idle/layer/canvas methods to the REAL native map with exact args, preserving identities', () => {
    const nativeCanvas = { style: { cursor: 'pointer' } }
    const nativeMap = {
      loaded: () => true,
      isStyleLoaded: () => true,
      fitBounds: vi.fn((bounds: unknown, options: unknown) => ({ nativeFit: bounds, options })),
      project: vi.fn((coord: [number, number]) => ({ x: coord[0] + 1, y: coord[1] + 1 })),
      queryRenderedFeatures: vi.fn((_geometry: unknown, _options: unknown) => [{ identity: { basin_id: 'basins_qhh' } }]),
      getLayer: vi.fn((id: string) => (id === HIT ? { id } : undefined)),
      getCanvas: () => nativeCanvas,
      once: vi.fn((_event: string, callback: () => void) => { queueMicrotask(callback) }),
      off: vi.fn(),
    }
    const adapted = adaptRiverClickHookMap(nativeMap)
    expect(adapted).not.toBeNull()
    const map = adapted as RiverClickHookMap
    expect(map.loaded()).toBe(true)
    expect(map.isStyleLoaded()).toBe(true)
    const bbox = [[100, 30], [102, 32]] as [[number, number], [number, number]]
    map.fitBounds(bbox, { padding: 48, duration: 0, maxZoom: 14 })
    expect(nativeMap.fitBounds).toHaveBeenCalledWith(bbox, { padding: 48, duration: 0, maxZoom: 14 })
    expect(map.project([101, 31])).toEqual({ x: 102, y: 32 })
    const box = [{ x: 40, y: 40 }, { x: 56, y: 56 }] as [{ x: number; y: number }, { x: number; y: number }]
    const features = map.queryRenderedFeatures(box, { layers: [HIT] })
    expect(nativeMap.queryRenderedFeatures).toHaveBeenCalledWith(box, { layers: [HIT] })
    expect(features[0]).toBe((nativeMap.queryRenderedFeatures as ReturnType<typeof vi.fn>).mock.results[0].value[0])
    map.queryRenderedFeatures({ x: 40, y: 40 }, { layers: [HIT] })
    expect(nativeMap.queryRenderedFeatures).toHaveBeenLastCalledWith({ x: 40, y: 40 }, { layers: [HIT] })
    expect(map.getLayer(HIT)).toEqual({ id: HIT })
    expect(map.getLayer('absent')).toBeUndefined()
    // The native canvas element itself (identity), never a copy.
    expect(map.getCanvas()).toBe(nativeCanvas)
    const callback = () => undefined
    map.once('idle', callback)
    expect(nativeMap.once).toHaveBeenCalledWith('idle', callback)
    map.off?.('idle', callback)
    expect(nativeMap.off).toHaveBeenCalledWith('idle', callback)
  })

  it('returns null for a null/absent/incomplete native map (transient readiness, never an unavailable map)', () => {
    expect(adaptRiverClickHookMap(null)).toBeNull()
    expect(adaptRiverClickHookMap(undefined)).toBeNull()
    expect(adaptRiverClickHookMap({ loaded: () => true })).toBeNull()
    const nativeMap = {
      loaded: () => true,
      isStyleLoaded: () => true,
      fitBounds: vi.fn(),
      project: vi.fn(() => ({ x: 0, y: 0 })),
      queryRenderedFeatures: vi.fn(() => []),
      getCanvas: () => ({ style: { cursor: '' } }),
      once: vi.fn(),
    }
    // getLayer is required: without it the interactive-layer filter cannot run.
    expect(adaptRiverClickHookMap(nativeMap)).toBeNull()
    expect(adaptRiverClickHookMap({ ...nativeMap, getLayer: () => undefined })).not.toBeNull()
  })
})

describe('river-click hook controller map-absent fail-closed', () => {
  it('waits for a DELAYED map ref under ONE total budget instead of failing immediately', async () => {
    let current: RiverClickHookMap | null = null
    let pollClock = 0
    const controller = createRiverClickHookController({
      getMap: () => current,
      getOverlayHitLayerId: () => (current === null ? null : HIT),
      productClick: productClick(),
      elementFromPoint: () => current?.getCanvas() ?? null,
      now: () => { pollClock += 10; return pollClock },
      locate: locateRenderedRiverFeature,
    })
    const promise = controller.locateRenderedRiver(selectionInput())
    setTimeout(() => {
      current = makeMap().map
    }, 5)
    const result = await promise
    expect(result).toMatchObject({ normalized: { basinId: 'basins_qhh' }, clientX: 140, clientY: 90 })
  })

  it('times out with HOOK_MAP_TIMEOUT when the map ref never appears (never HOOK_MAP_UNAVAILABLE)', async () => {
    let pollClock = 0
    const controller = createRiverClickHookController({
      getMap: () => null,
      getOverlayHitLayerId: () => null,
      productClick: productClick(),
      elementFromPoint: () => null,
      now: () => { pollClock += 6_000; return pollClock },
      locate: locateRenderedRiverFeature,
    })
    await expect(controller.locateRenderedRiver(selectionInput())).rejects.toMatchObject({ code: 'HOOK_MAP_TIMEOUT' })
  })
})
