import { StrictMode } from 'react'
import { render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { M11MapLibreSurface } from '@/components/map/M11MapLibreSurface'
import { installMaplibreStubMap } from '@/test/maplibreStub'
import type { LayerState } from '@/lib/m11/overviewDataContracts'
import type { M11QueryState } from '@/lib/m11/queryState'

vi.mock('react-map-gl/maplibre', async () => {
  const { MaplibreMapStub, MaplibreControlStub, MaplibreSourceStub, MaplibreLayerStub, MaplibreMarkerStub } = await import(
    '@/test/maplibreStub'
  )
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
  metStations: true,
  precip: true,
  basemap: 'vector',
  basinVersionId: null,
  riverNetworkVersionId: null,
  segmentId: null,
  q: null,
}

const layer: LayerState = {
  layerId: 'discharge',
  displayName: 'Discharge',
  group: 'hydrology',
  available: true,
  metadata: {
    layer_id: 'discharge',
    tile_format: 'mvt',
    maplibre_source_layer: 'hydro',
    min_zoom: 0,
    max_zoom: 14,
    valid_times: ['2026-09-02T00:00:00Z'],
    url_template: '/api/v1/tiles/hydro-national/q_down/{valid_time}/{z}/{x}/{y}.pbf',
    required_placeholders: ['valid_time', 'variable', 'z', 'x', 'y'],
    source_refs: { basin_version_id: 'bv-001', river_network_version_id: 'rn-001' },
  } as never,
  validTimes: ['2026-09-02T00:00:00Z'],
  currentValidTime: '2026-09-02T00:00:00Z',
  validTimeSource: 'api',
  disabledReason: null,
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

const STATION_LNGLAT: [number, number] = [100.5, 30.5]
const STATIONS = {
  type: 'FeatureCollection' as const,
  features: [
    {
      type: 'Feature' as const,
      geometry: { type: 'Point' as const, coordinates: STATION_LNGLAT },
      properties: { station_id: 'st-1', station_name: 'Station One', basin_id: 'basins_qhh' },
    },
  ],
}

const RENDERED_STATION = {
  id: 'st-1',
  layer: { id: 'met-stations-point' },
  geometry: { type: 'Point', coordinates: STATION_LNGLAT },
  properties: { station_id: 'st-1', station_name: 'Station One', basin_id: 'basins_qhh' },
}
const RENDERED_RIVER = {
  id: 'feature-1',
  layer: { id: 'm11-discharge-line-hit' },
  geometry: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
  properties: { basin_id: 'basins_qhh', river_segment_id: 'seg-001', basin_version_id: 'bv-001', river_network_version_id: 'rn-001' },
}

function installStubMap() {
  const canvas = {
    style: { cursor: '' },
    getBoundingClientRect: () => ({ left: 10, top: 20 }),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }
  const layers = new Set(['m11-discharge-line-hit', 'met-stations-point', 'clusters'])
  const map = {
    loaded: () => true,
    isStyleLoaded: () => true,
    fitBounds: vi.fn(),
    project: vi.fn((coord: [number, number]) => ({ x: 40 + (coord[0] - 100) * 10, y: 40 + (coord[1] - 30) * 10 })),
    // The station sits on top of a river: at the point both are rendered.
    queryRenderedFeatures: vi.fn((_geometry: unknown, query: { layers: string[] }) =>
      [RENDERED_RIVER, RENDERED_STATION].filter((feature) => query.layers.includes(feature.layer.id)),
    ),
    getLayer: vi.fn((id: string) => (layers.has(id) ? { id } : undefined)),
    getCanvas: () => canvas,
    once: (_event: string, callback: () => void) => {
      queueMicrotask(callback)
    },
    // Anything the hook must never reach for.
    flyTo: vi.fn(),
    fire: vi.fn(),
    getSource: vi.fn(),
  }
  installMaplibreStubMap({ getMap: () => map })
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
      stationFeatureCollection={STATIONS}
      {...props}
    />
  )
}

const globals = () => window as unknown as Record<string, unknown>
const stationHook = () =>
  globals().__nhmsStationLocateEvidence as { locateRenderedStation: (input: unknown) => Promise<Record<string, unknown>> }
const openGate = () => {
  globals().__NHMS_E2E_HOOKS__ = true
}

describe('M11MapLibreSurface station-locate hook', () => {
  const originalElementFromPoint = (document as unknown as { elementFromPoint?: unknown }).elementFromPoint

  afterEach(() => {
    delete globals().__nhmsStationLocateEvidence
    delete globals().__nhmsRiverClickEvidence
    delete globals().__NHMS_E2E_HOOKS__
    ;(document as unknown as { elementFromPoint?: unknown }).elementFromPoint = originalElementFromPoint
    vi.unstubAllGlobals()
  })

  it('exposes neither global without the exact pre-start boolean', () => {
    installStubMap()
    for (const flag of [undefined, 'true', 1, {}, false]) {
      if (flag === undefined) delete globals().__NHMS_E2E_HOOKS__
      else globals().__NHMS_E2E_HOOKS__ = flag
      const view = render(surface())
      expect(globals().__nhmsStationLocateEvidence, `flag ${String(flag)}`).toBeUndefined()
      expect(globals().__nhmsRiverClickEvidence, `flag ${String(flag)}`).toBeUndefined()
      view.unmount()
    }
  })

  it('with the gate, exposes exactly locateRenderedStation and leaves the river hook at exactly its three methods', () => {
    installStubMap()
    openGate()
    render(surface())
    const hook = stationHook() as unknown as Record<string, unknown>
    expect(Object.keys(hook)).toEqual(['locateRenderedStation'])
    expect(Object.getOwnPropertyNames(hook)).toEqual(['locateRenderedStation'])
    expect(typeof hook.locateRenderedStation).toBe('function')
    for (const forbidden of ['map', 'getMap', 'query', 'queryRenderedFeatures', 'mutate', 'dispatch', 'onOverlayClick']) {
      expect(hook).not.toHaveProperty(forbidden)
    }
    expect(Object.keys(globals().__nhmsRiverClickEvidence as object).sort()).toEqual([
      'armPointerCapture',
      'locateRenderedRiver',
      'takePointerCapture',
    ])
    expect(globals().__nhmsRiverClickEvidence).not.toBe(hook)
  })

  it('locates the rendered station through the product click-target resolution and returns only id + viewport point', async () => {
    const { map } = installStubMap()
    openGate()
    render(surface())
    const result = await stationHook().locateRenderedStation({ stationId: 'st-1', lngLat: STATION_LNGLAT })
    // project(100.5, 30.5) = (45, 45) in canvas pixels; canvas rect origin (10, 20).
    expect(result).toEqual({ stationId: 'st-1', clientX: 55, clientY: 65 })
    // The point query runs in the product's current interactive layers (stations first), read through refs.
    expect(map.queryRenderedFeatures).toHaveBeenCalledWith([45, 45], {
      layers: ['met-stations-point', 'clusters', 'm11-discharge-line-hit'],
    })
  })

  it('calling the hook triggers no product callback, no request, no pointer listener and no other map control', async () => {
    const { map, canvas } = installStubMap()
    openGate()
    const fetchSpy = vi.fn()
    vi.stubGlobal('fetch', fetchSpy)
    const onOverlayClick = vi.fn()
    const onOverlayHover = vi.fn()
    const view = render(surface({ onOverlayClick, onOverlayHover }))
    const before = view.getByTestId('m11-map-surface').outerHTML
    await stationHook().locateRenderedStation({ stationId: 'st-1', lngLat: STATION_LNGLAT })
    await expect(stationHook().locateRenderedStation({ stationId: 'st-missing', lngLat: STATION_LNGLAT })).rejects.toMatchObject({
      code: 'STATION_HOOK_NOT_RENDERED',
    })
    expect(onOverlayClick).not.toHaveBeenCalled()
    expect(onOverlayHover).not.toHaveBeenCalled()
    expect(fetchSpy).not.toHaveBeenCalled()
    expect(canvas.addEventListener).not.toHaveBeenCalled()
    expect(map.flyTo).not.toHaveBeenCalled()
    expect(map.fire).not.toHaveBeenCalled()
    expect(map.getSource).not.toHaveBeenCalled()
    // No selection / popup state moved: the surface renders exactly what it rendered before.
    expect(view.getByTestId('m11-map-surface').outerHTML).toBe(before)
    expect(view.getByTestId('m11-map-surface').getAttribute('data-selected-station-id') ?? '').toBe('')
  })

  it('reads the station switch through a ref: off -> STATION_HOOK_LAYER_OFF, on again -> located, same hook object', async () => {
    installStubMap()
    openGate()
    const off = { ...state, metStations: false }
    const view = render(surface({ state: off }))
    const hook = stationHook()
    await expect(hook.locateRenderedStation({ stationId: 'st-1', lngLat: STATION_LNGLAT })).rejects.toEqual({
      code: 'STATION_HOOK_LAYER_OFF',
      message: expect.any(String),
    })
    // On but with no features the product does not register the layer either.
    view.rerender(surface({ stationFeatureCollection: { type: 'FeatureCollection', features: [] } }))
    await expect(hook.locateRenderedStation({ stationId: 'st-1', lngLat: STATION_LNGLAT })).rejects.toMatchObject({
      code: 'STATION_HOOK_LAYER_OFF',
    })
    view.rerender(surface())
    expect(stationHook()).toBe(hook)
    await expect(hook.locateRenderedStation({ stationId: 'st-1', lngLat: STATION_LNGLAT })).resolves.toMatchObject({ stationId: 'st-1' })
  })

  it('rejects illegal coordinates with STATION_HOOK_INVALID_INPUT and an occluded point with STATION_HOOK_POINT_OCCLUDED, with no point', async () => {
    const { map } = installStubMap()
    openGate()
    render(surface())
    const invalid = await stationHook()
      .locateRenderedStation({ stationId: 'st-1', lngLat: [Number.NaN, 30.5] })
      .catch((error: unknown) => error)
    expect(invalid).toEqual({ code: 'STATION_HOOK_INVALID_INPUT', message: expect.any(String) })
    expect(map.fitBounds).not.toHaveBeenCalled()
    ;(document as unknown as { elementFromPoint: unknown }).elementFromPoint = vi.fn(() => document.body)
    const occluded = await stationHook()
      .locateRenderedStation({ stationId: 'st-1', lngLat: STATION_LNGLAT })
      .catch((error: unknown) => error)
    expect(occluded).toEqual({ code: 'STATION_HOOK_POINT_OCCLUDED', message: expect.any(String) })
  })

  it('removes the global on unmount and installs a working one on remount', async () => {
    installStubMap()
    openGate()
    const first = render(surface())
    const hookA = stationHook()
    expect(hookA).toBeDefined()
    first.unmount()
    expect(globals().__nhmsStationLocateEvidence).toBeUndefined()
    expect(globals().__nhmsRiverClickEvidence).toBeUndefined()

    render(surface())
    const hookB = stationHook()
    expect(hookB).toBeDefined()
    expect(hookB).not.toBe(hookA)
    await expect(hookB.locateRenderedStation({ stationId: 'st-1', lngLat: STATION_LNGLAT })).resolves.toMatchObject({ stationId: 'st-1' })
  })

  it('a stale cleanup cannot delete a newer instance', () => {
    installStubMap()
    openGate()
    const first = render(surface())
    const hookA = stationHook()
    const second = render(surface())
    const hookB = stationHook()
    expect(hookB).not.toBe(hookA)
    first.unmount()
    expect(stationHook()).toBe(hookB)
    expect(globals().__nhmsRiverClickEvidence).toBeDefined()
    second.unmount()
    expect(globals().__nhmsStationLocateEvidence).toBeUndefined()
  })

  it('survives the StrictMode double mount: one live hook afterwards, gone after unmount', async () => {
    installStubMap()
    openGate()
    const view = render(<StrictMode>{surface()}</StrictMode>)
    const hook = stationHook()
    expect(Object.keys(hook)).toEqual(['locateRenderedStation'])
    await expect(hook.locateRenderedStation({ stationId: 'st-1', lngLat: STATION_LNGLAT })).resolves.toMatchObject({ stationId: 'st-1' })
    view.rerender(<StrictMode>{surface({ onOverlayClick: vi.fn() })}</StrictMode>)
    expect(stationHook()).toBe(hook)
    view.unmount()
    expect(globals().__nhmsStationLocateEvidence).toBeUndefined()
  })
})
