import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import type { components } from '@/api/types'
import { BasinSelector } from '@/pages/hydroMet/BasinSelector'

vi.mock('@/api/client', () => ({
  client: {
    GET: vi.fn(),
  },
}))

type Basin = components['schemas']['Basin']

function basin(overrides: Partial<Basin> = {}): Basin {
  return {
    basin_id: 'basins_qhh',
    basin_name: '青海湖',
    basin_group: null,
    description: null,
    created_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function success<T>(data: T) {
  return { status: 'success', data }
}

describe('BasinSelector (#314)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  it('renders options data-driven from the discovery endpoint with has_display_product=true', async () => {
    vi.mocked(client.GET).mockResolvedValueOnce({
      data: success([basin(), basin({ basin_id: 'basins_heihe', basin_name: '黑河' })]),
      error: undefined,
    } as never)

    render(<BasinSelector selectedBasinId="basins_qhh" onSelect={vi.fn()} />)

    const select = await screen.findByTestId('hydro-met-basin-select')
    // Options come from the response, not a hardcoded whitelist.
    const options = Array.from(select.querySelectorAll('option')).map((o) => o.getAttribute('value'))
    expect(options).toEqual(['basins_qhh', 'basins_heihe'])
    expect(screen.getByRole('option', { name: '青海湖' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: '黑河' })).toBeInTheDocument()

    expect(client.GET).toHaveBeenCalledWith('/api/v1/basins', {
      params: { query: { has_display_product: true } },
    })
  })

  it('surfaces a newly registered basin without any code change (data-driven discovery)', async () => {
    vi.mocked(client.GET).mockResolvedValueOnce({
      data: success([
        basin(),
        basin({ basin_id: 'basins_heihe', basin_name: '黑河' }),
        basin({ basin_id: 'basins_brandnew', basin_name: '新流域' }),
      ]),
      error: undefined,
    } as never)

    render(<BasinSelector selectedBasinId="basins_qhh" onSelect={vi.fn()} />)

    expect(await screen.findByRole('option', { name: '新流域' })).toBeInTheDocument()
  })

  it('invokes onSelect with the chosen basin_id when the user switches basins', async () => {
    vi.mocked(client.GET).mockResolvedValueOnce({
      data: success([basin(), basin({ basin_id: 'basins_heihe', basin_name: '黑河' })]),
      error: undefined,
    } as never)
    const onSelect = vi.fn()

    render(<BasinSelector selectedBasinId="basins_qhh" onSelect={onSelect} />)

    const select = await screen.findByTestId('hydro-met-basin-select')
    await userEvent.selectOptions(select, 'basins_heihe')

    expect(onSelect).toHaveBeenCalledWith('basins_heihe')
  })

  it('shows a default-basin placeholder option when no basin is selected (backward compatible)', async () => {
    vi.mocked(client.GET).mockResolvedValueOnce({
      data: success([basin()]),
      error: undefined,
    } as never)

    render(<BasinSelector selectedBasinId={null} onSelect={vi.fn()} />)

    expect(await screen.findByRole('option', { name: '默认流域' })).toBeInTheDocument()
  })

  it('renders an error message when the discovery endpoint fails', async () => {
    vi.mocked(client.GET).mockResolvedValueOnce({
      data: undefined,
      error: { error: { code: 'boom', message: 'down' } },
    } as never)

    render(<BasinSelector selectedBasinId={null} onSelect={vi.fn()} />)

    await waitFor(() => expect(screen.getByTestId('hydro-met-basin-error')).toBeInTheDocument())
  })
})
