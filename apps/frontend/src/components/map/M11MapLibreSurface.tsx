import { useCallback, useEffect, useMemo, useRef } from 'react'
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
import type { LayerState } from '@/lib/m11/overviewDataContracts'
import type { M11Basemap, M11Layer, M11QueryState } from '@/lib/m11/queryState'

export interface M11MapOverlayInteraction {
  layerId: M11Layer
  event: MapLayerMouseEvent
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
  className,
  fitTo,
  flyTo,
  onOverlayHover,
  onOverlayClick,
}: M11MapLibreSurfaceProps) {
  const mapRef = useRef<MapRef | null>(null)
  const lastFitKeyRef = useRef<string | null>(null)
  const lastFlyKeyRef = useRef<string | null>(null)
  const overlay = useMemo(() => buildM11RegisteredOverlay(state, layers), [layers, state])
  const unavailableReason = useMemo(() => m11SelectedLayerUnavailableReason(state, layers, overlay), [layers, overlay, state])
  const interactiveLayerIds = overlay ? [overlay.layer.id] : []

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
      if (!overlay || !eventHasOverlayFeature(event, overlay.layer.id)) {
        onOverlayHover?.(null)
        event.target.getCanvas().style.cursor = ''
        return
      }
      onOverlayHover?.({ layerId: overlay.layerId, event })
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
      if (overlay && eventHasOverlayFeature(event, overlay.layer.id)) onOverlayClick?.({ layerId: overlay.layerId, event })
    },
    [onOverlayClick, overlay],
  )

  return (
    <div
      className={cn('absolute inset-0', className)}
      data-testid="m11-map-surface"
      data-basemap={state.basemap}
      data-basemap-style={m11MapStyleUrls[state.basemap]}
      {...(overlay ? { 'data-registered-overlays': overlay.layerId } : {})}
    >
      <Map
        ref={mapRef}
        initialViewState={CHINA_VIEW_STATE}
        mapStyle={m11MapStyles[state.basemap]}
        interactiveLayerIds={interactiveLayerIds}
        onMouseMove={handleMouseMove}
        onMouseLeave={handleMouseLeave}
        onClick={handleClick}
        attributionControl
      >
        <NavigationControl position="top-left" visualizePitch />
        <ScaleControl position="bottom-left" unit="metric" />
        {overlay ? <M11OverlayPrimitive overlay={overlay} /> : null}
      </Map>

      {unavailableReason ? (
        <div
          className="absolute left-5 top-20 z-[90] max-w-[min(28rem,calc(100%-2.5rem))] rounded-md border border-warning/40 bg-white/95 px-3 py-2 text-sm text-neutral-800 shadow-md"
          role="status"
          data-testid="m11-map-unavailable"
        >
          {unavailableReason}
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

function eventHasOverlayFeature(event: MapLayerMouseEvent, layerId: string) {
  return event.features?.some((feature) => feature.layer?.id === layerId) ?? false
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
