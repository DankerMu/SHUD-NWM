import { useEffect, useMemo } from 'react'

import type { M11StationFeatureCollection } from '@/components/map/M11MapLibreSurface'
import { getHydroMetStationCoordinates } from '@/lib/hydroMet/runtime'
import type { HydroMetStation } from '@/pages/hydroMet/bootstrap'
import {
  stationLayerRequestKey,
  type StationLayerBasinContext,
  useStationLayerDataStore,
} from '@/stores/stationLayerData'

export interface MetStationLayerModel {
  /** 气象代站叠加层是否激活（state.metStations）。 */
  active: boolean
  featureCollection: M11StationFeatureCollection | null
  loading: boolean
  error: string | null
  total: number
  loaded: number
  truncated: boolean
  /** honest 空态/状态文案；无可渲染数据时给出原因，禁止用空图层冒充完整。 */
  statusNote: string | null
}

function stationId(station: HydroMetStation): string | null {
  const value = (station as { station_id?: unknown }).station_id
  return typeof value === 'string' && value.length > 0 ? value : null
}

function stationName(station: HydroMetStation): string | null {
  const value = (station as { station_name?: unknown }).station_name
  return typeof value === 'string' && value.length > 0 ? value : null
}

function buildFeatureCollection(stations: HydroMetStation[], stationBasinIds: Record<string, string>): M11StationFeatureCollection {
  return {
    type: 'FeatureCollection',
    features: stations.flatMap((station) => {
      const id = stationId(station)
      const coordinates = getHydroMetStationCoordinates(station)
      if (!id || !coordinates) return []
      return [
        {
          type: 'Feature' as const,
          geometry: { type: 'Point' as const, coordinates: [coordinates.lon, coordinates.lat] as [number, number] },
          properties: { station_id: id, station_name: stationName(station), basin_id: stationBasinIds[id] ?? null },
        },
      ]
    }),
  }
}

/**
 * 代站图层数据接线（M26-3）。仅在叠加层激活且拿到可见/当前流域上下文时取数；
 * inventory 位置清单不依赖 source/cycle，truncated 显式标注。
 */
export function useMetStationLayer({
  active,
  basinContexts,
}: {
  active: boolean
  basinContexts: StationLayerBasinContext[]
}): MetStationLayerModel {
  const data = useStationLayerDataStore((store) => store.data)
  const loading = useStationLayerDataStore((store) => store.loading)
  const error = useStationLayerDataStore((store) => store.error)
  const requestKey = useStationLayerDataStore((store) => store.requestKey)
  const loadStationLayer = useStationLayerDataStore((store) => store.loadStationLayer)
  const clear = useStationLayerDataStore((store) => store.clear)

  const requestContexts = useMemo(
    () =>
      basinContexts
        .map((context) => ({
          basinId: context.basinId.trim(),
          basinVersionId: context.basinVersionId?.trim() || null,
        }))
        .filter((context) => context.basinId.length > 0),
    [basinContexts],
  )
  const shouldFetch = active && requestContexts.length > 0
  const expectedKey = shouldFetch ? stationLayerRequestKey({ basinContexts: requestContexts }) : null
  const stableRequestContexts = useMemo(() => requestContexts, [expectedKey])
  const matches = expectedKey !== null && requestKey === expectedKey
  const currentData = matches ? data : null

  useEffect(() => {
    if (!shouldFetch) {
      // 不取数分支（关闭 overlay / 无 basinId）：关闭时清掉过期数据，避免误展示别的流域代站。
      if (!active) clear()
      return
    }
    if (matches && (data || error)) return
    void loadStationLayer({ basinContexts: stableRequestContexts }).catch(() => undefined)
  }, [active, clear, data, error, loadStationLayer, matches, shouldFetch, stableRequestContexts])

  const featureCollection = useMemo(
    () => (currentData ? buildFeatureCollection(currentData.stations, currentData.stationBasinIds) : null),
    [currentData],
  )

  const statusNote = (() => {
    if (!active) return null
    if (requestContexts.length === 0) return '暂无可用流域版本以加载气象代站'
    if (loading && !currentData) return '气象代站加载中'
    if (error && !currentData) return error
    if (currentData?.truncated) return `已加载 ${currentData.loaded}/${currentData.total} 个代站，列表已截断`
    return null
  })()

  return {
    active,
    featureCollection,
    loading,
    error,
    total: currentData?.total ?? 0,
    loaded: currentData?.loaded ?? 0,
    truncated: currentData?.truncated ?? false,
    statusNote,
  }
}
