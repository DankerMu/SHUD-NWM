import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { RouterProvider, createMemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { M11MapOverlayInteraction } from '@/components/map/m11MapInteractions'
import { OverviewPage } from '@/pages/OverviewPage'
import { useOverviewDataStore } from '@/stores/overviewData'
import { installMobileFormMatchMedia } from '@/test/mobileFormMatchMedia'
import { layer, mockApi, resetOverviewDataTestState, success } from '@/test/overviewDataFixture'

/**
 * discharge 河段点击的身份门：要素缺 `basin_version_id` 或 `river_network_version_id` 时不开河段窗。
 *
 * 做法沿用 `OverviewPageRegionCrashProbes.test.tsx`：地图本体换成桩，由桩把一次命中要素的点击交给页面
 * 传下来的 `onOverlayClick`；河段面板换成桩，这里只测“面板是否在渲染”。三个点击除被测的那个属性外完全
 * 相同，完整身份的那个证明桩点击本身能开窗，两条否定用例才不是空转。
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

const FULL_IDENTITY = {
  river_segment_id: 'seg-under-test',
  segment_id: 'seg-under-test',
  basin_version_id: 'basin-version-under-test',
  river_network_version_id: 'river-network-under-test',
  basin_id: 'basin-under-test',
}

function dischargeClick(properties: Record<string, string>) {
  return {
    layerId: 'discharge',
    event: { lngLat: { lng: 100.5, lat: 36.5 } },
    feature: { properties },
  } as unknown as M11MapOverlayInteraction
}

const { basin_version_id: _omittedBasinVersion, ...withoutBasinVersion } = FULL_IDENTITY
const { river_network_version_id: _omittedRiverNetwork, ...withoutRiverNetwork } = FULL_IDENTITY

const CLICKS = {
  full: dischargeClick(FULL_IDENTITY),
  'no-basin-version': dischargeClick(withoutBasinVersion),
  'no-river-network': dischargeClick(withoutRiverNetwork),
}

vi.mock('@/components/map/M11MapLibreSurface', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/components/map/M11MapLibreSurface')>()),
  M11MapLibreSurface: ({ onOverlayClick }: { onOverlayClick?: (interaction: M11MapOverlayInteraction) => void }) => (
    <div data-testid="m11-map-surface">
      {Object.entries(CLICKS).map(([name, click]) => (
        <button key={name} type="button" data-testid={`stub-click-${name}`} onClick={() => onOverlayClick?.(click)} />
      ))}
    </div>
  ),
}))

vi.mock('@/components/map/M11RiverForecastPanel', () => ({
  M11RiverForecastPanel: () => <div data-testid="stub-river-panel" />,
}))

async function renderOverview() {
  const router = createMemoryRouter([{ path: '/', element: <OverviewPage /> }], { initialEntries: ['/'] })
  render(<RouterProvider router={router} />)
  await waitFor(() => expect(useOverviewDataStore.getState().mapBootstrapLoading).toBe(false))
  await waitFor(() => expect(useOverviewDataStore.getState().enrichmentLoading).toBe(false))
}

const riverPanel = () => screen.queryByTestId('stub-river-panel')

describe('OverviewPage discharge click identity guard', () => {
  let form: ReturnType<typeof installMobileFormMatchMedia> | undefined

  beforeEach(async () => {
    resetOverviewDataTestState()
    mockApi({ '/api/v1/layers': () => success([layer]) })
    form = installMobileFormMatchMedia(false)
    await renderOverview()
  })

  afterEach(() => {
    form?.restore()
    form = undefined
    vi.restoreAllMocks()
  })

  it('opens the river window for a discharge click with full identity', () => {
    fireEvent.click(screen.getByTestId('stub-click-full'))

    expect(riverPanel()).toBeInTheDocument()
  })

  it('opens no river window when the feature has no basin_version_id', () => {
    fireEvent.click(screen.getByTestId('stub-click-no-basin-version'))

    expect(riverPanel()).toBeNull()
  })

  it('opens no river window when the feature has no river_network_version_id', () => {
    fireEvent.click(screen.getByTestId('stub-click-no-river-network'))

    expect(riverPanel()).toBeNull()
  })
})
