import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { normalizeLayerStates, type LayerState } from '@/lib/m11/overviewDataContracts'
import { defaultM11QueryState, type M11QueryPatch } from '@/lib/m11/queryState'
import { M11BottomControlBar, deriveM11ControlBarModel } from '@/pages/m11/M11BottomControlBar'
import { M11Timeline } from '@/pages/m11/M11Controls'

/**
 * 时间轴的“让位即暂停”（openspec mobile-responsive-display task 4.6，design.md D11）：
 * `yielded` 为真时停止播放；它回到假时不续播（`playing` 仍是时间轴的本地 state，暂停是单向的）。
 * 不传该 prop 时播放行为与以往相同。控制条把同一个布尔原样传给时间轴，并据它给根节点加 `invisible`。
 */
const DEFAULT_CYCLE = '2026-05-18T00:00:00Z'
const VALID_TIMES = Array.from({ length: 9 }, (_, index) =>
  new Date(Date.parse(DEFAULT_CYCLE) + index * 3 * 3_600_000).toISOString().replace('.000Z', 'Z'),
)
/** `LayerState.validTimes` 的毫秒形（`normalizeIsoString` 的产物）。 */
const MILLISECOND_VALID_TIMES = VALID_TIMES.map((validTime) => new Date(Date.parse(validTime)).toISOString())
/** 默认速度 1x 的播放周期。 */
const PERIOD_MS = 1_000

const metadata = {
  layer_id: 'discharge',
  tile_format: 'mvt',
  maplibre_source_layer: 'hydro',
  min_zoom: 3,
  max_zoom: 10,
  url_template: '/api/v1/tiles/hydro-national/{source}/{cycle}/q_down/{valid_time}/{z}/{x}/{y}.pbf',
  required_placeholders: ['source', 'cycle', 'valid_time', 'z', 'x', 'y'],
  source_refs: { basin_version_id: 'bv-001', river_network_version_id: 'rn-001' },
  default_source: 'gfs',
  default_cycle: DEFAULT_CYCLE,
  valid_times: VALID_TIMES,
  fallback_available: false,
  release_blocking: false,
}

const layers: LayerState[] = normalizeLayerStates({
  query: defaultM11QueryState,
  layers: [
    { layer_id: 'discharge', layer_name: 'Discharge', layer_type: 'hydrology', variables: ['q_down'], metadata: metadata as never },
  ],
})

function advance(ms: number) {
  act(() => {
    vi.advanceTimersByTime(ms)
  })
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('M11Timeline yielded', () => {
  function timeline(onQueryChange: (patch: M11QueryPatch) => void, yielded?: boolean) {
    // `yielded` 为 undefined 时不把该 prop 写进 JSX：与今天的调用点逐字相同。
    const extra = yielded === undefined ? {} : { yielded }
    return (
      <M11Timeline
        state={defaultM11QueryState}
        layers={layers}
        sourceSelection={null}
        cycle={DEFAULT_CYCLE}
        onQueryChange={onQueryChange}
        {...extra}
      />
    )
  }

  it('pauses when yielded turns true while playing, and does not resume when it turns false', () => {
    const onQueryChange = vi.fn()
    const { rerender } = render(timeline(onQueryChange, false))

    fireEvent.click(screen.getByLabelText('播放时间轴'))
    expect(screen.getByLabelText('暂停时间轴')).toBeInTheDocument()
    // 前提：确实在播放——一个周期后前进一步。
    advance(PERIOD_MS)
    expect(onQueryChange).toHaveBeenCalledTimes(1)
    expect(onQueryChange).toHaveBeenLastCalledWith({ validTime: MILLISECOND_VALID_TIMES[1] })

    rerender(timeline(onQueryChange, true))
    expect(screen.getByLabelText('播放时间轴')).toBeInTheDocument()
    expect(screen.queryByLabelText('暂停时间轴')).toBeNull()
    advance(5 * PERIOD_MS)
    expect(onQueryChange).toHaveBeenCalledTimes(1)

    // 关闭不续播。
    rerender(timeline(onQueryChange, false))
    expect(screen.getByLabelText('播放时间轴')).toBeInTheDocument()
    expect(screen.queryByLabelText('暂停时间轴')).toBeNull()
    advance(5 * PERIOD_MS)
    expect(onQueryChange).toHaveBeenCalledTimes(1)
  })

  it('after yielding ends the user can start playback again', () => {
    const onQueryChange = vi.fn()
    const { rerender } = render(timeline(onQueryChange, false))
    fireEvent.click(screen.getByLabelText('播放时间轴'))
    rerender(timeline(onQueryChange, true))
    rerender(timeline(onQueryChange, false))
    expect(onQueryChange).not.toHaveBeenCalled()

    fireEvent.click(screen.getByLabelText('播放时间轴'))
    expect(screen.getByLabelText('暂停时间轴')).toBeInTheDocument()
    advance(PERIOD_MS)
    expect(onQueryChange).toHaveBeenCalledTimes(1)
  })

  it('without the prop playback keeps going across re-renders, as before', () => {
    const onQueryChange = vi.fn()
    const { rerender } = render(timeline(onQueryChange))

    fireEvent.click(screen.getByLabelText('播放时间轴'))
    advance(PERIOD_MS - 1)
    expect(onQueryChange).not.toHaveBeenCalled()
    advance(1)
    expect(onQueryChange).toHaveBeenCalledTimes(1)

    rerender(timeline(onQueryChange))
    expect(screen.getByLabelText('暂停时间轴')).toBeInTheDocument()
    advance(PERIOD_MS)
    expect(onQueryChange).toHaveBeenCalledTimes(2)
  })
})

describe('M11BottomControlBar yielded', () => {
  const model = deriveM11ControlBarModel({
    state: defaultM11QueryState,
    layers,
    metadata: metadata as never,
    cyclesBySource: {},
    sourceSelection: null,
  })
  const bar = () => screen.getByTestId('m11-bottom-control-bar')
  const classes = (element: Element) => element.className.split(/\s+/)

  function controlBar(onQueryChange: (patch: M11QueryPatch) => void, yielded?: boolean) {
    const extra = yielded === undefined ? {} : { yielded }
    return <M11BottomControlBar {...model} onQueryChange={onQueryChange} {...extra} />
  }

  it('hides its root with `invisible` only while yielded, keeping the same mounted node and its children', () => {
    const { rerender } = render(controlBar(vi.fn()))
    const root = bar()
    const childCount = root.children.length
    expect(classes(root)).not.toContain('invisible')

    rerender(controlBar(vi.fn(), true))
    expect(bar()).toBe(root)
    expect(classes(root)).toContain('invisible')
    // 只用 `visibility`：不是 `hidden`（display: none）、不是透明度，子节点一个不少。
    for (const token of ['hidden', 'opacity-0', 'pointer-events-none']) expect(classes(root)).not.toContain(token)
    expect(root.children).toHaveLength(childCount)

    rerender(controlBar(vi.fn(), false))
    expect(bar()).toBe(root)
    expect(classes(root)).not.toContain('invisible')
  })

  it('passes yielded on to the timeline: playing stops and stays stopped, and the chosen speed survives', () => {
    const onQueryChange = vi.fn()
    const { rerender } = render(controlBar(onQueryChange, false))
    const speed = () => screen.getByLabelText('播放速度') as HTMLSelectElement

    fireEvent.change(speed(), { target: { value: '2' } })
    fireEvent.click(screen.getByLabelText('播放时间轴'))
    advance(PERIOD_MS / 2)
    expect(onQueryChange).toHaveBeenCalledTimes(1)

    rerender(controlBar(onQueryChange, true))
    expect(screen.getByLabelText('播放时间轴')).toBeInTheDocument()
    advance(5 * PERIOD_MS)
    expect(onQueryChange).toHaveBeenCalledTimes(1)
    expect(speed().value).toBe('2')

    rerender(controlBar(onQueryChange, false))
    advance(5 * PERIOD_MS)
    expect(onQueryChange).toHaveBeenCalledTimes(1)
    expect(screen.getByLabelText('播放时间轴')).toBeInTheDocument()
    expect(speed().value).toBe('2')
  })
})
