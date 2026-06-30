import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { BrowserRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import {
  M11BackToOverviewButton,
  M11FloatingBasemapSwitcher,
  M11FloatingLayerSwitcher,
  M11FloatingLegend,
  M11OpsLink,
} from '@/components/map/M11FloatingControls'
import type { LayerState } from '@/lib/m11/overviewDataContracts'

const dischargeLayer: LayerState = {
  layerId: 'discharge',
  displayName: 'River discharge',
  group: 'hydrology',
  available: true,
  metadata: null,
  validTimes: [],
  currentValidTime: null,
  validTimeSource: 'none',
  disabledReason: null,
  freshness: {
    updatedAt: null,
    cycleTime: null,
    validTime: null,
    runId: null,
    source: 'GFS',
    isStale: false,
    staleAfterHours: 6,
    unavailableReason: null,
    basinVersionId: null,
    riverNetworkVersionId: null,
  },
  legend: [
    { label: '<500 m3/s', color: '#90CAF9', max: 500 },
    { label: '>5000 m3/s', color: '#0D47A1', min: 5000 },
  ],
}

describe('M11FloatingLayerSwitcher', () => {
  it('offers only the public discharge layer and dispatches station overlay separately', async () => {
    const onQueryChange = vi.fn()
    const user = userEvent.setup()
    render(<M11FloatingLayerSwitcher layer="discharge" metStations={false} onQueryChange={onQueryChange} />)

    expect(screen.getByRole('button', { name: /流量/, pressed: true })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /重现期/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /预警等级/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /气象栅格/ })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /气象代站/ })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /气象代站/ }))
    expect(onQueryChange).toHaveBeenCalledWith({ metStations: true })
  })

  it('dispatches false when disabling the station overlay toggle', async () => {
    const onQueryChange = vi.fn()
    const user = userEvent.setup()
    render(<M11FloatingLayerSwitcher layer="discharge" metStations onQueryChange={onQueryChange} />)

    await user.click(screen.getByRole('button', { name: /气象代站/, pressed: true }))
    expect(onQueryChange).toHaveBeenCalledWith({ metStations: false })
  })
})

describe('M11FloatingBasemapSwitcher', () => {
  it('offers vector/satellite/terrain basemaps and dispatches basemap changes', async () => {
    const onQueryChange = vi.fn()
    const user = userEvent.setup()
    render(<M11FloatingBasemapSwitcher basemap="vector" onQueryChange={onQueryChange} />)

    expect(screen.getByTestId('m11-floating-basemap-switcher')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '矢量底图', pressed: true })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '卫星底图' }))
    expect(onQueryChange).toHaveBeenCalledWith({ basemap: 'satellite' })
    await user.click(screen.getByRole('button', { name: '地形底图' }))
    expect(onQueryChange).toHaveBeenCalledWith({ basemap: 'terrain' })
  })

  it('marks the active basemap as pressed', () => {
    render(<M11FloatingBasemapSwitcher basemap="satellite" />)
    expect(screen.getByRole('button', { name: '卫星底图', pressed: true })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '矢量底图', pressed: false })).toBeInTheDocument()
  })
})

describe('M11FloatingLegend', () => {
  it('renders legend entries for the active discharge layer', () => {
    render(<M11FloatingLegend layer="discharge" layers={[dischargeLayer]} />)
    expect(screen.getByText('径流量图例')).toBeInTheDocument()
    expect(screen.getByTestId('m11-floating-legend-entries')).toBeInTheDocument()
    expect(screen.getByText('<500 m3/s')).toBeInTheDocument()
  })

  it('keeps the legend tied to the hydrology layer while stations are an overlay', () => {
    render(<M11FloatingLegend layer="discharge" layers={[{ ...dischargeLayer, legend: [] }]} />)
    expect(screen.getByText('径流量图例')).toBeInTheDocument()
    expect(screen.getByTestId('m11-floating-legend-entries')).toBeInTheDocument()
  })
})

describe('M11OpsLink + M11BackToOverviewButton', () => {
  it('hides the ops link for non-operator roles', () => {
    const { rerender } = render(
      <BrowserRouter>
        <M11OpsLink visible={false} />
      </BrowserRouter>,
    )
    expect(screen.queryByTestId('m11-ops-link')).not.toBeInTheDocument()

    rerender(
      <BrowserRouter>
        <M11OpsLink visible />
      </BrowserRouter>,
    )
    expect(screen.getByTestId('m11-ops-link')).toHaveAttribute('href', '/ops')
  })

  it('invokes the back-to-overview handler', async () => {
    const onClick = vi.fn()
    const user = userEvent.setup()
    render(<M11BackToOverviewButton onClick={onClick} />)
    await user.click(screen.getByTestId('m11-back-to-overview'))
    expect(onClick).toHaveBeenCalledTimes(1)
  })
})
