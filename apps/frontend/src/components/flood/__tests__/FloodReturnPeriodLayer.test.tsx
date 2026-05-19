import { render, screen, waitFor } from '@testing-library/react'
import { forwardRef, useImperativeHandle, type ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { FloodAlertMap } from '@/components/flood/FloodAlertMap'
import {
  FLOOD_RETURN_PERIOD_FEATURE_ID_PROPERTY,
  FloodReturnPeriodLayer,
  floodReturnPeriodLayer,
  floodTileUrl,
} from '@/components/flood/FloodReturnPeriodLayer'

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

function oversizedStreamResponse(maxBytes: number) {
  return new Response(
    new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('x'.repeat(maxBytes + 1)))
        controller.close()
      },
    }),
    { headers: { 'content-type': 'application/json' } },
  )
}

vi.mock('react-map-gl/maplibre', () => ({
  default: forwardRef(({ children }: { children: ReactNode }, ref) => {
    useImperativeHandle(ref, () => ({ flyTo: vi.fn() }))
    return <div data-testid="map">{children}</div>
  }),
  Source: ({ children, ...props }: { children: ReactNode }) => {
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
  it('uses the GeoJSON endpoint URL without z/x/y or pbf semantics', () => {
    const url = floodTileUrl('run 1', '2026-05-03T06:00:00Z')

    expect(url).toContain('https://api.example.test/api/v1/tiles/flood-return-period?')
    expect(url).toContain('run_id=run+1')
    expect(url).toContain('duration=1h')
    expect(url).toContain('limit=10000')
    expect(url).not.toContain('{z}')
    expect(url).not.toContain('.pbf')
  })

  it('routes flood tile fetches through the configured API base', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(geoJsonResponse({ type: 'FeatureCollection', features: [] })),
    )

    render(<FloodReturnPeriodLayer runId="run-1" validTime="2026-05-03T06:00:00Z" />)

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1))
    expect(fetch).toHaveBeenCalledWith(
      'https://api.example.test/api/v1/tiles/flood-return-period?run_id=run-1&duration=1h&valid_time=2026-05-03T06%3A00%3A00Z&limit=10000',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )
  })

  it('configures a geojson source instead of a vector source', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(geoJsonResponse({ type: 'FeatureCollection', features: [] })),
    )

    render(<FloodReturnPeriodLayer runId="run-1" validTime="2026-05-03T06:00:00Z" />)

    await waitFor(() => expect(sourceProps.at(-1)).toMatchObject({ type: 'geojson' }))
    expect(sourceProps.at(-1)).not.toHaveProperty('tiles')
    expect(floodReturnPeriodLayer()).not.toHaveProperty('source-layer')
  })

  it('promotes and filters flood features by river-network scoped feature identity', async () => {
    sourceProps.length = 0
    layerProps.length = 0
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        geoJsonResponse({
          type: 'FeatureCollection',
          features: [
            {
              type: 'Feature',
              properties: {
                segment_id: 'dup-seg',
                river_network_version_id: 'rn-a',
              },
              geometry: { type: 'LineString', coordinates: [[110, 30], [111, 31]] },
            },
            {
              type: 'Feature',
              properties: {
                segment_id: 'dup-seg',
                river_network_version_id: 'rn-b',
              },
              geometry: { type: 'LineString', coordinates: [[112, 32], [113, 33]] },
            },
          ],
        }),
      ),
    )

    render(
      <FloodReturnPeriodLayer
        runId="run-1"
        validTime="2026-05-03T06:00:00Z"
        hoveredFeatureId="rn-a::dup-seg"
        selectedFeatureId="rn-b::dup-seg"
      />,
    )

    await waitFor(() => expect(sourceProps.at(-1)).toMatchObject({ promoteId: FLOOD_RETURN_PERIOD_FEATURE_ID_PROPERTY }))
    expect(sourceProps.at(-1)).toMatchObject({
      data: {
        features: [
          { properties: { feature_id: 'rn-a::dup-seg' } },
          { properties: { feature_id: 'rn-b::dup-seg' } },
        ],
      },
    })
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
      vi.fn().mockResolvedValue({
        ok: false,
        status: 409,
        headers: new Headers(),
        text: vi.fn().mockResolvedValue(
          JSON.stringify({
            status: 'error',
            error: { code: 'FREQUENCY_NOT_COMPUTED', message: 'not ready' },
          }),
        ),
      }),
    )

    render(<FloodReturnPeriodLayer runId="run-pending" validTime="2026-05-03T06:00:00Z" />)

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1))
    expect(sourceProps).toHaveLength(0)
    expect(layerProps).toHaveLength(0)
  })

  it('rejects malformed FeatureCollections before retaining a MapLibre source', async () => {
    sourceProps.length = 0
    layerProps.length = 0
    const onUnavailableReason = vi.fn()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        geoJsonResponse({
          type: 'FeatureCollection',
          features: [{ type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates: [] } }],
        }),
      ),
    )

    render(
      <FloodReturnPeriodLayer
        runId="run-malformed"
        validTime="2026-05-03T06:00:00Z"
        onUnavailableReason={onUnavailableReason}
      />,
    )

    await waitFor(() => expect(onUnavailableReason).toHaveBeenLastCalledWith(expect.stringContaining('空坐标几何')))
    expect(sourceProps).toHaveLength(0)
    expect(layerProps).toHaveLength(0)
  })

  it('surfaces scoped unavailable state from the flood-alert map when return-period payloads exceed budget', async () => {
    sourceProps.length = 0
    layerProps.length = 0
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        geoJsonResponse({
          type: 'FeatureCollection',
          features: new Array(10_001).fill({ type: 'Feature', properties: {}, geometry: null }),
        }),
      ),
    )

    render(
      <FloodAlertMap
        runId="run-oversized"
        validTime="2026-05-03T06:00:00Z"
        onSegmentSelect={vi.fn()}
      />,
    )

    expect(await screen.findByTestId('flood-return-period-unavailable')).toHaveTextContent('超过客户端要素预算')
    expect(sourceProps).toHaveLength(0)
  })

  it('surfaces scoped unavailable state for oversized no-content-length streams on the flood-alert map', async () => {
    sourceProps.length = 0
    layerProps.length = 0
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(oversizedStreamResponse(2_000_000)))

    render(
      <FloodAlertMap
        runId="run-stream-oversized"
        validTime="2026-05-03T06:00:00Z"
        onSegmentSelect={vi.fn()}
      />,
    )

    expect(await screen.findByTestId('flood-return-period-unavailable')).toHaveTextContent('超过客户端序列化预算')
    expect(sourceProps).toHaveLength(0)
    expect(layerProps).toHaveLength(0)
  })
})
