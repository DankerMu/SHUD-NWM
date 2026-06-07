import { useEffect, useMemo } from 'react'

import type { M11StationFeatureCollection } from '@/components/map/M11MapLibreSurface'
import type { HydroMetSource } from '@/lib/hydroMet/queryState'
import { getHydroMetStationCoordinates } from '@/lib/hydroMet/runtime'
import type { HydroMetStation } from '@/pages/hydroMet/bootstrap'
import { useStationLayerDataStore } from '@/stores/stationLayerData'

export interface MetStationLayerModel {
  /** 该图层是否激活（state.layer==='met-stations'）。 */
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

function buildFeatureCollection(stations: HydroMetStation[]): M11StationFeatureCollection {
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
          properties: { station_id: id, station_name: stationName(station) },
        },
      ]
    }),
  }
}

/**
 * 代站图层数据接线（M26-3）。仅在图层激活且拿到 basinId + 已解析 GFS/IFS 时取数；
 * 源未解析（best 未落）或无 basinId 时不取数，给 honest 空态文案。truncated 显式标注。
 */
export function useMetStationLayer({
  active,
  basinId,
  resolvedSource,
  cycle,
}: {
  active: boolean
  basinId: string | null
  /** basin detail 的 resolvedSource；best/compare 未解析时为非 GFS/IFS（如 'Unknown'/'GFS+IFS'）。 */
  resolvedSource: string | null
  cycle: string | null
}): MetStationLayerModel {
  const data = useStationLayerDataStore((store) => store.data)
  const loading = useStationLayerDataStore((store) => store.loading)
  const error = useStationLayerDataStore((store) => store.error)
  const requestKey = useStationLayerDataStore((store) => store.requestKey)
  const loadStationLayer = useStationLayerDataStore((store) => store.loadStationLayer)
  const clear = useStationLayerDataStore((store) => store.clear)

  const concreteSource: HydroMetSource | null =
    resolvedSource === 'GFS' || resolvedSource === 'IFS' ? resolvedSource : null
  const shouldFetch = active && Boolean(basinId) && Boolean(concreteSource)
  const expectedKey = shouldFetch ? `${basinId}::${concreteSource}::${cycle ?? 'latest'}` : null

  useEffect(() => {
    if (!shouldFetch || !basinId || !concreteSource) {
      // 不取数分支（关闭图层 / 无 basinId / 源未解析）：清掉过期数据，避免误展示别的流域代站。
      if (!active) clear()
      return
    }
    void loadStationLayer({ basinId, resolvedSource: concreteSource, cycle }).catch(() => undefined)
  }, [active, basinId, concreteSource, cycle, clear, loadStationLayer, shouldFetch])

  const matches = expectedKey !== null && requestKey === expectedKey
  const currentData = matches ? data : null
  const featureCollection = useMemo(
    () => (currentData ? buildFeatureCollection(currentData.stations) : null),
    [currentData],
  )

  const statusNote = (() => {
    if (!active) return null
    if (!basinId) return '请选择流域以加载气象代站'
    if (!concreteSource) return '等待 Best Available 解析到具体源（GFS/IFS）后加载气象代站'
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
