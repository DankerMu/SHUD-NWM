import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import Map, {
  Layer,
  NavigationControl,
  ScaleControl,
  Source,
  type MapLayerMouseEvent,
  type MapRef,
  type MapStyle,
} from 'react-map-gl/maplibre'
import type { LayerProps } from 'react-map-gl/maplibre'
import 'maplibre-gl/dist/maplibre-gl.css'

import { buildApiUrl } from '@/api/base'
import { floodTileLayerPaint } from '@/components/flood/alertLevels'
import { cn } from '@/lib/cn'
import { getM11BasinGeometryBudgetStatus, type LayerState, type OverviewBasin } from '@/lib/m11/overviewDataContracts'
import type { M11Basemap, M11Layer, M11QueryState } from '@/lib/m11/queryState'

export interface M11MapOverlayInteraction {
  layerId: M11Layer | 'basin-boundaries'
  event: MapLayerMouseEvent
  feature?: NonNullable<MapLayerMouseEvent['features']>[number]
}

export interface M11MapCameraFit {
  bounds: [[number, number], [number, number]]
  padding?: number
}

export interface M11MapCameraFlyTo {
  center: [number, number]
  zoom?: number
}

interface M11MapLibreSurfaceProps {
  state: M11QueryState
  layers: LayerState[]
  basins?: OverviewBasin[]
  visibleBasinIds?: string[]
  className?: string
  fitTo?: M11MapCameraFit | null
  flyTo?: M11MapCameraFlyTo | null
  onOverlayHover?: (interaction: M11MapOverlayInteraction | null) => void
  onOverlayClick?: (interaction: M11MapOverlayInteraction) => void
}

interface M11RegisteredOverlay {
  layerId: M11Layer
  sourceId: string
  layer: LayerProps
  source: { type: 'geojson'; data: string }
}

interface BasinFeatureProperties {
  basin_id: string
  basin_name: string
  basin_group: string | null
  area_km2: number | null
  river_count: number | null
  active_model_count: number
  latest_forecast_time: string | null
  selected_basin_version_id: string | null
  unavailable_reason: string | null
}

interface BasinFeature {
  type: 'Feature'
  geometry: NonNullable<OverviewBasin['boundary']>
  properties: BasinFeatureProperties
}

interface BasinFeatureCollection {
  type: 'FeatureCollection'
  features: BasinFeature[]
}

const CHINA_VIEW_STATE = {
  longitude: 104,
  latitude: 35,
  zoom: 3.35,
}

export const m11MapStyleUrls: Record<M11Basemap, string> = {
  terrain: 'm11://basemaps/terrain',
  satellite: 'm11://basemaps/satellite',
  vector: 'm11://basemaps/vector',
}

const m11MapStyles: Record<M11Basemap, MapStyle> = {
  terrain: rasterStyle('m11-terrain', ['https://a.tile.opentopomap.org/{z}/{x}/{y}.png'], '© OpenTopoMap contributors'),
  satellite: rasterStyle(
    'm11-satellite',
    ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'],
    'Tiles © Esri',
  ),
  vector: rasterStyle('m11-vector', ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'], '© OpenStreetMap contributors'),
}

export function M11MapLibreSurface({
  state,
  layers,
  basins = [],
  visibleBasinIds,
  className,
  fitTo,
  flyTo,
  onOverlayHover,
  onOverlayClick,
}: M11MapLibreSurfaceProps) {
  const mapRef = useRef<MapRef | null>(null)
  const lastFitKeyRef = useRef<string | null>(null)
  const lastFlyKeyRef = useRef<string | null>(null)
  const [mapSourceError, setMapSourceError] = useState<string | null>(null)
  const overlay = useMemo(() => buildM11RegisteredOverlay(state, layers), [layers, state])
  const basinFeatureCollection = useMemo(
    () => buildBasinFeatureCollection(basins, visibleBasinIds),
    [basins, visibleBasinIds],
  )
  const skippedBasinGeometryCount = useMemo(
    () =>
      basins.filter((basin) => {
        if (!basin.boundary) return false
        const visible = visibleBasinIds ? visibleBasinIds.includes(basin.basinId) : true
        return visible && !getM11BasinGeometryBudgetStatus(basin.boundary).ok
      }).length,
    [basins, visibleBasinIds],
  )
  const unavailableReason = useMemo(() => m11SelectedLayerUnavailableReason(state, layers, overlay), [layers, overlay, state])
  const interactiveLayerIds = [...(basinFeatureCollection.features.length > 0 ? ['m11-basin-fill'] : []), ...(overlay ? [overlay.layer.id] : [])]

  useEffect(() => {
    setMapSourceError(null)
  }, [basinFeatureCollection.features.length, overlay?.sourceId, state.basemap, state.layer, state.validTime])

  useEffect(() => {
    if (!fitTo) return
    const fitKey = mapFitKey(fitTo)
    if (fitKey === lastFitKeyRef.current) return
    lastFitKeyRef.current = fitKey
    mapRef.current?.fitBounds(fitTo.bounds, { padding: fitTo.padding ?? 32, duration: 450 })
  }, [fitTo])

  useEffect(() => {
    if (!flyTo) return
    const flyKey = mapFlyKey(flyTo)
    if (flyKey === lastFlyKeyRef.current) return
    lastFlyKeyRef.current = flyKey
    mapRef.current?.flyTo({ center: flyTo.center, zoom: flyTo.zoom, duration: 450 })
  }, [flyTo])

  const handleMouseMove = useCallback(
    (event: MapLayerMouseEvent) => {
      const basinFeature = findEventFeature(event, 'm11-basin-fill')
      if (basinFeature) {
        onOverlayHover?.({ layerId: 'basin-boundaries', event, feature: basinFeature })
        event.target.getCanvas().style.cursor = 'pointer'
        return
      }
      const overlayFeature = overlay ? findEventFeature(event, overlay.layer.id) : null
      if (!overlay || !overlayFeature) {
        onOverlayHover?.(null)
        event.target.getCanvas().style.cursor = ''
        return
      }
      onOverlayHover?.({ layerId: overlay.layerId, event, feature: overlayFeature })
      event.target.getCanvas().style.cursor = 'pointer'
    },
    [onOverlayHover, overlay],
  )

  const handleMouseLeave = useCallback(
    (event: MapLayerMouseEvent) => {
      onOverlayHover?.(null)
      event.target.getCanvas().style.cursor = ''
    },
    [onOverlayHover],
  )

  const handleClick = useCallback(
    (event: MapLayerMouseEvent) => {
      const basinFeature = findEventFeature(event, 'm11-basin-fill')
      if (basinFeature) {
        onOverlayClick?.({ layerId: 'basin-boundaries', event, feature: basinFeature })
        return
      }
      const overlayFeature = overlay ? findEventFeature(event, overlay.layer.id) : null
      if (overlay && overlayFeature) onOverlayClick?.({ layerId: overlay.layerId, event, feature: overlayFeature })
    },
    [onOverlayClick, overlay],
  )

  const handleMapError = useCallback((event: { error?: { message?: string } }) => {
    setMapSourceError(event.error?.message ?? '地图源加载失败，受影响图层暂不可用。')
  }, [])

  return (
    <div
      className={cn('absolute inset-0', className)}
      data-testid="m11-map-surface"
      data-basemap={state.basemap}
      data-basemap-style={m11MapStyleUrls[state.basemap]}
      {...(overlay ? { 'data-registered-overlays': overlay.layerId } : {})}
      data-basin-feature-count={basinFeatureCollection.features.length}
      data-visible-basin-ids={basinFeatureCollection.features.map((feature) => feature.properties.basin_id).join(',')}
    >
      <Map
        ref={mapRef}
        initialViewState={CHINA_VIEW_STATE}
        mapStyle={m11MapStyles[state.basemap]}
        interactiveLayerIds={interactiveLayerIds}
        onMouseMove={handleMouseMove}
        onMouseLeave={handleMouseLeave}
        onClick={handleClick}
        onError={handleMapError}
        attributionControl
      >
        <NavigationControl position="top-left" visualizePitch />
        <ScaleControl position="bottom-left" unit="metric" />
        {basinFeatureCollection.features.length > 0 ? <M11BasinPrimitive collection={basinFeatureCollection} /> : null}
        {overlay ? <M11OverlayPrimitive overlay={overlay} /> : null}
      </Map>

      {basins.length > 0 && basinFeatureCollection.features.length === 0 ? (
        <div
          className="absolute left-5 top-20 z-[90] max-w-[min(28rem,calc(100%-2.5rem))] rounded-md border border-warning/40 bg-white/95 px-3 py-2 text-sm text-neutral-800 shadow-md"
          role="status"
          data-testid="m11-basin-layer-unavailable"
        >
          {skippedBasinGeometryCount > 0
            ? '当前可见流域边界超过客户端渲染预算，地图不会注册过大的边界源。'
            : '当前没有可见流域边界。请在左侧流域树恢复选择。'}
        </div>
      ) : null}

      {unavailableReason ? (
        <div
          className="absolute left-5 top-20 z-[90] max-w-[min(28rem,calc(100%-2.5rem))] rounded-md border border-warning/40 bg-white/95 px-3 py-2 text-sm text-neutral-800 shadow-md"
          role="status"
          data-testid="m11-map-unavailable"
        >
          {unavailableReason}
        </div>
      ) : null}

      {mapSourceError ? (
        <div
          className="absolute left-5 top-32 z-[90] max-w-[min(28rem,calc(100%-2.5rem))] rounded-md border border-warning/40 bg-white/95 px-3 py-2 text-sm text-neutral-800 shadow-md"
          role="status"
          data-testid="m11-map-source-error"
        >
          {mapSourceError}
        </div>
      ) : null}
    </div>
  )
}

export function buildM11RegisteredOverlay(state: M11QueryState, layers: LayerState[]): M11RegisteredOverlay | null {
  const selectedLayer = layers.find((layer) => layer.layerId === state.layer)
  if (!selectedLayer?.available) return null

  const runId = selectedLayer.freshness.runId
  const selectedValidTime = normalizeIso(state.validTime)
  const validTime =
    selectedValidTime && selectedLayer.validTimes.includes(selectedValidTime) ? selectedValidTime : selectedLayer.currentValidTime
  if (!runId || !validTime) return null

  const sourceId = `m11-${state.layer}-source`
  const layerId = `m11-${state.layer}-line`

  if (state.layer === 'flood-return-period') {
    return {
      layerId: state.layer,
      sourceId,
      source: { type: 'geojson', data: floodReturnPeriodGeoJsonUrl(runId, validTime) },
      layer: {
        id: layerId,
        type: 'line',
        source: sourceId,
        paint: floodTileLayerPaint(),
      },
    }
  }

  return null
}

function M11OverlayPrimitive({ overlay }: { overlay: M11RegisteredOverlay }) {
  return (
    <Source id={overlay.sourceId} type="geojson" data={overlay.source.data} promoteId="segment_id">
      <Layer {...overlay.layer} />
    </Source>
  )
}

function M11BasinPrimitive({ collection }: { collection: BasinFeatureCollection }) {
  return (
    <Source id="m11-basin-boundaries-source" type="geojson" data={collection} promoteId="basin_id">
      <Layer
        id="m11-basin-fill"
        type="fill"
        source="m11-basin-boundaries-source"
        paint={{
          'fill-color': '#1E88E5',
          'fill-opacity': 0.14,
        }}
      />
      <Layer
        id="m11-basin-outline"
        type="line"
        source="m11-basin-boundaries-source"
        paint={{
          'line-color': '#0F3460',
          'line-width': 1.4,
          'line-opacity': 0.72,
        }}
      />
      <Layer
        id="m11-basin-label"
        type="symbol"
        source="m11-basin-boundaries-source"
        layout={{
          'text-field': ['get', 'basin_name'],
          'text-size': 12,
          'text-anchor': 'center',
          'text-allow-overlap': false,
        }}
        paint={{
          'text-color': '#0A1929',
          'text-halo-color': '#FFFFFF',
          'text-halo-width': 1,
        }}
      />
    </Source>
  )
}

function buildBasinFeatureCollection(basins: OverviewBasin[], visibleBasinIds: string[] | undefined): BasinFeatureCollection {
  const visible = visibleBasinIds ? new Set(visibleBasinIds) : null
  return {
    type: 'FeatureCollection',
    features: basins
      .filter((basin) => basin.boundary && getM11BasinGeometryBudgetStatus(basin.boundary).ok && (!visible || visible.has(basin.basinId)))
      .map((basin) => ({
        type: 'Feature',
        geometry: basin.boundary as NonNullable<OverviewBasin['boundary']>,
        properties: {
          basin_id: basin.basinId,
          basin_name: basin.displayName,
          basin_group: basin.basinGroup,
          area_km2: basin.areaKm2,
          river_count: basin.riverCount,
          active_model_count: basin.activeModelCount,
          latest_forecast_time: basin.latestForecastTime,
          selected_basin_version_id: basin.selectedBasinVersionId,
          unavailable_reason: basin.unavailableReason,
        },
      })),
  }
}

function m11SelectedLayerUnavailableReason(
  state: M11QueryState,
  layers: LayerState[],
  overlay: M11RegisteredOverlay | null,
) {
  if (overlay) return null
  const selectedLayer = layers.find((layer) => layer.layerId === state.layer)
  if (!selectedLayer) return '当前图层尚未由 /api/v1/layers 注册，地图不会渲染该叠加层。'
  if (!selectedLayer.available) return selectedLayer.disabledReason ?? '当前图层没有可渲染的有效时间。'
  if (!selectedLayer.freshness.runId) return '当前图层缺少可追溯 run_id，地图不会注册叠加层。'
  if (!selectedLayer.currentValidTime) return '当前图层缺少有效时间，地图不会注册叠加层。'
  if (state.layer === 'discharge' || state.layer === 'water-level' || state.layer === 'warning-level') {
    return '当前水文图层的地图源尚未在本仓库实现，地图不会注册该叠加层。'
  }
  return '当前图层缺少可用地图源，地图不会注册叠加层。'
}

function rasterStyle(id: string, tiles: string[], attribution: string): MapStyle {
  return {
    version: 8,
    sources: {
      [id]: {
        type: 'raster',
        tiles,
        tileSize: 256,
        attribution,
      },
    },
    layers: [{ id, type: 'raster', source: id }],
  }
}

function floodReturnPeriodGeoJsonUrl(runId: string, validTime: string) {
  const params = new URLSearchParams({
    run_id: runId,
    duration: '1h',
    valid_time: validTime,
  })
  return buildApiUrl(`/api/v1/tiles/flood-return-period?${params.toString()}`)
}

function findEventFeature(event: MapLayerMouseEvent, layerId: string) {
  return event.features?.find((feature) => feature.layer?.id === layerId) ?? null
}

function mapFitKey(fitTo: M11MapCameraFit) {
  const [[minLon, minLat], [maxLon, maxLat]] = fitTo.bounds
  return `${minLon},${minLat},${maxLon},${maxLat},${fitTo.padding ?? 32}`
}

function mapFlyKey(flyTo: M11MapCameraFlyTo) {
  return `${flyTo.center[0]},${flyTo.center[1]},${flyTo.zoom ?? ''}`
}

function normalizeIso(value: string | null | undefined) {
  if (!value) return null
  const timestamp = Date.parse(value)
  return Number.isFinite(timestamp) ? new Date(timestamp).toISOString() : null
}
