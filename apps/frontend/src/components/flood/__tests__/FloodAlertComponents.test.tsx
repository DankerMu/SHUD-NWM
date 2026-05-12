import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { AlertRankingPanel } from '@/components/flood/AlertRankingPanel'
import { AlertStatsPanel } from '@/components/flood/AlertStatsPanel'
import { AlertTicker } from '@/components/flood/AlertTicker'
import { AlertTimeline } from '@/components/flood/AlertTimeline'
import { ALERT_LEVEL_META, floodLineColorExpression } from '@/components/flood/alertLevels'
import type { FloodAlertRanking, FloodAlertSummary } from '@/stores/floodAlert'

const summary: FloodAlertSummary = {
  runId: 'run-1',
  levels: [
    { level: 'extreme', count: 1, color: ALERT_LEVEL_META.extreme.color },
    { level: 'severe', count: 2, color: ALERT_LEVEL_META.severe.color },
    { level: 'high_risk', count: 3, color: ALERT_LEVEL_META.high_risk.color },
    { level: 'warning', count: 4, color: ALERT_LEVEL_META.warning.color },
    { level: 'watch', count: 5, color: ALERT_LEVEL_META.watch.color },
    { level: 'elevated', count: 6, color: ALERT_LEVEL_META.elevated.color },
    { level: 'normal', count: 7, color: ALERT_LEVEL_META.normal.color },
  ],
  totalSegments: 28,
  usableCurves: 28,
  unavailableCount: 0,
}

const ranking: FloodAlertRanking = {
  items: [
    {
      rank: 1,
      riverSegmentId: 'seg-1',
      segmentId: 'seg-1',
      segmentName: '测试河段',
      basinVersionId: 'basin-v1',
      qValue: 1234.5,
      qUnit: 'm3/s',
      returnPeriod: 66.6,
      warningLevel: 'severe',
      duration: '1h',
      validTime: '2026-05-12T00:00:00Z',
    },
  ],
  total: 1,
  limit: 20,
  offset: 0,
}

describe('flood alert components', () => {
  it('renders all warning level counts and toggles the selected level', async () => {
    const user = userEvent.setup()
    const onLevelSelect = vi.fn()

    render(<AlertStatsPanel summary={summary} selectedLevel="high_risk" onLevelSelect={onLevelSelect} />)

    expect(screen.getByText('极端')).toBeInTheDocument()
    expect(screen.getByText('高风险')).toBeInTheDocument()
    expect(screen.getByText('3 条')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /高风险/ })).toHaveAttribute('aria-pressed', 'true')

    await user.click(screen.getByRole('button', { name: /严重/ }))
    expect(onLevelSelect).toHaveBeenCalledWith('severe')
  })

  it('handles ranking row selection and top limit changes', async () => {
    const user = userEvent.setup()
    const onRowSelect = vi.fn()
    const onLimitChange = vi.fn()

    render(
      <AlertRankingPanel
        ranking={ranking}
        limit={20}
        onLimitChange={onLimitChange}
        onBasinChange={vi.fn()}
        onRowSelect={onRowSelect}
      />,
    )

    await user.click(screen.getByText('测试河段'))
    expect(onRowSelect).toHaveBeenCalledWith(expect.objectContaining({ riverSegmentId: 'seg-1' }))

    await user.click(screen.getByRole('button', { name: '50' }))
    expect(onLimitChange).toHaveBeenCalledWith(50)
  })

  it('exports the MapLibre warning-level color expression', () => {
    expect(JSON.stringify(floodLineColorExpression)).toContain(ALERT_LEVEL_META.normal.color)
    expect(JSON.stringify(floodLineColorExpression)).toContain(ALERT_LEVEL_META.extreme.color)
    expect(floodLineColorExpression).toEqual(
      expect.arrayContaining(['normal', ALERT_LEVEL_META.normal.color, 'extreme', ALERT_LEVEL_META.extreme.color]),
    )
  })

  it('emits selected timestep changes and playback toggles', async () => {
    const user = userEvent.setup()
    const onSelect = vi.fn()
    const onTogglePlayback = vi.fn()

    render(
      <AlertTimeline
        validTimes={['2026-05-12T00:00:00Z', '2026-05-12T01:00:00Z']}
        selectedValidTime={null}
        playing={false}
        onSelect={onSelect}
        onTogglePlayback={onTogglePlayback}
      />,
    )

    await user.click(screen.getByRole('button', { name: '12日 01Z' }))
    expect(onSelect).toHaveBeenCalledWith('2026-05-12T01:00:00Z')

    await user.click(screen.getByRole('button', { name: /播放/ }))
    expect(onTogglePlayback).toHaveBeenCalledTimes(1)
  })

  it('shows the empty ticker state when there are no super-warning segments', () => {
    render(
      <AlertTicker
        items={[{ ...ranking.items[0], warningLevel: 'watch', returnPeriod: 8 }]}
        onItemSelect={vi.fn()}
      />,
    )

    expect(screen.getByText('当前无超警河段')).toBeInTheDocument()
  })
})
