import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { RouterProvider, createMemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { M11SheetAutoPan } from '@/components/map/M11MapLibreSurface'
import type { M11MapOverlayInteraction } from '@/components/map/m11MapInteractions'
import { OverviewPage } from '@/pages/OverviewPage'
import { useOverviewDataStore } from '@/stores/overviewData'
import { installMobileFormMatchMedia } from '@/test/mobileFormMatchMedia'
import { layer, mockApi, resetOverviewDataTestState, success } from '@/test/overviewDataFixture'

/**
 * 抽屉自动平移的页面侧（openspec mobile-responsive-display task 4.10，design.md D17）：页面由收敛后的
 * 曲线窗状态与自己那一份形态值算出“选中锚点”与“触发键”，交给地图组件。用例 (p1)–(p10) 对应 tasks.md 里
 * #2811 的 Triage。
 *
 * 做法沿用 `OverviewPageSingleCurveWindow.test.tsx`：地图本体换成桩，桩把收到的两个 prop 写到 DOM 属性，
 * 并把**每一次渲染**收到的值记进 `received`——“替换只产生一次键变化”“首次崩溃的键序列”要看的是序列，
 * 包括不会被画出来的中间提交。两个曲线面板换成带关闭 / 激活按钮的桩。
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

const RIVER_LNGLAT: [number, number] = [100.5, 36.5]
/** 同一条河段上的另一个点击点。 */
const RIVER_ELSEWHERE_LNGLAT: [number, number] = [100.625, 36.375]
const OTHER_RIVER_LNGLAT: [number, number] = [100.75, 36.25]
const STATION_POINT: [number, number] = [101.25, 37.125]
const STATION_EVENT_LNGLAT: [number, number] = [101.5, 37.5]

function riverClick(segmentId: string, [lng, lat]: [number, number]) {
  return {
    layerId: 'discharge',
    event: { lngLat: { lng, lat } },
    feature: {
      // 河段是线要素：锚点取点击点，不取几何。
      geometry: { type: 'LineString', coordinates: [[90, 30], [91, 31]] },
      properties: {
        river_segment_id: segmentId,
        segment_id: segmentId,
        basin_version_id: 'basin-version-under-test',
        river_network_version_id: 'river-network-under-test',
        basin_id: 'basin-under-test',
      },
    },
  } as unknown as M11MapOverlayInteraction
}

function stationClick(options: { geometry: boolean; eventLngLat: boolean }) {
  return {
    layerId: 'met-stations',
    event: options.eventLngLat ? { lngLat: { lng: STATION_EVENT_LNGLAT[0], lat: STATION_EVENT_LNGLAT[1] } } : {},
    feature: {
      ...(options.geometry ? { geometry: { type: 'Point', coordinates: STATION_POINT } } : {}),
      properties: { station_id: 'station-under-test', station_name: 'Station under test', basin_id: 'basin-under-test' },
    },
  } as unknown as M11MapOverlayInteraction
}

const CLICKS = {
  river: riverClick('seg-under-test', RIVER_LNGLAT),
  'river-elsewhere': riverClick('seg-under-test', RIVER_ELSEWHERE_LNGLAT),
  'other-river': riverClick('seg-other', OTHER_RIVER_LNGLAT),
  // 有 Point 几何：锚点取几何，不取点击点。
  station: stationClick({ geometry: true, eventLngLat: true }),
  'station-without-geometry': stationClick({ geometry: false, eventLngLat: true }),
  'station-without-anchor': stationClick({ geometry: false, eventLngLat: false }),
} as const
type ClickName = keyof typeof CLICKS

interface Received {
  key: string
  side: string | null
  anchor: string | null
}
/** 地图组件桩每一次渲染收到的值，按渲染顺序。 */
const received: Received[] = []

vi.mock('@/components/map/M11MapLibreSurface', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/components/map/M11MapLibreSurface')>()),
  M11MapLibreSurface: ({
    selectedAnchor,
    autoPan,
    onOverlayClick,
  }: {
    selectedAnchor?: [number, number] | null
    autoPan?: M11SheetAutoPan | null
    onOverlayClick?: (interaction: M11MapOverlayInteraction) => void
  }) => {
    const entry: Received = { key: autoPan?.key ?? '', side: autoPan?.side ?? null, anchor: selectedAnchor ? selectedAnchor.join(',') : null }
    received.push(entry)
    return (
      <div
        data-testid="m11-map-surface"
        data-auto-pan-key={entry.key}
        data-auto-pan-side={entry.side ?? undefined}
        data-auto-pan-kind={autoPan?.kind ?? undefined}
        data-selected-anchor={entry.anchor ?? undefined}
      >
        {(Object.keys(CLICKS) as ClickName[]).map((name) => (
          <button key={name} type="button" data-testid={`stub-click-${name}`} onClick={() => onOverlayClick?.(CLICKS[name])} />
        ))}
      </div>
    )
  },
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

type SwitchWindow = { __NHMS_E2E_HOOKS__?: unknown; __NHMS_E2E_CRASH_REGION__?: unknown }
const switchWindow = window as unknown as SwitchWindow

async function renderOverview() {
  const router = createMemoryRouter([{ path: '/', element: <OverviewPage /> }], { initialEntries: ['/'] })
  const view = render(<RouterProvider router={router} />)
  await waitFor(() => expect(useOverviewDataStore.getState().mapBootstrapLoading).toBe(false))
  await waitFor(() => expect(useOverviewDataStore.getState().enrichmentLoading).toBe(false))
  return view
}

const click = (name: ClickName) => fireEvent.click(screen.getByTestId(`stub-click-${name}`))
const mapSurface = () => screen.getByTestId('m11-map-surface')
const currentKey = () => mapSurface().getAttribute('data-auto-pan-key')
const currentSide = () => mapSurface().getAttribute('data-auto-pan-side')
const currentAnchor = () => mapSurface().getAttribute('data-selected-anchor')

/** 触发键的变化序列：把相邻的重复渲染折成一项。首项是开窗前的空键。 */
function keySequence(): string[] {
  return received.map((entry) => entry.key).filter((key, index, keys) => index === 0 || key !== keys[index - 1])
}

/** 序列里第一次出现某个键的那次渲染所带的锚点。 */
function anchorOfKey(key: string) {
  return received.find((entry) => entry.key === key)?.anchor
}

describe('OverviewPage: the sheet auto-pan trigger key and the selected anchor (D17)', () => {
  let form: ReturnType<typeof installMobileFormMatchMedia> | undefined

  async function renderIn(mobile: boolean) {
    form = installMobileFormMatchMedia(mobile)
    await renderOverview()
    // 数据加载期间的渲染不在观察范围内；此刻没有窗，键为空。
    expect(new Set(received.map((entry) => entry.key))).toEqual(new Set(['']))
    received.length = 0
    received.push({ key: '', side: null, anchor: null })
  }

  beforeEach(() => {
    received.length = 0
    resetOverviewDataTestState()
    mockApi({ '/api/v1/layers': () => success([layer]) })
  })

  afterEach(() => {
    form?.restore()
    form = undefined
    delete switchWindow.__NHMS_E2E_HOOKS__
    delete switchWindow.__NHMS_E2E_CRASH_REGION__
    vi.restoreAllMocks()
  })

  describe('mobile form', () => {
    it('(p1) tapping a river gives the map a non-empty key, the bottom side and the click point as the anchor', async () => {
      await renderIn(true)
      expect(currentKey()).toBe('')
      expect(currentAnchor()).toBeNull()

      click('river')

      expect(currentKey()).not.toBe('')
      expect(currentSide()).toBe('bottom')
      expect(mapSurface()).toHaveAttribute('data-auto-pan-kind', 'river')
      expect(currentAnchor()).toBe(RIVER_LNGLAT.join(','))
      expect(keySequence()).toHaveLength(2)
    })

    it('(p2) replacing the river window by a station changes the key exactly once, and no key is ever built from the replaced window\'s anchor', async () => {
      await renderIn(true)
      click('river')
      const riverKey = currentKey()!
      const rendersBefore = received.length

      click('station')

      expect(screen.queryByTestId('stub-river-panel')).toBeNull()
      expect(screen.getByTestId('stub-station-panel')).toHaveAttribute('data-active', 'true')
      const stationKey = currentKey()!
      expect(stationKey).not.toBe('')
      expect(stationKey).not.toBe(riverKey)
      expect(keySequence()).toEqual(['', riverKey, stationKey])
      // 替换之后的每一次渲染（含两窗同时非空的那个未绘制提交）都已是站点的键与站点的锚点。
      const afterReplacement = received.slice(rendersBefore)
      expect(afterReplacement.length).toBeGreaterThanOrEqual(2)
      for (const entry of afterReplacement) {
        expect(entry).toEqual({ key: stationKey, side: 'bottom', anchor: STATION_POINT.join(',') })
      }
      expect(mapSurface()).toHaveAttribute('data-auto-pan-kind', 'station')
    })

    it('(p2) replacing the station window by a river: one key change, to the river anchor', async () => {
      await renderIn(true)
      click('station')
      const stationKey = currentKey()!
      click('river')

      expect(screen.queryByTestId('stub-station-panel')).toBeNull()
      expect(keySequence()).toEqual(['', stationKey, currentKey()])
      expect(currentKey()).not.toBe(stationKey)
      expect(anchorOfKey(currentKey()!)).toBe(RIVER_LNGLAT.join(','))
      expect(new Set(received.map((entry) => entry.anchor))).toEqual(new Set([null, STATION_POINT.join(','), RIVER_LNGLAT.join(',')]))
    })

    it('(p3) closing the sheet empties the key and the anchor', async () => {
      await renderIn(true)
      click('river')
      const riverKey = currentKey()!
      fireEvent.click(screen.getByTestId('stub-river-close'))

      expect(currentKey()).toBe('')
      expect(currentSide()).toBeNull()
      expect(currentAnchor()).toBeNull()
      expect(keySequence()).toEqual(['', riverKey, ''])
    })

    it('(p4) leaving mobile form with the sheet open empties the key and keeps the anchor', async () => {
      await renderIn(true)
      click('river')
      const riverKey = currentKey()!

      act(() => form?.setMobile(false))

      expect(screen.getByTestId('stub-river-panel')).toBeInTheDocument()
      expect(currentKey()).toBe('')
      expect(currentAnchor()).toBe(RIVER_LNGLAT.join(','))
      expect(keySequence()).toEqual(['', riverKey, ''])
    })

    it('(p5) portrait -> short landscape with the sheet open changes the key exactly once and the covered side becomes the right', async () => {
      await renderIn(true)
      click('river')
      const portraitKey = currentKey()!
      expect(currentSide()).toBe('bottom')

      act(() => form?.setForm({ mobile: true, landscape: true }))

      const landscapeKey = currentKey()!
      expect(landscapeKey).not.toBe('')
      expect(landscapeKey).not.toBe(portraitKey)
      expect(currentSide()).toBe('right')
      expect(currentAnchor()).toBe(RIVER_LNGLAT.join(','))
      expect(keySequence()).toEqual(['', portraitKey, landscapeKey])

      // 转回竖屏：再变一次，回到竖屏的键。
      act(() => form?.setForm({ mobile: true, landscape: false }))
      expect(currentSide()).toBe('bottom')
      expect(keySequence()).toEqual(['', portraitKey, landscapeKey, portraitKey])
    })

    it('(p10) selecting another river segment while a river sheet is open changes the key exactly once, to the new anchor', async () => {
      await renderIn(true)
      click('river')
      const firstKey = currentKey()!
      click('other-river')

      const secondKey = currentKey()!
      expect(secondKey).not.toBe('')
      expect(secondKey).not.toBe(firstKey)
      expect(keySequence()).toEqual(['', firstKey, secondKey])
      expect(currentAnchor()).toBe(OTHER_RIVER_LNGLAT.join(','))
      expect(anchorOfKey(secondKey)).toBe(OTHER_RIVER_LNGLAT.join(','))
    })

    it('(p10) tapping the same segment at another point changes the key once (the anchor is the click point)', async () => {
      await renderIn(true)
      click('river')
      const firstKey = currentKey()!
      // 同一个要素、同一个点：键不变。
      click('river')
      expect(keySequence()).toEqual(['', firstKey])

      click('river-elsewhere')
      expect(currentAnchor()).toBe(RIVER_ELSEWHERE_LNGLAT.join(','))
      expect(keySequence()).toEqual(['', firstKey, currentKey()])
      expect(currentKey()).not.toBe(firstKey)
    })

    describe('(p8) where the station anchor comes from', () => {
      it('a Point geometry wins over the click point', async () => {
        await renderIn(true)
        click('station')
        expect(screen.getByTestId('stub-station-panel')).toBeInTheDocument()
        expect(currentAnchor()).toBe(STATION_POINT.join(','))
        expect(currentKey()).not.toBe('')
      })

      it('without a geometry it falls back to the click point', async () => {
        await renderIn(true)
        click('station-without-geometry')
        expect(screen.getByTestId('stub-station-panel')).toBeInTheDocument()
        expect(currentAnchor()).toBe(STATION_EVENT_LNGLAT.join(','))
        expect(currentKey()).not.toBe('')
      })

      it('with neither the window still opens, with no anchor and an empty key', async () => {
        await renderIn(true)
        click('station-without-anchor')
        expect(screen.getByTestId('stub-station-panel')).toHaveAttribute('data-active', 'true')
        expect(currentAnchor()).toBeNull()
        expect(currentKey()).toBe('')
        expect(keySequence()).toEqual([''])
      })
    })

    it('(p9) curve region fallback: the key sequence is empty -> K -> empty on a first-render crash, and K again after a successful retry', async () => {
      // 边界会把捕获的错误打进日志；这里是预期内的抛错。
      vi.spyOn(console, 'error').mockImplementation(() => {})
      await renderIn(true)
      switchWindow.__NHMS_E2E_HOOKS__ = true
      switchWindow.__NHMS_E2E_CRASH_REGION__ = 'curve'

      click('river')

      expect(await screen.findByTestId('region-error-map-panels')).toBeInTheDocument()
      expect(screen.queryByTestId('stub-river-panel')).toBeNull()
      expect(currentKey()).toBe('')
      // 兜底时仍有选中要素：锚点还在，只是不平移。
      expect(currentAnchor()).toBe(RIVER_LNGLAT.join(','))
      const sequence = keySequence()
      expect(sequence).toHaveLength(3)
      const [, crashedKey] = sequence
      expect(crashedKey).not.toBe('')
      expect(sequence).toEqual(['', crashedKey, ''])

      // 开关仍在时「重试」：再次兜底。提交过的键里没有新的非空键。
      fireEvent.click(screen.getByRole('button', { name: '重试' }))
      expect(screen.getByTestId('region-error-map-panels')).toBeInTheDocument()
      expect(keySequence()).toEqual(['', crashedKey, ''])

      delete switchWindow.__NHMS_E2E_CRASH_REGION__
      fireEvent.click(screen.getByRole('button', { name: '重试' }))

      expect(screen.queryByTestId('region-error-map-panels')).toBeNull()
      expect(screen.getByTestId('stub-river-panel')).toBeInTheDocument()
      expect(keySequence()).toEqual(['', crashedKey, '', crashedKey])
      expect(currentAnchor()).toBe(RIVER_LNGLAT.join(','))
    })
  })

  describe('desktop form', () => {
    it('(p6) the key stays empty; with both windows open the anchor is the active window\'s and follows activation', async () => {
      await renderIn(false)
      click('river')
      expect(currentAnchor()).toBe(RIVER_LNGLAT.join(','))

      click('station')
      expect(screen.getByTestId('stub-river-panel')).toHaveAttribute('data-active', 'false')
      expect(screen.getByTestId('stub-station-panel')).toHaveAttribute('data-active', 'true')
      expect(currentAnchor()).toBe(STATION_POINT.join(','))

      fireEvent.click(screen.getByTestId('stub-river-activate'))
      expect(screen.getByTestId('stub-river-panel')).toHaveAttribute('data-active', 'true')
      expect(currentAnchor()).toBe(RIVER_LNGLAT.join(','))

      fireEvent.click(screen.getByTestId('stub-station-activate'))
      expect(currentAnchor()).toBe(STATION_POINT.join(','))

      // 关掉活动窗：锚点落回仍开着的那个窗。
      fireEvent.click(screen.getByTestId('stub-station-close'))
      expect(currentAnchor()).toBe(RIVER_LNGLAT.join(','))
      fireEvent.click(screen.getByTestId('stub-river-close'))
      expect(currentAnchor()).toBeNull()

      expect(keySequence()).toEqual([''])
      expect(new Set(received.map((entry) => entry.side))).toEqual(new Set([null]))
    })

    it('(p7) entering mobile form with a window open turns the key from empty to non-empty once', async () => {
      await renderIn(false)
      click('river')
      expect(currentKey()).toBe('')

      act(() => form?.setMobile(true))

      expect(currentKey()).not.toBe('')
      expect(currentSide()).toBe('bottom')
      expect(currentAnchor()).toBe(RIVER_LNGLAT.join(','))
      expect(keySequence()).toEqual(['', currentKey()])
    })

    it.each([
      ['station', ['river', 'station'], STATION_POINT, 'river'],
      ['river', ['station', 'river'], RIVER_LNGLAT, 'station'],
    ] as const)('(p7) entering mobile form with both windows open (%s active): the only key is the surviving window\'s', async (active, order, anchor, closed) => {
      await renderIn(false)
      for (const name of order) click(name)
      expect(screen.getByTestId(`stub-${active}-panel`)).toHaveAttribute('data-active', 'true')
      const rendersBefore = received.length

      act(() => form?.setMobile(true))

      expect(screen.queryByTestId(`stub-${closed}-panel`)).toBeNull()
      expect(mapSurface()).toHaveAttribute('data-auto-pan-kind', active)
      expect(keySequence()).toEqual(['', currentKey()])
      expect(currentKey()).not.toBe('')
      for (const entry of received.slice(rendersBefore)) expect(entry.anchor).toBe(anchor.join(','))
    })
  })
})
