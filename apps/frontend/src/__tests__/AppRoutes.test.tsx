import { render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import App from '@/App'
import { useAuthStore } from '@/stores/auth'
import { useFloodAlertStore } from '@/stores/floodAlert'
import { useMonitoringStore } from '@/stores/monitoring'
import { useOverviewDataStore } from '@/stores/overviewData'

vi.mock('@/components/map/MapView', () => ({
  MapView: () => <div aria-label="河网地图">mock map</div>,
}))

vi.mock('@/components/forecast/ForecastPanel', () => ({
  ForecastPanel: () => <aside>mock forecast panel</aside>,
}))

vi.mock('@/components/flood/FloodAlertMap', () => ({
  FloodAlertMap: () => <div>mock flood map</div>,
}))

const noopAsync = vi.fn().mockResolvedValue(undefined)
const overviewAsync = vi.fn().mockResolvedValue(undefined)

beforeEach(() => {
  overviewAsync.mockResolvedValue(undefined)
  useAuthStore.setState({ role: 'viewer' })
  useFloodAlertStore.setState({
    selectedRunId: null,
    latestRun: null,
    selectedAlertLevel: null,
    selectedValidTime: null,
    topLimit: 20,
    basinId: '',
    validTimes: [],
    summaryData: null,
    rankingData: null,
    loading: false,
    summaryLoading: false,
    rankingLoading: false,
    error: null,
    empty: false,
    fetchLatestFrequencyDoneRun: noopAsync,
    fetchSummary: noopAsync,
    fetchRanking: noopAsync,
  })
  useMonitoringStore.setState({
    source: 'GFS',
    cycleTime: '2026-05-09T00:00:00Z',
    cycle: null,
    stages: [],
    jobs: [],
    jobTotal: 0,
    queue: null,
    queueError: null,
    jobFilters: { page: 1, pageSize: 12, sortBy: 'submitted_at', sortOrder: 'desc' },
    isPolling: false,
    isJobsLoading: false,
    error: null,
    fetchAll: noopAsync,
    fetchJobs: noopAsync,
  })
  useOverviewDataStore.setState({
    overview: null,
    basinDetail: null,
    loading: false,
    basinLoading: false,
    error: null,
    basinError: null,
    loadOverview: overviewAsync,
    loadBasinDetail: overviewAsync,
  })
})

describe('App route state', () => {
  it('routes / to the national overview shell and marks navigation active', async () => {
    window.history.pushState({}, '', '/')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '全国总览' })).toBeInTheDocument()
    expect(screen.getByLabelText('全国总览地图')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /全国总览/ })).toHaveClass('border-accent')
  })

  it('routes /overview with normalized query state', async () => {
    window.history.pushState({}, '', '/overview?source=gfs&layer=flood-return-period&basemap=terrain')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '全国总览' })).toBeInTheDocument()
    expect(screen.getByText('source')).toBeInTheDocument()
    expect(screen.getByText('gfs')).toBeInTheDocument()
    expect(screen.getAllByText('flood-return-period').length).toBeGreaterThan(0)
    expect(screen.getAllByText('terrain').length).toBeGreaterThan(0)
  })

  it('does not emit fabricated basin or basin-version IDs when overview data is unavailable', async () => {
    window.history.pushState({}, '', '/overview')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '全国总览' })).toBeInTheDocument()
    const link = screen.getByRole('link', { name: '等待可用流域' })
    expect(link).toHaveAttribute('href', '/overview')
    expect(link).not.toHaveAttribute('href', expect.stringContaining('bv-demo'))
    expect(link).not.toHaveAttribute('href', expect.stringContaining('basin-demo'))
  })

  it('renders unavailable markers for null overview summary fields and preserves real zero values', async () => {
    useOverviewDataStore.setState({
      overview: {
        basins: [],
        layers: [],
        aggregationDecision: {
          needsAggregationEndpoint: false,
          reason: 'reuse-existing',
          evidence: 'test',
        },
        summary: {
          completedCyclesToday: 0,
          runningJobs: null,
          warningSegmentCount: null,
          latestUpdate: null,
          totalBasins: 0,
          totalSegments: null,
          sourceSelection: {
            requestedSource: 'gfs',
            resolvedSource: 'GFS',
            scenarioIds: ['forecast_gfs_deterministic'],
            cycleTime: null,
            validTime: null,
            comparisonAvailable: false,
            provenanceLabel: 'GFS / latest cycle / current valid time',
            unavailableReason: null,
          },
          freshness: {
            updatedAt: null,
            cycleTime: null,
            validTime: null,
            runId: null,
            source: 'GFS',
            isStale: false,
            staleAfterHours: 6,
            unavailableReason: 'No freshness metadata is available.',
          },
          qualityNotes: [],
          partialErrors: [],
        },
      },
    })
    window.history.pushState({}, '', '/overview')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '全国总览' })).toBeInTheDocument()
    expect(screen.getByText('0')).toBeInTheDocument()
    expect(screen.getByText('当前运行中').parentElement).toHaveTextContent('-')
    expect(screen.getByText('超警河段').parentElement).toHaveTextContent('-')
    expect(screen.getByText('最新更新时间').parentElement).toHaveTextContent('-')
    expect(screen.queryByText('23')).not.toBeInTheDocument()
    expect(screen.queryByText('7')).not.toBeInTheDocument()
    expect(screen.queryByText('18')).not.toBeInTheDocument()
    expect(screen.queryByText('08:00')).not.toBeInTheDocument()
  })

  it('routes /forecast to the preserved hydrologic forecast workflow', async () => {
    window.history.pushState({}, '', '/forecast')

    render(<App />)

    expect((await screen.findAllByLabelText('河网地图')).length).toBeGreaterThan(0)
    expect(screen.getByText('请在地图上选择河段查看预报')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /水文预报/ })).toHaveClass('border-accent')
  })

  it('routes basin deep links and restores normalized query state once', async () => {
    window.history.pushState(
      {},
      '',
      '/basins/basin-demo?basinVersionId=bv-001&segmentId=seg-009&source=best&cycle=2026-05-18T00:00:00.123456Z&validTime=2026-05-18T14:00:00.250001%2B08:00&warningLevel=orange&q=main',
    )
    const replaceState = vi.spyOn(window.history, 'replaceState')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '流域分析' })).toBeInTheDocument()
    expect(screen.getByText('basin-demo')).toBeInTheDocument()
    expect(screen.getByText('seg-009')).toBeInTheDocument()
    expect(screen.getAllByText('orange').length).toBeGreaterThan(0)
    await waitFor(() =>
      expect(window.location.search).toBe(
        '?cycle=2026-05-18T00%3A00%3A00.123Z&validTime=2026-05-18T06%3A00%3A00.250Z&basinVersionId=bv-001&segmentId=seg-009&warningLevel=orange&q=main',
      ),
    )
    const normalizedRouteReplacements = replaceState.mock.calls.filter(([, , url]) =>
      String(url).endsWith(
        '/basins/basin-demo?cycle=2026-05-18T00%3A00%3A00.123Z&validTime=2026-05-18T06%3A00%3A00.250Z&basinVersionId=bv-001&segmentId=seg-009&warningLevel=orange&q=main',
      ),
    )
    expect(normalizedRouteReplacements).toHaveLength(1)
    replaceState.mockRestore()
  })

  it('normalizes invalid overview query values without repeated URL updates', async () => {
    window.history.pushState(
      {},
      '',
      '/overview?source=unknown&basemap=bad&warningLevel=invalid&cycle=2026-02-30T00:00:00.123456Z&validTime=2026-05-18T00:00:00.123456',
    )
    const replaceState = vi.spyOn(window.history, 'replaceState')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '全国总览' })).toBeInTheDocument()
    await waitFor(() => expect(window.location.search).toBe(''))
    const normalizedRouteReplacements = replaceState.mock.calls.filter(([, , url]) => String(url).endsWith('/overview'))
    expect(normalizedRouteReplacements).toHaveLength(1)
    replaceState.mockRestore()
  })

  it('routes /flood-alerts to the flood alert workflow content', async () => {
    useFloodAlertStore.setState({
      selectedRunId: 'run-flood-1',
      latestRun: {
        run_id: 'run-flood-1',
        run_type: 'forecast',
        scenario_id: 'forecast_gfs_deterministic',
        model_id: 'model-1',
        basin_version_id: 'basin-v1',
        source_id: 'gfs',
        cycle_time: '2026-05-12T00:00:00Z',
        status: 'frequency_done',
        start_time: '2026-05-12T00:00:00Z',
        end_time: '2026-05-12T03:00:00Z',
        created_at: '2026-05-12T00:00:00Z',
        updated_at: '2026-05-12T04:00:00Z',
      },
      validTimes: ['2026-05-12T00:00:00.000Z', '2026-05-12T03:00:00.000Z'],
      summaryData: {
        runId: 'run-flood-1',
        levels: [{ level: 'warning', count: 2, color: '#f59e0b' }],
        totalSegments: 4,
        usableCurves: 3,
        unavailableCount: 1,
      },
      rankingData: {
        items: [
          {
            rank: 1,
            riverSegmentId: 'seg-1',
            segmentId: 'seg-1',
            segmentName: 'Flood Segment 1',
            basinVersionId: 'basin-v1',
            qValue: 1234,
            qUnit: 'm3/s',
            returnPeriod: 20,
            warningLevel: 'warning',
            validTime: '2026-05-12T03:00:00Z',
          },
        ],
        total: 1,
        limit: 20,
        offset: 0,
      },
    })
    window.history.pushState({}, '', '/flood-alerts?warningLevel=major')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '洪水预警' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '预警统计' })).toBeInTheDocument()
    expect(screen.getByLabelText('洪水预警地图')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '预报时刻' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '风险排名' })).toBeInTheDocument()
    expect(screen.getByRole('row', { name: /Flood Segment 1/ })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /洪水预警/ })).toHaveClass('border-accent')
  })

  it('routes /monitoring through allowed RBAC to the monitoring workflow content', async () => {
    useAuthStore.setState({ role: 'operator' })
    useMonitoringStore.setState({
      cycle: {
        source: 'GFS',
        cycle_time: '2026-05-09T00:00:00Z',
        current_state: 'partially_failed',
        started_at: '2026-05-09T00:00:30Z',
        updated_at: '2026-05-09T00:08:00Z',
        job_counts: { succeeded: 3, failed: 1, running: 1, pending: 2 },
      },
      stages: [
        {
          stage: 'forcing',
          display_status: 'partially_failed',
          status: 'partially_failed',
          duration_seconds: 35,
          basin_progress: { completed: 3, total: 4, failed: 1 },
          basin_results: [],
        },
      ],
      jobs: [
        {
          job_id: 'job-failed',
          run_id: 'run-failed',
          cycle_id: 'cycle-1',
          job_type: 'forecast',
          slurm_job_id: '1001',
          model_id: 'model-b',
          status: 'failed',
          stage: 'forecast',
          submitted_at: '2026-05-09T00:03:00Z',
          started_at: '2026-05-09T00:04:00Z',
          finished_at: '2026-05-09T00:06:00Z',
          exit_code: 1,
          retry_count: 0,
          error_code: 'E_MODEL',
          error_message: 'model failed',
          log_uri: null,
          duration_seconds: 120,
        },
      ],
      jobTotal: 1,
      queue: { running: 2, pending: 4, idle: 6 },
    })
    window.history.pushState({}, '', '/monitoring')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '监控工作台' })).toBeInTheDocument()
    expect(screen.queryByText('权限不足')).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '当前周期' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '七阶段流水线' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '作业列表' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '趋势' })).toBeInTheDocument()
    expect(within(screen.getByRole('row', { name: /run-failed/ })).getByText('model-b')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /产品监控/ })).toHaveClass('border-accent')
  })
})
