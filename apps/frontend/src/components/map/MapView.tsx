import { useCallback, useEffect, useMemo, useState } from 'react'
import Map, {
  NavigationControl,
  ScaleControl,
  type ErrorEvent,
  type MapLayerMouseEvent,
  type MapStyle,
} from 'react-map-gl/maplibre'
import 'maplibre-gl/dist/maplibre-gl.css'

import { buildApiUrl } from '@/api/base'
import { client } from '@/api/client'
import { getApiErrorMessage, unwrapApiData } from '@/api/response'
import type { components } from '@/api/types'
import {
  demoRivers,
  RIVER_HOVER_LAYER_ID,
  RIVER_LAYER_ID,
  RIVER_SELECTED_LAYER_ID,
  RiverLayer,
  type RiverFeatureCollection,
  type RiverFeatureProperties,
} from '@/components/map/RiverLayer'
import { useToast } from '@/hooks/useToast'
import { cn } from '@/lib/cn'
import type { ForecastSegmentInfo } from '@/stores/forecast'

const LEGACY_MAP_STYLE: MapStyle = {
  version: 8,
  sources: {
    osm: {
      type: 'raster',
      tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
      tileSize: 256,
      attribution: '© OpenStreetMap contributors',
    },
  },
  layers: [{ id: 'osm', type: 'raster', source: 'osm' }],
}

const RIVER_INTERACTIVE_LAYER_IDS = [
  RIVER_LAYER_ID,
  RIVER_HOVER_LAYER_ID,
  RIVER_SELECTED_LAYER_ID,
]
const RIVER_SEGMENT_PAGE_LIMIT = 500
const RIVER_SEGMENT_INITIAL_PAGE_COUNT = 2

const allowDemoRiverFallback = import.meta.env.DEV && import.meta.env.VITE_ENABLE_DEMO_RIVERS === 'true'

interface TooltipState {
  x: number
  y: number
  segmentId: string
  name?: string
}

interface MapViewProps {
  selectedSegmentId?: string | null
  onSegmentSelect: (segment: ForecastSegmentInfo) => void
  onClearSelection: () => void
  onBasinContextLoaded?: (context: ForecastBasinContext | null) => void
  className?: string
}

type ModelPage = components['schemas']['ModelInstancePage']

export interface ForecastBasinContext {
  basinId: string
  basinVersionId: string
}

function validateRiverCollection(payload: unknown): RiverFeatureCollection {
  const collection = unwrapApiData<RiverFeatureCollection>(payload, '河网数据加载失败')
  if (collection.type !== 'FeatureCollection' || !Array.isArray(collection.features)) {
    throw new Error('河网数据格式无效')
  }
  return collection
}

function paginationTotal(value: number | undefined, fallback: number) {
  return Number.isFinite(value) && value !== undefined && value >= 0 ? value : fallback
}

async function fetchRiverSegmentPage(
  basinVersionId: string,
  riverNetworkVersionId: string,
  offset: number,
): Promise<RiverFeatureCollection> {
  const params = new URLSearchParams({
    river_network_version_id: riverNetworkVersionId,
    limit: String(RIVER_SEGMENT_PAGE_LIMIT),
    offset: String(offset),
  })
  const response = await fetch(
    buildApiUrl(`/api/v1/basin-versions/${encodeURIComponent(basinVersionId)}/river-segments?${params}`),
  )
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    if (allowDemoRiverFallback) return demoRivers
    throw new Error(getApiErrorMessage(payload, response.statusText || '河网数据加载失败'))
  }

  return validateRiverCollection(payload)
}

async function loadRiverSegmentPages(
  basinVersionId: string,
  riverNetworkVersionId: string,
): Promise<RiverFeatureCollection> {
  let merged: RiverFeatureCollection | null = null
  const features: RiverFeatureCollection['features'] = []

  for (let pageIndex = 0; pageIndex < RIVER_SEGMENT_INITIAL_PAGE_COUNT; pageIndex += 1) {
    const offset = pageIndex * RIVER_SEGMENT_PAGE_LIMIT
    const page = await fetchRiverSegmentPage(basinVersionId, riverNetworkVersionId, offset)
    const featureTotal = paginationTotal(page.feature_total, page.features.length)
    const total = paginationTotal(page.total, featureTotal)

    if (!merged) {
      merged = {
        ...page,
        features,
        total,
        feature_total: featureTotal,
        limit: 0,
        offset: 0,
      }
    } else {
      merged.total = total
      merged.feature_total = featureTotal
    }

    if (page.features.length === 0) break
    features.push(...page.features)

    if (features.length >= featureTotal || page.features.length < RIVER_SEGMENT_PAGE_LIMIT) break
  }

  if (!merged) {
    return {
      type: 'FeatureCollection',
      features: [],
      total: 0,
      feature_total: 0,
      limit: 0,
      offset: 0,
    }
  }

  merged.limit = features.length
  return merged
}

async function loadRiverNetwork(): Promise<{ data: RiverFeatureCollection; basinContext: ForecastBasinContext | null }> {
  const { data, error } = await client.GET('/api/v1/models', {
    params: { query: { active: 'true', limit: 1, offset: 0 } },
  })
  if (error) {
    if (allowDemoRiverFallback) return { data: demoRivers, basinContext: null }
    throw new Error(getApiErrorMessage(error, '模型版本加载失败'))
  }

  const models = unwrapApiData<ModelPage>(data, '模型版本加载失败')
  const model = models.items[0]
  if (!model?.basin_version_id || !model.river_network_version_id) {
    if (allowDemoRiverFallback) return { data: demoRivers, basinContext: null }
    throw new Error('未找到活动模型的 basin_version_id 或 river_network_version_id')
  }

  const basinContext =
    model.basin_id && model.basin_version_id
      ? { basinId: model.basin_id, basinVersionId: model.basin_version_id }
      : null

  return {
    data: await loadRiverSegmentPages(model.basin_version_id, model.river_network_version_id),
    basinContext,
  }
}

function loadedRiverFeatureCount(data: RiverFeatureCollection) {
  return data.features.length
}

function totalRiverFeatureCount(data: RiverFeatureCollection) {
  return paginationTotal(data.feature_total, loadedRiverFeatureCount(data))
}

function hasPartialRiverData(data: RiverFeatureCollection) {
  return totalRiverFeatureCount(data) > loadedRiverFeatureCount(data)
}

function readRiverProperties(properties: unknown): RiverFeatureProperties | null {
  if (!properties || typeof properties !== 'object') return null
  const record = properties as Partial<RiverFeatureProperties>
  if (!record.segment_id) return null

  return {
    segment_id: String(record.segment_id),
    name: record.name ? String(record.name) : String(record.segment_id),
    stream_order: Number(record.stream_order ?? 0),
    basin_version_id: record.basin_version_id ? String(record.basin_version_id) : '',
    river_network_version_id: record.river_network_version_id ? String(record.river_network_version_id) : '',
  }
}

function findRiverFeature(event: MapLayerMouseEvent) {
  return event.features?.find((feature) => RIVER_INTERACTIVE_LAYER_IDS.includes(feature.layer.id))
}

export function toForecastSegment(properties: RiverFeatureProperties): ForecastSegmentInfo {
  return {
    segmentId: properties.segment_id,
    name: properties.name,
    basinVersionId: properties.basin_version_id,
    riverNetworkVersionId: properties.river_network_version_id,
    streamOrder: properties.stream_order,
  }
}

export function MapView({
  selectedSegmentId,
  onSegmentSelect,
  onClearSelection,
  onBasinContextLoaded,
  className,
}: MapViewProps) {
  const toast = useToast((state) => state.toast)
  const [riverData, setRiverData] = useState<RiverFeatureCollection | null>(null)
  const [riverError, setRiverError] = useState<string | null>(null)
  const [mapError, setMapError] = useState<string | null>(null)
  const [hoveredSegmentId, setHoveredSegmentId] = useState<string | null>(null)
  const [tooltip, setTooltip] = useState<TooltipState | null>(null)

  useEffect(() => {
    let mounted = true

    void loadRiverNetwork()
      .then((result) => {
        if (!mounted) return
        setRiverData(result.data)
        onBasinContextLoaded?.(result.basinContext)
        setRiverError(null)
      })
      .catch((error) => {
        if (!mounted) return
        const message = error instanceof Error ? error.message : '河网数据加载失败'
        onBasinContextLoaded?.(null)
        setRiverError(message)
        toast({
          title: '河网数据加载失败',
          description: message,
          variant: 'destructive',
        })
      })

    return () => {
      mounted = false
    }
  }, [onBasinContextLoaded, toast])

  const clearHover = useCallback((event?: MapLayerMouseEvent) => {
    setHoveredSegmentId(null)
    setTooltip(null)
    if (event) event.target.getCanvas().style.cursor = ''
  }, [])

  const handleMouseMove = useCallback(
    (event: MapLayerMouseEvent) => {
      const feature = findRiverFeature(event)
      const properties = readRiverProperties(feature?.properties)

      if (!properties) {
        clearHover(event)
        return
      }

      setHoveredSegmentId(properties.segment_id)
      setTooltip({
        x: event.point.x + 14,
        y: event.point.y + 14,
        segmentId: properties.segment_id,
        name: properties.name,
      })
      event.target.getCanvas().style.cursor = 'pointer'
    },
    [clearHover],
  )

  const handleMouseLeave = useCallback(
    (event: MapLayerMouseEvent) => {
      clearHover(event)
    },
    [clearHover],
  )

  const handleClick = useCallback(
    (event: MapLayerMouseEvent) => {
      const feature = findRiverFeature(event)
      const properties = readRiverProperties(feature?.properties)

      if (!properties) {
        onClearSelection()
        return
      }

      onSegmentSelect(toForecastSegment(properties))
    },
    [onClearSelection, onSegmentSelect],
  )

  const handleMapError = useCallback(
    (event: ErrorEvent) => {
      const message = event.error?.message ?? '地图加载失败'
      setMapError(message)
      toast({
        title: '地图加载失败',
        description: message,
        variant: 'destructive',
      })
    },
    [toast],
  )

  const initialViewState = useMemo(
    () => ({
      longitude: 109.4,
      latitude: 30.9,
      zoom: 5.2,
    }),
    [],
  )
  const riverPreviewStatus = riverData && hasPartialRiverData(riverData)
    ? `河网预览：当前显示前 ${loadedRiverFeatureCount(riverData).toLocaleString()} / ${totalRiverFeatureCount(riverData).toLocaleString()} 条河段，完整视口/瓦片加载将在后续版本提供。`
    : null

  return (
    <div className={cn('relative h-full min-h-[32rem] overflow-hidden', className)}>
      {mapError || riverError ? (
        <div
          className="absolute left-3 top-3 z-10 max-w-[min(26rem,calc(100%-1.5rem))] rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger shadow-sm"
          role="status"
        >
          {mapError ?? riverError}
        </div>
      ) : null}

      {riverPreviewStatus ? (
        <div
          className="absolute right-3 top-3 z-10 max-w-[min(30rem,calc(100%-1.5rem))] rounded-md border border-amber-500/35 bg-panel/95 px-3 py-2 text-sm text-foreground shadow-sm"
          role="status"
        >
          {riverPreviewStatus}
        </div>
      ) : null}

      {tooltip ? (
        <div
          className="pointer-events-none absolute z-10 max-w-72 rounded-md border border-foreground/15 bg-panel px-3 py-2 text-sm leading-5 text-foreground shadow-lg"
          style={{ left: tooltip.x, top: tooltip.y }}
        >
          <div>
            <span className="font-semibold">河段：</span>
            {tooltip.segmentId}
          </div>
          <div>
            <span className="font-semibold">名称：</span>
            {tooltip.name || '-'}
          </div>
        </div>
      ) : null}

      <Map
        initialViewState={initialViewState}
        mapStyle={LEGACY_MAP_STYLE}
        interactiveLayerIds={RIVER_INTERACTIVE_LAYER_IDS}
        onMouseMove={handleMouseMove}
        onMouseLeave={handleMouseLeave}
        onClick={handleClick}
        onError={handleMapError}
        attributionControl
      >
        <NavigationControl position="top-left" visualizePitch />
        <ScaleControl position="bottom-left" unit="metric" />
        {riverData ? (
          <RiverLayer
            data={riverData}
            hoveredSegmentId={hoveredSegmentId}
            selectedSegmentId={selectedSegmentId}
          />
        ) : null}
      </Map>
    </div>
  )
}
