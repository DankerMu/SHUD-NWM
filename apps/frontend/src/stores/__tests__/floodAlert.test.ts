import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useFloodAlertStore } from '@/stores/floodAlert'

const apiBase = 'https://api.example.test'

vi.mock('@/api/base', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/base')>()
  return {
    ...actual,
    buildApiUrl: (path: string) => actual.buildApiUrl(path, apiBase),
  }
})

vi.mock('@/api/client', () => ({
  client: {
    GET: vi.fn(),
  },
}))

function success<T>(data: T) {
  return { status: 'success', data }
}

describe('useFloodAlertStore', () => {
  beforeEach(() => {
    useFloodAlertStore.setState(
      {
        ...useFloodAlertStore.getInitialState(),
        selectedRunId: 'run-1',
      },
      true,
    )
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: vi.fn().mockResolvedValue(
          success({
            run_id: 'run-1',
            segment_id: 'seg-1',
            river_segment_id: 'seg-1',
            timesteps: [
              {
                valid_time: '2026-05-12T00:00:00Z',
                return_period: 20,
                warning_level: 'warning',
                q_value: 1234,
              },
            ],
            timeline: [],
            peak: {
              valid_time: '2026-05-12T00:00:00Z',
              return_period: 20,
              warning_level: 'warning',
              q_value: 1234,
            },
            frequency_thresholds: {
              Q2: 100,
              Q5: 200,
              Q10: 300,
              Q20: 400,
              Q50: 500,
              Q100: 600,
              sample_quality: { count: 30 },
            },
            quality_note: null,
          }),
        ),
      }),
    )
  })

  it('loads timeline through the configured API base and derives convenience thresholds from API fields', async () => {
    await useFloodAlertStore.getState().fetchTimeline('seg-1')

    expect(fetch).toHaveBeenCalledWith(
      `${apiBase}/api/v1/flood-alerts/timeline?run_id=run-1&segment_id=seg-1`,
    )
    expect(useFloodAlertStore.getState().timelineData).toMatchObject({
      runId: 'run-1',
      segmentId: 'seg-1',
      riverSegmentId: 'seg-1',
      frequencyThresholds: {
        Q20: 400,
        q20: 400,
        sample_quality: { count: 30 },
      },
    })
  })
})
