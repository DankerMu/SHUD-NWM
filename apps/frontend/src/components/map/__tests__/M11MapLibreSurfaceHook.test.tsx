import { fireEvent, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { M11MapLibreSurface } from '@/components/map/M11MapLibreSurface'
import type { M11MapOverlayInteraction } from '@/components/map/m11MapInteractions'
import { installMaplibreStubMap } from '@/test/maplibreStub'
import type { LayerState } from '@/lib/m11/overviewDataContracts'
import type { M11QueryState } from '@/lib/m11/queryState'

vi.mock('react-map-gl/maplibre', async () => {
  const { MaplibreMapStub, MaplibreControlStub, MaplibreSourceStub, MaplibreLayerStub, MaplibreMarkerStub } = await import(
    '@/test/maplibreStub'
  )
  // M11MapLibreSurface imports the DEFAULT export as Map; Source/Layer/Marker
  // are used by the map primitives and need no map context in jsdom.
  return {
    default: MaplibreMapStub,
    Map: MaplibreMapStub,
    NavigationControl: MaplibreControlStub,
    ScaleControl: MaplibreControlStub,
    Source: MaplibreSourceStub,
    Layer: MaplibreLayerStub,
    Marker: MaplibreMarkerStub,
  }
})

const state: M11QueryState = {
  source: 'best',
  cycle: '2026-09-02T00:00:00Z',
  validTime: null,
  layer: 'discharge',
  metStations: false,
  precip: true,
  basemap: 'vector',
  basinVersionId: null,
  riverNetworkVersionId: null,
  segmentId: null,
  q: null,
}

const metadata = {
  layer_id: 'discharge',
  tile_format: 'mvt',
  maplibre_source_layer: 'hydro',
  min_zoom: 0,
  max_zoom: 14,
  valid_times: ['2026-09-02T00:00:00Z'],
  url_template: '/api/v1/tiles/hydro-national/q_down/{valid_time}/{z}/{x}/{y}.pbf',
  required_placeholders: ['valid_time', 'variable', 'z', 'x', 'y'],
  source_refs: { basin_version_id: 'bv-001', river_network_version_id: 'rn-001' },
} as never

const layer: LayerState = {
  layerId: 'discharge',
  displayName: 'Discharge',
  group: 'hydrology',
  available: true,
  metadata,
  validTimes: ['2026-09-02T00:00:00Z'],
  currentValidTime: '2026-09-02T00:00:00Z',
  validTimeSource: 'api',
  disabledReason: null,
  // 本 fixture 的 `url_template` 没有 `{cycle}` 占位符（点击链路测试），周期章不参与替换。
  activeNationalCycle: null,
  freshness: {
    updatedAt: null,
    cycleTime: state.cycle,
    validTime: state.cycle,
    runId: null,
    basinVersionId: 'bv-001',
    riverNetworkVersionId: 'rn-001',
    source: 'GFS+IFS',
    isStale: false,
    staleAfterHours: 6,
    unavailableReason: null,
  },
  legend: [],
}

const ORDINARY_CLICK_FEATURE = {
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

const ORDINARY_CLICK_LNGLAT = { lng: 100.5, lat: 30.5 }

function ordinaryMapClickEvent() {
  return {
    features: [ORDINARY_CLICK_FEATURE],
    lngLat: ORDINARY_CLICK_LNGLAT,
    point: { x: 40, y: 40 },
    target: { getCanvas: () => ({ style: { cursor: '' } }) },
  }
}

const LOCATE_INPUT = {
  bbox: [[100, 30], [102, 32]],
  anchor: [100.5, 30.5],
  basinId: 'basins_qhh',
  riverSegmentId: 'seg-001',
  basinVersionId: 'bv-001',
  riverNetworkVersionId: 'rn-001',
}

type Listener = (event: unknown) => void

/** One stable canvas object (identity matters for elementFromPoint and the pointer listener). */
function makeStubCanvas() {
  const listeners = new Set<Listener>()
  const canvas = {
    style: { cursor: '' },
    listeners,
    getBoundingClientRect: () => ({ left: 10, top: 20 }),
    addEventListener: vi.fn((_type: string, listener: Listener) => {
      listeners.add(listener)
    }),
    removeEventListener: vi.fn((_type: string, listener: Listener) => {
      listeners.delete(listener)
    }),
    firePointerDown(event: Record<string, unknown>) {
      for (const listener of [...listeners]) listener({ isTrusted: true, target: canvas, ...event })
    },
  }
  return canvas
}

function installStubMap(options: { pointFeatures?: (layers: string[]) => unknown[] } = {}) {
  const canvas = makeStubCanvas()
  const layers = new Set(['m11-discharge-line-hit', 'met-stations-point', 'clusters'])
  const map = {
    loaded: () => true,
    isStyleLoaded: () => true,
    fitBounds: vi.fn(),
    project: vi.fn((coord: [number, number]) => ({ x: 40 + (coord[0] - 100) * 10, y: 40 + (coord[1] - 30) * 10 })),
    queryRenderedFeatures: vi.fn((geometry: unknown, query: { layers: string[] }) => {
      if (Array.isArray(geometry)) return [ORDINARY_CLICK_FEATURE]
      return options.pointFeatures ? options.pointFeatures(query.layers) : [ORDINARY_CLICK_FEATURE]
    }),
    getLayer: vi.fn((id: string) => (layers.has(id) ? { id } : undefined)),
    getCanvas: () => canvas,
    once: (_event: string, callback: () => void) => {
      queueMicrotask(callback)
    },
  }
  // react-map-gl MapRef: getMap() returns the underlying maplibre map.
  installMaplibreStubMap({ getMap: () => map })
  // jsdom has no layout: the top element at the located point is the map canvas.
  ;(document as unknown as { elementFromPoint: (x: number, y: number) => unknown }).elementFromPoint = vi.fn(() => canvas)
  return { map, canvas }
}

type SurfaceProps = Partial<Parameters<typeof M11MapLibreSurface>[0]>

function surface(props: SurfaceProps = {}) {
  return (
    <M11MapLibreSurface
      state={state}
      layers={[layer]}
      loading={false}
      boundaryLoading={false}
      {...props}
    />
  )
}

function renderSurface(onOverlayClick?: (interaction: M11MapOverlayInteraction) => void, props: SurfaceProps = {}) {
  return render(surface({ onOverlayClick, ...props }))
}

function currentHook() {
  return (window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence as {
    locateRenderedRiver: (input: unknown) => Promise<Record<string, unknown>>
    armPointerCapture: () => void
    takePointerCapture: () => Record<string, unknown>
  }
}

const STATIONS = {
  type: 'FeatureCollection' as const,
  features: [
    {
      type: 'Feature' as const,
      geometry: { type: 'Point' as const, coordinates: [100.5, 30.5] as [number, number] },
      properties: { station_id: 'st-1', station_name: null, basin_id: 'basins_qhh' },
    },
  ],
}

describe('M11MapLibreSurface river-click hook', () => {
  const originalElementFromPoint = (document as unknown as { elementFromPoint?: unknown }).elementFromPoint

  beforeEach(() => {
    installStubMap()
  })

  afterEach(() => {
    delete (window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence
    delete (window as unknown as Record<string, unknown>).__NHMS_E2E_HOOKS__
    ;(document as unknown as { elementFromPoint?: unknown }).elementFromPoint = originalElementFromPoint
  })

  it('leaves the hook global absent and installs no canvas listener when the exact pre-start flag is not set', () => {
    const { canvas } = installStubMap()
    renderSurface()
    expect((window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence).toBeUndefined()
    expect(canvas.addEventListener).not.toHaveBeenCalled()
  })

  it('registers exactly locateRenderedRiver, armPointerCapture and takePointerCapture when the exact boolean flag is set before startup', () => {
    ;(window as unknown as Record<string, unknown>).__NHMS_E2E_HOOKS__ = true
    renderSurface()
    const hook = currentHook()
    expect(hook).toBeDefined()
    expect(Object.keys(hook).sort()).toEqual(['armPointerCapture', 'locateRenderedRiver', 'takePointerCapture'])
  })

  it('keeps the EXACT hook object identity across a parent rerender that supplies a new onOverlayClick closure', () => {
    ;(window as unknown as Record<string, unknown>).__NHMS_E2E_HOOKS__ = true
    const { rerender } = renderSurface(vi.fn())
    const before = currentHook()
    expect(before).toBeDefined()
    rerender(surface({ onOverlayClick: vi.fn() }))
    expect(currentHook()).toBe(before)
  })

  it('locates the rendered river (identities + viewport point) without calling onOverlayClick', async () => {
    ;(window as unknown as Record<string, unknown>).__NHMS_E2E_HOOKS__ = true
    const { map } = installStubMap()
    const onOverlayClick = vi.fn()
    renderSurface(onOverlayClick)
    const result = await currentHook().locateRenderedRiver(LOCATE_INPUT)
    // project(100.5, 30.5) = (45, 45) in canvas pixels; canvas rect origin (10, 20).
    expect(result).toEqual({
      basinId: 'basins_qhh',
      riverSegmentId: 'seg-001',
      basinVersionId: 'bv-001',
      riverNetworkVersionId: 'rn-001',
      clientX: 55,
      clientY: 65,
    })
    expect(onOverlayClick).not.toHaveBeenCalled()
    // The product point query uses the current interactive layer ids (read through a ref).
    expect(map.queryRenderedFeatures).toHaveBeenCalledWith({ x: 45, y: 45 }, { layers: ['m11-discharge-line-hit'] })
  })

  it('reads the render-local station flag through a ref: with stations shown a station at the point makes the located point occluded', async () => {
    ;(window as unknown as Record<string, unknown>).__NHMS_E2E_HOOKS__ = true
    const station = { layer: { id: 'met-stations-point' }, geometry: { type: 'Point', coordinates: [100.5, 30.5] }, properties: {} }
    installStubMap({ pointFeatures: () => [ORDINARY_CLICK_FEATURE, station] })
    const { rerender } = renderSurface(vi.fn())
    const hook = currentHook()
    await expect(hook.locateRenderedRiver(LOCATE_INPUT)).resolves.toMatchObject({ riverSegmentId: 'seg-001' })
    rerender(surface({ onOverlayClick: vi.fn(), metStations: true, stationFeatureCollection: STATIONS }))
    expect(currentHook()).toBe(hook)
    await expect(hook.locateRenderedRiver(LOCATE_INPUT)).rejects.toMatchObject({ code: 'HOOK_POINT_OCCLUDED' })
  })

  it('rejects with a closed hook code when zero features match and dispatches nothing', async () => {
    ;(window as unknown as Record<string, unknown>).__NHMS_E2E_HOOKS__ = true
    const { map } = installStubMap()
    map.queryRenderedFeatures = vi.fn(() => [])
    const onOverlayClick = vi.fn()
    renderSurface(onOverlayClick)
    await expect(currentHook().locateRenderedRiver(LOCATE_INPUT)).rejects.toMatchObject({ code: 'HOOK_FEATURE_MISMATCH' })
    expect(onOverlayClick).not.toHaveBeenCalled()
  })

  it('captures a trusted canvas pointer-down as t0 and rejects an absent one', async () => {
    ;(window as unknown as Record<string, unknown>).__NHMS_E2E_HOOKS__ = true
    const { canvas } = installStubMap()
    renderSurface(vi.fn())
    const hook = currentHook()
    hook.armPointerCapture()
    const located = await hook.locateRenderedRiver(LOCATE_INPUT)
    canvas.firePointerDown({ timeStamp: 321.5, clientX: located.clientX, clientY: located.clientY })
    expect(hook.takePointerCapture()).toEqual({ timeStamp: 321.5, clientX: 55, clientY: 65, isTrusted: true })
    hook.armPointerCapture()
    expect(hook.takePointerCapture()).toEqual({ error: 'HOOK_POINTER_MISSING' })
  })

  it('dispatches the same ordinary MapLibre click through onOverlayClick with the gate absent and present, without invoking the hook', () => {
    const fireOrdinaryClick = (onOverlayClick: (interaction: M11MapOverlayInteraction) => void) => {
      const view = renderSurface(onOverlayClick)
      const button = view.getByTestId('mock-maplibre-ordinary-click')
      ;(window as unknown as { __nhmsOrdinaryMapClickEvent?: unknown }).__nhmsOrdinaryMapClickEvent = ordinaryMapClickEvent()
      fireEvent.click(button)
      view.unmount()
      delete (window as unknown as { __nhmsOrdinaryMapClickEvent?: unknown }).__nhmsOrdinaryMapClickEvent
    }
    const withoutGate = vi.fn()
    fireOrdinaryClick(withoutGate)
    ;(window as unknown as Record<string, unknown>).__NHMS_E2E_HOOKS__ = true
    const withGate = vi.fn()
    fireOrdinaryClick(withGate)
    expect(withoutGate).toHaveBeenCalledTimes(1)
    expect(withGate).toHaveBeenCalledTimes(1)
    const expected = {
      layerId: 'discharge',
      feature: ORDINARY_CLICK_FEATURE,
      event: expect.objectContaining({
        lngLat: ORDINARY_CLICK_LNGLAT,
        features: [ORDINARY_CLICK_FEATURE],
      }),
    }
    expect(withoutGate.mock.calls[0][0]).toMatchObject(expected)
    expect(withGate.mock.calls[0][0]).toMatchObject(expected)
    expect(withoutGate.mock.calls[0][0].feature).toBe(ORDINARY_CLICK_FEATURE)
    expect(withGate.mock.calls[0][0].feature).toBe(ORDINARY_CLICK_FEATURE)
    expect(Number.isFinite(withoutGate.mock.calls[0][0].event.lngLat.lng)).toBe(true)
    expect(Number.isFinite(withoutGate.mock.calls[0][0].event.lngLat.lat)).toBe(true)
  })

  it('removes the hook global and its armed listener on unmount and cannot delete a newer generation', () => {
    ;(window as unknown as Record<string, unknown>).__NHMS_E2E_HOOKS__ = true
    const { canvas } = installStubMap()
    const first = renderSurface()
    const hookA = currentHook()
    expect(hookA).toBeDefined()
    hookA.armPointerCapture()
    expect(canvas.listeners.size).toBe(1)

    // A second concurrent surface installs a newer hook, replacing the global.
    const second = renderSurface()
    const hookB = currentHook()
    expect(hookB).toBeDefined()
    expect(hookB).not.toBe(hookA)
    hookB.armPointerCapture()
    expect(canvas.listeners.size).toBe(2)

    // Unmounting the older owner removes ITS listener but must not delete the newer instance.
    first.unmount()
    expect(currentHook()).toBe(hookB)
    expect(canvas.listeners.size).toBe(1)

    // Unmounting the current owner deletes the global and its listener.
    second.unmount()
    expect((window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence).toBeUndefined()
    expect(canvas.listeners.size).toBe(0)
  })

  it('exposes no map ref, generic query method, dispatch, or mutation surface', () => {
    ;(window as unknown as Record<string, unknown>).__NHMS_E2E_HOOKS__ = true
    renderSurface()
    const hook = currentHook() as unknown as Record<string, unknown>
    expect(Object.keys(hook).sort()).toEqual(['armPointerCapture', 'locateRenderedRiver', 'takePointerCapture'])
    for (const forbidden of ['map', 'query', 'mutate', 'onOverlayClick', 'dispatch']) {
      expect(hook).not.toHaveProperty(forbidden)
    }
  })
})
