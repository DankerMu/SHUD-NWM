import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { M11MapLibreSurface } from '@/components/map/M11MapLibreSurface'
import { M11_NATIONAL_RIVER_LINE_LAYER_ID, M11_PRECIP_RASTER_LAYER_ID } from '@/components/map/m11MapPrimitives'
import { resolveM11PrecipOverlay, type M11PrecipOverlayModel } from '@/components/map/m11PrecipOverlay'
import { installMaplibreStubMap } from '@/test/maplibreStub'
import type { LayerState } from '@/lib/m11/overviewDataContracts'
import { defaultM11QueryState, type M11QueryState } from '@/lib/m11/queryState'
import { m11SourceCycleKey, type PrecipIndex, type PrecipIndexState } from '@/stores/overviewData'
import { precipIndex } from '@/test/overviewDataFixture'

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

const CYCLE = '2026-05-18T00:00:00Z'
const VALID_TIME = '2026-05-18T03:00:00Z'
const EXPECTED_URL = `/api/v1/precip/gfs/${CYCLE}/${VALID_TIME}.png`

const state: M11QueryState = { ...defaultM11QueryState, source: 'gfs', cycle: CYCLE, validTime: VALID_TIME }

/** 全国河网层：`isMvtLayerMetadata` 通过 + 模板含 `/river-network-national/` ⇒ 河网 source 真的注册。 */
const nationalRiverLayer: LayerState = {
  layerId: 'river-network',
  displayName: 'River network',
  group: 'base',
  available: true,
  metadata: {
    layer_id: 'river-network',
    tile_format: 'mvt',
    maplibre_source_layer: 'river_network',
    min_zoom: 3,
    max_zoom: 10,
    url_template: '/api/v1/tiles/river-network-national/{z}/{x}/{y}.pbf',
    required_placeholders: ['z', 'x', 'y'],
    source_refs: {},
    release_blocking: false,
  } as never,
  validTimes: [],
  currentValidTime: null,
  validTimeSource: 'none',
  disabledReason: null,
  activeNationalCycle: null,
  freshness: {
    updatedAt: null,
    cycleTime: null,
    validTime: null,
    runId: null,
    source: 'GFS',
    isStale: false,
    staleAfterHours: 6,
    unavailableReason: null,
    basinVersionId: null,
    riverNetworkVersionId: null,
  },
  legend: [],
}

function indexState(overrides: Partial<PrecipIndex> = {}): PrecipIndexState {
  return { status: 'available', index: { ...(precipIndex as PrecipIndex), ...overrides } }
}

function visibleModel(): M11PrecipOverlayModel {
  return resolveM11PrecipOverlay({
    precip: true,
    concreteSource: 'gfs',
    cycle: CYCLE,
    validTime: VALID_TIME,
    precipIndexByCycle: {
      [m11SourceCycleKey('gfs', CYCLE)]: indexState({ valid_times: [VALID_TIME] }),
    },
  })
}

function hiddenModel(): M11PrecipOverlayModel {
  return resolveM11PrecipOverlay({
    precip: true,
    concreteSource: 'gfs',
    cycle: CYCLE,
    validTime: VALID_TIME,
    precipIndexByCycle: { [m11SourceCycleKey('gfs', CYCLE)]: { status: 'not_mirrored' } },
  })
}

function renderSurface(props: { precipOverlay?: M11PrecipOverlayModel | null; layers?: LayerState[] }) {
  return render(
    <M11MapLibreSurface
      state={state}
      layers={props.layers ?? []}
      precipOverlay={props.precipOverlay}
      loading={false}
      boundaryLoading={false}
    />,
  )
}

function imageSources() {
  return screen.queryAllByTestId('maplibre-source').filter((node) => node.getAttribute('data-source-type') === 'image')
}

function precipRasterLayer() {
  return screen
    .queryAllByTestId('maplibre-layer')
    .find((node) => node.getAttribute('data-layer-id') === M11_PRECIP_RASTER_LAYER_ID)
}

beforeEach(() => {
  installMaplibreStubMap({
    loaded: () => true,
    isStyleLoaded: () => true,
    fitBounds: vi.fn(),
    project: vi.fn(() => ({ x: 0, y: 0 })),
    queryRenderedFeatures: vi.fn(() => []),
    getCanvas: () => ({ style: { cursor: '' } }),
    once: (_event: string, callback: () => void) => {
      queueMicrotask(callback)
    },
  })
})

describe('M11MapLibreSurface precipitation overlay mount', () => {
  it('registers no image source at all while the overlay is hidden', () => {
    const model = hiddenModel()
    expect(model.hiddenReason).toBe('cycle_not_mirrored')
    renderSurface({ precipOverlay: model })

    // 「隐藏 ⇒ 不发 PNG 请求」的诚实 oracle：闸门就是「Source 根本没渲染」。
    expect(imageSources()).toHaveLength(0)
    expect(precipRasterLayer()).toBeUndefined()
    const surface = screen.getByTestId('m11-map-surface')
    expect(surface.hasAttribute('data-precip-url')).toBe(false)
    expect(surface.getAttribute('data-precip-hidden-reason')).toBe('cycle_not_mirrored')
  })

  it('registers exactly one image source carrying the model url while the overlay is visible', () => {
    const model = visibleModel()
    expect(model.url).toBe(EXPECTED_URL)
    renderSurface({ precipOverlay: model })

    const sources = imageSources()
    expect(sources).toHaveLength(1)
    expect(sources[0].getAttribute('data-source-url')).toBe(EXPECTED_URL)
    const surface = screen.getByTestId('m11-map-surface')
    expect(surface.getAttribute('data-precip-url')).toBe(EXPECTED_URL)
    expect(surface.hasAttribute('data-precip-hidden-reason')).toBe(false)
  })

  it('omits beforeId when no national river source is registered', () => {
    // `addLayer(layer, beforeId)` 在 beforeId 图层不存在时触发 ErrorEvent 且不添加图层，
    // 所以河网 source 缺席时必须**不传** beforeId（fixture 决策 4）。
    renderSurface({ precipOverlay: visibleModel(), layers: [] })
    const raster = precipRasterLayer()
    expect(raster).toBeDefined()
    expect(raster?.hasAttribute('data-layer-before-id')).toBe(false)
  })

  it('passes beforeId only when the national river layer is registered', () => {
    renderSurface({ precipOverlay: visibleModel(), layers: [nationalRiverLayer] })
    // 前置条件：河网 source 真的注册了（否则本用例什么也不鉴别）。
    expect(screen.getByTestId('m11-map-surface').getAttribute('data-national-river-source-type')).toBe('vector')
    expect(precipRasterLayer()?.getAttribute('data-layer-before-id')).toBe(M11_NATIONAL_RIVER_LINE_LAYER_ID)
  })

  it('renders nothing precipitation-related when the surface gets no overlay model', () => {
    renderSurface({ precipOverlay: null, layers: [nationalRiverLayer] })
    expect(imageSources()).toHaveLength(0)
    const surface = screen.getByTestId('m11-map-surface')
    expect(surface.hasAttribute('data-precip-url')).toBe(false)
    expect(surface.hasAttribute('data-precip-hidden-reason')).toBe(false)
  })
})
