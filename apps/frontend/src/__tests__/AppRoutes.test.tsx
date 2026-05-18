import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import App from '@/App'

vi.mock('@/components/map/MapView', () => ({
  MapView: () => <div aria-label="河网地图">mock map</div>,
}))

vi.mock('@/components/forecast/ForecastPanel', () => ({
  ForecastPanel: () => <aside>mock forecast panel</aside>,
}))

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
      '/basins/basin-demo?basinVersionId=bv-001&segmentId=seg-009&source=best&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&warningLevel=orange&q=main',
    )

    render(<App />)

    expect(await screen.findByRole('heading', { name: '流域分析' })).toBeInTheDocument()
    expect(screen.getByText('basin-demo')).toBeInTheDocument()
    expect(screen.getByText('seg-009')).toBeInTheDocument()
    expect(screen.getAllByText('orange').length).toBeGreaterThan(0)
    await waitFor(() => expect(window.location.search).toContain('cycle=2026-05-18T00%3A00%3A00.000Z'))
  })

  it('normalizes invalid query values without repeated URL updates', async () => {
    window.history.pushState({}, '', '/overview?source=unknown&basemap=bad&warningLevel=invalid')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '全国总览' })).toBeInTheDocument()
    await waitFor(() => expect(window.location.search).toBe(''))
  })
})
