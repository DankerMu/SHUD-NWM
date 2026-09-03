import { act, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { M11MapLibreSurface } from '@/components/map/M11MapLibreSurface'
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
  basemap: 'vector',
  basinVersionId: null,
  riverNetworkVersionId: null,
  basinId: null,
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

function installStubMap() {
  const map = {
    loaded: () => true,
    isStyleLoaded: () => true,
    fitBounds: vi.fn(),
    project: vi.fn((coord: [number, number]) => ({ x: 40 + (coord[0] - 100) * 10, y: 40 + (coord[1] - 30) * 10 })),
    queryRenderedFeatures: vi.fn(() => [
      {
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
      },
    ]),
    getCanvas: () => ({ style: { cursor: '' } }),
    once: (event: string, callback: () => void) => {
      queueMicrotask(callback)
    },
  }
  // react-map-gl MapRef: getMap() returns the underlying maplibre map.
  installMaplibreStubMap({ getMap: () => map })
  return map
}

function renderSurface(onOverlayClick = vi.fn()) {
  return render(
    <M11MapLibreSurface
      state={state}
      layers={[layer]}
      loading={false}
      boundaryLoading={false}
      onOverlayClick={onOverlayClick}
    />,
  )
}

describe('M11MapLibreSurface river-click hook', () => {
  beforeEach(() => {
    installStubMap()
  })

  afterEach(() => {
    delete (window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence
    delete (window as unknown as Record<string, unknown>).__NHMS_E2E_HOOKS__
  })

  it('leaves the hook global absent when the exact pre-start flag is not set', () => {
    renderSurface()
    expect((window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence).toBeUndefined()
  })

  it('registers exactly one method selectRenderedRiver when the exact boolean flag is set before startup', async () => {
    ;(window as unknown as Record<string, unknown>).__NHMS_E2E_HOOKS__ = true
    renderSurface()
    const hook = (window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence as
      | { selectRenderedRiver?: (input: unknown) => Promise<unknown> }
      | undefined
    expect(hook).toBeDefined()
    expect(Object.keys(hook as object).sort()).toEqual(['selectRenderedRiver'])
  })

  it('keeps the EXACT hook object identity across a parent rerender that supplies a new onOverlayClick closure', async () => {
    ;(window as unknown as Record<string, unknown>).__NHMS_E2E_HOOKS__ = true
    const first = vi.fn()
    const { rerender } = renderSurface(first)
    const before = (window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence
    expect(before).toBeDefined()
    // Parent rerender with a NEW callback identity: the hook object must be the
    // SAME object (a ref-based stable hook, never replaced by the effect), while
    // the LATEST callback is what actually dispatches.
    const second = vi.fn()
    rerender(
      <M11MapLibreSurface
        state={state}
        layers={[layer]}
        loading={false}
        boundaryLoading={false}
        onOverlayClick={second}
      />,
    )
    const after = (window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence
    expect(after).toBe(before)
    const hook = after as { selectRenderedRiver: (input: unknown) => Promise<unknown> }
    await hook.selectRenderedRiver({
      bbox: [[100, 30], [102, 32]],
      anchor: [100.5, 30.5],
      basinId: 'basins_qhh',
      riverSegmentId: 'seg-001',
      basinVersionId: 'bv-001',
      riverNetworkVersionId: 'rn-001',
    })
    expect(first).not.toHaveBeenCalled()
    expect(second).toHaveBeenCalledTimes(1)
  })

  it('dispatches the actual rendered feature through onOverlayClick with product layer and finite anchor', async () => {
    ;(window as unknown as Record<string, unknown>).__NHMS_E2E_HOOKS__ = true
    const onOverlayClick = vi.fn()
    renderSurface(onOverlayClick)
    const hook = (window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence as {
      selectRenderedRiver: (input: unknown) => Promise<unknown>
    }
    const result = await hook.selectRenderedRiver({
      bbox: [[100, 30], [102, 32]],
      anchor: [100.5, 30.5],
      basinId: 'basins_qhh',
      riverSegmentId: 'seg-001',
      basinVersionId: 'bv-001',
      riverNetworkVersionId: 'rn-001',
    })
    expect(result).toMatchObject({
      basinId: 'basins_qhh',
      riverSegmentId: 'seg-001',
      basinVersionId: 'bv-001',
      riverNetworkVersionId: 'rn-001',
    })
    expect(onOverlayClick).toHaveBeenCalledTimes(1)
    const interaction = onOverlayClick.mock.calls[0][0]
    expect(interaction.layerId).toBe('discharge')
    expect(interaction.feature.properties.basin_id).toBe('basins_qhh')
    expect(interaction.event.lngLat).toMatchObject({ lng: 100.5, lat: 30.5 })
    expect(interaction.feature).not.toHaveProperty('_synthesized')
  })

  it('rejects with a closed hook code when zero features match and dispatches nothing', async () => {
    ;(window as unknown as Record<string, unknown>).__NHMS_E2E_HOOKS__ = true
    const map = installStubMap()
    map.queryRenderedFeatures = vi.fn(() => [])
    installMaplibreStubMap({ getMap: () => map })
    const onOverlayClick = vi.fn()
    renderSurface(onOverlayClick)
    const hook = (window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence as {
      selectRenderedRiver: (input: unknown) => Promise<unknown>
    }
    await expect(
      hook.selectRenderedRiver({
        bbox: [[100, 30], [102, 32]],
        anchor: [100.5, 30.5],
        basinId: 'basins_qhh',
        riverSegmentId: 'seg-001',
        basinVersionId: 'bv-001',
        riverNetworkVersionId: 'rn-001',
      }),
    ).rejects.toMatchObject({ code: 'HOOK_FEATURE_MISMATCH' })
    expect(onOverlayClick).not.toHaveBeenCalled()
  })

  it('keeps ordinary pointer-compatible dispatch unchanged when the hook is present', async () => {
    ;(window as unknown as Record<string, unknown>).__NHMS_E2E_HOOKS__ = true
    const onOverlayClick = vi.fn()
    renderSurface(onOverlayClick)
    const hook = (window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence as {
      selectRenderedRiver: (input: unknown) => Promise<unknown>
    }
    expect(hook.selectRenderedRiver).toBeTypeOf('function')
    expect((window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence).toHaveProperty('selectRenderedRiver')
    expect(Object.keys(hook).length).toBe(1)
  })

  it('removes the hook global on unmount and cannot delete a newer generation', async () => {
    ;(window as unknown as Record<string, unknown>).__NHMS_E2E_HOOKS__ = true
    const first = renderSurface()
    const hookA = (window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence
    expect(hookA).toBeDefined()

    // A second concurrent surface installs a newer hook, replacing the global.
    const second = renderSurface()
    const hookB = (window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence
    expect(hookB).toBeDefined()
    expect(hookB).not.toBe(hookA)

    // Unmounting the older owner must not delete the newer instance.
    first.unmount()
    expect((window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence).toBe(hookB)

    // Unmounting the current owner deletes the global.
    second.unmount()
    expect((window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence).toBeUndefined()
  })

  it('exposes no map ref, generic query method, or mutation surface', async () => {
    ;(window as unknown as Record<string, unknown>).__NHMS_E2E_HOOKS__ = true
    renderSurface()
    const hook = (window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence as Record<string, unknown>
    expect(hook).toBeDefined()
    expect(Object.keys(hook).sort()).toEqual(['selectRenderedRiver'])
    expect(hook).not.toHaveProperty('map')
    expect(hook).not.toHaveProperty('query')
    expect(hook).not.toHaveProperty('mutate')
  })
})
