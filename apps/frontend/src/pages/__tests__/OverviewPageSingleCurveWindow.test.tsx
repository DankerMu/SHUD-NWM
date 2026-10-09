import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { RouterProvider, createMemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { M11MapOverlayInteraction } from '@/components/map/m11MapInteractions'
import { OverviewPage } from '@/pages/OverviewPage'
import { useOverviewDataStore } from '@/stores/overviewData'
import { installMobileFormMatchMedia } from '@/test/mobileFormMatchMedia'
import { layer, mockApi, resetOverviewDataTestState, success } from '@/test/overviewDataFixture'

/**
 * 单窗策略（openspec mobile-responsive-display task 4.1，design.md D10）：移动形态下河段窗与
 * 气象代站窗至多开一个，两者同开时留下活动窗；桌面形态双窗并存不变。
 *
 * 做法沿用 `OverviewPageRegionCrashProbes.test.tsx`：地图本体换成桩，由桩把一次命中要素的点击交给
 * 页面传下来的 `onOverlayClick`，并把收到的选中 id 写到 DOM 属性；两个曲线面板换成桩，把收到的
 * `active` 写到 DOM 属性并暴露 `onClose` / `onActivate`。
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

const SEGMENT_ID = 'seg-under-test'
const STATION_ID = 'station-under-test'

const RIVER_CLICK = {
  layerId: 'discharge',
  event: { lngLat: { lng: 100.5, lat: 36.5 } },
  feature: {
    properties: {
      river_segment_id: SEGMENT_ID,
      segment_id: SEGMENT_ID,
      basin_version_id: 'basin-version-under-test',
      river_network_version_id: 'river-network-under-test',
      basin_id: 'basin-under-test',
    },
  },
} as unknown as M11MapOverlayInteraction

const STATION_CLICK = {
  layerId: 'met-stations',
  event: { lngLat: { lng: 100.5, lat: 36.5 } },
  feature: { properties: { station_id: STATION_ID, station_name: 'Station under test', basin_id: 'basin-under-test' } },
} as unknown as M11MapOverlayInteraction

vi.mock('@/components/map/M11MapLibreSurface', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/components/map/M11MapLibreSurface')>()),
  M11MapLibreSurface: ({
    selectedSegmentId,
    selectedStationId,
    onOverlayClick,
  }: {
    selectedSegmentId?: string | null
    selectedStationId?: string | null
    onOverlayClick?: (interaction: M11MapOverlayInteraction) => void
  }) => (
    <div
      data-testid="m11-map-surface"
      data-selected-segment-id={selectedSegmentId ?? undefined}
      data-selected-station-id={selectedStationId ?? undefined}
    >
      <button type="button" data-testid="stub-click-river" onClick={() => onOverlayClick?.(RIVER_CLICK)} />
      <button type="button" data-testid="stub-click-station" onClick={() => onOverlayClick?.(STATION_CLICK)} />
    </div>
  ),
}))

type PanelStubProps = { active?: boolean; onActivate?: () => void; onClose?: () => void }

vi.mock('@/components/map/M11RiverForecastPanel', () => ({
  M11RiverForecastPanel: ({ active, onActivate, onClose }: PanelStubProps) => (
    <div data-testid="stub-river-panel" data-active={String(active)}>
      <button type="button" data-testid="stub-river-activate" onClick={() => onActivate?.()} />
      <button type="button" data-testid="stub-river-close" onClick={() => onClose?.()} />
    </div>
  ),
}))

vi.mock('@/components/map/M11StationForcingPopup', () => ({
  M11StationForcingPopup: ({ active, onActivate, onClose }: PanelStubProps) => (
    <div data-testid="stub-station-panel" data-active={String(active)}>
      <button type="button" data-testid="stub-station-activate" onClick={() => onActivate?.()} />
      <button type="button" data-testid="stub-station-close" onClick={() => onClose?.()} />
    </div>
  ),
}))

async function renderOverview() {
  const router = createMemoryRouter([{ path: '/', element: <OverviewPage /> }], { initialEntries: ['/'] })
  const view = render(<RouterProvider router={router} />)
  await waitFor(() => expect(useOverviewDataStore.getState().mapBootstrapLoading).toBe(false))
  await waitFor(() => expect(useOverviewDataStore.getState().enrichmentLoading).toBe(false))
  return view
}

const clickRiver = () => fireEvent.click(screen.getByTestId('stub-click-river'))
const clickStation = () => fireEvent.click(screen.getByTestId('stub-click-station'))
const riverPanel = () => screen.queryByTestId('stub-river-panel')
const stationPanel = () => screen.queryByTestId('stub-station-panel')
const mapSurface = () => screen.getByTestId('m11-map-surface')

describe('OverviewPage single curve window (D10)', () => {
  let form: ReturnType<typeof installMobileFormMatchMedia> | undefined

  async function renderIn(mobile: boolean) {
    form = installMobileFormMatchMedia(mobile)
    await renderOverview()
  }

  function setMobile(next: boolean) {
    act(() => form?.setMobile(next))
  }

  beforeEach(() => {
    resetOverviewDataTestState()
    mockApi({ '/api/v1/layers': () => success([layer]) })
  })

  afterEach(() => {
    form?.restore()
    form = undefined
    vi.restoreAllMocks()
  })

  describe('mobile form', () => {
    it('(a) river then station: only the station window remains and it is active', async () => {
      await renderIn(true)
      clickRiver()
      expect(riverPanel()).toBeInTheDocument()
      clickStation()

      expect(riverPanel()).toBeNull()
      expect(stationPanel()).toHaveAttribute('data-active', 'true')
    })

    it('(b) station then river: only the river window remains and it is active', async () => {
      await renderIn(true)
      clickStation()
      expect(stationPanel()).toBeInTheDocument()
      clickRiver()

      expect(stationPanel()).toBeNull()
      expect(riverPanel()).toHaveAttribute('data-active', 'true')
    })

    it('(c) the closed kind loses its map selection and the remaining kind keeps it', async () => {
      await renderIn(true)
      clickRiver()
      expect(mapSurface()).toHaveAttribute('data-selected-segment-id', SEGMENT_ID)
      clickStation()

      expect(mapSurface()).not.toHaveAttribute('data-selected-segment-id')
      expect(mapSurface()).toHaveAttribute('data-selected-station-id', STATION_ID)

      clickRiver()

      expect(mapSurface()).not.toHaveAttribute('data-selected-station-id')
      expect(mapSurface()).toHaveAttribute('data-selected-segment-id', SEGMENT_ID)
    })

    it('(g) closing the only window leaves none open, and a river click reopens it', async () => {
      await renderIn(true)
      clickRiver()
      fireEvent.click(screen.getByTestId('stub-river-close'))

      expect(riverPanel()).toBeNull()
      expect(stationPanel()).toBeNull()
      expect(mapSurface()).not.toHaveAttribute('data-selected-segment-id')

      clickRiver()

      expect(riverPanel()).toHaveAttribute('data-active', 'true')
      expect(stationPanel()).toBeNull()
    })
  })

  describe('entering mobile form with both windows open', () => {
    it('(d) station window active: only the station window remains', async () => {
      await renderIn(false)
      clickRiver()
      clickStation()
      expect(riverPanel()).toBeInTheDocument()
      expect(stationPanel()).toHaveAttribute('data-active', 'true')

      setMobile(true)

      expect(riverPanel()).toBeNull()
      expect(stationPanel()).toHaveAttribute('data-active', 'true')
      expect(mapSurface()).not.toHaveAttribute('data-selected-segment-id')
      expect(mapSurface()).toHaveAttribute('data-selected-station-id', STATION_ID)
    })

    it('(e) river window active: only the river window remains', async () => {
      await renderIn(false)
      clickStation()
      clickRiver()
      expect(stationPanel()).toBeInTheDocument()
      expect(riverPanel()).toHaveAttribute('data-active', 'true')

      setMobile(true)

      expect(stationPanel()).toBeNull()
      expect(riverPanel()).toHaveAttribute('data-active', 'true')
      expect(mapSurface()).not.toHaveAttribute('data-selected-station-id')
      expect(mapSurface()).toHaveAttribute('data-selected-segment-id', SEGMENT_ID)
    })

    it('(f) going back to desktop form does not bring the closed window back', async () => {
      await renderIn(false)
      clickRiver()
      clickStation()
      setMobile(true)
      setMobile(false)

      expect(riverPanel()).toBeNull()
      expect(stationPanel()).toHaveAttribute('data-active', 'true')
      expect(mapSurface()).not.toHaveAttribute('data-selected-segment-id')
    })

    it('(k) the window activated last wins, not the one opened last', async () => {
      await renderIn(false)
      clickRiver()
      clickStation()
      expect(riverPanel()).toHaveAttribute('data-active', 'false')
      fireEvent.click(screen.getByTestId('stub-river-activate'))
      expect(riverPanel()).toHaveAttribute('data-active', 'true')
      expect(stationPanel()).toHaveAttribute('data-active', 'false')

      setMobile(true)

      expect(stationPanel()).toBeNull()
      expect(riverPanel()).toHaveAttribute('data-active', 'true')
    })
  })

  describe('desktop form keeps both windows', () => {
    it('(h) river then station: both windows stay open', async () => {
      await renderIn(false)
      clickRiver()
      clickStation()

      expect(riverPanel()).toHaveAttribute('data-active', 'false')
      expect(stationPanel()).toHaveAttribute('data-active', 'true')
      expect(mapSurface()).toHaveAttribute('data-selected-segment-id', SEGMENT_ID)
      expect(mapSurface()).toHaveAttribute('data-selected-station-id', STATION_ID)
    })

    it('(i) station then river: both windows stay open', async () => {
      await renderIn(false)
      clickStation()
      clickRiver()

      expect(stationPanel()).toHaveAttribute('data-active', 'false')
      expect(riverPanel()).toHaveAttribute('data-active', 'true')
      expect(mapSurface()).toHaveAttribute('data-selected-segment-id', SEGMENT_ID)
      expect(mapSurface()).toHaveAttribute('data-selected-station-id', STATION_ID)
    })

    it.each([
      ['river', 'station'],
      ['station', 'river'],
    ] as const)('(j) closing the %s window leaves the %s window open', async (closed, kept) => {
      await renderIn(false)
      clickRiver()
      clickStation()
      fireEvent.click(screen.getByTestId(`stub-${closed}-close`))

      expect(screen.queryByTestId(`stub-${closed}-panel`)).toBeNull()
      expect(screen.getByTestId(`stub-${kept}-panel`)).toHaveAttribute('data-active', 'true')
      expect(mapSurface()).not.toHaveAttribute(`data-selected-${closed === 'river' ? 'segment' : 'station'}-id`)
      expect(mapSurface()).toHaveAttribute(`data-selected-${kept === 'river' ? 'segment' : 'station'}-id`)
    })
  })
})
