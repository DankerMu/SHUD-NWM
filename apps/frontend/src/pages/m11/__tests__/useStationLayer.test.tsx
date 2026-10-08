import { renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { HydroMetStation } from '@/pages/hydroMet/bootstrap'
import { useMetStationLayer } from '@/pages/m11/useStationLayer'
import {
  STATION_CLIENT_CAP,
  STATION_CONTEXT_CAP,
  type StationLayerBasinContext,
  useStationLayerDataStore,
} from '@/stores/stationLayerData'

const fetchHydroMetStationsByIdentityMock = vi.fn()
const fetchHydroMetLatestProductMock = vi.fn()

vi.mock('@/pages/hydroMet/bootstrap', async () => {
  const actual = await vi.importActual<typeof import('@/pages/hydroMet/bootstrap')>('@/pages/hydroMet/bootstrap')
  return {
    ...actual,
    fetchHydroMetLatestProduct: (...args: unknown[]) => fetchHydroMetLatestProductMock(...args),
    fetchHydroMetStationsByIdentity: (...args: unknown[]) => fetchHydroMetStationsByIdentityMock(...args),
  }
})

function station(id: string, withGeom = true): HydroMetStation {
  return {
    station_id: id,
    basin_version_id: 'bv-1',
    station_name: `Station ${id}`,
    ...(withGeom ? { geom: { type: 'Point', coordinates: [100, 30] } } : {}),
    station_role: 'representative',
    active_flag: true,
    created_at: '2026-01-01T00:00:00Z',
  } as HydroMetStation
}

function stations(prefix: string, count: number, start = 0): HydroMetStation[] {
  return Array.from({ length: count }, (_, index) => station(`${prefix}-${start + index}`))
}

type Page = { items: HydroMetStation[]; total_count: number }

/** 按 basinVersionId 决定每个流域的站点响应：Error = 该流域请求失败。 */
function respondByBasinVersion(responses: Record<string, Page | Error>) {
  fetchHydroMetStationsByIdentityMock.mockImplementation(async (identity: { basinVersionId: string }) => {
    const response = responses[identity.basinVersionId]
    if (!response) throw new Error(`Unexpected basin version ${identity.basinVersionId}`)
    if (response instanceof Error) throw response
    return response
  })
}

function contexts(...basinIds: string[]): StationLayerBasinContext[] {
  return basinIds.map((basinId) => ({ basinId, basinVersionId: `bv-${basinId}` }))
}

// basinContexts 用模块级常量，避免每次渲染换引用。
const QHH = contexts('qhh')
const CHINA = contexts('china')
const QHH_AND_UNRESOLVED: StationLayerBasinContext[] = [...QHH, { basinId: 'heihe', basinVersionId: null }]
const UNRESOLVED_ONLY: StationLayerBasinContext[] = [{ basinId: 'qhh', basinVersionId: null }]
const FIRST_SECOND = contexts('first', 'second')
const OVER_CONTEXT_CAP = contexts(...Array.from({ length: STATION_CONTEXT_CAP + 1 }, (_, index) => `b${index}`))
const OK_PLUS_ONE_FAILED = contexts('ok', 'basins_hlj')
const OK_PLUS_THREE_FAILED = contexts('ok', 'a', 'b', 'c')
const OK_PLUS_FOUR_FAILED = contexts('ok', 'a', 'b', 'c', 'd')
const EMPTY_PLUS_ONE_FAILED = contexts('empty', 'basins_hlj')
const OVER_CONTEXT_CAP_ONE_FAILED = contexts(
  'basins_hlj',
  ...Array.from({ length: STATION_CONTEXT_CAP }, (_, index) => `b${index}`),
)

function renderLayer(basinContexts: StationLayerBasinContext[], active = true) {
  return renderHook(() => useMetStationLayer({ active, basinContexts }))
}

beforeEach(() => {
  fetchHydroMetStationsByIdentityMock.mockReset()
  fetchHydroMetLatestProductMock.mockReset()
  useStationLayerDataStore.getState().clear()
})

describe('useMetStationLayer status note — existing branches', () => {
  it('is null and does not fetch while the overlay is inactive', () => {
    const { result } = renderLayer(QHH, false)

    expect(result.current.statusNote).toBeNull()
    expect(result.current.featureCollection).toBeNull()
    expect(fetchHydroMetStationsByIdentityMock).not.toHaveBeenCalled()
  })

  it('reports the honest empty state when no basin version is resolved', () => {
    const { result } = renderLayer(UNRESOLVED_ONLY)

    expect(result.current.statusNote).toBe('暂无可用流域版本以加载气象代站')
    expect(fetchHydroMetStationsByIdentityMock).not.toHaveBeenCalled()
  })

  it('reports loading while the first request is pending', async () => {
    fetchHydroMetStationsByIdentityMock.mockImplementation(() => new Promise(() => undefined))

    const { result } = renderLayer(QHH)

    await waitFor(() => expect(result.current.statusNote).toBe('气象代站加载中'))
    expect(result.current.loading).toBe(true)
  })

  it('reports the store error when the load fails', async () => {
    respondByBasinVersion({ 'bv-qhh': new Error('boom') })

    const { result } = renderLayer(QHH)

    await waitFor(() => expect(result.current.error).not.toBeNull())
    expect(result.current.statusNote).toBe('boom')
    expect(result.current.error).toBe('boom')
    expect(result.current.featureCollection).toBeNull()
  })

  it('reports cap truncation with a known total', async () => {
    fetchHydroMetStationsByIdentityMock.mockImplementation(async (_identity: unknown, query: { limit: number; offset: number }) => ({
      items: stations('cn', query.limit, query.offset),
      total_count: 12000,
    }))

    const { result } = renderLayer(CHINA)

    await waitFor(() => expect(result.current.loaded).toBe(STATION_CLIENT_CAP))
    expect(result.current.statusNote).toBe(`已加载 ${STATION_CLIENT_CAP}/12000 个代站，列表已截断`)
    expect(result.current.truncated).toBe(true)
    expect(result.current.totalKnown).toBe(true)
  })

  it('reports truncation with an unknown total when the context cap drops basins', async () => {
    fetchHydroMetStationsByIdentityMock.mockImplementation(async (identity: { basinVersionId: string }) => ({
      items: [station(`s-${identity.basinVersionId}`)],
      total_count: 1,
    }))

    const { result } = renderLayer(OVER_CONTEXT_CAP)

    await waitFor(() => expect(result.current.loaded).toBe(STATION_CONTEXT_CAP))
    expect(result.current.statusNote).toBe(`已加载 ${STATION_CONTEXT_CAP} 个代站，列表已截断（总数未完全统计）`)
    expect(result.current.totalKnown).toBe(false)
  })

  it('reports an empty layer when the basins have no station', async () => {
    respondByBasinVersion({ 'bv-qhh': { items: [], total_count: 0 } })

    const { result } = renderLayer(QHH)

    await waitFor(() => expect(result.current.statusNote).toBe('暂无可渲染气象代站'))
    expect(result.current.featureCollection?.features).toEqual([])
  })

  it('reports unusable coordinates when no loaded station can be drawn', async () => {
    respondByBasinVersion({ 'bv-qhh': { items: [station('nogeom', false)], total_count: 1 } })

    const { result } = renderLayer(QHH)

    await waitFor(() => expect(result.current.loaded).toBe(1))
    expect(result.current.statusNote).toBe('暂无可渲染气象代站：代站坐标不可用')
  })

  it('reports basins without a resolved version next to the loaded ones', async () => {
    respondByBasinVersion({ 'bv-qhh': { items: stations('qhh', 2), total_count: 2 } })

    const { result } = renderLayer(QHH_AND_UNRESOLVED)

    await waitFor(() => expect(result.current.loaded).toBe(2))
    expect(result.current.statusNote).toBe('部分流域缺少可用流域版本，代站图层仅显示已解析流域')
  })

  it('is null on a complete load', async () => {
    respondByBasinVersion({
      'bv-first': { items: stations('first', 2), total_count: 2 },
      'bv-second': { items: stations('second', 3), total_count: 3 },
    })

    const { result } = renderLayer(FIRST_SECOND)

    await waitFor(() => expect(result.current.loaded).toBe(5))
    expect(result.current.statusNote).toBeNull()
    expect(result.current.truncated).toBe(false)
    expect(result.current.total).toBe(5)
    expect(result.current.totalKnown).toBe(true)
    expect(result.current.featureCollection?.features).toHaveLength(5)
    expect(result.current.featureCollection?.features[2]?.properties.basin_id).toBe('second')
  })
})

describe('useMetStationLayer status note — failed basins (#2694)', () => {
  const ok: Page = { items: stations('ok', 71), total_count: 71 }

  it('names the single failed basin and keeps the loaded stations', async () => {
    respondByBasinVersion({ 'bv-ok': ok, 'bv-basins_hlj': new Error('statement timeout') })

    const { result } = renderLayer(OK_PLUS_ONE_FAILED)

    await waitFor(() => expect(result.current.loaded).toBe(71))
    expect(result.current.statusNote).toBe('已加载 71 个代站，1 个流域加载失败：basins_hlj')
    expect(result.current.failedBasinIds).toEqual(['basins_hlj'])
    expect(result.current.error).toBeNull()
    expect(result.current.truncated).toBe(true)
    expect(result.current.totalKnown).toBe(false)
    expect(result.current.featureCollection?.features).toHaveLength(71)
  })

  it('lists three failed basins without a suffix', async () => {
    const failure = new Error('statement timeout')
    respondByBasinVersion({ 'bv-ok': ok, 'bv-a': failure, 'bv-b': failure, 'bv-c': failure })

    const { result } = renderLayer(OK_PLUS_THREE_FAILED)

    await waitFor(() => expect(result.current.loaded).toBe(71))
    expect(result.current.statusNote).toBe('已加载 71 个代站，3 个流域加载失败：a、b、c')
    expect(result.current.failedBasinIds).toEqual(['a', 'b', 'c'])
  })

  it('lists the first three of four failed basins and marks the rest', async () => {
    const failure = new Error('statement timeout')
    respondByBasinVersion({ 'bv-ok': ok, 'bv-a': failure, 'bv-b': failure, 'bv-c': failure, 'bv-d': failure })

    const { result } = renderLayer(OK_PLUS_FOUR_FAILED)

    await waitFor(() => expect(result.current.loaded).toBe(71))
    expect(result.current.statusNote).toBe('已加载 71 个代站，4 个流域加载失败：a、b、c 等')
    expect(result.current.failedBasinIds).toEqual(['a', 'b', 'c', 'd'])
  })

  it('wins over the empty-layer note when one basin is empty and another failed', async () => {
    respondByBasinVersion({ 'bv-empty': { items: [], total_count: 0 }, 'bv-basins_hlj': new Error('statement timeout') })

    const { result } = renderLayer(EMPTY_PLUS_ONE_FAILED)

    await waitFor(() => expect(result.current.failedBasinIds).toEqual(['basins_hlj']))
    expect(result.current.statusNote).toBe('已加载 0 个代站，1 个流域加载失败：basins_hlj')
    expect(result.current.loaded).toBe(0)
  })

  it('hides a simultaneous cap-truncation note', async () => {
    fetchHydroMetStationsByIdentityMock.mockImplementation(async (identity: { basinVersionId: string }) => {
      if (identity.basinVersionId === 'bv-basins_hlj') throw new Error('statement timeout')
      return { items: [station(`s-${identity.basinVersionId}`)], total_count: 1 }
    })

    const { result } = renderLayer(OVER_CONTEXT_CAP_ONE_FAILED)

    await waitFor(() => expect(result.current.loaded).toBe(STATION_CONTEXT_CAP - 1))
    expect(result.current.statusNote).toBe(`已加载 ${STATION_CONTEXT_CAP - 1} 个代站，1 个流域加载失败：basins_hlj`)
  })

  it('exposes an empty failedBasinIds without data', () => {
    expect(renderLayer(QHH, false).result.current.failedBasinIds).toEqual([])
  })

  it('exposes an empty failedBasinIds on a complete load', async () => {
    respondByBasinVersion({ 'bv-qhh': { items: stations('qhh', 2), total_count: 2 } })

    const { result } = renderLayer(QHH)
    await waitFor(() => expect(result.current.loaded).toBe(2))
    expect(result.current.failedBasinIds).toEqual([])
    expect(result.current.statusNote).toBeNull()
  })

  it('keeps the error note when every basin failed', async () => {
    respondByBasinVersion({ 'bv-first': new Error('first failed'), 'bv-second': new Error('second failed') })

    const { result } = renderLayer(FIRST_SECOND)

    await waitFor(() => expect(result.current.error).toBe('first failed'))
    expect(result.current.statusNote).toBe('first failed')
    expect(result.current.failedBasinIds).toEqual([])
    expect(result.current.featureCollection).toBeNull()
  })
})
