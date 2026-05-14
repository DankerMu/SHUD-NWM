import { render, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import {
  FloodReturnPeriodLayer,
  floodReturnPeriodLayer,
  floodTileUrl,
} from '@/components/flood/FloodReturnPeriodLayer'

const sourceProps: unknown[] = []
const layerProps: unknown[] = []

vi.mock('react-map-gl/maplibre', () => ({
  Source: ({ children, ...props }: { children: React.ReactNode }) => {
    sourceProps.push(props)
    return <div data-testid="source">{children}</div>
  },
  Layer: (props: Record<string, unknown>) => {
    layerProps.push(props)
    return <div data-testid="layer" />
  },
}))

describe('FloodReturnPeriodLayer', () => {
  it('uses the GeoJSON endpoint URL without z/x/y or pbf semantics', () => {
    const url = floodTileUrl('run 1', '2026-05-03T06:00:00Z')

    expect(url).toContain('/api/v1/tiles/flood-return-period?')
    expect(url).toContain('run_id=run+1')
    expect(url).toContain('duration=1h')
    expect(url).not.toContain('{z}')
    expect(url).not.toContain('.pbf')
  })

  it('configures a geojson source instead of a vector source', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: vi.fn().mockResolvedValue({ type: 'FeatureCollection', features: [] }),
      }),
    )

    render(<FloodReturnPeriodLayer runId="run-1" validTime="2026-05-03T06:00:00Z" />)

    await waitFor(() => expect(sourceProps.at(-1)).toMatchObject({ type: 'geojson' }))
    expect(sourceProps.at(-1)).not.toHaveProperty('tiles')
    expect(floodReturnPeriodLayer()).not.toHaveProperty('source-layer')
  })

  it('does not render a broken layer when the endpoint is not frequency-ready', async () => {
    sourceProps.length = 0
    layerProps.length = 0
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 409,
        json: vi.fn().mockResolvedValue({
          status: 'error',
          error: { code: 'FREQUENCY_NOT_COMPUTED', message: 'not ready' },
        }),
      }),
    )

    render(<FloodReturnPeriodLayer runId="run-pending" validTime="2026-05-03T06:00:00Z" />)

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1))
    expect(sourceProps).toHaveLength(0)
    expect(layerProps).toHaveLength(0)
  })
})
