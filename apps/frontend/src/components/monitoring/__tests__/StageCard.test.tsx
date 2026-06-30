import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { StageCard } from '@/components/monitoring/StageCard'
import { StageList } from '@/components/monitoring/StageList'
import type { PipelineStatus } from '@/lib/constants'
import type { PipelineStage } from '@/stores/monitoring'

vi.mock('@/components/charts/StageDurationBar', () => ({
  StageDurationBar: () => <div>mock stage chart</div>,
}))

function makeStage(overrides: Partial<PipelineStage> = {}): PipelineStage {
  return {
    stage: 'download',
    display_status: 'pending',
    status: 'pending',
    duration_seconds: 0,
    basin_progress: { completed: 0, total: 0, failed: 0 },
    basin_results_limit: 50,
    basin_results_total: 0,
    basin_results_returned: 0,
    basin_results_truncated: false,
    basin_results: [],
    ...overrides,
  }
}

describe('StageCard', () => {
  it.each([
    ['succeeded', '✓'],
    ['failed', '✗'],
    ['running', '◉'],
    ['pending', '○'],
    ['partially_failed', '⚠'],
    ['skipped', '⊘'],
  ] satisfies Array<[PipelineStatus, string]>)('renders %s status icon', (status, icon) => {
    render(<StageCard stage={makeStage({ display_status: status, status })} />)

    const card = screen.getByRole('button')
    expect(within(card).getByText(icon)).toBeInTheDocument()
    expect(within(card).getByText(status)).toBeInTheDocument()
  })

  it('renders localized stage names from constants', () => {
    render(<StageCard stage={makeStage({ stage: 'forecast', display_status: 'running', status: 'running' })} />)

    expect(screen.getByText('预报')).toBeInTheDocument()
    expect(screen.getByText('forecast')).toBeInTheDocument()
  })

  it('renders completion rate from basin progress', () => {
    render(
      <StageCard
        stage={makeStage({
          basin_progress: { completed: 3, total: 4, failed: 1 },
        })}
      />,
    )

    expect(screen.getByText(/完成率 75% \(3\/4\)/)).toBeInTheDocument()
  })

  it('renders the canonical stages in display order with raw ids', () => {
    const canonicalStages = [
      ['download', '下载'],
      ['convert', '标准化'],
      ['forcing', '强迫场'],
      ['forecast', '预报'],
      ['parse', '解析'],
      ['publish', '发布'],
    ] as const

    render(
      <StageList
        stages={canonicalStages.map(([stage], index) =>
          makeStage({
            stage,
            display_status: 'succeeded',
            status: 'succeeded',
            duration_seconds: (index + 1) * 10,
            basin_progress: { completed: index + 1, total: 7, failed: 0 },
          }),
        )}
      />,
    )

    const cards = screen.getAllByRole('button')
    expect(cards).toHaveLength(canonicalStages.length)
    canonicalStages.forEach(([stage, label], index) => {
      expect(within(cards[index]).getByText(label)).toBeInTheDocument()
      expect(within(cards[index]).getByText(stage)).toBeInTheDocument()
      expect(within(cards[index]).getByText(new RegExp(`完成率 .*\\(${index + 1}/7\\)`))).toBeInTheDocument()
    })
  })

  it('keeps non-display failed-stage diagnostics guidance role-neutral', async () => {
    const user = userEvent.setup()

    render(
      <StageList
        diagnosticContext={{
          sourceId: 'GFS',
          cycleTime: '2026-05-18T00:00:00.000Z',
          runId: 'run-stage',
          modelId: 'model-stage',
        }}
        diagnosticsDisplayReadonly={false}
        diagnosticsEnabled
        showPendingPlaceholders={false}
        stages={[
          makeStage({
            stage: 'forecast',
            display_status: 'failed',
            status: 'failed',
            duration_seconds: 120,
            basin_progress: { completed: 0, total: 1, failed: 1 },
          }),
        ]}
      />,
    )

    await user.click(screen.getByRole('button', { name: /预报.*forecast.*failed/ }))

    expect(screen.getByTestId('ops-stage-manual-recovery-guidance')).toHaveTextContent('22 compute-control')
    expect(screen.getByTestId('ops-stage-manual-recovery-guidance')).not.toHaveTextContent(/display_readonly|27/)
  })
})
