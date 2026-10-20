import { render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { M11StationForcingPopup } from '@/components/map/M11StationForcingPopup'
import { fetchHydroMetLatestProduct } from '@/stores/hydroMetBootstrap'

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
  it('shows an empty state and draws no curve when station_id mismatches for both sources', async () => {
    mockSeriesBySource((source, query) => seriesResponseFor(source, { ...seriesOverridesFromQuery(query), station_id: 'OTHER' }))
    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />)

    expect(await screen.findByTestId('m11-station-popup-empty')).toHaveTextContent('station_id=OTHER')
    expect(screen.queryByTestId('mock-station-echarts')).not.toBeInTheDocument()
  })

  it('shows an empty state and draws no curve when model_id mismatches for both sources', async () => {
    mockSeriesBySource((source, query) => seriesResponseFor(source, { ...seriesOverridesFromQuery(query), model_id: 'other-model' }))
    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />)

    expect(await screen.findByTestId('m11-station-popup-empty')).toHaveTextContent('model_id=other-model')
    expect(screen.queryByTestId('mock-station-echarts')).not.toBeInTheDocument()
  })

  it('shows an empty state and draws no curve when cycle_time is missing for both sources', async () => {
    mockSeriesBySource((source, query) => {
      const { cycle_time: _omittedCycleTime, ...body } = seriesResponseFor(source, seriesOverridesFromQuery(query))
      return body
    })
    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />)

    expect(await screen.findByTestId('m11-station-popup-empty')).toHaveTextContent('cycle_time 元数据格式无效')
    expect(screen.queryByTestId('mock-station-echarts')).not.toBeInTheDocument()
  })

  it('shows honest empty state and never resolves a product when basinId=null', () => {
    render(<M11StationForcingPopup basinId={null} initialSource={null} station={station} />)

    expect(screen.getByTestId('m11-station-popup-no-product')).toHaveTextContent('请选择流域')
    expect(screen.queryByTestId('mock-station-echarts')).not.toBeInTheDocument()
    expect(vi.mocked(fetchHydroMetLatestProduct)).not.toHaveBeenCalled()
  })

  it('rejects the selected variable when any point is malformed/NaN for both sources', async () => {
    mockSeriesBySource((source, query) => {
      const body = seriesResponseFor(source, seriesOverridesFromQuery(query))
      prcpSeries(body).points = [
        { valid_time: '2026-05-21T06:00:00Z', value: 1.2, quality_flag: 'ok' },
        { valid_time: '2026-05-21T12:00:00Z', value: Number.NaN, quality_flag: 'ok' },
      ]
      return body
    })
    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />)

    expect(await screen.findByTestId('m11-station-popup-empty')).toHaveTextContent('value 不是有限数值')
    expect(screen.queryByTestId('m11-station-variable-PRCP-chart')).not.toBeInTheDocument()
    expect(screen.queryByTestId('mock-station-echarts')).not.toBeInTheDocument()
  })

  it('gates the selected variable when unit is null for both sources', async () => {
    mockSeriesBySource((source, query) => {
      const body = seriesResponseFor(source, seriesOverridesFromQuery(query))
      prcpSeries(body).unit = null
      return body
    })
    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />)

    expect(await screen.findByTestId('m11-station-popup-empty')).toHaveTextContent('缺少 unit 元数据')
    expect(screen.queryByTestId('m11-station-variable-PRCP-chart')).not.toBeInTheDocument()
    expect(screen.queryByTestId('mock-station-echarts')).not.toBeInTheDocument()
  })

  it('rejects the selected variable when metadata is malformed for both sources', async () => {
    mockSeriesBySource((source, query) => {
      const body = seriesResponseFor(source, seriesOverridesFromQuery(query))
      prcpSeries(body).metadata = { ...metadata(), returned_points: -1, returned_from: 'not-a-time' }
      return body
    })
    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />)

    expect(await screen.findByTestId('m11-station-popup-empty')).toHaveTextContent('metadata.returned_points')
    expect(screen.queryByTestId('m11-station-variable-PRCP-chart')).not.toBeInTheDocument()
    expect(screen.queryByTestId('mock-station-echarts')).not.toBeInTheDocument()
  })
})
