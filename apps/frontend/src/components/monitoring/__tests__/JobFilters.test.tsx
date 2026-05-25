import { useState } from 'react'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { JobFilters } from '@/components/monitoring/JobFilters'
import type { JobFilters as JobFilterState } from '@/stores/monitoring'

function FiltersHarness({ initialFilters = {} }: { initialFilters?: JobFilterState }) {
  const [filters, setFilters] = useState<JobFilterState>(initialFilters)

  return (
    <>
      <JobFilters filters={filters} onChange={setFilters} />
      <output aria-label="Current filters">{JSON.stringify(filters)}</output>
    </>
  )
}

function currentFilters() {
  return JSON.parse(screen.getByLabelText('Current filters').textContent || '{}') as JobFilterState
}

async function chooseFilter(label: string, option: string) {
  const user = userEvent.setup()
  await user.click(screen.getByLabelText(label))
  await user.click(await screen.findByRole('option', { name: option }))
}

describe('JobFilters', () => {
  it('selects queued as a concrete status filter', async () => {
    render(<FiltersHarness />)

    await chooseFilter('Status filter', 'queued')

    expect(currentFilters()).toEqual({ status: 'queued' })
  })

  it('updates status, run type, and scenario filters', async () => {
    render(<FiltersHarness />)

    await chooseFilter('Status filter', 'failed')
    expect(currentFilters()).toMatchObject({ status: 'failed' })

    await chooseFilter('Run type filter', 'analysis')
    expect(currentFilters()).toMatchObject({ status: 'failed', runType: 'analysis' })

    await chooseFilter('Scenario filter', 'IFS')
    expect(currentFilters()).toMatchObject({
      status: 'failed',
      runType: 'analysis',
      scenario: 'forecast_ifs_deterministic',
    })
  })

  it('resets a selected filter to all while preserving other filters', async () => {
    render(
      <FiltersHarness
        initialFilters={{
          status: 'failed',
          runType: 'forecast',
          scenario: 'forecast_gfs_deterministic',
        }}
      />,
    )

    await chooseFilter('Status filter', '全部状态')

    expect(currentFilters()).not.toHaveProperty('status')
    expect(currentFilters()).toMatchObject({
      runType: 'forecast',
      scenario: 'forecast_gfs_deterministic',
    })
  })
})
