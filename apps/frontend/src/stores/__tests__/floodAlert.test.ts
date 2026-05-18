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

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function floodResponse<T>(data: T) {
  return {
    ok: true,
    json: async () => success(data),
  }
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

  it('clears stale run-scoped payloads when an explicit IFS handoff resolves a different run', async () => {
    const ifsRun = {
      ...latestRun,
      run_id: 'run-ifs',
      source_id: 'ifs',
      cycle_time: '2026-05-13T00:00:00Z',
      start_time: '2026-05-13T00:00:00Z',
      end_time: '2026-05-13T06:00:00Z',
    }
    useFloodAlertStore.setState({
      selectedRunId: 'run-1',
      summaryData: {
        runId: 'run-1',
        levels: [{ level: 'warning', count: 2, color: '#f59e0b' }],
        totalSegments: 4,
        usableCurves: 3,
        unavailableCount: 1,
      },
      rankingData: {
        items: [
          {
            rank: 1,
            riverSegmentId: 'old-seg',
            segmentId: 'old-seg',
            segmentName: 'Old Segment',
            returnPeriod: 20,
            warningLevel: 'warning',
          },
        ],
        total: 1,
        limit: 20,
        offset: 0,
      },
      timelineData: {
        runId: 'run-1',
        segmentId: 'old-seg',
        riverSegmentId: 'old-seg',
        timesteps: [{ validTime: '2026-05-12T03:00:00Z', returnPeriod: 20, warningLevel: 'warning' }],
      },
      summaryLoading: true,
      rankingLoading: true,
      timelineLoading: true,
    })
    vi.mocked(client.GET).mockResolvedValue({
      data: success({
        items: [ifsRun],
        total: 1,
        limit: 50,
        offset: 0,
      }),
      error: undefined,
    } as never)

    await useFloodAlertStore.getState().fetchLatestFrequencyDoneRun({
      source: 'ifs',
      cycleTime: '2026-05-13T00:00:00.000Z',
      validTime: '2026-05-13T06:00:00.000Z',
    })

    expect(useFloodAlertStore.getState()).toMatchObject({
      selectedRunId: 'run-ifs',
      summaryData: null,
      rankingData: null,
      timelineData: null,
      summaryLoading: false,
      rankingLoading: false,
      timelineLoading: false,
    })
  })

  it('preserves run-scoped payloads when the resolved run is unchanged', async () => {
    const summaryData = {
      runId: 'run-1',
      levels: [{ level: 'warning' as const, count: 2, color: '#f59e0b' }],
      totalSegments: 4,
      usableCurves: 3,
      unavailableCount: 1,
    }
    const rankingData = {
      items: [{ rank: 1, riverSegmentId: 'seg-1', segmentId: 'seg-1', returnPeriod: 20, warningLevel: 'warning' as const }],
      total: 1,
      limit: 20,
      offset: 0,
    }
    const timelineData = {
      runId: 'run-1',
      segmentId: 'seg-1',
      riverSegmentId: 'seg-1',
      timesteps: [{ validTime: '2026-05-12T03:00:00Z', returnPeriod: 20, warningLevel: 'warning' as const }],
    }
    useFloodAlertStore.setState({
      selectedRunId: 'run-1',
      summaryData,
      rankingData,
      timelineData,
    })
    vi.mocked(client.GET).mockResolvedValue({
      data: success({
        items: [latestRun],
        total: 1,
        limit: 50,
        offset: 0,
      }),
      error: undefined,
    } as never)

    await useFloodAlertStore.getState().fetchLatestFrequencyDoneRun()

    expect(useFloodAlertStore.getState().summaryData).toBe(summaryData)
    expect(useFloodAlertStore.getState().rankingData).toBe(rankingData)
    expect(useFloodAlertStore.getState().timelineData).toBe(timelineData)
  })

  it('ignores an older same-run summary response after a newer valid-time request owns the state', async () => {
    const older = deferred<ReturnType<typeof floodResponse>>()
    const newer = deferred<ReturnType<typeof floodResponse>>()
    vi.stubGlobal('fetch', vi.fn().mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise))

    useFloodAlertStore.getState().setSelectedValidTime('2026-05-12T01:00:00.000Z')
    const olderRequest = useFloodAlertStore.getState().fetchSummary()
    expect(useFloodAlertStore.getState().summaryLoading).toBe(true)

    useFloodAlertStore.getState().setSelectedValidTime('2026-05-12T02:00:00.000Z')
    const newerRequest = useFloodAlertStore.getState().fetchSummary()
    newer.resolve(
      floodResponse({
        run_id: 'run-1',
        levels: [{ level: 'severe', count: 9, color: '#dc2626' }],
        total_segments: 9,
        usable_curves: 8,
        unavailable_count: 1,
        quality_note: null,
      }),
    )
    await newerRequest

    expect(useFloodAlertStore.getState().summaryData).toMatchObject({
      totalSegments: 9,
      levels: [{ level: 'severe', count: 9 }],
    })
    expect(useFloodAlertStore.getState().summaryLoading).toBe(false)

    older.resolve(
      floodResponse({
        run_id: 'run-1',
        levels: [{ level: 'watch', count: 1, color: '#0ea5e9' }],
        total_segments: 1,
        usable_curves: 1,
        unavailable_count: 0,
        quality_note: null,
      }),
    )
    await olderRequest

    expect(useFloodAlertStore.getState().summaryData).toMatchObject({
      totalSegments: 9,
      levels: [{ level: 'severe', count: 9 }],
    })
    expect(useFloodAlertStore.getState().summaryLoading).toBe(false)
  })

  it('ignores an older same-run ranking response after newer filter scope owns the state', async () => {
    const older = deferred<ReturnType<typeof floodResponse>>()
    const newer = deferred<ReturnType<typeof floodResponse>>()
    vi.stubGlobal('fetch', vi.fn().mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise))

    useFloodAlertStore.getState().setSelectedValidTime('2026-05-12T01:00:00.000Z')
    useFloodAlertStore.getState().setBasinId('basin-old')
    useFloodAlertStore.getState().setTopLimit(10)
    const olderRequest = useFloodAlertStore.getState().fetchRanking()

    useFloodAlertStore.getState().setSelectedValidTime('2026-05-12T02:00:00.000Z')
    useFloodAlertStore.getState().setBasinId('basin-new')
    useFloodAlertStore.getState().setTopLimit(50)
    const newerRequest = useFloodAlertStore.getState().fetchRanking()
    newer.resolve(
      floodResponse({
        items: [
          {
            rank: 1,
            river_segment_id: 'seg-new',
            segment_id: 'seg-new',
            segment_name: 'New Segment',
            basin_version_id: 'basin-new',
            q_value: 222,
            q_unit: 'm3/s',
            return_period: 50,
            warning_level: 'severe',
            duration: '2h',
            valid_time: '2026-05-12T02:00:00.000Z',
          },
        ],
        total: 1,
        limit: 50,
        offset: 0,
      }),
    )
    await newerRequest

    expect(useFloodAlertStore.getState().rankingData?.items[0]).toMatchObject({
      riverSegmentId: 'seg-new',
      returnPeriod: 50,
    })
    expect(useFloodAlertStore.getState().rankingLoading).toBe(false)

    older.resolve(
      floodResponse({
        items: [
          {
            rank: 1,
            river_segment_id: 'seg-old',
            segment_id: 'seg-old',
            segment_name: 'Old Segment',
            basin_version_id: 'basin-old',
            q_value: 111,
            q_unit: 'm3/s',
            return_period: 10,
            warning_level: 'watch',
            duration: '1h',
            valid_time: '2026-05-12T01:00:00.000Z',
          },
        ],
        total: 1,
        limit: 10,
        offset: 0,
      }),
    )
    await olderRequest

    expect(useFloodAlertStore.getState().rankingData?.items[0]).toMatchObject({
      riverSegmentId: 'seg-new',
      returnPeriod: 50,
    })
    expect(useFloodAlertStore.getState().rankingLoading).toBe(false)
  })

  it('ignores an older same-run timeline response after a newer segment request owns the state', async () => {
    const older = deferred<ReturnType<typeof floodResponse>>()
    const newer = deferred<ReturnType<typeof floodResponse>>()
    vi.stubGlobal('fetch', vi.fn().mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise))

    const olderRequest = useFloodAlertStore.getState().fetchTimeline('seg-old')
    const newerRequest = useFloodAlertStore.getState().fetchTimeline('seg-new')
    newer.resolve(
      floodResponse({
        run_id: 'run-1',
        segment_id: 'seg-new',
        river_segment_id: 'seg-new',
        timesteps: [{ valid_time: '2026-05-12T02:00:00.000Z', return_period: 50, warning_level: 'severe', q_value: 222 }],
        timeline: [],
        peak: null,
        frequency_thresholds: null,
        quality_note: null,
      }),
    )
    await newerRequest

    expect(useFloodAlertStore.getState().timelineData).toMatchObject({
      segmentId: 'seg-new',
      timesteps: [{ validTime: '2026-05-12T02:00:00.000Z' }],
    })
    expect(useFloodAlertStore.getState().timelineLoading).toBe(false)

    older.resolve(
      floodResponse({
        run_id: 'run-1',
        segment_id: 'seg-old',
        river_segment_id: 'seg-old',
        timesteps: [{ valid_time: '2026-05-12T01:00:00.000Z', return_period: 10, warning_level: 'watch', q_value: 111 }],
        timeline: [],
        peak: null,
        frequency_thresholds: null,
        quality_note: null,
      }),
    )
    await olderRequest

    expect(useFloodAlertStore.getState().timelineData).toMatchObject({
      segmentId: 'seg-new',
      timesteps: [{ validTime: '2026-05-12T02:00:00.000Z' }],
    })
    expect(useFloodAlertStore.getState().validTimes).not.toContain('2026-05-12T01:00:00.000Z')
    expect(useFloodAlertStore.getState().timelineLoading).toBe(false)
  })

  it('ignores an older source/cycle lookup response after a newer lookup owns the state', async () => {
    const older = deferred<unknown>()
    const newer = deferred<unknown>()
    vi.mocked(client.GET).mockReturnValueOnce(older.promise as never).mockReturnValueOnce(newer.promise as never)

    const olderRequest = useFloodAlertStore.getState().fetchLatestFrequencyDoneRun({
      source: 'gfs',
      cycleTime: '2026-05-12T00:00:00.000Z',
      validTime: '2026-05-12T03:00:00.000Z',
    })
    const newerRequest = useFloodAlertStore.getState().fetchLatestFrequencyDoneRun({
      source: 'ifs',
      cycleTime: '2026-05-13T00:00:00.000Z',
      validTime: '2026-05-13T03:00:00.000Z',
    })

    newer.resolve({
      data: success({
        items: [
          {
            ...latestRun,
            run_id: 'run-ifs',
            source_id: 'ifs',
            cycle_time: '2026-05-13T00:00:00.000Z',
            start_time: '2026-05-13T00:00:00.000Z',
            end_time: '2026-05-13T03:00:00.000Z',
          },
        ],
        total: 1,
        limit: 50,
        offset: 0,
      }),
      error: undefined,
    })
    await newerRequest

    expect(useFloodAlertStore.getState()).toMatchObject({
      selectedRunId: 'run-ifs',
      selectedValidTime: '2026-05-13T03:00:00.000Z',
      loading: false,
    })

    older.resolve({
      data: success({
        items: [
          {
            ...latestRun,
            run_id: 'run-gfs',
            source_id: 'gfs',
            cycle_time: '2026-05-12T00:00:00.000Z',
            start_time: '2026-05-12T00:00:00.000Z',
            end_time: '2026-05-12T03:00:00.000Z',
          },
        ],
        total: 1,
        limit: 50,
        offset: 0,
      }),
      error: undefined,
    })
    await olderRequest

    expect(useFloodAlertStore.getState()).toMatchObject({
      selectedRunId: 'run-ifs',
      selectedValidTime: '2026-05-13T03:00:00.000Z',
      loading: false,
    })
  })
})
