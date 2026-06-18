import { describe, expect, it } from 'vitest'

import type { components } from '@/api/types'

type FloodReturnPeriodProductQuality = components['schemas']['FloodReturnPeriodProductQuality']
type QhhLatestProductQuality = components['schemas']['QhhLatestProductQuality']
type HydroRun = components['schemas']['HydroRun']
type FloodReturnPeriodFeatureCollection = components['schemas']['FloodReturnPeriodFeatureCollection']

const readyQuality = {
  quality_state: 'ready',
  quality_source: 'explicit',
  max_over_window: false,
  result_rows: 3,
  return_period_rows: 3,
  warning_rows: 3,
  expected_result_rows: 3,
  expected_max_result_rows: 0,
  expected_timestep_result_rows: 3,
  meaningful_result_rows: 3,
  meaningful_max_result_rows: 0,
  meaningful_timestep_result_rows: 3,
  no_frequency_curve_rows: 0,
  no_usable_frequency_curve_rows: 0,
  warning_threshold_unavailable_rows: 0,
  unavailable_products: [],
  residual_blockers: [],
} satisfies FloodReturnPeriodProductQuality

describe('generated flood product quality API types', () => {
  it('expose flood_return_period quality fields and counters as typed properties', () => {
    const productQuality = {
      flood_return_period: readyQuality,
    } satisfies QhhLatestProductQuality

    const hydroRunProductQuality: NonNullable<HydroRun['product_quality']> = productQuality
    const collectionProductQuality: NonNullable<FloodReturnPeriodFeatureCollection['product_quality']> = readyQuality

    const state: components['schemas']['FloodReturnPeriodQualityState'] =
      hydroRunProductQuality.flood_return_period.quality_state
    const expectedRows: number = hydroRunProductQuality.flood_return_period.expected_result_rows
    const meaningfulRows: number = collectionProductQuality.meaningful_result_rows
    const noCurveRows: number = collectionProductQuality.no_frequency_curve_rows
    const noUsableRows: number = collectionProductQuality.no_usable_frequency_curve_rows

    expect(state).toBe('ready')
    expect(expectedRows).toBe(3)
    expect(meaningfulRows).toBe(3)
    expect(noCurveRows).toBe(0)
    expect(noUsableRows).toBe(0)
  })
})
