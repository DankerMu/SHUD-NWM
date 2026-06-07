import { render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import type { QhhLatestProduct } from '@/pages/hydroMet/bootstrap'
import {
  ProductStatusBar,
  RETURN_PERIOD_LEGEND,
  ReturnPeriodLegend,
  ReturnPeriodSection,
} from '@/components/m11/ReturnPeriodSection'

vi.mock('@/api/client', () => ({
  client: {
    GET: vi.fn(),
  },
}))

function product(overrides: Partial<QhhLatestProduct> = {}): QhhLatestProduct {
  return {
    basin_id: 'basins_qhh',
    model_id: 'basins_qhh_shud',
    basin_version_id: 'basins_qhh_vbasins',
    river_network_version_id: 'basins_qhh_rivnet_vbasins',
    source_id: 'GFS',
    cycle_time: '2026-05-21T00:00:00Z',
    run_id: 'qhh_gfs_2026052100_smoke',
    forcing_version_id: 'forc_gfs_2026052100_basins_qhh_shud',
    station_count: 386,
    expected_station_count: 386,
    segment_count: 1633,
    expected_segment_count: 1633,
    status: 'ready',
    run_status: 'frequency_done',
    valid_time_start: '2026-05-21T00:00:00Z',
    valid_time_end: '2026-05-28T00:00:00Z',
    river_valid_time_start: '2026-05-21T00:00:00Z',
    river_valid_time_end: '2026-05-28T00:00:00Z',
    forcing_valid_time_start: '2026-05-21T00:00:00Z',
    forcing_valid_time_end: '2026-05-28T00:00:00Z',
    available_horizon_hours: 168,
    expected_horizon_hours: 168,
    shorter_horizon: false,
    availability: {
      ready: true,
      unavailable_reasons: [],
      quality_flags: [],
      quality_notes: [],
      return_period_status: 'unavailable',
      return_period_reasons: [
        { code: 'RETURN_PERIOD_RESULT_UNAVAILABLE', message: 'no peak rows' },
      ],
    },
    quality: {
      station_sample_count: 10,
      river_sample_count: 10,
      required_station_variables: ['PRCP', 'TEMP', 'RH', 'wind', 'Rn', 'Press'],
      station_variable_coverage: [],
      candidate_limit: 20,
      search_limit: 20,
      context_limit: 20,
      query_indexes: [],
    },
    ...overrides,
  }
}

const LEGEND_LEVELS = ['2y', '5y', '10y', '20y', '50y', '100y']

describe('ReturnPeriodSection (#316)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })
  afterEach(() => {
    vi.clearAllMocks()
  })

  it('shows the honest unavailable placeholder + full static legend when return_period_status=unavailable', () => {
    render(<ReturnPeriodSection product={product()} />)

    const unavailable = screen.getByTestId('hydro-met-return-period-unavailable')
    expect(unavailable).toHaveTextContent('暂未发布正式产品')

    const legend = screen.getByTestId('hydro-met-return-period-legend')
    LEGEND_LEVELS.forEach((level) => {
      expect(legend.querySelector(`[data-level="${level}"]`)).not.toBeNull()
    })
    expect(screen.getAllByTestId('hydro-met-return-period-legend-item')).toHaveLength(LEGEND_LEVELS.length)
  })

  it('never renders fake return-period product rows or a "published" claim when unavailable', () => {
    const { container } = render(<ReturnPeriodSection product={product()} />)

    // 红线：无真实产品时绝不出现"正式产品已发布"类文案（"暂未发布" 是诚实文案，不违规）。
    expect(container.textContent).not.toMatch(/已发布/)
    expect(container.textContent).not.toMatch(/正式产品已发布|正式洪水重现期产品已发布/)
    expect(container.textContent).toMatch(/暂未发布/)
    // 红线：不渲染任何河段重现期产品数据（只有静态图例 item，没有产品河段行）。
    expect(container.querySelector('[data-testid="hydro-met-return-period-product-row"]')).toBeNull()
    expect(container.querySelector('[data-testid="hydro-met-return-period-river"]')).toBeNull()
  })

  it('does NOT call any flood-return-period preview/status endpoint while rendering', () => {
    render(<ReturnPeriodSection product={product()} />)
    render(<ProductStatusBar product={product()} />)

    const calls = vi.mocked(client.GET).mock.calls
    const calledPaths = calls.map(([path]) => String(path))
    expect(calledPaths.some((path) => path.includes('flood-return-period/preview'))).toBe(false)
    expect(calledPaths.some((path) => path.includes('flood-return-period/status'))).toBe(false)
    expect(calledPaths.some((path) => path.includes('flood-return-period'))).toBe(false)
    // 整个区块不依赖任何产品数据接口。
    expect(client.GET).not.toHaveBeenCalled()
  })

  it('renders the static legend even with no product context (pure domain knowledge)', () => {
    render(<ReturnPeriodLegend />)

    const legend = screen.getByTestId('hydro-met-return-period-legend')
    LEGEND_LEVELS.forEach((level) => {
      expect(legend.querySelector(`[data-level="${level}"]`)).not.toBeNull()
    })
    expect(client.GET).not.toHaveBeenCalled()
    expect(RETURN_PERIOD_LEGEND.map((entry) => entry.level)).toEqual(LEGEND_LEVELS)
  })
})

describe('ProductStatusBar (#316)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders ready tone for q_down/forcing on a healthy product', () => {
    render(<ProductStatusBar product={product({ availability: { ...product().availability, return_period_status: 'ready', return_period_reasons: [] } })} />)

    expect(screen.getByTestId('hydro-met-status-forcing')).toHaveAttribute('data-tone', 'ready')
    expect(screen.getByTestId('hydro-met-status-q_down')).toHaveAttribute('data-tone', 'ready')
    expect(screen.getByTestId('hydro-met-status-return_period')).toHaveAttribute('data-tone', 'ready')
  })

  it('renders forcing degraded tone when shorter_horizon is set', () => {
    render(<ProductStatusBar product={product({ shorter_horizon: true })} />)

    expect(screen.getByTestId('hydro-met-status-forcing')).toHaveAttribute('data-tone', 'degraded')
  })

  it('keeps return_period independent of overall product ready (ready product, unavailable return period)', () => {
    // 产品整体 ready，但 return_period_status=unavailable —— 两者必须解耦。
    render(<ProductStatusBar product={product({ status: 'ready' })} />)

    expect(screen.getByTestId('hydro-met-status-q_down')).toHaveAttribute('data-tone', 'ready')
    expect(screen.getByTestId('hydro-met-status-forcing')).toHaveAttribute('data-tone', 'ready')
    expect(screen.getByTestId('hydro-met-status-return_period')).toHaveAttribute('data-tone', 'unavailable')
  })

  it('does not fetch any endpoint to render the status bar', () => {
    render(<ProductStatusBar product={product()} />)
    expect(client.GET).not.toHaveBeenCalled()
  })

  it('does not mark cells ready when an unknown unavailable reason code is present (M-2)', () => {
    // 未被任何桶（FORCING_ / RIVER|Q_DOWN|RUN_STATUS）认领的 reason code 不得静默落入绿色 ready。
    render(
      <ProductStatusBar
        product={product({
          availability: {
            ...product().availability,
            ready: true,
            unavailable_reasons: [{ code: 'STRICT_IDENTITY_X', message: 'unknown reason' }],
          },
        })}
      />,
    )

    const qDown = screen.getByTestId('hydro-met-status-q_down')
    expect(qDown).not.toHaveAttribute('data-tone', 'ready')
    expect(qDown).toHaveAttribute('data-tone', 'unavailable')
    // The unknown reason code is surfaced honestly in the cell's detail (title) attribute.
    expect(qDown.getAttribute('title')).toContain('STRICT_IDENTITY_X')
  })

  it('does not mark forcing/q_down ready when overall availability.ready is false (M-2)', () => {
    render(
      <ProductStatusBar
        product={product({
          availability: { ...product().availability, ready: false, unavailable_reasons: [] },
        })}
      />,
    )

    expect(screen.getByTestId('hydro-met-status-forcing')).not.toHaveAttribute('data-tone', 'ready')
    expect(screen.getByTestId('hydro-met-status-q_down')).not.toHaveAttribute('data-tone', 'ready')
  })

  it('does not mark return_period ready when overall availability.ready is false (M-25 round 2)', () => {
    const unavailableProduct = product({
      availability: {
        ...product().availability,
        ready: false,
        unavailable_reasons: [],
        return_period_status: 'ready',
        return_period_reasons: [],
      },
    })

    render(
      <>
        <ProductStatusBar product={unavailableProduct} />
        <ReturnPeriodSection product={unavailableProduct} />
      </>,
    )

    expect(screen.getByTestId('hydro-met-status-return_period')).toHaveAttribute('data-tone', 'unavailable')
    expect(screen.getByTestId('hydro-met-return-period-unavailable')).toBeInTheDocument()
  })
})
