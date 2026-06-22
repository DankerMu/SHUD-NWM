import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { HydroMetStation } from '@/pages/hydroMet/bootstrap'
import {
  STATION_CLIENT_CAP,
  STATION_PAGE_LIMIT,
  stationLayerRequestKey,
  useStationLayerDataStore,
} from '@/stores/stationLayerData'

const fetchHydroMetStationsByIdentityMock = vi.fn()
const clientGetMock = vi.fn()

vi.mock('@/api/client', () => ({
  client: { GET: (...args: unknown[]) => clientGetMock(...args) },
}))

vi.mock('@/pages/hydroMet/bootstrap', async () => {
  const actual = await vi.importActual<typeof import('@/pages/hydroMet/bootstrap')>('@/pages/hydroMet/bootstrap')
  return {
    ...actual,
    fetchHydroMetStationsByIdentity: (...args: unknown[]) => fetchHydroMetStationsByIdentityMock(...args),
  }
})

function station(id: string): HydroMetStation {
  return {
    station_id: id,
    basin_version_id: 'bv-1',
    station_name: `Station ${id}`,
    geom: { type: 'Point', coordinates: [100, 30] },
    station_role: 'representative',
    active_flag: true,
    created_at: '2026-01-01T00:00:00Z',
  } as HydroMetStation
}

function stations(prefix: string, count: number, start = 0): HydroMetStation[] {
  return Array.from({ length: count }, (_, index) => station(`${prefix}-${start + index}`))
}

function stationPage(items: HydroMetStation[], totalCount?: number) {
  return {
    items,
    total_count: totalCount,
    limit: STATION_PAGE_LIMIT,
    offset: 0,
  }
}

describe('stationLayerData store (M26-3)', () => {
  beforeEach(() => {
    fetchHydroMetStationsByIdentityMock.mockReset()
    clientGetMock.mockReset()
    useStationLayerDataStore.getState().clear()
  })

  it('loads every visible basin version and pages by offset until total is reached', async () => {
    // Heihe-style: total 1709 → 首页 500 + 3 翻页 (500/500/209)。
    fetchHydroMetStationsByIdentityMock
      .mockResolvedValueOnce(stationPage(stations('heihe', STATION_PAGE_LIMIT, 0), 1709))
      .mockResolvedValueOnce({ items: stations('heihe', STATION_PAGE_LIMIT, 500) })
      .mockResolvedValueOnce({ items: stations('heihe', STATION_PAGE_LIMIT, 1000) })
      .mockResolvedValueOnce({ items: stations('heihe', 209, 1500) })
      .mockResolvedValueOnce(stationPage(stations('qhh', 386, 0), 386))

    const data = await useStationLayerDataStore.getState().loadStationLayer({
      basinContexts: [
        { basinId: 'heihe', basinVersionId: 'bv-heihe' },
        { basinId: 'qhh', basinVersionId: 'bv-qhh' },
      ],
      resolvedSource: 'GFS',
      cycle: null,
    })

    expect(data.total).toBe(2095)
    expect(data.loaded).toBe(2095)
    expect(data.truncated).toBe(false)
    expect(data.stationBasinIds['heihe-0']).toBe('heihe')
    expect(data.stationBasinIds['qhh-0']).toBe('qhh')
    // 身份直接来自流域版本，不依赖 latest-product ready。
    expect(fetchHydroMetStationsByIdentityMock.mock.calls[0][0]).toEqual({ basinVersionId: 'bv-heihe' })
    // offset 翻页正确：首页 0，后续 500 / 1000 / 1500。
    expect(fetchHydroMetStationsByIdentityMock.mock.calls.slice(0, 4).map((call) => (call[1] as { offset: number }).offset)).toEqual([
      0,
      500,
      1000,
      1500,
    ])
    expect(useStationLayerDataStore.getState().data?.loaded).toBe(2095)
  })

  it('caps oversized basins and flags truncation honestly', async () => {
    // 每次翻页返回满页，直到触顶 cap。
    fetchHydroMetStationsByIdentityMock.mockImplementation(async (_identity: unknown, query: { limit: number; offset: number }) =>
      stationPage(stations('cn', query.limit, query.offset), 12000),
    )

    const data = await useStationLayerDataStore.getState().loadStationLayer({
      basinContexts: [{ basinId: 'china', basinVersionId: 'bv-cn' }],
      resolvedSource: 'IFS',
      cycle: null,
    })

    expect(data.total).toBe(12000)
    expect(data.loaded).toBe(STATION_CLIENT_CAP)
    expect(data.truncated).toBe(true)
  })

  it('loads a single page basin without truncation', async () => {
    // Qhh-style: total 386 ≤ page limit → 不翻页。
    fetchHydroMetStationsByIdentityMock.mockResolvedValueOnce(stationPage(stations('qhh', 386, 0), 386))

    const data = await useStationLayerDataStore.getState().loadStationLayer({
      basinContexts: [{ basinId: 'qhh', basinVersionId: 'bv-qhh' }],
      resolvedSource: 'GFS',
      cycle: null,
    })

    expect(data.total).toBe(386)
    expect(data.loaded).toBe(386)
    expect(data.truncated).toBe(false)
    expect(fetchHydroMetStationsByIdentityMock).toHaveBeenCalledTimes(1)
  })

  it('resolves a missing basin version from the basin versions endpoint before loading stations', async () => {
    clientGetMock.mockResolvedValueOnce({
      data: {
        status: 'ok',
        data: [
          { basin_version_id: 'bv-qhh-old', active_flag: false },
          { basin_version_id: 'bv-qhh-active', active_flag: true },
        ],
      },
      error: undefined,
    })
    fetchHydroMetStationsByIdentityMock.mockResolvedValueOnce(stationPage(stations('qhh', 2, 0), 2))

    const data = await useStationLayerDataStore.getState().loadStationLayer({
      basinContexts: [{ basinId: 'qhh', basinVersionId: null }],
      resolvedSource: 'GFS',
      cycle: null,
    })

    expect(clientGetMock).toHaveBeenCalledWith(
      '/api/v1/basins/{basin_id}/versions',
      { params: { path: { basin_id: 'qhh' }, query: { limit: 20, offset: 0 } } },
    )
    expect(fetchHydroMetStationsByIdentityMock).toHaveBeenCalledWith(
      { basinVersionId: 'bv-qhh-active' },
      { limit: STATION_PAGE_LIMIT, offset: 0 },
    )
    expect(data.loaded).toBe(2)
    expect(data.stationBasinIds['qhh-0']).toBe('qhh')
  })

  it('handles a zero-station basin without false truncation', async () => {
    // 空流域：total_count=0 → 不翻页、loaded=0、truncated=false（0<0 为假）。
    fetchHydroMetStationsByIdentityMock.mockResolvedValueOnce(stationPage([], 0))

    const data = await useStationLayerDataStore.getState().loadStationLayer({
      basinContexts: [{ basinId: 'empty', basinVersionId: 'bv-empty' }],
      resolvedSource: 'GFS',
      cycle: null,
    })

    expect(data.total).toBe(0)
    expect(data.loaded).toBe(0)
    expect(data.truncated).toBe(false)
    expect(fetchHydroMetStationsByIdentityMock).toHaveBeenCalledTimes(1)
  })

  it('falls back to first-page length when total_count is missing and does not over-report truncation', async () => {
    // total_count 缺失/非有限：回退首页长度，truncated 不被误报（不静默截断也不假完整）。
    const firstPage = stations('nocount', 386, 0)
    fetchHydroMetStationsByIdentityMock.mockResolvedValueOnce(stationPage(firstPage))

    const data = await useStationLayerDataStore.getState().loadStationLayer({
      basinContexts: [{ basinId: 'nocount', basinVersionId: 'bv-nocount' }],
      resolvedSource: 'GFS',
      cycle: null,
    })

    expect(data.loaded).toBe(386)
    expect(data.truncated).toBe(false)
    expect(fetchHydroMetStationsByIdentityMock).toHaveBeenCalledTimes(1)
  })

  it('surfaces a mid-pagination error without silently flagging a complete load', async () => {
    // Heihe-style 首页 ready，但第二页抛错：整体 reject、error 暴露、data 置空，
    // 不得返回 truncated=false 的"看似完整"结果掩盖缺失。
    fetchHydroMetStationsByIdentityMock
      .mockResolvedValueOnce(stationPage(stations('heihe', STATION_PAGE_LIMIT, 0), 1709))
      .mockRejectedValueOnce(new Error('第二页加载失败'))

    await expect(
      useStationLayerDataStore.getState().loadStationLayer({
        basinContexts: [{ basinId: 'heihe', basinVersionId: 'bv-heihe' }],
        resolvedSource: 'GFS',
        cycle: null,
      }),
    ).rejects.toThrow('第二页加载失败')

    const state = useStationLayerDataStore.getState()
    expect(state.error).toBeTruthy()
    expect(state.data).toBeNull()
  })

  it('dedupes concurrent identical requests', async () => {
    fetchHydroMetStationsByIdentityMock.mockResolvedValue(stationPage(stations('qhh', 386, 0), 386))

    const request = { basinContexts: [{ basinId: 'qhh', basinVersionId: 'bv-qhh' }], resolvedSource: 'GFS' as const, cycle: null }
    const [a, b] = await Promise.all([
      useStationLayerDataStore.getState().loadStationLayer(request),
      useStationLayerDataStore.getState().loadStationLayer(request),
    ])

    expect(a).toBe(b)
    expect(fetchHydroMetStationsByIdentityMock).toHaveBeenCalledTimes(1)
  })

  it('does not overwrite a newer request with a stale earlier response', async () => {
    let resolveFirst: ((value: unknown) => void) | null = null
    fetchHydroMetStationsByIdentityMock
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveFirst = resolve
          }),
      )
      .mockResolvedValueOnce(stationPage(stations('heihe', 10, 0), 10))

    const store = useStationLayerDataStore.getState()
    const firstPromise = store.loadStationLayer({
      basinContexts: [{ basinId: 'qhh', basinVersionId: 'bv-qhh' }],
      resolvedSource: 'GFS',
      cycle: null,
    }).catch(() => undefined)
    const secondData = await store.loadStationLayer({
      basinContexts: [{ basinId: 'heihe', basinVersionId: 'bv-heihe' }],
      resolvedSource: 'GFS',
      cycle: null,
    })

    // 让过期的第一个请求晚一步 resolve。
    resolveFirst?.(stationPage(stations('qhh', 5, 0), 5))
    await firstPromise

    expect(secondData.total).toBe(10)
    expect(useStationLayerDataStore.getState().data?.total).toBe(10)
    expect(useStationLayerDataStore.getState().requestKey).toContain('heihe')
  })

  it('surfaces an error when no basin version identity is available', async () => {
    const request = { basinContexts: [], resolvedSource: 'GFS' as const, cycle: null }

    await expect(
      useStationLayerDataStore.getState().loadStationLayer(request),
    ).rejects.toThrow()
    expect(useStationLayerDataStore.getState().error).toBeTruthy()
    expect(useStationLayerDataStore.getState().data).toBeNull()
    expect(useStationLayerDataStore.getState().requestKey).toBe(stationLayerRequestKey(request))
  })
})
