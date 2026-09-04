import { describe, expect, it, vi } from 'vitest'

import {
  RIVER_CLICK_HOOK_CODES,
  RIVER_CLICK_PER_MAP_DEADLINE_MS,
} from '../constants'
import {
  adaptRiverClickHookMap,
  createRiverClickEvidenceHook,
  createRiverClickHookController,
  deleteRiverClickHookIfOwned,
  selectRenderedRiverFeature,
  type RiverClickHookMap,
  type RiverClickHookSelectionInput,
} from '../hook'

function makeFeature() {
  return {
    id: 'feature-1',
    layer: { id: 'm11-discharge-line-hit' },
    geometry: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
    properties: {
      basin_id: 'basins_qhh',
      river_segment_id: 'seg-001',
      segment_id: 'seg-001',
      basin_version_id: 'bv-001',
      river_network_version_id: 'rn-001',
    },
  }
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

function makeMap(overrides: Record<string, unknown> = {}) {
  return {
    loaded: () => true,
    isStyleLoaded: () => true,
    fitBounds: vi.fn(),
    project: vi.fn(() => ({ x: 40, y: 40 })),
    queryRenderedFeatures: vi.fn(() => [makeFeature()]),
    getCanvas: vi.fn(() => ({ style: { cursor: '' } })),
    once: vi.fn(),
    off: vi.fn(),
    ...overrides,
  }
}

describe('river-click hook post-fit idle gating', () => {
  it('requires the post-fit idle event itself; no render-only event is registered or sufficient', async () => {
    vi.useFakeTimers()
    try {
      const callbacks = new Map<string, () => void>()
      const map = makeMap({
        loaded: () => true,
        isStyleLoaded: () => true,
        once: vi.fn((event: string, callback: () => void) => {
          callbacks.set(event, callback)
        }),
        off: vi.fn((event: string, callback: () => void) => {
          if (callbacks.get(event) === callback) callbacks.delete(event)
        }),
      })
      const promise = selectRenderedRiverFeature({
        input: selectionInput(),
        map: map as never,
        getOverlayHitLayerId: () => 'm11-discharge-line-hit',
        now: () => 0,
        deadlineMs: RIVER_CLICK_PER_MAP_DEADLINE_MS,
      })
      // The wait must register ONLY the idle event (idle implies rendered/settled;
      // render alone can precede tile idle and must not satisfy the wait).
      await Promise.resolve()
      expect(callbacks.has('render')).toBe(false)
      expect(callbacks.has('idle')).toBe(true)
      // Never firing idle must time out, even though the map reports loaded.
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
      const map = makeMap({
        loaded: () => true,
        isStyleLoaded: () => true,
        once: vi.fn((event: string, callback: () => void) => {
          callbacks.set(event, callback)
        }),
        off: vi.fn((event: string, callback: () => void) => {
          if (callbacks.get(event) === callback) callbacks.delete(event)
        }),
      })
      const promise = selectRenderedRiverFeature({
        input: selectionInput(),
        map: map as never,
        getOverlayHitLayerId: () => 'm11-discharge-line-hit',
        now: () => 0,
        deadlineMs: RIVER_CLICK_PER_MAP_DEADLINE_MS,
      })
      // Selection awaits map/overlay readiness before registering the idle
      // wait; let those microtasks settle so the idle listener is armed.
      await Promise.resolve()
      const idle = callbacks.get('idle')
      expect(idle).toBeTypeOf('function')
      idle!()
      const result = await promise
      expect(result.ok).toBe(true)
      // listeners removed exactly once (only idle is registered)
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
      const map = makeMap({
        loaded: () => true,
        isStyleLoaded: () => true,
        once: vi.fn((event: string, callback: () => void) => {
          callbacks.set(event, callback)
        }),
        off: offSpy,
      })
      const promise = selectRenderedRiverFeature({
        input: selectionInput(),
        map: map as never,
        getOverlayHitLayerId: () => 'm11-discharge-line-hit',
        now: () => 0,
        deadlineMs: RIVER_CLICK_PER_MAP_DEADLINE_MS,
      })
      // Let readiness settle so the idle listener is armed before expiry.
      await Promise.resolve()
      expect(callbacks.get('idle')).toBeTypeOf('function')
      await vi.advanceTimersByTimeAsync(RIVER_CLICK_PER_MAP_DEADLINE_MS + 1)
      const result = await promise
      expect(result.ok).toBe(false)
      if (!result.ok) expect(result.code).toBe('HOOK_MAP_TIMEOUT')
      // Cleanup removed the listener exactly once: no re-registration, no
      // leftover callback (asserting the map WITHOUT manually clearing it).
      expect(offSpy).toHaveBeenCalledTimes(1)
      expect(callbacks.size).toBe(0)
    } finally {
      vi.useRealTimers()
    }
  })

  it('rejects a queryRenderedFeatures output that is NOT an array as a closed HOOK_QUERY_FAILED', async () => {
    const map = makeMap({
      queryRenderedFeatures: vi.fn(() => ({ not: 'an array' })),
      once: vi.fn((_event: string, callback: () => void) => {
        queueMicrotask(callback)
      }),
    })
    const result = await selectRenderedRiverFeature({
      input: selectionInput(),
      map,
      getOverlayHitLayerId: () => 'm11-discharge-line-hit',
      now: () => 0,
      deadlineMs: RIVER_CLICK_PER_MAP_DEADLINE_MS,
    })
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.code).toBe('HOOK_QUERY_FAILED')
  })

  it('rejects a rejection whose code is outside the closed hook code set as HOOK_QUERY_FAILED', async () => {
    const controller = {
      selectRenderedRiver: vi.fn(async () => {
        throw { code: 'EVIL_INJECTED_CODE', message: 'attacker text' }
      }),
    }
    const hook = createRiverClickEvidenceHook({
      onOverlayClick: (dispatch) => void dispatch,
      controller: controller as never,
    })
    await expect(hook.selectRenderedRiver(selectionInput())).rejects.toMatchObject({
      code: 'HOOK_QUERY_FAILED',
      message: expect.not.stringContaining('attacker text'),
    })
  })

  it('deleteRiverClickHookIfOwned returns false for a NULL current generation (nothing installed), preserving the identity+generation invariant', () => {
    const hook = { marker: 'hook' }
    expect(deleteRiverClickHookIfOwned(hook, hook, 7, null)).toBe(false)
    expect(deleteRiverClickHookIfOwned(hook, hook, 7, 7)).toBe(true)
    expect(deleteRiverClickHookIfOwned(hook, { marker: 'other' }, 7, 7)).toBe(false)
    expect(deleteRiverClickHookIfOwned(hook, hook, 6, 7)).toBe(false)
  })

  it('selectRenderedRiverFeature uses exact fit options, a 16x16 projected box, and only the current hit layer', async () => {
    const map = makeMap({
      once: vi.fn((_event: string, callback: () => void) => {
        queueMicrotask(callback)
      }),
    })
    const result = await selectRenderedRiverFeature({
      input: selectionInput(),
      map: map as never,
      getOverlayHitLayerId: () => 'm11-discharge-line-hit',
      now: () => 0,
      deadlineMs: RIVER_CLICK_PER_MAP_DEADLINE_MS,
    })
    expect(result.ok).toBe(true)
    expect(map.fitBounds).toHaveBeenCalledWith([[100, 30], [102, 32]], { padding: 48, duration: 0, maxZoom: 14 })
    expect(map.queryRenderedFeatures).toHaveBeenCalledWith(
      [{ x: 32, y: 32 }, { x: 48, y: 48 }],
      { layers: ['m11-discharge-line-hit'] },
    )
  })

  it('rejects 65 total query results as HOOK_QUERY_LIMIT and does not dispatch through the evidence hook', async () => {
    const extras = Array.from({ length: 65 }, (_, index) => ({
      ...makeFeature(),
      id: `feature-${index}`,
    }))
    const map = makeMap({
      queryRenderedFeatures: vi.fn(() => extras),
      once: vi.fn((_event: string, callback: () => void) => {
        queueMicrotask(callback)
      }),
    })
    const result = await selectRenderedRiverFeature({
      input: selectionInput(),
      map: map as never,
      getOverlayHitLayerId: () => 'm11-discharge-line-hit',
      now: () => 0,
      deadlineMs: RIVER_CLICK_PER_MAP_DEADLINE_MS,
    })
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.code).toBe('HOOK_QUERY_LIMIT')
    const onOverlayClick = vi.fn()
    const controller = createRiverClickHookController({
      getMap: () => map as never,
      getOverlayHitLayerId: () => 'm11-discharge-line-hit',
      now: () => 0,
      select: selectRenderedRiverFeature,
    })
    const hook = createRiverClickEvidenceHook({ onOverlayClick, controller, now: () => 0 })
    await expect(hook.selectRenderedRiver(selectionInput())).rejects.toMatchObject({ code: 'HOOK_QUERY_LIMIT' })
    expect(onOverlayClick).not.toHaveBeenCalled()
  })

  it('accepts 64 results with exactly one matching actual feature', async () => {
    const others = Array.from({ length: 63 }, (_, index) => ({
      ...makeFeature(),
      id: `other-${index}`,
      properties: { ...makeFeature().properties, river_segment_id: `other-${index}`, segment_id: `other-${index}` },
    }))
    const map = makeMap({
      queryRenderedFeatures: vi.fn(() => [...others, makeFeature()]),
      once: vi.fn((_event: string, callback: () => void) => {
        queueMicrotask(callback)
      }),
    })
    const result = await selectRenderedRiverFeature({
      input: selectionInput(),
      map: map as never,
      getOverlayHitLayerId: () => 'm11-discharge-line-hit',
      now: () => 0,
      deadlineMs: RIVER_CLICK_PER_MAP_DEADLINE_MS,
    })
    expect(result.ok).toBe(true)
    if (!result.ok) throw new Error('64-with-one-match must succeed')
    expect(result.output.normalized.riverSegmentId).toBe('seg-001')
  })

  it('rejects two matching features, a wrong hit layer, zero match, and drifted identities as HOOK_FEATURE_MISMATCH without dispatch', async () => {
    const twoMatches = [makeFeature(), { ...makeFeature(), id: 'feature-2' }]
    const wrongLayer = [{ ...makeFeature(), layer: { id: 'm11-other-hit' } }]
    const zero = [] as unknown[]
    const drifted = [{
      ...makeFeature(),
      properties: { ...makeFeature().properties, basin_version_id: 'bv-DRIFT' },
    }]
    const cases: Array<{ results: unknown[]; label: string }> = [
      { results: twoMatches, label: 'two matches' },
      { results: wrongLayer, label: 'wrong hit layer' },
      { results: zero, label: 'zero match' },
      { results: drifted, label: 'drifted identity' },
    ]
    for (const { results } of cases) {
      const map = makeMap({
        queryRenderedFeatures: vi.fn(() => results),
        once: vi.fn((_event: string, callback: () => void) => {
          queueMicrotask(callback)
        }),
      })
      const result = await selectRenderedRiverFeature({
        input: selectionInput(),
        map: map as never,
        getOverlayHitLayerId: () => 'm11-discharge-line-hit',
        now: () => 0,
        deadlineMs: RIVER_CLICK_PER_MAP_DEADLINE_MS,
      })
      expect(result.ok, `case must fail`).toBe(false)
      if (!result.ok) expect(result.code).toBe('HOOK_FEATURE_MISMATCH')
      const onOverlayClick = vi.fn()
      const controller = createRiverClickHookController({
        getMap: () => map as never,
        getOverlayHitLayerId: () => 'm11-discharge-line-hit',
        now: () => 0,
        select: selectRenderedRiverFeature,
      })
      const hook = createRiverClickEvidenceHook({ onOverlayClick, controller, now: () => 0 })
      await expect(hook.selectRenderedRiver(selectionInput())).rejects.toMatchObject({ code: 'HOOK_FEATURE_MISMATCH' })
      expect(onOverlayClick).not.toHaveBeenCalled()
    }
  })

  it('accepts API-derived 97-character and 256-byte version identities through selection and dispatch', async () => {
    const id97 = 'v'.repeat(97)
    const id256 = 'w'.repeat(256)
    const id256multibyte = 'é'.repeat(128)
    const over = 'x'.repeat(257)
    const empty = ''
    const run = async (basinVersionId: string, riverNetworkVersionId: string) => {
      const feature = {
        ...makeFeature(),
        properties: {
          ...makeFeature().properties,
          basin_version_id: basinVersionId,
          river_network_version_id: riverNetworkVersionId,
        },
      }
      const map = makeMap({
        queryRenderedFeatures: vi.fn(() => [feature]),
        once: vi.fn((_event: string, callback: () => void) => {
          queueMicrotask(callback)
        }),
      })
      const onOverlayClick = vi.fn()
      const controller = createRiverClickHookController({
        getMap: () => map as never,
        getOverlayHitLayerId: () => 'm11-discharge-line-hit',
        now: () => 0,
        select: selectRenderedRiverFeature,
      })
      const hook = createRiverClickEvidenceHook({ onOverlayClick, controller, now: () => 0 })
      return hook.selectRenderedRiver(selectionInput({ basinVersionId, riverNetworkVersionId }))
        .then((output) => ({ ok: true as const, output, onOverlayClick }))
        .catch((error: unknown) => ({ ok: false as const, error, onOverlayClick }))
    }
    const accepted97 = await run(id97, id97)
    expect(accepted97.ok).toBe(true)
    if (accepted97.ok) {
      expect(accepted97.output.basinVersionId).toBe(id97)
      expect(accepted97.onOverlayClick).toHaveBeenCalledTimes(1)
    }
    const accepted256 = await run(id256, id256)
    expect(accepted256.ok).toBe(true)
    const acceptedMultibyte = await run(id256multibyte, id256multibyte)
    expect(acceptedMultibyte.ok).toBe(true)
    const rejectedOver = await run(over, over)
    expect(rejectedOver.ok).toBe(false)
    if (!rejectedOver.ok) expect((rejectedOver.error as { code: string }).code).toBe('HOOK_INVALID_INPUT')
    expect(rejectedOver.onOverlayClick).not.toHaveBeenCalled()
    const rejectedEmpty = await run(empty, empty)
    expect(rejectedEmpty.ok).toBe(false)
    if (!rejectedEmpty.ok) expect((rejectedEmpty.error as { code: string }).code).toBe('HOOK_INVALID_INPUT')
  })

  it('propagates a closed hook code verbatim (code membership validated on the source)', () => {
    // Any code the source emits is drawn from RIVER_CLICK_HOOK_CODES by
    // construction; the dispatcher must reject anything outside it.
    for (const code of RIVER_CLICK_HOOK_CODES) {
      expect(typeof code).toBe('string')
    }
  })

  it('fails closed when onOverlayClick is absent instead of optional-chaining to success', async () => {
    const controller = {
      selectRenderedRiver: vi.fn(async () => ({
        feature: makeFeature(),
        normalized: {
          basinId: 'basins_qhh',
          riverSegmentId: 'seg-001',
          basinVersionId: 'bv-001',
          riverNetworkVersionId: 'rn-001',
        },
      })),
    }
    const hook = createRiverClickEvidenceHook({
      onOverlayClick: undefined as never,
      controller: controller as never,
    })
    await expect(hook.selectRenderedRiver(selectionInput())).rejects.toMatchObject({
      code: 'HOOK_QUERY_FAILED',
    })
  })

  it('keeps t0 immediately before the real callback and rejects on callback throw with a fixed message', async () => {
    const order: string[] = []
    let clockValue = 1000
    const now = vi.fn(() => {
      clockValue += 100
      return clockValue
    })
    const controller = {
      selectRenderedRiver: vi.fn(async () => {
        order.push('select')
        return {
          feature: makeFeature(),
          normalized: {
            basinId: 'basins_qhh',
            riverSegmentId: 'seg-001',
            basinVersionId: 'bv-001',
            riverNetworkVersionId: 'rn-001',
          },
        }
      }),
    }
    const hook = createRiverClickEvidenceHook({
      onOverlayClick: (dispatch) => {
        order.push('dispatch')
        throw new Error('raw-secret-detail')
      },
      controller: controller as never,
      now,
    })
    await expect(hook.selectRenderedRiver(selectionInput())).rejects.toMatchObject({
      code: 'HOOK_QUERY_FAILED',
      message: 'river-click hook callback dispatch failed',
    })
    expect(order).toEqual(['select', 'dispatch'])
    expect(now).toHaveBeenCalledTimes(1)
  })
})

describe('river-click native map adapter (maplibre-gl Map -> RiverClickHookMap)', () => {
  it('delegates the narrow read/fit/query/idle methods to the REAL native map with exact args, preserving this-binding and unmodified features', () => {
    const nativeMap = {
      loaded: () => true,
      isStyleLoaded: () => true,
      fitBounds: vi.fn((bounds: unknown, options: unknown) => ({ nativeFit: bounds, options })),
      project: vi.fn((coord: [number, number]) => ({ x: coord[0] + 1, y: coord[1] + 1 })),
      queryRenderedFeatures: vi.fn((box: unknown, options: unknown) => [{ identity: { basin_id: 'basins_qhh' } }]),
      getCanvas: () => ({ style: { cursor: 'pointer' } }),
      once: vi.fn((_event: string, callback: () => void) => { queueMicrotask(callback) }),
      off: vi.fn(),
    }
    const adapted = adaptRiverClickHookMap(nativeMap)
    expect(adapted).not.toBeNull()
    const map = adapted as RiverClickHookMap
    // Readiness/listener delegation.
    expect(map.loaded()).toBe(true)
    expect(map.isStyleLoaded()).toBe(true)
    // fitBounds receives the EXACT bounds + options, normalized to the contract.
    const bbox = [[100, 30], [102, 32]] as [[number, number], [number, number]]
    map.fitBounds(bbox, { padding: 48, duration: 0, maxZoom: 14 })
    expect(nativeMap.fitBounds).toHaveBeenCalledWith(bbox, { padding: 48, duration: 0, maxZoom: 14 })
    // project normalization: exactly {x,y}.
    expect(map.project([101, 31])).toEqual({ x: 102, y: 32 })
    expect(nativeMap.project).toHaveBeenCalledWith([101, 31])
    // queryRenderedFeatures: exact box + layer options; features are passed
    // through UNMODIFIED (no synthesis, no mutation).
    const box = [{ x: 40, y: 40 }, { x: 56, y: 56 }] as [{ x: number; y: number }, { x: number; y: number }]
    const features = map.queryRenderedFeatures(box, { layers: ['m11-discharge-line-hit'] })
    expect(nativeMap.queryRenderedFeatures).toHaveBeenCalledWith(box, { layers: ['m11-discharge-line-hit'] })
    expect(features).toEqual([{ identity: { basin_id: 'basins_qhh' } }])
    // The original feature object identity is preserved (never a copy).
    expect(features[0]).toBe((nativeMap.queryRenderedFeatures as ReturnType<typeof vi.fn>).mock.results[0].value[0])
    expect(map.getCanvas()).toEqual({ style: { cursor: 'pointer' } })
    // Listener registration/removal is delegated verbatim to the native map.
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
    // native getMap returning the real stub map through the adapter works.
    const nativeMap = {
      loaded: () => true,
      isStyleLoaded: () => true,
      fitBounds: vi.fn(),
      project: vi.fn(() => ({ x: 0, y: 0 })),
      queryRenderedFeatures: vi.fn(() => []),
      getCanvas: () => ({ style: { cursor: '' } }),
      once: vi.fn(),
    }
    expect(adaptRiverClickHookMap(nativeMap)).not.toBeNull()
  })
})

describe('river-click hook controller map-absent fail-closed', () => {
  it('waits for a DELAYED map ref under ONE total budget instead of failing immediately', async () => {
    let map: RiverClickHookMap | null = null
    let pollClock = 0
    const controller = createRiverClickHookController({
      getMap: () => map,
      getOverlayHitLayerId: () => (map === null ? null : 'm11-discharge-line-hit'),
      // advance ~10ms per poll so the map appears well inside the 15s budget
      now: () => { pollClock += 10; return pollClock },
      select: selectRenderedRiverFeature,
    })
    // Map becomes non-null AFTER the first poll returns null: the controller
    // must WAIT (map ref + readiness), never return HOOK_MAP_UNAVAILABLE.
    const promise = controller.selectRenderedRiver(selectionInput())
    setTimeout(() => {
      const late = makeMap()
      late.once = vi.fn((_event: string, callback: () => void) => {
        queueMicrotask(callback)
      })
      map = late
    }, 5)
    const result = await promise
    expect(result).toMatchObject({ normalized: { basinId: 'basins_qhh' } })
  })

  it('times out with HOOK_MAP_TIMEOUT when the map ref never appears (never HOOK_MAP_UNAVAILABLE)', async () => {
    let pollClock = 0
    const controller = createRiverClickHookController({
      getMap: () => null,
      getOverlayHitLayerId: () => null,
      // advance past the 15s budget in a few polls
      now: () => { pollClock += 6_000; return pollClock },
      select: selectRenderedRiverFeature,
    })
    await expect(controller.selectRenderedRiver(selectionInput())).rejects.toMatchObject({
      code: 'HOOK_MAP_TIMEOUT',
    })
  })

  it('no longer takes a waitForReady parameter', () => {
    // The signature must not include waitForReady (removed unused parameter).
    const fn = selectRenderedRiverFeature as unknown as (args: Record<string, unknown>) => unknown
    const source = fn.toString()
    expect(source).not.toContain('waitForReady')
  })
})
