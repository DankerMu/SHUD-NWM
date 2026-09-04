import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { M11RiverTooltip } from '@/components/map/M11MapLibreSurface'
import type { BasinRiverFeature } from '@/components/map/m11MapBuilders'

function feature(overrides: Partial<BasinRiverFeature['properties']> = {}): BasinRiverFeature {
  return {
    type: 'Feature',
    geometry: { type: 'LineString', coordinates: [[100, 30], [100.1, 30.1]] },
    properties: {
      segment_id: 'seg-1',
      river_segment_id: 'seg-1',
      basin_version_id: 'bv-1',
      river_network_version_id: 'rnv-1',
      segment_name: '测试河段',
      q_value: 1234.5,
      // The transported spelling is ASCII: `m3/s` is a value of the
      // `hydro.river_unit` enum and arrives verbatim from the API.
      q_unit: 'm3/s',
      layer_color: '#2171B5',
      ...overrides,
    },
  }
}

describe('M11RiverTooltip', () => {
  it('renders the flow unit with a superscript exponent, not the transported m3/s', () => {
    render(<M11RiverTooltip feature={feature()} />)

    const value = screen.getByText(/1,234.5/)
    expect(value).toHaveTextContent('m³/s')
    expect(value).not.toHaveTextContent('m3/s')
  })

  it('leaves a unit that carries no exponent alone', () => {
    render(<M11RiverTooltip feature={feature({ q_value: 7, q_unit: 'mm' })} />)

    expect(screen.getByText(/^7 mm$/)).toBeInTheDocument()
  })

  it('says 无数据 instead of a bare unit when the segment has no value', () => {
    render(<M11RiverTooltip feature={feature({ q_value: null })} />)

    expect(screen.getByText('无数据')).toBeInTheDocument()
    expect(screen.queryByText(/m3\/s/)).not.toBeInTheDocument()
  })
})
