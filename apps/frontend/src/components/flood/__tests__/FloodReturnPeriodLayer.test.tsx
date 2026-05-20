import { render, screen, waitFor } from '@testing-library/react'
import { forwardRef, useImperativeHandle, type ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { FloodAlertMap } from '@/components/flood/FloodAlertMap'
import {
  FLOOD_RETURN_PERIOD_FEATURE_ID_PROPERTY,
  FloodReturnPeriodLayer,
  floodMvtTileUrlTemplate,
  floodReturnPeriodLayer,
  floodTileUrl,
} from '@/components/flood/FloodReturnPeriodLayer'
import { DEFAULT_FLOOD_RETURN_PERIOD_DURATION } from '@/lib/floodReturnPeriodDuration'
import type { MvtLayerMetadata } from '@/lib/mvtLayerMetadata'
import { FLOOD_TILE_SOURCE_ID } from '@/components/flood/alertLevels'

vi.mock('@/api/base', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/base')>()
  return {
    ...actual,
    buildApiUrl: (path: string) => actual.buildApiUrl(path, 'https://api.example.test'),
  }
})

const sourceProps: unknown[] = []
const layerProps: unknown[] = []

function geoJsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } })
}

function emptyLayerCatalogResponse() {
  return geoJsonResponse({ request_id: 'req-test', status: 'ok', data: [] })
}

vi.mock('react-map-gl/maplibre', () => ({
  default: forwardRef(({ children }: { children: ReactNode }, ref) => {
    useImperativeHandle(ref, () => ({ flyTo: vi.fn() }))
    return <div data-testid="map">{children}</div>
  }),
  Source: ({ children, ...props }: { children: ReactNode; key?: string }) => {
    sourceProps.push(props)
    return <div data-testid="source">{children}</div>
  },
  Layer: (props: Record<string, unknown>) => {
    layerProps.push(props)
    return <div data-testid="layer" />
  },
  NavigationControl: () => <div data-testid="navigation-control" />,
  ScaleControl: () => <div data-testid="scale-control" />,
}))

describe('FloodReturnPeriodLayer', () => {
  const mvtMetadata: MvtLayerMetadata = {
    layer_id: 'flood-return-period',
    tile_format: 'mvt',
    url_template: '/api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf',
    tile_url_template: '/api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf',
    maplibre_source_layer: 'flood_return_period',
    source_layer: 'flood_return_period',
    fallback_available: true,
    release_blocking: false,
    required_placeholders: ['run_id', 'duration', 'valid_time', 'z', 'x', 'y'],
    valid_times: ['2026-05-03T06:00:00Z'],
    source_refs: { run_id: 'run-1', source_version: 'rnv_v1', basin_version_id: 'basin_v1', duration: '1h' },
    cache_version: 'cache-v1',
    cache_etag: 'etag-v1',
    property_schema_version: 'schema-v1',
    schema_version: 'schema-v1',
    encoder_version: 'encoder-v1',
  }

  function mvtLayerCatalogResponse() {
    return geoJsonResponse({
      request_id: 'req-test',
      status: 'ok',
      data: [
        {
          layer_id: 'flood-return-period',
          layer_name: 'Flood return period',
          layer_type: 'hydrology',
          variables: ['return_period'],
          metadata: mvtMetadata,
        },
      ],
    })
  }

  it('builds the bounded GeoJSON compatibility endpoint without z/x/y or pbf semantics', () => {
    const url = floodTileUrl('run 1', '2026-05-03T06:00:00Z', {
      minLon: 100,
      minLat: 30,
      maxLon: 101,
      maxLat: 31,
    })

    expect(url).toContain('https://api.example.test/api/v1/tiles/flood-return-period?')
    expect(url).toContain('run_id=run+1')
    expect(url).toContain('duration=1h')
    expect(url).toContain('limit=500')
    expect(url).toContain('bbox=100%2C30%2C101%2C31')
    expect(url).not.toContain('{z}')
    expect(url).not.toContain('.pbf')
  })

  it('builds MVT URLs from layer metadata placeholders', () => {
    const url = floodMvtTileUrlTemplate(mvtMetadata, 'run-1', '2026-05-03T06:00:00Z')

    expect(DEFAULT_FLOOD_RETURN_PERIOD_DURATION).toBe('1h')
    expect(url).toBe(
      'https://api.example.test/api/v1/tiles/flood-return-period/run-1/1h/2026-05-03T06%3A00%3A00Z/{z}/{x}/{y}.pbf?_mvt_cache_version=cache-v1',
    )
  })

  it('configures a vector source when MVT metadata is available', async () => {
    sourceProps.length = 0
    layerProps.length = 0
    vi.stubGlobal('fetch', vi.fn())

    render(<FloodReturnPeriodLayer runId="run-1" validTime="2026-05-03T06:00:00Z" metadata={mvtMetadata} />)

    await waitFor(() => expect(sourceProps.at(-1)).toMatchObject({ type: 'vector' }))
    expect(sourceProps.at(-1)).toMatchObject({
      tiles: [
        'https://api.example.test/api/v1/tiles/flood-return-period/run-1/1h/2026-05-03T06%3A00%3A00Z/{z}/{x}/{y}.pbf?_mvt_cache_version=cache-v1',
      ],
    })
    expect(floodReturnPeriodLayer(null, 'flood_return_period')).toMatchObject({ 'source-layer': 'flood_return_period' })
    expect(fetch).not.toHaveBeenCalledWith(expect.stringContaining('/api/v1/tiles/flood-return-period?'), expect.anything())
  })

  it('recreates the vector source when route or metadata identity changes', async () => {
    sourceProps.length = 0
    layerProps.length = 0
    vi.stubGlobal('fetch', vi.fn())
    const { rerender } = render(
      <FloodReturnPeriodLayer runId="run-1" validTime="2026-05-03T06:00:00Z" metadata={mvtMetadata} />,
    )

    await waitFor(() => expect(sourceProps.at(-1)).toMatchObject({ type: 'vector' }))
    const initialSource = sourceProps.at(-1)

    rerender(<FloodReturnPeriodLayer runId="run-2" validTime="2026-05-03T06:00:00Z" metadata={mvtMetadata} />)
    await waitFor(() => expect(sourceProps.at(-1)).not.toBe(initialSource))
    expect(sourceProps.at(-1)).toMatchObject({
      tiles: [
        'https://api.example.test/api/v1/tiles/flood-return-period/run-2/1h/2026-05-03T06%3A00%3A00Z/{z}/{x}/{y}.pbf?_mvt_cache_version=cache-v1',
      ],
    })
    const runChangedSource = sourceProps.at(-1)

    rerender(<FloodReturnPeriodLayer runId="run-2" validTime="2026-05-03T12:00:00Z" metadata={mvtMetadata} />)
    await waitFor(() => expect(sourceProps.at(-1)).not.toBe(runChangedSource))
    expect(sourceProps.at(-1)).toMatchObject({
      tiles: [
        'https://api.example.test/api/v1/tiles/flood-return-period/run-2/1h/2026-05-03T12%3A00%3A00Z/{z}/{x}/{y}.pbf?_mvt_cache_version=cache-v1',
      ],
    })
    const timeChangedSource = sourceProps.at(-1)

    rerender(
      <FloodReturnPeriodLayer
        runId="run-2"
        validTime="2026-05-03T12:00:00Z"
        metadata={{ ...mvtMetadata, cache_version: 'cache-v2', cache_etag: 'etag-v2' }}
      />,
    )
    await waitFor(() => expect(sourceProps.at(-1)).not.toBe(timeChangedSource))
    expect(sourceProps.at(-1)).toMatchObject({
      id: FLOOD_TILE_SOURCE_ID,
      type: 'vector',
      tiles: [
        'https://api.example.test/api/v1/tiles/flood-return-period/run-2/1h/2026-05-03T12%3A00%3A00Z/{z}/{x}/{y}.pbf?_mvt_cache_version=cache-v2',
      ],
    })
  })

  it('blocks direct release-blocking metadata without GeoJSON fallback', async () => {
    sourceProps.length = 0
    layerProps.length = 0
    const onUnavailableReason = vi.fn()
    vi.stubGlobal('fetch', vi.fn())

    render(
      <FloodReturnPeriodLayer
        runId="run-1"
        validTime="2026-05-03T06:00:00Z"
        metadata={{ ...mvtMetadata, release_blocking: true }}
        fallbackBbox={{ minLon: 100, minLat: 30, maxLon: 101, maxLat: 31 }}
        degradedFallback
        onUnavailableReason={onUnavailableReason}
      />,
    )

    await waitFor(() => expect(onUnavailableReason).toHaveBeenLastCalledWith(expect.stringContaining('release-blocking')))
    expect(sourceProps).toHaveLength(0)
    expect(layerProps).toHaveLength(0)
    expect(fetch).not.toHaveBeenCalled()
  })

  it('renders map MVT tiles with the API-resolved sample tail instead of run end time fallback', async () => {
    sourceProps.length = 0
    layerProps.length = 0
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(mvtLayerCatalogResponse()))

    render(
      <FloodAlertMap
        runId="run-1"
        validTime={null}
        tileFallbackTime="2026-05-03T07:17:00.000Z"
        onSegmentSelect={vi.fn()}
      />,
    )

    await waitFor(() => expect(sourceProps.at(-1)).toMatchObject({ type: 'vector' }))
    expect(sourceProps.at(-1)).toMatchObject({
      tiles: [
        'https://api.example.test/api/v1/tiles/flood-return-period/run-1/1h/2026-05-03T07%3A17%3A00.000Z/{z}/{x}/{y}.pbf?_mvt_cache_version=cache-v1',
      ],
    })
  })

  it('does not fetch unbounded GeoJSON while catalog metadata is loading', async () => {
    sourceProps.length = 0
    layerProps.length = 0
    const onUnavailableReason = vi.fn()
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(new Promise(() => undefined)))

    render(
      <FloodReturnPeriodLayer
        runId="run-1"
        validTime="2026-05-03T06:00:00Z"
        onUnavailableReason={onUnavailableReason}
      />,
    )

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1))
    expect(fetch).toHaveBeenCalledWith(
      '/api/v1/layers?limit=100&offset=0',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )
    expect(fetch).not.toHaveBeenCalledWith(expect.stringContaining('/api/v1/tiles/flood-return-period?'), expect.anything())
    expect(onUnavailableReason).toHaveBeenLastCalledWith(expect.stringContaining('元数据正在加载'))
    expect(sourceProps).toHaveLength(0)
  })

  it('shows unavailable instead of unbounded GeoJSON when metadata is missing', async () => {
    sourceProps.length = 0
    layerProps.length = 0
    const onUnavailableReason = vi.fn()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(emptyLayerCatalogResponse()))

    render(
      <FloodReturnPeriodLayer
        runId="run-1"
        validTime="2026-05-03T06:00:00Z"
        onUnavailableReason={onUnavailableReason}
      />,
    )

    await waitFor(() => expect(onUnavailableReason).toHaveBeenLastCalledWith(expect.stringContaining('已阻止无边界 GeoJSON')))
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(fetch).not.toHaveBeenCalledWith(expect.stringContaining('/api/v1/tiles/flood-return-period?'), expect.anything())
    expect(sourceProps).toHaveLength(0)
    expect(layerProps).toHaveLength(0)
  })

  it('uses bounded GeoJSON fallback for degraded small bbox views when metadata is missing', async () => {
    sourceProps.length = 0
    layerProps.length = 0
    const onUnavailableReason = vi.fn()
    const featureCollection = {
      type: 'FeatureCollection',
      features: [
        {
          type: 'Feature',
          properties: {
            feature_id: 'rnv_v1::seg-1',
            river_network_version_id: 'rnv_v1',
            segment_id: 'seg-1',
            warning_level: 'watch',
          },
          geometry: { type: 'LineString', coordinates: [[100, 30], [100.1, 30.1]] },
        },
      ],
    }
    vi.stubGlobal(
      'fetch',
      vi.fn()
        .mockResolvedValueOnce(emptyLayerCatalogResponse())
        .mockResolvedValueOnce(geoJsonResponse(featureCollection)),
    )

    render(
      <FloodReturnPeriodLayer
        runId="run-small"
        validTime="2026-05-03T06:00:00Z"
        fallbackBbox={{ minLon: 100, minLat: 30, maxLon: 101, maxLat: 31 }}
        degradedFallback
        onUnavailableReason={onUnavailableReason}
      />,
    )

    await waitFor(() => expect(sourceProps.at(-1)).toMatchObject({ type: 'geojson' }))
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/api/v1/tiles/flood-return-period?'), expect.anything())
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining('bbox=100%2C30%2C101%2C31'), expect.anything())
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining('limit=500'), expect.anything())
    expect(sourceProps.at(-1)).toMatchObject({ data: featureCollection, promoteId: FLOOD_RETURN_PERIOD_FEATURE_ID_PROPERTY })
    expect(onUnavailableReason).toHaveBeenLastCalledWith(expect.stringContaining('bbox 限定的 GeoJSON 降级源'))
  })

  it('blocks degraded GeoJSON fallback when no bbox is supplied', async () => {
    sourceProps.length = 0
    layerProps.length = 0
    const onUnavailableReason = vi.fn()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(emptyLayerCatalogResponse()))

    render(
      <FloodReturnPeriodLayer
        runId="run-national"
        validTime="2026-05-03T06:00:00Z"
        degradedFallback
        onUnavailableReason={onUnavailableReason}
      />,
    )

    await waitFor(() => expect(onUnavailableReason).toHaveBeenLastCalledWith(expect.stringContaining('已阻止无边界 GeoJSON')))
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(fetch).not.toHaveBeenCalledWith(expect.stringContaining('/api/v1/tiles/flood-return-period?'), expect.anything())
    expect(sourceProps).toHaveLength(0)
  })

  it('keeps vector feature identity filters when metadata is discovered from the catalog', async () => {
    sourceProps.length = 0
    layerProps.length = 0
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(mvtLayerCatalogResponse()))

    render(
      <FloodReturnPeriodLayer
        runId="run-1"
        validTime="2026-05-03T06:00:00Z"
        hoveredFeatureId="rn-a::dup-seg"
        selectedFeatureId="rn-b::dup-seg"
      />,
    )

    await waitFor(() => expect(sourceProps.at(-1)).toMatchObject({ type: 'vector', promoteId: FLOOD_RETURN_PERIOD_FEATURE_ID_PROPERTY }))
    expect(layerProps.at(-2)).toMatchObject({
      filter: ['==', ['get', FLOOD_RETURN_PERIOD_FEATURE_ID_PROPERTY], 'rn-a::dup-seg'],
    })
    expect(layerProps.at(-1)).toMatchObject({
      filter: ['==', ['get', FLOOD_RETURN_PERIOD_FEATURE_ID_PROPERTY], 'rn-b::dup-seg'],
    })
  })

  it('does not render a broken layer when the endpoint is not frequency-ready', async () => {
    sourceProps.length = 0
    layerProps.length = 0
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValueOnce(emptyLayerCatalogResponse()),
    )

    render(<FloodReturnPeriodLayer runId="run-pending" validTime="2026-05-03T06:00:00Z" />)

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1))
    expect(sourceProps).toHaveLength(0)
    expect(layerProps).toHaveLength(0)
  })

  it('does not fetch malformed GeoJSON compatibility data when MVT metadata is unavailable', async () => {
    sourceProps.length = 0
    layerProps.length = 0
    const onUnavailableReason = vi.fn()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValueOnce(emptyLayerCatalogResponse()),
    )

    render(
      <FloodReturnPeriodLayer
        runId="run-malformed"
        validTime="2026-05-03T06:00:00Z"
        onUnavailableReason={onUnavailableReason}
      />,
    )

    await waitFor(() => expect(onUnavailableReason).toHaveBeenLastCalledWith(expect.stringContaining('已阻止无边界 GeoJSON')))
    expect(sourceProps).toHaveLength(0)
    expect(layerProps).toHaveLength(0)
  })

  it('surfaces scoped unavailable state from the flood-alert map when MVT metadata is unavailable', async () => {
    sourceProps.length = 0
    layerProps.length = 0
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValueOnce(emptyLayerCatalogResponse()),
    )

    render(
      <FloodAlertMap
        runId="run-oversized"
        validTime="2026-05-03T06:00:00Z"
        onSegmentSelect={vi.fn()}
      />,
    )

    await waitFor(() =>
      expect(screen.getByTestId('flood-return-period-unavailable')).toHaveTextContent('已阻止无边界 GeoJSON'),
    )
    expect(sourceProps).toHaveLength(0)
  })

  it('uses bounded GeoJSON fallback from the flood-alert map wrapper when MVT metadata is unavailable', async () => {
    sourceProps.length = 0
    layerProps.length = 0
    const featureCollection = {
      type: 'FeatureCollection',
      features: [
        {
          type: 'Feature',
          properties: {
            feature_id: 'rnv_v1::seg-1',
            river_network_version_id: 'rnv_v1',
            segment_id: 'seg-1',
            warning_level: 'watch',
          },
          geometry: { type: 'LineString', coordinates: [[100, 30], [100.1, 30.1]] },
        },
      ],
    }
    vi.stubGlobal(
      'fetch',
      vi.fn()
        .mockResolvedValueOnce(emptyLayerCatalogResponse())
        .mockResolvedValueOnce(geoJsonResponse(featureCollection)),
    )

    render(
      <FloodAlertMap
        runId="run-small"
        validTime="2026-05-03T06:00:00Z"
        fallbackBbox={{ minLon: 100, minLat: 30, maxLon: 101, maxLat: 31 }}
        degradedFallback
        onSegmentSelect={vi.fn()}
      />,
    )

    await waitFor(() =>
      expect(screen.getByTestId('flood-return-period-unavailable')).toHaveTextContent('bbox 限定的 GeoJSON 降级源'),
    )
    await waitFor(() => expect(sourceProps.at(-1)).toMatchObject({ type: 'geojson' }))
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/api/v1/tiles/flood-return-period?'), expect.anything())
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining('bbox=100%2C30%2C101%2C31'), expect.anything())
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining('limit=500'), expect.anything())
    expect(sourceProps.at(-1)).toMatchObject({ data: featureCollection })
  })

  it('registers flood-alert map vector source when MVT metadata is available', async () => {
    sourceProps.length = 0
    layerProps.length = 0
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(mvtLayerCatalogResponse()))

    render(
      <FloodAlertMap
        runId="run-vector"
        validTime="2026-05-03T06:00:00Z"
        onSegmentSelect={vi.fn()}
      />,
    )

    await waitFor(() => expect(sourceProps.at(-1)).toMatchObject({ type: 'vector' }))
    expect(fetch).not.toHaveBeenCalledWith(expect.stringContaining('/api/v1/tiles/flood-return-period?'), expect.anything())
  })
})
