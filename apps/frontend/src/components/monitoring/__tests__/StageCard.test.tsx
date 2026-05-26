import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { StageCard } from '@/components/monitoring/StageCard'
import type { PipelineStatus } from '@/lib/constants'
import type { PipelineStage } from '@/stores/monitoring'

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
})
