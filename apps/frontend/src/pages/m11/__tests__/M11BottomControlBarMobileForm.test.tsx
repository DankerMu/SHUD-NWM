import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  activeCycleValidTimesErrorDisabledReason,
  failClosedDischargeDisabledReason,
  normalizeLayerStates,
  type LayerState,
} from '@/lib/m11/overviewDataContracts'
import { defaultM11QueryState, type M11QueryPatch, type M11QueryState } from '@/lib/m11/queryState'
import { M11BottomControlBar, deriveM11ControlBarModel } from '@/pages/m11/M11BottomControlBar'
import { M11Timeline } from '@/pages/m11/M11Controls'
import { installMobileFormMatchMedia } from '@/test/mobileFormMatchMedia'

/**
 * 控制条的形态分支（openspec mobile-responsive-display task 3.7，design.md D6）：
 * 竖屏（移动形态且非矮视口横屏）把播放速度选择器上提到控制条第一行，矮视口横屏与桌面仍在时间轴里；
 * 速度值与 `playing` 不因形态翻转丢失，`M11Timeline` 不重挂。
 * 形态用用例内可控的 `matchMedia` 桩造出，用后还原（全局桩恒为桌面）。
 */
const DEFAULT_CYCLE = '2026-05-18T00:00:00Z'
const VALID_TIMES = Array.from({ length: 9 }, (_, index) =>
  new Date(Date.parse(DEFAULT_CYCLE) + index * 3 * 3_600_000).toISOString().replace('.000Z', 'Z'),
)
/** `LayerState.validTimes` 的毫秒形（`normalizeIsoString` 的产物）。 */
const MILLISECOND_VALID_TIMES = VALID_TIMES.map((validTime) => new Date(Date.parse(validTime)).toISOString())

type CatalogMetadata = Record<string, unknown>

function dischargeMetadata(overrides: CatalogMetadata = {}): CatalogMetadata {
  return {
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
    ...overrides,
  }
}

function layersFor(
  query: M11QueryState,
  metadata: CatalogMetadata,
  activeCycleValidTimes?: Parameters<typeof normalizeLayerStates>[0]['activeCycleValidTimes'],
): LayerState[] {
  return normalizeLayerStates({
    query,
    layers: [
      {
        layer_id: 'discharge',
        layer_name: 'Discharge',
        layer_type: 'hydrology',
        variables: ['q_down'],
        metadata: metadata as never,
      },
    ],
    activeCycleValidTimes,
  })
}

const PORTRAIT = { mobile: true, landscape: false }
const SHORT_LANDSCAPE = { mobile: true, landscape: true }
const DESKTOP = { mobile: false, landscape: false }
type Form = typeof PORTRAIT

let form: ReturnType<typeof installMobileFormMatchMedia> | null = null

function installForm(initial: Form) {
  form = installMobileFormMatchMedia(initial.mobile, initial.landscape)
  return form
}

function flipTo(next: Form) {
  act(() => form!.setForm(next))
}

function renderControlBar(
  options: {
    query?: M11QueryState
    metadata?: CatalogMetadata
    layers?: LayerState[]
    onQueryChange?: (patch: M11QueryPatch) => void
  } = {},
) {
  const query = options.query ?? defaultM11QueryState
  const metadata = options.metadata ?? dischargeMetadata()
  const onQueryChange = options.onQueryChange ?? vi.fn()
  const model = deriveM11ControlBarModel({
    state: query,
    layers: options.layers ?? layersFor(query, metadata),
    metadata: metadata as never,
    cyclesBySource: {},
    sourceSelection: null,
  })
  const view = render(<M11BottomControlBar {...model} onQueryChange={onQueryChange} />)
  return { ...view, model, onQueryChange }
}

const bar = () => screen.getByTestId('m11-bottom-control-bar')
const timeline = () => screen.getByTestId('m11-timeline')
const speedSelects = () => screen.getAllByLabelText('播放速度') as HTMLSelectElement[]
const speedSelect = () => {
  const selects = speedSelects()
  expect(selects, '任一形态下页面上恰有一个「播放速度」选择器').toHaveLength(1)
  return selects[0]
}
const classes = (element: Element) => element.className.split(/\s+/)

afterEach(() => {
  form?.restore()
  form = null
  vi.useRealTimers()
})

describe('M11BottomControlBar 形态分支', () => {
  it('竖屏：播放速度选择器在控制条里、不在时间轴里，恰一个', () => {
    installForm(PORTRAIT)
    renderControlBar()

    const select = speedSelect()
    expect(bar().contains(select)).toBe(true)
    expect(timeline().contains(select)).toBe(false)
    expect(within(timeline()).queryByLabelText('播放速度')).toBeNull()
    expect(select.disabled).toBe(false)
  })

  it.each([
    ['桌面', DESKTOP],
    ['矮视口横屏', SHORT_LANDSCAPE],
  ])('%s：播放速度选择器在时间轴里，恰一个', (_name, initial) => {
    installForm(initial)
    renderControlBar()

    expect(timeline().contains(speedSelect())).toBe(true)
  })

  it('竖屏：子节点顺序 = 预报源分段、起报时次、播放速度、时间轴、禁用原因，没有包裹元素', () => {
    installForm(PORTRAIT)
    const metadata = dischargeMetadata({ valid_times: [], default_cycle: null })
    renderControlBar({ metadata })

    const children = [...bar().children]
    expect(children).toHaveLength(5)
    expect(children[0]).toBe(screen.getByRole('group', { name: '预报源' }))
    expect(children[1].contains(screen.getByLabelText('起报时次'))).toBe(true)
    expect(children[2].contains(speedSelect())).toBe(true)
    expect(children[3]).toBe(timeline())
    expect(children[4]).toBe(screen.getByTestId('m11-control-bar-disabled-reason'))
    expect(children[4].textContent).toBe(failClosedDischargeDisabledReason)
  })

  it('竖屏：条高不套 64px token 类、根节点可换行、时间轴占满整行；矮视口横屏与桌面仍套 h-16 且不换行', () => {
    installForm(PORTRAIT)
    renderControlBar()

    expect(classes(bar())).not.toContain('h-16')
    expect(classes(bar())).toContain('flex-wrap')
    expect(classes(bar())).toContain('bottom-10')
    expect(classes(timeline())).toContain('basis-full')

    for (const next of [SHORT_LANDSCAPE, DESKTOP]) {
      flipTo(next)
      expect(classes(bar())).toContain('h-16')
      expect(classes(bar())).not.toContain('flex-wrap')
      expect(classes(bar())).toContain('bottom-10')
      expect(timeline().className).toBe('flex min-w-0 flex-1 items-center gap-3 text-sm')
    }
  })

  it('改速度后翻转形态，速度值保持，且始终恰一个选择器', () => {
    installForm(PORTRAIT)
    renderControlBar()

    fireEvent.change(speedSelect(), { target: { value: '4' } })
    expect(speedSelect().value).toBe('4')

    flipTo(DESKTOP)
    expect(timeline().contains(speedSelect())).toBe(true)
    expect(speedSelect().value).toBe('4')

    fireEvent.change(speedSelect(), { target: { value: '2' } })
    flipTo(SHORT_LANDSCAPE)
    expect(timeline().contains(speedSelect())).toBe(true)
    expect(speedSelect().value).toBe('2')

    flipTo(PORTRAIT)
    expect(timeline().contains(speedSelect())).toBe(false)
    expect(speedSelect().value).toBe('2')
  })

  it('形态翻转前后时间轴是同一个 DOM 节点，播放中翻转后仍在播放', () => {
    installForm(PORTRAIT)
    renderControlBar()

    const before = timeline()
    fireEvent.click(screen.getByLabelText('播放时间轴'))
    expect(screen.getByLabelText('暂停时间轴')).toBeTruthy()

    for (const next of [SHORT_LANDSCAPE, DESKTOP, PORTRAIT]) {
      flipTo(next)
      expect(timeline()).toBe(before)
      expect(screen.getByLabelText('暂停时间轴')).toBeTruthy()
      expect(screen.queryByLabelText('播放时间轴')).toBeNull()
    }
  })

  it('上提后的速度驱动播放间隔：竖屏 2x 时 500ms 前进一步', () => {
    vi.useFakeTimers()
    installForm(PORTRAIT)
    const onQueryChange = vi.fn()
    renderControlBar({ onQueryChange })

    fireEvent.change(speedSelect(), { target: { value: '2' } })
    fireEvent.click(screen.getByLabelText('播放时间轴'))

    act(() => {
      vi.advanceTimersByTime(499)
    })
    expect(onQueryChange).not.toHaveBeenCalled()
    act(() => {
      vi.advanceTimersByTime(1)
    })
    expect(onQueryChange).toHaveBeenCalledTimes(1)
    expect(onQueryChange).toHaveBeenLastCalledWith({ validTime: MILLISECOND_VALID_TIMES[1] })
  })

  it('竖屏 fail-closed：上提的速度选择器与其余每个选择器、按钮、滑块都禁用', () => {
    installForm(PORTRAIT)
    const metadata = dischargeMetadata({ valid_times: [], default_cycle: null })
    renderControlBar({ metadata })

    expect(screen.getByTestId('m11-control-bar-disabled-reason').textContent).toBe(failClosedDischargeDisabledReason)
    const select = speedSelect()
    expect(bar().contains(select)).toBe(true)
    expect(timeline().contains(select)).toBe(false)
    const selects = [...bar().querySelectorAll('select')]
    expect(selects).toHaveLength(2)
    for (const candidate of selects) expect(candidate.disabled).toBe(true)
    for (const name of ['上一个有效时刻', '播放时间轴', '下一个有效时刻']) {
      expect((screen.getByLabelText(name) as HTMLButtonElement).disabled).toBe(true)
    }
    expect((screen.getByLabelText('有效时间滑块') as HTMLInputElement).disabled).toBe(true)
  })

  it('竖屏、非 fail-closed 的空有效时刻列表：速度选择器禁用、起报时次仍可用', () => {
    installForm(PORTRAIT)
    const onQueryChange = vi.fn()
    const query = { ...defaultM11QueryState, cycle: '2026-05-17T12:00:00.000Z' }
    const metadata = dischargeMetadata()
    renderControlBar({
      query,
      metadata,
      layers: layersFor(query, metadata, { discharge: { status: 'error' } }),
      onQueryChange,
    })

    expect(screen.getByTestId('m11-control-bar-disabled-reason').textContent).toBe(activeCycleValidTimesErrorDisabledReason)
    const select = speedSelect()
    expect(bar().contains(select)).toBe(true)
    expect(timeline().contains(select)).toBe(false)
    expect(select.disabled).toBe(true)

    const cycle = screen.getByLabelText('起报时次') as HTMLSelectElement
    expect(cycle.disabled).toBe(false)
    fireEvent.change(cycle, { target: { value: DEFAULT_CYCLE } })
    expect(onQueryChange).toHaveBeenLastCalledWith({ cycle: DEFAULT_CYCLE })
  })

  it('三个控件的 patch 与形态无关：切源清 cycle、下一步写 validTime', () => {
    const patches = (initial: Form) => {
      installForm(initial)
      const onQueryChange = vi.fn()
      const view = renderControlBar({ onQueryChange })
      fireEvent.click(screen.getByRole('button', { name: 'IFS' }))
      fireEvent.click(screen.getByRole('button', { name: 'GFS' }))
      fireEvent.click(screen.getByLabelText('下一个有效时刻'))
      fireEvent.change(screen.getByLabelText('有效时间滑块'), { target: { value: '5' } })
      view.unmount()
      form?.restore()
      form = null
      return onQueryChange.mock.calls.map(([patch]) => patch)
    }

    const desktop = patches(DESKTOP)
    expect(desktop).toEqual([
      { source: 'ifs', cycle: null },
      { validTime: MILLISECOND_VALID_TIMES[1] },
      { validTime: MILLISECOND_VALID_TIMES[5] },
    ])
    expect(patches(PORTRAIT)).toEqual(desktop)
    expect(patches(SHORT_LANDSCAPE)).toEqual(desktop)
  })
})

describe('M11Timeline 的播放速度受控入口', () => {
  const query = { ...defaultM11QueryState }
  const layers = layersFor(query, dischargeMetadata())

  it('不传新 props：自己持有速度并渲染选择器（与今天相同）', () => {
    vi.useFakeTimers()
    const onQueryChange = vi.fn()
    render(<M11Timeline state={query} layers={layers} cycle={DEFAULT_CYCLE} onQueryChange={onQueryChange} />)

    const select = speedSelect()
    expect(timeline().contains(select)).toBe(true)
    expect(select.value).toBe('1')
    fireEvent.change(select, { target: { value: '4' } })
    expect(speedSelect().value).toBe('4')

    fireEvent.click(screen.getByLabelText('播放时间轴'))
    act(() => {
      vi.advanceTimersByTime(249)
    })
    expect(onQueryChange).not.toHaveBeenCalled()
    act(() => {
      vi.advanceTimersByTime(1)
    })
    expect(onQueryChange).toHaveBeenCalledTimes(1)
  })

  it('受控：显示传入的速度，变更只经回调上报', () => {
    const onSpeedChange = vi.fn()
    render(
      <M11Timeline state={query} layers={layers} cycle={DEFAULT_CYCLE} onQueryChange={vi.fn()} speed={2} onSpeedChange={onSpeedChange} />,
    )

    expect(speedSelect().value).toBe('2')
    fireEvent.change(speedSelect(), { target: { value: '4' } })
    expect(onSpeedChange).toHaveBeenCalledTimes(1)
    expect(onSpeedChange).toHaveBeenLastCalledWith(4)
    // 调用方没把新值传回来：选择器仍显示受控值。
    expect(speedSelect().value).toBe('2')
  })

  it('受控且不渲染选择器：时间轴里没有速度选择器，传入的速度仍驱动播放间隔', () => {
    vi.useFakeTimers()
    const onQueryChange = vi.fn()
    render(
      <M11Timeline
        state={query}
        layers={layers}
        cycle={DEFAULT_CYCLE}
        onQueryChange={onQueryChange}
        speed={4}
        onSpeedChange={vi.fn()}
        renderSpeedSelect={false}
      />,
    )

    expect(screen.queryByLabelText('播放速度')).toBeNull()
    fireEvent.click(screen.getByLabelText('播放时间轴'))
    act(() => {
      vi.advanceTimersByTime(249)
    })
    expect(onQueryChange).not.toHaveBeenCalled()
    act(() => {
      vi.advanceTimersByTime(1)
    })
    expect(onQueryChange).toHaveBeenCalledTimes(1)
    expect(onQueryChange).toHaveBeenLastCalledWith({ validTime: MILLISECOND_VALID_TIMES[1] })
  })
})
