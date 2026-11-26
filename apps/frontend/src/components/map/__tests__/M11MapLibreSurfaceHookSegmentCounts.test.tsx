import { render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { M11MapLibreSurface } from '@/components/map/M11MapLibreSurface'
import { resolveM11ClickTarget } from '@/components/map/m11MapInteractions'
import { installMaplibreStubMap } from '@/test/maplibreStub'
import type { LayerState } from '@/lib/m11/overviewDataContracts'
import type { M11QueryState } from '@/lib/m11/queryState'

/**
 * shared-reach-click-ownership (#3390) task 4.4: the station-locate adapter calls the product
 * click-target resolution with the same per-network segment counts, read through a ref (so counts that
 * arrive after the hook was installed are the ones passed). The station hook accepts only a station
 * target, so the overlay order cannot change its result: the assertion is on the call's argument.
 */
vi.mock('@/components/map/m11MapInteractions', async (importOriginal) => {
  const original = await importOriginal<typeof import('@/components/map/m11MapInteractions')>()
  return { ...original, resolveM11ClickTarget: vi.fn(original.resolveM11ClickTarget) }
})

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
  precip: false,
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
    queryRenderedFeatures: vi.fn((_geometry: unknown, query: { layers: string[] }) =>
      [RENDERED_STATION].filter((feature) => query.layers.includes(feature.layer.id)),
    ),
    getLayer: vi.fn((id: string) => (layers.has(id) ? { id } : undefined)),
    getCanvas: () => canvas,
    once: (_event: string, callback: () => void) => {
      queueMicrotask(callback)
    },
  }
  installMaplibreStubMap({ getMap: () => map })
  ;(document as unknown as { elementFromPoint: (x: number, y: number) => unknown }).elementFromPoint = vi.fn(() => canvas)
}

type SurfaceProps = Partial<Parameters<typeof M11MapLibreSurface>[0]>

function surface(props: SurfaceProps = {}) {
  return <M11MapLibreSurface state={state} layers={[layer]} stationFeatureCollection={STATIONS} {...props} />
}

const globals = () => window as unknown as Record<string, unknown>
const stationHook = () =>
  globals().__nhmsStationLocateEvidence as { locateRenderedStation: (input: unknown) => Promise<Record<string, unknown>> }

describe('M11MapLibreSurface station-locate adapter: segment counts (#3390)', () => {
  const originalElementFromPoint = (document as unknown as { elementFromPoint?: unknown }).elementFromPoint

  beforeEach(() => {
    vi.mocked(resolveM11ClickTarget).mockClear()
    installStubMap()
    globals().__NHMS_E2E_HOOKS__ = true
  })

  afterEach(() => {
    delete globals().__nhmsStationLocateEvidence
    delete globals().__nhmsRiverClickEvidence
    delete globals().__NHMS_E2E_HOOKS__
    ;(document as unknown as { elementFromPoint?: unknown }).elementFromPoint = originalElementFromPoint
  })

  it('passes the counts current at call time, including counts that arrived after the hook was installed', async () => {
    const view = render(surface())
    const hook = stationHook()
    await expect(hook.locateRenderedStation({ stationId: 'st-1', lngLat: STATION_LNGLAT })).resolves.toMatchObject({ stationId: 'st-1' })
    expect(vi.mocked(resolveM11ClickTarget).mock.calls.at(-1)?.[0]).toMatchObject({ segmentCounts: null })

    const counts = { 'rn-b': 7971, 'rn-c': 25971 }
    view.rerender(surface({ networkSegmentCounts: counts }))
    expect(stationHook()).toBe(hook)
    vi.mocked(resolveM11ClickTarget).mockClear()
    await expect(hook.locateRenderedStation({ stationId: 'st-1', lngLat: STATION_LNGLAT })).resolves.toMatchObject({ stationId: 'st-1' })
    expect(resolveM11ClickTarget).toHaveBeenCalledTimes(1)
    expect(vi.mocked(resolveM11ClickTarget).mock.calls[0][0].segmentCounts).toBe(counts)
  })
})
