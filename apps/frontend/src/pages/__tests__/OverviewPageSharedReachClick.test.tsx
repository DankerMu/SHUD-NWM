import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { RouterProvider, createMemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { OverviewPage } from '@/pages/OverviewPage'
import { useOverviewDataStore } from '@/stores/overviewData'
import { installMaplibreStubMap } from '@/test/maplibreStub'
import { installMobileFormMatchMedia } from '@/test/mobileFormMatchMedia'
import { layer, mockApi, model, resetOverviewDataTestState, success } from '@/test/overviewDataFixture'

/**
 * shared-reach-click-ownership (#3390) task 4.4, wiring: the page reads the per-network segment counts
 * from the overview snapshot (built from the `/api/v1/models` records) and hands them through the
 * full-screen map to the real `M11MapLibreSurface`; a real product click (MapLibre `onClick`, through the
 * stub's ordinary-click seam) on two coincident segments then opens the nested network's segment.
 * Only the API client, the map library and the river panel body are replaced.
 */
vi.mock('@/api/client', () => ({
  client: { GET: vi.fn() },
}))

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

vi.mock('@/components/map/M11RiverForecastPanel', () => ({
  M11RiverForecastPanel: () => <div data-testid="stub-river-panel" />,
}))

// The network with fewer segments has the SMALLER id: without the counts the id picks the other one.
const NESTED = { network: 'rn-b-nested', basinVersion: 'bv-nested', basin: 'basin-nested', segment: 'seg-nested', count: 7971 }
const LARGER = { network: 'rn-c-larger', basinVersion: 'bv-larger', basin: 'basin-larger', segment: 'seg-larger', count: 25971 }

function riverFeature(network: typeof NESTED) {
  return {
    layer: { id: 'm11-discharge-line-hit' },
    geometry: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
    properties: {
      river_segment_id: network.segment,
      segment_id: network.segment,
      basin_version_id: network.basinVersion,
      river_network_version_id: network.network,
      basin_id: network.basin,
    },
  }
}

function modelRecord(network: typeof NESTED) {
  return {
    ...model,
    model_id: `model-${network.basin}`,
    basin_id: network.basin,
    basin_version_id: network.basinVersion,
    river_network_version_id: network.network,
    segment_count: network.count,
  }
}

async function renderOverview() {
  const router = createMemoryRouter([{ path: '/', element: <OverviewPage /> }], { initialEntries: ['/'] })
  render(<RouterProvider router={router} />)
  await waitFor(() => expect(useOverviewDataStore.getState().mapBootstrapLoading).toBe(false))
  await waitFor(() => expect(useOverviewDataStore.getState().enrichmentLoading).toBe(false))
  await waitFor(() => expect(screen.getByTestId('m11-map-surface').getAttribute('data-registered-overlays')).toBe('discharge'))
}

function clickMapWith(features: unknown[]) {
  ;(window as unknown as { __nhmsOrdinaryMapClickEvent?: unknown }).__nhmsOrdinaryMapClickEvent = {
    features,
    lngLat: { lng: 100.5, lat: 30.5 },
    point: { x: 40, y: 40 },
    target: { getCanvas: () => ({ style: { cursor: '' } }) },
  }
  fireEvent.click(screen.getByTestId('mock-maplibre-ordinary-click'))
}

const selectedSegment = () => screen.getByTestId('m11-map-surface').getAttribute('data-selected-segment-id')

describe('OverviewPage: a click on a reach shared by two networks opens the nested network (#3390)', () => {
  let form: ReturnType<typeof installMobileFormMatchMedia> | undefined

  beforeEach(() => {
    resetOverviewDataTestState()
    installMaplibreStubMap({
      loaded: () => true,
      isStyleLoaded: () => true,
      fitBounds: vi.fn(),
      flyTo: vi.fn(),
      project: vi.fn(() => ({ x: 0, y: 0 })),
      queryRenderedFeatures: vi.fn(() => []),
      getCanvas: () => ({ style: { cursor: '' } }),
      once: (_event: string, callback: () => void) => {
        queueMicrotask(callback)
      },
    })
    form = installMobileFormMatchMedia(false)
  })

  afterEach(() => {
    form?.restore()
    form = undefined
    delete (window as unknown as { __nhmsOrdinaryMapClickEvent?: unknown }).__nhmsOrdinaryMapClickEvent
    vi.restoreAllMocks()
  })

  it.each([
    ['nested first', [NESTED, LARGER]],
    ['larger first', [LARGER, NESTED]],
  ])('with both model records loaded (%s) the click opens the nested segment', async (_label, order) => {
    mockApi({
      '/api/v1/layers': () => success([layer]),
      '/api/v1/models': () => success({ items: [modelRecord(LARGER), modelRecord(NESTED)], total: 2, limit: 200, offset: 0 }),
    })
    await renderOverview()
    clickMapWith(order.map(riverFeature))
    expect(screen.getByTestId('stub-river-panel')).toBeInTheDocument()
    expect(selectedSegment()).toBe(NESTED.segment)
  })

  it('without the model records the larger network id decides, as before', async () => {
    mockApi({ '/api/v1/layers': () => success([layer]), '/api/v1/models': () => success({ items: [], total: 0, limit: 200, offset: 0 }) })
    await renderOverview()
    clickMapWith([NESTED, LARGER].map(riverFeature))
    expect(selectedSegment()).toBe(LARGER.segment)
  })
})
