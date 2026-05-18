import { beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
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
  const latestRun = {
    run_id: 'run-1',
    model_id: 'model-1',
    basin_version_id: 'basin-v1',
    river_network_version_id: 'rivnet-v1',
    mesh_version_id: 'mesh-v1',
    calibration_version_id: 'cal-v1',
    run_type: 'forecast',
    scenario_id: 'forecast_gfs_deterministic',
    start_time: '2026-05-12T00:00:00Z',
    end_time: '2026-05-12T03:00:00Z',
    cycle_time: '2026-05-12T00:00:00Z',
    status: 'frequency_done',
    forcings_version_id: 'forc-1',
    forcing_version_id: 'forc-1',
    output_uri: 's3://runs/run-1',
    error_code: null,
    error_message: null,
    created_at: '2026-05-12T00:00:00Z',
    updated_at: '2026-05-12T04:00:00Z',
  }

  beforeEach(() => {
    vi.clearAllMocks()
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

  it('loads latest run, summary, ranking, and selected segment timeline through the configured API base', async () => {
    const fetchUrls: string[] = []
    vi.mocked(client.GET).mockResolvedValue(
      {
        data: success({
          items: [latestRun],
          total: 1,
          limit: 50,
          offset: 0,
        }),
        error: undefined,
      } as never,
    )
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(async (url: string) => {
        fetchUrls.push(url)
        const parsed = new URL(url)
        if (parsed.pathname === '/api/v1/flood-alerts/summary') {
          expect(parsed.searchParams.get('run_id')).toBe('run-1')
          expect(parsed.searchParams.get('valid_time')).toBe('2026-05-12T03:00:00Z')
          return {
            ok: true,
            json: async () =>
              success({
                run_id: 'run-1',
                levels: [{ level: 'warning', count: 2, color: '#f59e0b' }],
                total_segments: 4,
                usable_curves: 3,
                unavailable_count: 1,
                quality_note: null,
              }),
          }
        }
        if (parsed.pathname === '/api/v1/flood-alerts/ranking') {
          expect(parsed.searchParams.get('run_id')).toBe('run-1')
          expect(parsed.searchParams.get('basin_id')).toBe('basin-a')
          expect(parsed.searchParams.get('valid_time')).toBe('2026-05-12T03:00:00Z')
          return {
            ok: true,
            json: async () =>
              success({
                items: [
                  {
                    rank: 1,
                    river_segment_id: 'seg-1',
                    segment_id: 'seg-1',
                    segment_name: 'Segment 1',
                    basin_version_id: 'basin-v1',
                    q_value: 1234,
                    q_unit: 'm3/s',
                    return_period: 20,
                    warning_level: 'warning',
                    duration: '1h',
                    valid_time: '2026-05-12T03:00:00Z',
                  },
                ],
                total: 1,
                limit: 20,
                offset: 0,
              }),
          }
        }
        if (parsed.pathname === '/api/v1/flood-alerts/timeline') {
          expect(parsed.searchParams.get('run_id')).toBe('run-1')
          expect(parsed.searchParams.get('segment_id')).toBe('seg-1')
          return {
            ok: true,
            json: async () =>
              success({
                run_id: 'run-1',
                segment_id: 'seg-1',
                river_segment_id: 'seg-1',
                timesteps: [
                  {
                    valid_time: '2026-05-12T03:00:00Z',
                    return_period: 20,
                    warning_level: 'warning',
                    q_value: 1234,
                  },
                ],
                timeline: [],
                peak: null,
                frequency_thresholds: null,
                quality_note: null,
              }),
          }
        }
        throw new Error(`Unexpected flood request ${url}`)
      }),
    )

    await useFloodAlertStore.getState().fetchLatestFrequencyDoneRun()
    useFloodAlertStore.getState().setBasinId('basin-a')
    useFloodAlertStore.getState().setSelectedValidTime('2026-05-12T03:00:00Z')
    await useFloodAlertStore.getState().fetchSummary()
    await useFloodAlertStore.getState().fetchRanking()
    await useFloodAlertStore.getState().fetchTimeline('seg-1')

    expect(client.GET).toHaveBeenCalledWith('/api/v1/runs', {
      params: { query: { status: 'frequency_done', limit: 50 } },
    })
    expect(fetchUrls).toEqual([
      `${apiBase}/api/v1/flood-alerts/summary?run_id=run-1&valid_time=2026-05-12T03%3A00%3A00Z`,
      `${apiBase}/api/v1/flood-alerts/ranking?run_id=run-1&limit=20&offset=0&basin_id=basin-a&valid_time=2026-05-12T03%3A00%3A00Z`,
      `${apiBase}/api/v1/flood-alerts/timeline?run_id=run-1&segment_id=seg-1`,
    ])
    expect(useFloodAlertStore.getState().latestRun?.run_id).toBe('run-1')
    expect(useFloodAlertStore.getState().summaryData?.totalSegments).toBe(4)
    expect(useFloodAlertStore.getState().rankingData?.items[0]).toMatchObject({
      riverSegmentId: 'seg-1',
      validTime: '2026-05-12T03:00:00Z',
    })
    expect(useFloodAlertStore.getState().timelineData?.segmentId).toBe('seg-1')
  })

  it('hydrates latest flood-alert run and valid time from overview query context', async () => {
    const siblingRun = {
      ...latestRun,
      run_id: 'run-sibling',
      source_id: 'ifs',
      cycle_time: '2026-05-11T00:00:00Z',
      start_time: '2026-05-11T00:00:00Z',
      end_time: '2026-05-11T03:00:00Z',
    }
    vi.mocked(client.GET).mockResolvedValue({
      data: success({
        items: [siblingRun, { ...latestRun, source_id: 'gfs' }],
        total: 2,
        limit: 50,
        offset: 0,
      }),
      error: undefined,
    } as never)

    await useFloodAlertStore.getState().fetchLatestFrequencyDoneRun({
      source: 'ifs',
      cycleTime: '2026-05-11T00:00:00.000Z',
      validTime: '2026-05-11T03:00:00.000Z',
    })

    expect(client.GET).toHaveBeenCalledWith('/api/v1/runs', {
      params: { query: { source: 'IFS', cycle_time: '2026-05-11T00:00:00.000Z', status: 'frequency_done', limit: 50 } },
    })
    expect(useFloodAlertStore.getState().selectedRunId).toBe('run-sibling')
    expect(useFloodAlertStore.getState().selectedValidTime).toBe('2026-05-11T03:00:00.000Z')
  })

  it('does not fall back to an unrelated latest run when explicit overview context is absent', async () => {
    vi.mocked(client.GET).mockResolvedValue({
      data: success({
        items: [{ ...latestRun, source_id: 'gfs' }],
        total: 1,
        limit: 50,
        offset: 0,
      }),
      error: undefined,
    } as never)

    await useFloodAlertStore.getState().fetchLatestFrequencyDoneRun({
      source: 'ifs',
      cycleTime: '2026-05-12T00:00:00.000Z',
      validTime: '2026-05-12T03:00:00.000Z',
    })

    expect(client.GET).toHaveBeenCalledWith('/api/v1/runs', {
      params: { query: { source: 'IFS', cycle_time: '2026-05-12T00:00:00.000Z', status: 'frequency_done', limit: 50 } },
    })
    expect(useFloodAlertStore.getState().selectedRunId).toBeNull()
    expect(useFloodAlertStore.getState().latestRun).toBeNull()
    expect(useFloodAlertStore.getState().empty).toBe(true)
    expect(useFloodAlertStore.getState().error).toContain('未找到 IFS 周期 2026-05-12T00:00:00.000Z')
  })
})
