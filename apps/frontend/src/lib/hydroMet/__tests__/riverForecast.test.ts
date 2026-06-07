import { describe, expect, it } from 'vitest'

import {
  HYDRO_MET_RIVER_FORECAST_VARIABLE,
  validateHydroMetRiverForecastForChart,
  type HydroMetRiverForecastPayload,
  type HydroMetRiverForecastProductIdentity,
  type HydroMetRiverForecastSegmentIdentity,
} from '@/lib/hydroMet/riverForecast'

/**
 * river q_down 诚实展示不变量的金标准直测：
 * shorter-horizon 标注 / 不补齐 padded 值 / discharge(q_down) 措辞而非水位。
 * 此前仅由 AppRoutes 玩具页页级测试覆盖（已随 HydroMetPage 删除），在 lib 层补回等价直测。
 */

const CYCLE = '2026-05-21T00:00:00Z'

function product(
  overrides: Partial<HydroMetRiverForecastProductIdentity> = {},
): HydroMetRiverForecastProductIdentity {
  return {
    basin_version_id: 'bv-1',
    river_network_version_id: 'rn-1',
    source_id: 'GFS',
    cycle_time: CYCLE,
    river_valid_time_start: CYCLE,
    river_valid_time_end: '2026-05-28T00:00:00Z',
    valid_time_start: CYCLE,
    valid_time_end: '2026-05-28T00:00:00Z',
    available_horizon_hours: 168,
    expected_horizon_hours: 168,
    shorter_horizon: false,
    ...overrides,
  }
}

const segment: HydroMetRiverForecastSegmentIdentity = {
  river_segment_id: 'seg-009',
  segment_id: 'seg-009',
  river_network_version_id: 'rn-1',
  basin_version_id: 'bv-1',
  name: 'Main Stem 009',
}

/** 在 cycle 之后第 hour 小时生成一个真实 q_down 点。 */
function pointAt(hour: number, value: number) {
  return {
    valid_time: new Date(Date.parse(CYCLE) + hour * 60 * 60 * 1000).toISOString(),
    value,
  }
}

function payload(
  points: Array<{ valid_time: string; value: number }>,
  seriesOverrides: Record<string, unknown> = {},
  topOverrides: Record<string, unknown> = {},
): HydroMetRiverForecastPayload {
  return {
    river_segment_id: 'seg-009',
    issue_time: CYCLE,
    variable: 'q_down',
    unit: 'm3/s',
    series: [
      {
        scenario_id: 'forecast_gfs_deterministic',
        source_id: 'GFS',
        cycle_time: CYCLE,
        points,
        ...seriesOverrides,
      },
    ],
    ...topOverrides,
  } as unknown as HydroMetRiverForecastPayload
}

describe('validateHydroMetRiverForecastForChart (gold standard honest display)', () => {
  it('annotates shorter-horizon and does NOT pad q_down to the expected horizon', () => {
    // 实际 lead 只到 144h，expected 168h：标注 shorter，且渲染点数 = 实际 series 点数（不补尾点）。
    const points = [pointAt(0, 3000), pointAt(72, 3120), pointAt(144, 3300)]
    const result = validateHydroMetRiverForecastForChart(
      payload(points, { available_lead_hours: 144 }),
      product({ available_horizon_hours: 144, shorter_horizon: true }),
      segment,
    )

    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect(result.horizonShorter).toBe(true)
    expect(result.horizonLabel).toContain('144h')
    expect(result.horizonLabel).toContain('168h')
    // 关键：不被 padding 到 expected 的小时步长，渲染点数严格等于真实 series 点数。
    expect(result.renderedPoints).toHaveLength(points.length)
    expect(result.renderedPoints[result.renderedPoints.length - 1].value).toBe(3300)
  })

  it('does not mislabel a full-length horizon as shorter', () => {
    const points = [pointAt(0, 3000), pointAt(84, 3150), pointAt(168, 3400)]
    const result = validateHydroMetRiverForecastForChart(
      payload(points, { available_lead_hours: 168 }),
      product({ available_horizon_hours: 168, shorter_horizon: false }),
      segment,
    )

    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect(result.horizonShorter).toBe(false)
    expect(result.horizonLabel).toContain('168h')
    expect(result.renderedPoints).toHaveLength(points.length)
  })

  it('uses q_down discharge identity and never water-level / stage wording', () => {
    const result = validateHydroMetRiverForecastForChart(
      payload([pointAt(0, 3000), pointAt(24, 3100)], { available_lead_hours: 168 }),
      product(),
      segment,
    )

    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect(result.variable).toBe('q_down')
    expect(HYDRO_MET_RIVER_FORECAST_VARIABLE).toBe('q_down')
    // discharge 单位，且 lib 输出中不含任何水位措辞字段/值。
    expect(result.unit).toBe('m3/s')
    const serialized = JSON.stringify(result).toLowerCase()
    expect(serialized).not.toContain('water level')
    expect(serialized).not.toContain('water-level')
    expect(serialized).not.toContain('stage')
    expect(serialized).not.toContain('水位')
  })

  it('rejects identity mismatch (segment id) and does not produce rendered points', () => {
    const result = validateHydroMetRiverForecastForChart(
      payload([pointAt(0, 3000)], { available_lead_hours: 168 }, { river_segment_id: 'seg-OTHER' }),
      product(),
      segment,
    )

    expect(result.ok).toBe(false)
    if (result.ok) return
    expect(result.messages.length).toBeGreaterThan(0)
    expect('renderedPoints' in result).toBe(false)
  })

  it('rejects non-finite / illegal-date points and does not chart a partial clean line', () => {
    const badPoints = [
      pointAt(0, 3000),
      { valid_time: 'not-a-time', value: 3100 },
      { valid_time: pointAt(24, 0).valid_time, value: Number.NaN },
    ]
    const result = validateHydroMetRiverForecastForChart(
      payload(badPoints, { available_lead_hours: 168 }),
      product(),
      segment,
    )

    expect(result.ok).toBe(false)
    if (result.ok) return
    expect(result.messages.length).toBeGreaterThan(0)
    expect('renderedPoints' in result).toBe(false)
  })
})
