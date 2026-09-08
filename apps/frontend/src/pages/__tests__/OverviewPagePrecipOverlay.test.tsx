import { render, screen, waitFor } from '@testing-library/react'
import { RouterProvider, createMemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { M11_PRECIP_NOTICE_CYCLE_NOT_MIRRORED } from '@/components/map/m11PrecipOverlay'
import { defaultM11QueryState, serializeM11QueryState } from '@/lib/m11/queryState'
import { OverviewPage } from '@/pages/OverviewPage'
import { useOverviewDataStore } from '@/stores/overviewData'
import { installMaplibreStubMap } from '@/test/maplibreStub'
import {
  DEFAULT_CYCLE,
  PRECIP_INDEX_PATH,
  VALID_TIMES_PATH,
  apiError,
  layer,
  mockApi,
  precipIndex,
  precipLayer,
  resetOverviewDataTestState,
  success,
} from '@/test/overviewDataFixture'

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

/** URL 不带 validTime → lead 0 = 目录 `valid_times[0]`；周期 = 目录 `default_cycle`。 */
const LEAD_ZERO_VALID_TIME = '2026-05-18T00:00:00Z'
const EXPECTED_URL = `/api/v1/precip/gfs/${DEFAULT_CYCLE}/${LEAD_ZERO_VALID_TIME}.png`

function renderOverview(overrides: Partial<typeof defaultM11QueryState> = {}) {
  const search = serializeM11QueryState({ ...defaultM11QueryState, ...overrides })
  const router = createMemoryRouter([{ path: '/', element: <OverviewPage /> }], {
    initialEntries: [search ? `/?${search}` : '/'],
  })
  render(<RouterProvider router={router} />)
}

async function settled() {
  await waitFor(() => expect(useOverviewDataStore.getState().mapBootstrapLoading).toBe(false))
  await waitFor(() => expect(useOverviewDataStore.getState().enrichmentLoading).toBe(false))
  await waitFor(() => expect(screen.queryByTestId('m11-overview-loading')).toBeNull())
}

function surface() {
  return screen.getByTestId('m11-map-surface')
}

beforeEach(() => {
  resetOverviewDataTestState()
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

describe('OverviewPage precipitation overlay mount seam', () => {
  it('threads the resolved overlay url through OverviewMode into the map surface', async () => {
    // 前置条件：index 确实 available 且含 lead 0 的时次（否则本用例断的是隐藏态，什么也不鉴别）。
    expect(precipIndex.valid_times).toContain(LEAD_ZERO_VALID_TIME)
    mockApi()
    renderOverview()
    await settled()

    await waitFor(() => expect(surface().getAttribute('data-precip-url')).toBe(EXPECTED_URL))
    expect(surface().hasAttribute('data-precip-hidden-reason')).toBe(false)
    const imageSources = screen
      .queryAllByTestId('maplibre-source')
      .filter((node) => node.getAttribute('data-source-type') === 'image')
    expect(imageSources.map((node) => node.getAttribute('data-source-url'))).toEqual([EXPECTED_URL])
    expect(screen.queryByTestId('m11-precip-notice')).toBeNull()
  })

  it('hides the overlay with the unmirrored-cycle notice when the index answers PRECIP_CYCLE_NOT_MIRRORED', async () => {
    mockApi({ [PRECIP_INDEX_PATH]: () => apiError('PRECIP_CYCLE_NOT_MIRRORED') })
    renderOverview()
    await settled()

    await waitFor(() => expect(surface().getAttribute('data-precip-hidden-reason')).toBe('cycle_not_mirrored'))
    expect(surface().hasAttribute('data-precip-url')).toBe(false)
    expect(screen.getByTestId('m11-precip-notice').textContent).toBe(M11_PRECIP_NOTICE_CYCLE_NOT_MIRRORED)
    // 「该周期无降水镜像」与「窗口不完整」必须是两条不同的文案（spec scenario
    // 「Unmirrored cycle is distinguishable from an incomplete window」）。
    expect(screen.getByTestId('m11-precip-notice').textContent).not.toContain('窗口不完整')
  })

  it('marks the floating precipitation toggle 未实现 while the catalog serves no precip entry', async () => {
    // 挂载接缝的另一半：`OverviewMode` 的 `precipAvailable` 推导（目录里有没有 `precip` 条目）
    // 必须真的到达浮层开关。默认目录只有 `discharge`。
    mockApi()
    renderOverview()
    await settled()

    const toggle = screen.getByRole('button', { name: /过去 24h 累积降水/ }) as HTMLButtonElement
    expect(toggle.disabled).toBe(true)
    expect(toggle.textContent).toContain('未实现')
  })

  it('enables the floating precipitation toggle once the catalog serves a precip entry', async () => {
    mockApi({ '/api/v1/layers': () => success([layer, precipLayer]) })
    renderOverview()
    await settled()

    await waitFor(() => {
      const toggle = screen.getByRole('button', { name: /过去 24h 累积降水/ }) as HTMLButtonElement
      expect(toggle.disabled).toBe(false)
      // 按下态来自 URL 的 `precip`（默认 true），不是组件自造的常量。
      expect(toggle.getAttribute('aria-pressed')).toBe('true')
      expect(toggle.textContent).not.toContain('未实现')
    })
  })

  it('keeps the overlay pending while the active cycle valid-time list is unresolved', async () => {
    // 三元组必须与流量层**同源**：时次取的是 discharge `LayerState.currentValidTime`，
    // 不是 URL 上的 `state.validTime`。活动列表未定时前者为 null（校正 effect 此刻刻意不改写
    // URL），叠加层诚实地停在 index_pending；改读 `state.validTime` 就会为一个活动周期未必
    // 承认的时次拼出 PNG URL 并真的去取它。
    const urlValidTime = '2026-05-18T03:00:00.000Z'
    expect(precipIndex.valid_times).toContain(urlValidTime.replace('.000Z', 'Z'))
    // `source=ifs` 是非默认源 → 该周期的 valid-times 必须自己取；这条请求永不落地即 pending 窗口。
    mockApi({ [VALID_TIMES_PATH]: () => new Promise(() => undefined) })
    renderOverview({ source: 'ifs', validTime: urlValidTime })
    await settled()

    // 前置条件：index 确实 available（隐藏不是因为 index 没到），而列表确实未定。
    await waitFor(() =>
      expect(useOverviewDataStore.getState().precipIndexByCycle[`ifs|${DEFAULT_CYCLE}`]?.status).toBe('available'),
    )
    expect(useOverviewDataStore.getState().validTimesByCycle[`ifs|${DEFAULT_CYCLE}`]).toBeUndefined()

    expect(surface().hasAttribute('data-precip-url')).toBe(false)
    expect(surface().getAttribute('data-precip-hidden-reason')).toBe('index_pending')
    expect(screen.queryByTestId('m11-precip-notice')).toBeNull()
  })

  it('shows the precipitation notice ahead of the empty-basin notice once the surface has settled', async () => {
    // 决策 9 的链位判别用例：只有「流域清单为空 **且** 降水隐藏有提示」这一格能把
    // 「排在 emptyBasinReason 之前」与「排在它之后」区分开。
    mockApi({
      '/api/v1/basins': () => success([]),
      [PRECIP_INDEX_PATH]: () => apiError('PRECIP_CYCLE_NOT_MIRRORED'),
    })
    renderOverview()
    await settled()

    await waitFor(() => expect(screen.getByTestId('m11-precip-notice')).toBeInTheDocument())
    // 前置条件：空流域提示这一支确实"想说话"（basins 为空、surface 已 settle）。
    expect(useOverviewDataStore.getState().overview?.basins ?? []).toHaveLength(0)
    expect(screen.getByTestId('m11-precip-notice').textContent).toBe(M11_PRECIP_NOTICE_CYCLE_NOT_MIRRORED)
    // 互斥链：一次只出一条（所有 M11FloatingNotice 同坐标绝对定位，并列会像素重叠）。
    expect(screen.queryByTestId('m11-overview-empty')).toBeNull()
  })
})
