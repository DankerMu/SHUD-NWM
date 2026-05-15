import { render, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import { TrendPanel } from '@/components/monitoring/TrendPanel'

vi.mock('@/api/client', () => ({
  client: {
    GET: vi.fn(),
  },
}))

vi.mock('@/components/charts/TrendLine', () => ({
  TrendLine: ({ title }: { title: string }) => <div>{title}</div>,
}))

function success<T>(data: T) {
  return { data: { status: 'success', data }, error: undefined }
}

describe('TrendPanel', () => {
  it('passes selected source and scenario to both metrics endpoints', async () => {
    vi.mocked(client.GET).mockResolvedValue(success([]) as never)

    render(<TrendPanel source="IFS" scenario="forecast_ifs_deterministic" />)

    await waitFor(() => expect(client.GET).toHaveBeenCalledTimes(2))
    expect(client.GET).toHaveBeenCalledWith('/api/v1/metrics/stage-duration', {
      params: { query: { days: 7, source: 'IFS', scenario: 'forecast_ifs_deterministic' } },
    })
    expect(client.GET).toHaveBeenCalledWith('/api/v1/metrics/success-rate', {
      params: { query: { days: 7, source: 'IFS', scenario: 'forecast_ifs_deterministic' } },
    })
  })
})
