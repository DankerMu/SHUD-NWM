import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import { useHydroMetProduct } from '@/pages/m11/useHydroMetProduct'
import { useHydroMetProductDataStore } from '@/stores/hydroMetProductData'

vi.mock('@/api/client', () => ({
  client: { GET: vi.fn() },
}))

function success<T>(data: T) {
  return { status: 'success', data }
}

function latestProduct(overrides: Record<string, unknown> = {}) {
  return {
    basin_id: 'basins_qhh',
    model_id: 'm-1',
    basin_version_id: 'bv-1',
    river_network_version_id: 'rn-1',
    source_id: 'GFS',
    cycle_time: '2026-05-21T00:00:00Z',
    run_id: 'run-1',
    forcing_version_id: 'forc-1',
    station_count: 10,
    expected_station_count: 10,
    segment_count: 20,
    expected_segment_count: 20,
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
      return_period_reasons: [],
    },
    quality: {
      station_sample_count: 1,
      river_sample_count: 1,
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

beforeEach(() => {
  vi.clearAllMocks()
  useHydroMetProductDataStore.getState().clear()
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('useHydroMetProduct', () => {
  it('resolves product when basinId + concrete GFS/IFS source are present', async () => {
    vi.mocked(client.GET).mockResolvedValue({ data: success(latestProduct()), error: undefined } as never)

    const { result } = renderHook(() =>
      useHydroMetProduct({ basinId: 'basins_qhh', resolvedSource: 'GFS', cycle: null }),
    )

    await waitFor(() => expect(result.current.product).not.toBeNull())
    expect(result.current.product?.basin_version_id).toBe('bv-1')
    expect(result.current.reason).toBeNull()
  })

  it('does NOT fetch when source is unresolved (best); product=null + honest reason', async () => {
    const { result } = renderHook(() =>
      useHydroMetProduct({ basinId: 'basins_qhh', resolvedSource: 'Unknown', cycle: null }),
    )

    expect(result.current.product).toBeNull()
    expect(result.current.reason).toBe('等待 Best Available 解析')
    expect(vi.mocked(client.GET)).not.toHaveBeenCalled()
  })

  it('does NOT fetch when basinId is absent; product=null + 请选择流域', async () => {
    const { result } = renderHook(() =>
      useHydroMetProduct({ basinId: null, resolvedSource: 'GFS', cycle: null }),
    )

    expect(result.current.product).toBeNull()
    expect(result.current.reason).toBe('请选择流域')
    expect(vi.mocked(client.GET)).not.toHaveBeenCalled()
  })

  it('dedupes concurrent same-key requests via in-flight cache', async () => {
    vi.mocked(client.GET).mockResolvedValue({ data: success(latestProduct()), error: undefined } as never)
    const store = useHydroMetProductDataStore.getState()

    await act(async () => {
      await Promise.all([
        store.loadProduct({ basinId: 'basins_qhh', resolvedSource: 'GFS', cycle: null }),
        store.loadProduct({ basinId: 'basins_qhh', resolvedSource: 'GFS', cycle: null }),
      ])
    })

    // latest-product 只打一次（bootstrap 内部仅 latest-product GET，无 basin → 不取站点/河段）。
    const latestCalls = vi.mocked(client.GET).mock.calls.filter(([path]) => path === '/api/v1/mvp/qhh/latest-product')
    expect(latestCalls).toHaveLength(1)
  })
})
