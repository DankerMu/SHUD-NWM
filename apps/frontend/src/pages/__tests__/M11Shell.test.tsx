import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { BrowserRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { m11VisualTokens } from '@/lib/m11/visualTokens'
import type { LayerState, SourceScenarioSelectionState } from '@/lib/m11/overviewDataContracts'
import { defaultM11QueryState, type M11QueryPatch, type M11QueryState } from '@/lib/m11/queryState'
import {
  LayerGroupControls,
  LayerLegendPanel,
  M11MapSurface,
  M11Timeline,
  SourceScenarioControls,
  buildM11TimelineViewModel,
  resolveM11ValidTimeCorrection,
} from '@/pages/m11/M11Controls'
import { OverviewPage } from '@/pages/OverviewPage'
import { useOverviewDataStore } from '@/stores/overviewData'

const state: M11QueryState = {
  ...defaultM11QueryState,
  source: 'gfs',
  cycle: '2026-05-18T00:00:00.000Z',
  validTime: '2026-05-18T00:00:00.000Z',
  layer: 'discharge',
}

const freshness = {
  updatedAt: null,
  cycleTime: '2026-05-18T00:00:00.000Z',
  validTime: '2026-05-18T00:00:00.000Z',
  runId: 'run-gfs',
  source: 'GFS' as const,
  isStale: false,
  staleAfterHours: 6,
  unavailableReason: null,
}

const layers: LayerState[] = [
  {
    layerId: 'discharge',
    displayName: 'River discharge',
    group: 'hydrology',
    available: true,
    validTimes: ['2026-05-18T00:00:00.000Z', '2026-05-18T06:00:00.000Z', '2026-05-18T12:00:00.000Z'],
    currentValidTime: '2026-05-18T00:00:00.000Z',
    validTimeSource: 'api',
    disabledReason: null,
    freshness,
    legend: [
      { label: '<500 m3/s', color: '#90CAF9', max: 500 },
      { label: '>5000 m3/s', color: '#0D47A1', min: 5000 },
    ],
  },
  {
    layerId: 'flood-return-period',
    displayName: 'Flood return period',
    group: 'hydrology',
    available: true,
    validTimes: ['2026-05-18T06:00:00.000Z', '2026-05-18T12:00:00.000Z'],
    currentValidTime: '2026-05-18T12:00:00.000Z',
    validTimeSource: 'api',
    disabledReason: null,
    freshness: { ...freshness, validTime: '2026-05-18T12:00:00.000Z' },
    legend: [{ label: 'warning', color: '#FF8C00', min: 10, max: 20 }],
  },
  {
    layerId: 'water-level',
    displayName: 'Water level',
    group: 'hydrology',
    available: false,
    validTimes: [],
    currentValidTime: null,
    validTimeSource: 'none',
    disabledReason: 'Layer has no valid times.',
    freshness: { ...freshness, validTime: null, unavailableReason: 'No valid-time metadata is available.' },
    legend: [],
  },
]

const sourceSelection: SourceScenarioSelectionState = {
  requestedSource: 'best',
  resolvedSource: 'IFS',
  scenarioIds: ['forecast_ifs_deterministic'],
  cycleTime: '2026-05-18T00:00:00.000Z',
  validTime: '2026-05-18T06:00:00.000Z',
  comparisonAvailable: true,
  provenanceLabel: 'Best Available (IFS) / cycle 2026-05-18T00:00:00.000Z / valid 2026-05-18T06:00:00.000Z',
  unavailableReason: null,
}

describe('M11 visual foundation shell', () => {
  beforeEach(() => {
    useOverviewDataStore.setState({
      ...useOverviewDataStore.getInitialState(),
      loadOverview: vi.fn().mockResolvedValue(undefined),
      loadBasinDetail: vi.fn().mockResolvedValue(undefined),
    })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('exposes mapped layout tokens for nav, panels, timeline, and warning colors', () => {
    window.history.pushState({}, '', '/overview?warningLevel=major')

    render(
      <BrowserRouter>
        <OverviewPage />
      </BrowserRouter>,
    )

    const shell = screen.getByTestId('m11-shell')
    expect(shell).toHaveStyle({
      '--m11-left-panel-width': '280px',
      '--m11-right-panel-width': '340px',
      '--m11-timeline-height': '64px',
    })
    expect(m11VisualTokens.navHeight).toBe('56px')
    expect(m11VisualTokens.warningLevels.major).toBe('#FF8A65')
    expect(screen.getByLabelText('M11 左侧面板')).toBeInTheDocument()
    expect(screen.getByLabelText('M11 右侧面板')).toBeInTheDocument()
    expect(screen.getByLabelText('M11 时间轴')).toBeInTheDocument()
  })

  it('switches basemaps through query patches and preserves active overlay state', async () => {
    const onQueryChange = vi.fn()
    const user = userEvent.setup()

    render(<M11MapSurface state={state} layers={layers} onQueryChange={onQueryChange} />)

    const surface = screen.getByTestId('m11-map-surface')
    expect(surface).toHaveAttribute('data-basemap', 'vector')
    expect(surface).toHaveAttribute('data-active-overlays', expect.stringContaining('discharge'))

    await user.click(screen.getByRole('button', { name: '地形底图' }))
    expect(onQueryChange).toHaveBeenCalledWith({ basemap: 'terrain' })

    await user.click(screen.getByRole('button', { name: '卫星底图' }))
    expect(onQueryChange).toHaveBeenCalledWith({ basemap: 'satellite' })
  })

  it('renders grouped layers and marks meteorology/base placeholders unavailable without fake data', async () => {
    const onQueryChange = vi.fn()
    const user = userEvent.setup()

    render(<LayerGroupControls state={state} layers={layers} onQueryChange={onQueryChange} />)

    expect(screen.getByText('水文图层')).toBeInTheDocument()
    expect(screen.getByText('气象图层')).toBeInTheDocument()
    expect(screen.getByText('基础图层')).toBeInTheDocument()
    expect(screen.getByText('降水格点')).toBeInTheDocument()
    expect(screen.getAllByText('气象格点合同未在 M11 接入')).toHaveLength(2)
    expect(screen.getByText('DEM 合同未在 M11 接入')).toBeInTheDocument()
    expect(screen.getByText('Layer has no valid times.')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /洪水重现期/ }))
    expect(onQueryChange).toHaveBeenCalledWith({ layer: 'flood-return-period' })
  })

  it('updates source/scenario query state and exposes best provenance plus compare availability', async () => {
    const onQueryChange = vi.fn()
    const user = userEvent.setup()

    const { rerender } = render(
      <SourceScenarioControls state={{ ...state, source: 'best' }} sourceSelection={sourceSelection} onQueryChange={onQueryChange} />,
    )

    expect(screen.getByTestId('m11-source-provenance')).toHaveTextContent('Best Available (IFS)')
    await user.click(screen.getByRole('button', { name: /GFS \+ IFS 对比/ }))
    expect(onQueryChange).toHaveBeenCalledWith({ source: 'compare' })
    expect(JSON.stringify(onQueryChange.mock.calls)).not.toContain('best_available')
    expect(JSON.stringify(onQueryChange.mock.calls)).not.toContain('forecast_best_available')

    rerender(
      <SourceScenarioControls
        state={{ ...state, source: 'compare' }}
        sourceSelection={{ ...sourceSelection, requestedSource: 'compare', resolvedSource: 'GFS+IFS', comparisonAvailable: false, unavailableReason: 'Comparison requires both GFS and IFS series.' }}
        onQueryChange={onQueryChange}
      />,
    )
    expect(screen.getByTestId('m11-source-provenance')).toHaveTextContent('对比数据不可用')
    expect(screen.getByText('Comparison requires both GFS and IFS series.')).toBeInTheDocument()
  })

  it('selects legends for discharge, flood return period, and warning level semantics', () => {
    const { rerender } = render(<LayerLegendPanel state={state} layers={layers} />)
    expect(screen.getByText('径流量图例')).toBeInTheDocument()
    expect(screen.getByText('<500 m3/s')).toBeInTheDocument()

    rerender(<LayerLegendPanel state={{ ...state, layer: 'flood-return-period' }} layers={layers} />)
    expect(screen.getByText('重现期图例')).toBeInTheDocument()
    expect(screen.getByText('warning')).toBeInTheDocument()

    rerender(<LayerLegendPanel state={{ ...state, layer: 'warning-level' }} layers={[]} />)
    expect(screen.getByText('预警等级图例')).toBeInTheDocument()
    expect(screen.getByText('高风险')).toBeInTheDocument()
  })

  it('builds timeline state from layer API valid times and corrects stale valid times', () => {
    const staleState = { ...state, validTime: '2026-05-17T00:00:00.000Z' }
    const model = buildM11TimelineViewModel(staleState, layers, null, sourceSelection)

    expect(model.validTimes).toEqual(layers[0].validTimes)
    expect(model.currentValidTime).toBe('2026-05-18T00:00:00.000Z')
    expect(model.sourceKind).toBe('api')
    expect(model.sourceLabel).toContain('/api/v1/layers/{layer_id}/valid-times')
    expect(model.dividerPercent).toBe(50)
    expect(resolveM11ValidTimeCorrection(staleState, layers)).toBe('2026-05-18T00:00:00.000Z')
    expect(resolveM11ValidTimeCorrection({ ...state, layer: 'flood-return-period' }, layers)).toBe('2026-05-18T12:00:00.000Z')
    expect(resolveM11ValidTimeCorrection({ ...state, layer: 'water-level' }, layers)).toBeNull()
  })

  it('uses payload-derived valid times only when no layer contract applies', () => {
    const model = buildM11TimelineViewModel(
      { ...state, layer: 'warning-level', validTime: null },
      [],
      { label: 'selected segment forecast payload', validTimes: ['2026-05-18T09:00:00Z', '2026-05-18T03:00:00Z'] },
      sourceSelection,
    )

    expect(model.sourceKind).toBe('derived')
    expect(model.validTimes).toEqual(['2026-05-18T03:00:00.000Z', '2026-05-18T09:00:00.000Z'])
    expect(model.currentValidTime).toBe('2026-05-18T09:00:00.000Z')
    expect(model.sourceLabel).toContain('selected segment forecast payload / derived')
  })

  it('disables empty timelines and bounds previous/next controls', async () => {
    const onQueryChange = vi.fn()
    const user = userEvent.setup()
    const emptyLayer = [{ ...layers[0], validTimes: [], currentValidTime: null, available: false, validTimeSource: 'none' as const }]

    const { rerender } = render(<M11Timeline state={state} layers={emptyLayer} onQueryChange={onQueryChange} />)

    expect(screen.getByText('当前图层没有有效时间')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '上一个有效时刻' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '播放时间轴' })).toBeDisabled()
    expect(screen.getByRole('slider', { name: '有效时间滑块' })).toBeDisabled()

    rerender(<M11Timeline state={state} layers={layers} onQueryChange={onQueryChange} />)
    expect(screen.getByRole('button', { name: '上一个有效时刻' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: '下一个有效时刻' }))
    expect(onQueryChange).toHaveBeenCalledWith({ validTime: '2026-05-18T06:00:00.000Z' })

    rerender(<M11Timeline state={{ ...state, validTime: '2026-05-18T12:00:00.000Z' }} layers={layers} onQueryChange={onQueryChange} />)
    expect(screen.getByRole('button', { name: '下一个有效时刻' })).toBeDisabled()
  })

  it('updates valid time from slider and cleans up bounded playback timers', async () => {
    vi.useFakeTimers()
    const onQueryChange = vi.fn((patch: M11QueryPatch) => {
      currentState = { ...currentState, ...patch }
      rerender(<M11Timeline state={currentState} layers={layers} onQueryChange={onQueryChange} />)
    })
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    let currentState = state
    const { rerender, unmount } = render(<M11Timeline state={currentState} layers={layers} onQueryChange={onQueryChange} />)

    fireEvent.change(screen.getByRole('slider', { name: '有效时间滑块' }), { target: { value: '2' } })
    expect(onQueryChange).toHaveBeenCalledWith({ validTime: '2026-05-18T12:00:00.000Z' })

    currentState = state
    rerender(<M11Timeline state={currentState} layers={layers} onQueryChange={onQueryChange} />)
    fireEvent.click(screen.getByRole('button', { name: '播放时间轴' }))
    expect(vi.getTimerCount()).toBe(1)
    vi.advanceTimersByTime(1000)
    expect(onQueryChange).toHaveBeenCalledWith({ validTime: '2026-05-18T06:00:00.000Z' })
    unmount()
    expect(vi.getTimerCount()).toBe(0)
  })
})
