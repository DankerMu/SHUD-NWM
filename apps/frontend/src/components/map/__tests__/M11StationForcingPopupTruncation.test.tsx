import { render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { M11StationForcingPopup } from '@/components/map/M11StationForcingPopup'

import {
  station,
  metadata,
  seriesOverridesFromQuery,
  seriesResponseFor,
  mockLatestProducts,
  mockSeriesBySource,
  prcpSeries,
  originalViewport,
  setViewport,
} from './m11StationForcingPopupFixtures'

vi.mock('@/api/client', () => ({
  client: { GET: vi.fn() },
}))

vi.mock('@/stores/hydroMetBootstrap', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/stores/hydroMetBootstrap')>()
  return { ...actual, fetchHydroMetLatestProduct: vi.fn() }
})

vi.mock('echarts-for-react/lib/core', () => ({
  default: ({ option }: { option: unknown }) => <pre data-testid="mock-station-echarts">{JSON.stringify(option)}</pre>,
}))
vi.mock('@/components/charts/echartsCore', () => ({ echarts: {} }))

beforeEach(() => {
  vi.clearAllMocks()
  setViewport(originalViewport.width, originalViewport.height)
  mockLatestProducts()
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('M11StationForcingPopup', () => {
  it('renders echarts and discloses source-specific truncation/cap badges when series is truncated', async () => {
    mockSeriesBySource((source, query) => {
      const body = seriesResponseFor(source, seriesOverridesFromQuery(query))
      const series = prcpSeries(body)
      series.truncated = true
      series.metadata = { ...metadata(), returned_points: 1000, truncated: true }
      return body
    })
    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />)

    expect(await screen.findByTestId('m11-station-popup-loaded')).toBeInTheDocument()
    expect(screen.getByTestId('m11-station-variable-PRCP-chart')).toBeInTheDocument()
    expect(screen.getByTestId('m11-station-variable-PRCP-GFS-truncated')).toBeInTheDocument()
    expect(screen.getByTestId('m11-station-variable-PRCP-GFS-capped')).toBeInTheDocument()
    expect(screen.getByTestId('m11-station-variable-PRCP-IFS-truncated')).toBeInTheDocument()
    expect(screen.getByTestId('m11-station-variable-PRCP-IFS-capped')).toBeInTheDocument()
  })

  it('locally caps long selected-variable series without marking API truncation when metadata is complete', async () => {
    mockSeriesBySource((source, query) => {
      const body = seriesResponseFor(source, seriesOverridesFromQuery(query))
      const points = Array.from({ length: 280 }, (_, index) => ({
        valid_time: new Date(Date.UTC(2026, 4, 21, index)).toISOString(),
        value: source === 'GFS' ? index : index + 0.5,
        quality_flag: 'ok',
      }))
      const series = prcpSeries(body)
      series.points = points
      series.truncated = false
      series.metadata = {
        ...metadata(),
        returned_points: points.length,
        returned_from: points[0].valid_time,
        returned_to: points[points.length - 1].valid_time,
        truncated: false,
      }
      return body
    })
    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />)

    expect(await screen.findByTestId('m11-station-popup-loaded')).toBeInTheDocument()
    expect(screen.getByTestId('m11-station-variable-PRCP-GFS-capped')).toBeInTheDocument()
    expect(screen.getByTestId('m11-station-variable-PRCP-IFS-capped')).toBeInTheDocument()
    expect(screen.queryByTestId('m11-station-variable-PRCP-GFS-truncated')).not.toBeInTheDocument()
    expect(screen.queryByTestId('m11-station-variable-PRCP-IFS-truncated')).not.toBeInTheDocument()
  })
})
