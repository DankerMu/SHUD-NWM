import { describe, expect, it, vi } from 'vitest'

import {
  DataShapeError,
  isDataShapeError,
  validateBasins,
  validateDischargeCycles,
  validateLayers,
  validateLayerValidTimes,
  validatePrecipIndex,
  validateRunsPage,
} from '@/lib/m11/overviewShapeGuards'
import {
  DEFAULT_CYCLE,
  OTHER_CYCLE,
  basin,
  layer,
  precipIndex,
  precipLayer,
  run,
} from '@/test/overviewDataFixture'

// accept 用例直接复用共享 fixture 的真实载荷；fixture 模块依赖 api client，这里把它 mock 掉。
vi.mock('@/api/client', () => ({
  client: { GET: vi.fn() },
}))

type Case = { name: string; value: unknown }

function expectShapeError(validate: (value: unknown) => unknown, value: unknown, label: string) {
  let caught: unknown
  try {
    validate(value)
  } catch (error) {
    caught = error
  }
  expect(caught).toBeInstanceOf(DataShapeError)
  expect(isDataShapeError(caught)).toBe(true)
  expect((caught as DataShapeError).label).toBe(label)
  expect((caught as DataShapeError).message).toBe(`${label}: 数据异常`)
}

const rows: Array<{
  endpoint: string
  label: string
  validate: (value: unknown) => unknown
  accept: Case[]
  reject: Case[]
}> = [
  {
    endpoint: '/api/v1/layers',
    label: '图层目录',
    validate: validateLayers,
    accept: [
      { name: 'fixture discharge + precip catalog', value: [layer, precipLayer] },
      { name: 'empty catalog', value: [] },
      { name: 'metadata null', value: [{ layer_id: 'discharge', metadata: null }] },
      { name: 'metadata absent', value: [{ layer_id: 'discharge' }] },
      {
        name: 'fail-closed catalog metadata',
        value: [{ ...layer, metadata: { ...layer.metadata, default_cycle: null, valid_times: [] } }],
      },
    ],
    reject: [
      { name: 'wrong container (object)', value: { items: [layer] } },
      { name: 'wrong container (null)', value: null },
      { name: 'null element', value: [null] },
      { name: 'wrong element type (string array)', value: ['discharge'] },
      { name: 'missing layer_id', value: [{ layer_name: 'Discharge' }] },
      { name: 'metadata not an object', value: [{ layer_id: 'discharge', metadata: 'x' }] },
    ],
  },
  {
    endpoint: '/api/v1/layers/discharge/cycles',
    label: '起报时次',
    validate: validateDischargeCycles,
    accept: [
      {
        name: 'fixture gfs cycles',
        value: {
          source: 'gfs',
          cycles: [
            { cycle_time: DEFAULT_CYCLE, valid_time_start: DEFAULT_CYCLE, valid_time_end: '2026-05-18T06:00:00Z' },
            { cycle_time: OTHER_CYCLE, valid_time_start: OTHER_CYCLE, valid_time_end: '2026-05-17T18:00:00Z' },
          ],
          default_cycle: DEFAULT_CYCLE,
        },
      },
      { name: 'entry with only cycle_time', value: { source: 'gfs', cycles: [{ cycle_time: OTHER_CYCLE }], default_cycle: DEFAULT_CYCLE } },
      { name: 'fail-closed empty list', value: { source: 'ifs', cycles: [], default_cycle: null } },
      { name: 'default_cycle absent', value: { cycles: [] } },
    ],
    reject: [
      { name: 'wrong container (array)', value: [{ cycle_time: DEFAULT_CYCLE }] },
      { name: 'cycles null', value: { source: 'gfs', cycles: null, default_cycle: DEFAULT_CYCLE } },
      { name: 'null element', value: { cycles: [null, { cycle_time: DEFAULT_CYCLE }], default_cycle: DEFAULT_CYCLE } },
      { name: 'wrong element type (string array)', value: { cycles: [DEFAULT_CYCLE], default_cycle: DEFAULT_CYCLE } },
      { name: 'missing cycle_time', value: { cycles: [{ valid_time_start: DEFAULT_CYCLE }], default_cycle: DEFAULT_CYCLE } },
      { name: 'default_cycle not a string', value: { cycles: [], default_cycle: 42 } },
    ],
  },
  {
    endpoint: '/api/v1/layers/{layer_id}/valid-times',
    label: '有效时次',
    validate: validateLayerValidTimes,
    accept: [
      { name: 'object form (fixture)', value: { layer_id: 'discharge', valid_times: [OTHER_CYCLE, '2026-05-17T18:00:00Z'] } },
      { name: 'object form empty list', value: { layer_id: 'discharge', valid_times: [] } },
      { name: 'string array form', value: ['2026-05-18T06:00:00Z'] },
    ],
    reject: [
      { name: 'wrong container (null)', value: null },
      { name: 'object without valid_times', value: { layer_id: 'discharge' } },
      { name: 'null element', value: { valid_times: [null] } },
      { name: 'null element in array form', value: [null] },
      { name: 'wrong element type', value: { valid_times: [1, 2] } },
    ],
  },
  {
    endpoint: '/api/v1/precip/{source}/{cycle}/index',
    label: '降水索引',
    validate: validatePrecipIndex,
    accept: [{ name: 'fixture precip index', value: precipIndex }],
    reject: [
      { name: 'wrong container (array)', value: [precipIndex] },
      { name: 'bounds of 3', value: { ...precipIndex, bounds: [1, 2, 3] } },
      { name: 'bounds missing', value: { ...precipIndex, bounds: undefined } },
      { name: 'bounds non-finite', value: { ...precipIndex, bounds: [73, 18, Number.NaN, 54] } },
      { name: 'bounds wrong element type', value: { ...precipIndex, bounds: ['73', '18', '135', '54'] } },
      { name: 'valid_times null element', value: { ...precipIndex, valid_times: [null] } },
      { name: 'valid_times not an array', value: { ...precipIndex, valid_times: 'x' } },
    ],
  },
  {
    endpoint: '/api/v1/basins',
    label: '流域清单',
    validate: validateBasins,
    accept: [
      { name: 'fixture basin list', value: [basin] },
      { name: 'empty list', value: [] },
    ],
    reject: [
      { name: 'wrong container (object)', value: { items: [basin] } },
      { name: 'null element', value: [null] },
      { name: 'wrong element type (string array)', value: ['basin-demo'] },
      { name: 'missing basin_id', value: [{ basin_name: 'Demo Basin' }] },
    ],
  },
  {
    endpoint: '/api/v1/runs',
    label: '运行记录',
    validate: validateRunsPage,
    accept: [
      { name: 'fixture runs page', value: { items: [run], total: 1, limit: 20, offset: 0 } },
      { name: 'empty page', value: { items: [], total: 0, limit: 20, offset: 0 } },
    ],
    reject: [
      { name: 'wrong container (array)', value: [run] },
      { name: 'items not an array', value: { items: 'x', total: 0, limit: 20, offset: 0 } },
      { name: 'null element', value: { items: [null], total: 1, limit: 20, offset: 0 } },
      { name: 'wrong element type (string array)', value: { items: ['run-001'], total: 1, limit: 20, offset: 0 } },
      { name: 'missing run_id', value: { items: [{ status: 'published' }], total: 1, limit: 20, offset: 0 } },
    ],
  },
]

describe.each(rows)('$endpoint shape guard', ({ label, validate, accept, reject }) => {
  it.each(accept)('accepts $name and returns the value', ({ value }) => {
    expect(validate(value)).toBe(value)
  })

  it.each(reject)(`rejects $name with DataShapeError(${label})`, ({ value }) => {
    expectShapeError(validate, value, label)
  })
})

describe('isDataShapeError', () => {
  it('is true only for DataShapeError', () => {
    expect(isDataShapeError(new DataShapeError('起报时次'))).toBe(true)
    expect(isDataShapeError(new Error('起报时次: 数据异常'))).toBe(false)
    expect(isDataShapeError({ label: '起报时次', message: '起报时次: 数据异常' })).toBe(false)
    expect(isDataShapeError(null)).toBe(false)
    expect(isDataShapeError(undefined)).toBe(false)
  })
})
