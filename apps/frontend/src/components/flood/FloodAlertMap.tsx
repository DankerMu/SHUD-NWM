import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import Map, {
  NavigationControl,
  ScaleControl,
  type ErrorEvent,
  type MapLayerMouseEvent,
  type MapRef,
  type MapStyle,
} from 'react-map-gl/maplibre'
import 'maplibre-gl/dist/maplibre-gl.css'

import {
  alertLevelLabel,
  FLOOD_TILE_HOVER_LAYER_ID,
  FLOOD_TILE_LAYER_ID,
  FLOOD_TILE_SELECTED_LAYER_ID,
  isAlertLevel,
  type AlertLevel,
} from '@/components/flood/alertLevels'
import { FloodReturnPeriodLayer } from '@/components/flood/FloodReturnPeriodLayer'
import { useToast } from '@/hooks/useToast'
import { cn } from '@/lib/cn'
import { floodReturnPeriodFeatureId } from '@/lib/floodReturnPeriodGeoJson'
import type { FloodAlertRankingItem } from '@/stores/floodAlert'

const BASE_MAP_STYLE: MapStyle = {
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

const INTERACTIVE_LAYER_IDS = [
  FLOOD_TILE_LAYER_ID,
  FLOOD_TILE_HOVER_LAYER_ID,
  FLOOD_TILE_SELECTED_LAYER_ID,
]

interface FloodMapSegment {
  featureId: string
  riverSegmentId: string
  segmentName?: string | null
  basinVersionId?: string | null
  riverNetworkVersionId?: string | null
  qValue?: number | null
  returnPeriod?: number | null
  warningLevel?: AlertLevel | null
}

interface TooltipState extends FloodMapSegment {
  x: number
  y: number
}

interface FloodAlertMapProps {
  runId: string | null
  validTime: string | null
  tileFallbackTime?: string | null
  selectedLevel?: AlertLevel | null
  selectedSegment?: FloodAlertRankingItem | null
  onSegmentSelect: (segment: FloodAlertRankingItem) => void
  className?: string
}

function numberOrNull(value: unknown) {
  const numeric = Number(value)
  return Number.isFinite(numeric) ? numeric : null
}

function formatNumber(value: number | null | undefined) {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(1) : '-'
}

function readFloodProperties(properties: unknown): FloodMapSegment | null {
  if (!properties || typeof properties !== 'object') return null
  const record = properties as Record<string, unknown>
  const riverSegmentId = String(record.segment_id ?? record.river_segment_id ?? '')
  if (!riverSegmentId) return null
  const featureId = floodReturnPeriodFeatureId(record)
  if (!featureId) return null

  const warningLevel = isAlertLevel(record.warning_level) ? record.warning_level : null
  return {
    featureId,
    riverSegmentId,
    segmentName: typeof record.segment_name === 'string' ? record.segment_name : null,
    basinVersionId: typeof record.basin_version_id === 'string' ? record.basin_version_id : null,
    riverNetworkVersionId: typeof record.river_network_version_id === 'string' ? record.river_network_version_id : null,
    qValue: numberOrNull(record.value ?? record.q_value),
    returnPeriod: numberOrNull(record.return_period),
    warningLevel,
  }
}

function findFloodFeature(event: MapLayerMouseEvent) {
  return event.features?.find((feature) => INTERACTIVE_LAYER_IDS.includes(feature.layer.id))
}

export function FloodAlertMap({
  runId,
  validTime,
  tileFallbackTime,
  selectedLevel,
  selectedSegment,
  onSegmentSelect,
  className,
}: FloodAlertMapProps) {
  const toast = useToast((state) => state.toast)
  const mapRef = useRef<MapRef | null>(null)
  const [mapError, setMapError] = useState<string | null>(null)
  const [returnPeriodUnavailableReason, setReturnPeriodUnavailableReason] = useState<string | null>(null)
  const [hoveredFeatureId, setHoveredFeatureId] = useState<string | null>(null)
  const [tooltip, setTooltip] = useState<TooltipState | null>(null)

  const tileTime = validTime ?? tileFallbackTime
  const selectedFeatureId =
    selectedSegment?.riverNetworkVersionId && selectedSegment.riverSegmentId
      ? `${selectedSegment.riverNetworkVersionId}::${selectedSegment.riverSegmentId}`
      : null

  const initialViewState = useMemo(
    () => ({
      longitude: 105,
      latitude: 34,
      zoom: 3.4,
    }),
    [],
  )

  useEffect(() => {
    const coordinates = selectedSegment?.geomCentroid?.coordinates
    if (!coordinates || !mapRef.current) return
    mapRef.current.flyTo({ center: coordinates, zoom: 8, duration: 650 })
  }, [selectedSegment])

  const clearHover = useCallback((event?: MapLayerMouseEvent) => {
    setHoveredFeatureId(null)
    setTooltip(null)
    if (event) event.target.getCanvas().style.cursor = ''
  }, [])

  const handleMouseMove = useCallback(
    (event: MapLayerMouseEvent) => {
      const properties = readFloodProperties(findFloodFeature(event)?.properties)
      if (!properties) {
        clearHover(event)
        return
      }

      setHoveredFeatureId(properties.featureId)
      setTooltip({
        ...properties,
        x: event.point.x + 14,
        y: event.point.y + 14,
      })
      event.target.getCanvas().style.cursor = 'pointer'
    },
    [clearHover],
  )

  const handleClick = useCallback(
    (event: MapLayerMouseEvent) => {
      const properties = readFloodProperties(findFloodFeature(event)?.properties)
      if (!properties) return

      onSegmentSelect({
        rank: 0,
        riverSegmentId: properties.riverSegmentId,
        segmentId: properties.riverSegmentId,
        segmentName: properties.segmentName,
        basinVersionId: properties.basinVersionId,
        riverNetworkVersionId: properties.riverNetworkVersionId,
        qValue: properties.qValue,
        returnPeriod: properties.returnPeriod,
        warningLevel: properties.warningLevel,
        validTime: validTime ?? undefined,
      })
    },
    [onSegmentSelect, validTime],
  )

  const handleMapError = useCallback(
    (event: ErrorEvent) => {
      const message = event.error?.message ?? '地图加载失败'
      setMapError(message)
      toast({ title: '地图加载失败', description: message, variant: 'destructive' })
    },
    [toast],
  )

  return (
    <div className={cn('relative h-full min-h-[32rem] overflow-hidden', className)}>
      {mapError ? (
        <div
          className="absolute left-3 top-3 z-10 max-w-[min(26rem,calc(100%-1.5rem))] rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger shadow-sm"
          role="status"
        >
          {mapError}
        </div>
      ) : null}

      {returnPeriodUnavailableReason ? (
        <div
          className="absolute left-3 top-14 z-10 max-w-[min(26rem,calc(100%-1.5rem))] rounded-md border border-warning/40 bg-panel/95 px-3 py-2 text-sm text-foreground shadow-sm"
          role="status"
          data-testid="flood-return-period-unavailable"
        >
          {returnPeriodUnavailableReason}
        </div>
      ) : null}

      {tooltip ? (
        <div
          className="pointer-events-none absolute z-10 max-w-72 rounded-md border border-foreground/15 bg-panel px-3 py-2 text-sm leading-5 text-foreground shadow-lg"
          style={{ left: tooltip.x, top: tooltip.y }}
        >
          <div>
            <span className="font-semibold">河段：</span>
            {tooltip.segmentName || tooltip.riverSegmentId}
          </div>
          <div>
            <span className="font-semibold">Q：</span>
            {formatNumber(tooltip.qValue)} m³/s
          </div>
          <div>
            <span className="font-semibold">T：</span>
            {formatNumber(tooltip.returnPeriod)}
          </div>
          <div>
            <span className="font-semibold">等级：</span>
            {alertLevelLabel(tooltip.warningLevel)}
          </div>
        </div>
      ) : null}

      {!runId || !tileTime ? (
        <div className="absolute inset-0 z-10 grid place-items-center bg-panel/80 text-sm text-muted">
          暂无可渲染的预警瓦片
        </div>
      ) : null}

      <Map
        ref={mapRef}
        initialViewState={initialViewState}
        mapStyle={BASE_MAP_STYLE}
        interactiveLayerIds={INTERACTIVE_LAYER_IDS}
        onMouseMove={handleMouseMove}
        onMouseLeave={clearHover}
        onClick={handleClick}
        onError={handleMapError}
        attributionControl
      >
        <NavigationControl position="top-left" visualizePitch />
        <ScaleControl position="bottom-left" unit="metric" />
        {runId && tileTime ? (
          <FloodReturnPeriodLayer
            runId={runId}
            validTime={tileTime}
            selectedLevel={selectedLevel}
            hoveredFeatureId={hoveredFeatureId}
            selectedFeatureId={selectedFeatureId}
            onUnavailableReason={setReturnPeriodUnavailableReason}
          />
        ) : null}
      </Map>
    </div>
  )
}
