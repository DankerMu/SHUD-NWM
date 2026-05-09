import { useCallback, useEffect, useMemo, useState } from 'react'
import Map, {
  NavigationControl,
  ScaleControl,
  type ErrorEvent,
  type MapLayerMouseEvent,
  type MapStyle,
} from 'react-map-gl/maplibre'
import 'maplibre-gl/dist/maplibre-gl.css'

import {
  demoRivers,
  RIVER_LAYER_ID,
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
  className?: string
}

async function loadRiverNetwork(): Promise<RiverFeatureCollection> {
  return demoRivers
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

function toForecastSegment(properties: RiverFeatureProperties): ForecastSegmentInfo {
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
      .then((data) => {
        if (!mounted) return
        setRiverData(data)
        setRiverError(null)
      })
      .catch((error) => {
        if (!mounted) return
        const message = error instanceof Error ? error.message : '河网数据加载失败'
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
  }, [toast])

  const clearHover = useCallback((event?: MapLayerMouseEvent) => {
    setHoveredSegmentId(null)
    setTooltip(null)
    if (event) event.target.getCanvas().style.cursor = ''
  }, [])

  const handleMouseMove = useCallback(
    (event: MapLayerMouseEvent) => {
      const feature = event.features?.find((feature) => feature.layer.id === RIVER_LAYER_ID)
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
      const feature = event.features?.find((feature) => feature.layer.id === RIVER_LAYER_ID)
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
        interactiveLayerIds={[RIVER_LAYER_ID]}
        onMouseMove={handleMouseMove}
        onMouseLeave={handleMouseLeave}
        onClick={handleClick}
        onError={handleMapError}
        attributionControl
        reuseMaps
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
