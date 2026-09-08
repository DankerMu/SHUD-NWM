import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { BrowserRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import {
  M11BackToOverviewButton,
  M11FloatingBasemapSwitcher,
  M11FloatingLayerSwitcher,
  M11FloatingLegend,
  M11FloatingNotice,
  M11OpsLink,
} from '@/components/map/M11FloatingControls'
import type { LayerState } from '@/lib/m11/overviewDataContracts'
import { precipLegend } from '@/test/overviewDataFixture'

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
  // 图例/浮层不消费全国周期章（本 fixture 的 metadata 就是 null）。
  activeNationalCycle: null,
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

/**
 * 两张浮层卡片共用的宽度不变量：宽度跟着内容走、只保留一个上限。
 * jsdom 不做布局，量不出像素，所以守的是意图——回填 `w-52`/`w-56` 之类的
 * 固定宽度会让调用方变红。真实像素由 headless 浏览器量测覆盖（见 PR 证据）。
 */
function expectSizedToContent(card: HTMLElement) {
  const classes = card.className.split(/\s+/)
  expect(classes).toContain('w-max')
  expect(classes.filter((name) => /^w-(?!max$|fit$)/.test(name))).toHaveLength(0)
  expect(classes.some((name) => name.startsWith('max-w-'))).toBe(true)
}

/**
 * 底部偏移不变量：按**空白切 token** 比对，不用子串。
 * 朴素的 `not.toContain('bottom-4')` 会被 `bottom-40` 假红（反之 `toContain('bottom-4')` 会被
 * `bottom-40` 假绿），而这三个类名恰好两两互为前缀。
 */
function expectBottomOffset(element: HTMLElement, expected: string, replaced: string) {
  const classes = element.className.split(/\s+/)
  expect(classes).toContain(expected)
  expect(classes).not.toContain(replaced)
}

/** jsdom 把 `style.backgroundColor` 归一成 `rgb(...)`；用同一条 CSSOM 路径把期望值也归一，
 * 避免测试里再手写一份 hex→rgb 换算（那就是第二份会漂的颜色表示）。 */
function normalizedColor(color: string) {
  const probe = document.createElement('span')
  probe.style.backgroundColor = color
  return probe.style.backgroundColor
}

function precipSwatchColors() {
  return screen.getAllByTestId('m11-floating-legend-precip-swatch').map((node) => node.style.backgroundColor)
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

  it('sizes the card to its widest row instead of pinning a fixed width', () => {
    render(<M11FloatingLayerSwitcher layer="discharge" />)
    expectSizedToContent(screen.getByTestId('m11-floating-layer-switcher'))
  })

  it('dispatches false when disabling the station overlay toggle', async () => {
    const onQueryChange = vi.fn()
    const user = userEvent.setup()
    render(<M11FloatingLayerSwitcher layer="discharge" metStations onQueryChange={onQueryChange} />)

    await user.click(screen.getByRole('button', { name: /气象代站/, pressed: true }))
    expect(onQueryChange).toHaveBeenCalledWith({ metStations: false })
  })

  it('groups hydrology and meteorology controls and puts the precipitation toggle in meteorology', () => {
    render(<M11FloatingLayerSwitcher layer="discharge" precip precipAvailable />)

    const hydrology = screen.getByRole('group', { name: '水文' })
    const meteorology = screen.getByRole('group', { name: '气象' })
    // 流量留在水文组、降水与代站在气象组：结构断言，不靠文本相邻。
    expect(within(hydrology).getByRole('button', { name: /流量/ })).toBeInTheDocument()
    expect(within(meteorology).getByRole('button', { name: /过去 24h 累积降水/ })).toBeInTheDocument()
    expect(within(meteorology).getByRole('button', { name: /气象代站/ })).toBeInTheDocument()
    expect(within(hydrology).queryByRole('button', { name: /过去 24h 累积降水/ })).not.toBeInTheDocument()
  })

  it('marks the precipitation toggle pressed and implemented when the catalog serves a precip entry', () => {
    render(<M11FloatingLayerSwitcher layer="discharge" precip precipAvailable />)

    const toggle = screen.getByRole('button', { name: /过去 24h 累积降水/ }) as HTMLButtonElement
    expect(toggle.getAttribute('aria-pressed')).toBe('true')
    expect(toggle.disabled).toBe(false)
    expect(toggle.getAttribute('aria-disabled')).toBe('false')
    expect(toggle.textContent).not.toContain('未实现')
  })

  it('dispatches precip=false when the operator turns the precipitation overlay off', async () => {
    const onQueryChange = vi.fn()
    const user = userEvent.setup()
    render(<M11FloatingLayerSwitcher layer="discharge" precip precipAvailable onQueryChange={onQueryChange} />)

    await user.click(screen.getByRole('button', { name: /过去 24h 累积降水/, pressed: true }))
    expect(onQueryChange).toHaveBeenCalledWith({ precip: false })
  })

  it('dispatches precip=true when the overlay is currently off', async () => {
    const onQueryChange = vi.fn()
    const user = userEvent.setup()
    render(<M11FloatingLayerSwitcher layer="discharge" precip={false} precipAvailable onQueryChange={onQueryChange} />)

    await user.click(screen.getByRole('button', { name: /过去 24h 累积降水/, pressed: false }))
    expect(onQueryChange).toHaveBeenCalledWith({ precip: true })
  })

  it.each([
    ['explicitly unavailable', { precipAvailable: false }],
    ['prop omitted', {}],
  ])('disables the precipitation toggle and marks it 未实现 when the catalog has no precip entry (%s)', async (
    _label,
    props: { precipAvailable?: boolean },
  ) => {
    const onQueryChange = vi.fn()
    const user = userEvent.setup()
    render(<M11FloatingLayerSwitcher layer="discharge" precip onQueryChange={onQueryChange} {...props} />)

    const toggle = screen.getByRole('button', { name: /过去 24h 累积降水/ }) as HTMLButtonElement
    expect(toggle.disabled).toBe(true)
    expect(toggle.getAttribute('aria-disabled')).toBe('true')
    expect(toggle.getAttribute('title')).toBe('降水叠加未实现')
    expect(toggle.textContent).toContain('未实现')
    // 禁用却显示"已按下"就是在假装该图层正在渲染（spec 明令禁止）。
    expect(toggle.getAttribute('aria-pressed')).toBe('false')

    await user.click(toggle)
    expect(onQueryChange).not.toHaveBeenCalled()
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

  it('sizes the card to its widest row instead of pinning a fixed width', () => {
    render(<M11FloatingLegend layer="discharge" layers={[dischargeLayer]} />)
    expectSizedToContent(screen.getByTestId('m11-floating-legend'))
  })

  it('keeps the legend tied to the hydrology layer while stations are an overlay', () => {
    render(<M11FloatingLegend layer="discharge" layers={[{ ...dischargeLayer, legend: [] }]} />)
    expect(screen.getByText('径流量图例')).toBeInTheDocument()
    expect(screen.getByTestId('m11-floating-legend-entries')).toBeInTheDocument()
  })

  it('renders the six-class precipitation legend beneath the discharge legend inside the same card', () => {
    render(<M11FloatingLegend layer="discharge" layers={[dischargeLayer]} precipLegend={precipLegend} />)

    const card = screen.getByTestId('m11-floating-legend')
    const dischargeBlock = screen.getByTestId('m11-floating-legend-entries')
    const precipBlock = screen.getByTestId('m11-floating-legend-precip')
    // 同一张卡片内（并列第二张浮层会与右下角这张像素重叠），且排在流量段**之后**。
    expect(card).toContainElement(precipBlock)
    expect(dischargeBlock.compareDocumentPosition(precipBlock) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // 标题必须自报单位，否则六级色阶读不出量纲（spec「Legend shows both layers」）。
    expect(within(card).getByText(/mm\/24h/)).toBeInTheDocument()
  })

  it('takes every precipitation swatch colour and threshold text from the legend data, in order', () => {
    render(<M11FloatingLegend layer="discharge" layers={[dischargeLayer]} precipLegend={precipLegend} />)

    expect(precipSwatchColors()).toEqual(precipLegend.map((entry) => normalizedColor(entry.color)))
    expect(
      screen.getAllByTestId('m11-floating-legend-precip-row').map((row) => row.textContent),
    ).toEqual(precipLegend.map((entry) => entry.label))
  })

  it('follows the legend data when a swatch colour and a threshold label change instead of a hardcoded palette', () => {
    const mutatedColor = '#123456'
    // spec「Legend shows both layers」说的是 colours **and** thresholds：只变色的话，一张写死
    // 六条 fixture 阈值文字的前端表格照样全绿（行数仍来自数据数组，长度对得上）。
    const mutatedLabel = '0.1-12'
    // 前置条件：这两个值都不在 fixture 里，否则"跟着变"与"写死"无法区分。
    expect(precipLegend.map((entry) => entry.color)).not.toContain(mutatedColor)
    expect(precipLegend.map((entry) => entry.label)).not.toContain(mutatedLabel)
    // 复制后改，不动共享 fixture 导出。
    const mutated = precipLegend.map((entry, index) =>
      index === 0 ? { ...entry, color: mutatedColor, label: mutatedLabel } : entry,
    )

    const { rerender } = render(
      <M11FloatingLegend layer="discharge" layers={[dischargeLayer]} precipLegend={precipLegend} />,
    )
    expect(precipSwatchColors()[0]).toBe(normalizedColor(precipLegend[0].color))
    expect(screen.getAllByTestId('m11-floating-legend-precip-row')[0].textContent).toBe(precipLegend[0].label)

    rerender(<M11FloatingLegend layer="discharge" layers={[dischargeLayer]} precipLegend={mutated} />)
    expect(precipSwatchColors()).toEqual(mutated.map((entry) => normalizedColor(entry.color)))
    expect(precipSwatchColors()[0]).toBe(normalizedColor(mutatedColor))
    expect(screen.getAllByTestId('m11-floating-legend-precip-row').map((row) => row.textContent)).toEqual(
      mutated.map((entry) => entry.label),
    )
  })

  it.each([
    ['prop omitted', {}],
    ['prop null', { precipLegend: null }],
    ['prop empty', { precipLegend: [] }],
  ])('renders no precipitation legend section when there is no legend data (%s)', (
    _label,
    props: { precipLegend?: typeof precipLegend | null },
  ) => {
    render(<M11FloatingLegend layer="discharge" layers={[dischargeLayer]} {...props} />)

    expect(screen.queryByTestId('m11-floating-legend-precip')).toBeNull()
    expect(screen.queryAllByTestId('m11-floating-legend-precip-swatch')).toHaveLength(0)
    expect(within(screen.getByTestId('m11-floating-legend')).queryByText(/mm\/24h/)).toBeNull()
    // 流量图例不受影响。
    expect(screen.getByTestId('m11-floating-legend-entries')).toBeInTheDocument()
  })

  it('keeps sizing the card to its content once the precipitation section is present', () => {
    render(<M11FloatingLegend layer="discharge" layers={[dischargeLayer]} precipLegend={precipLegend} />)
    expectSizedToContent(screen.getByTestId('m11-floating-legend'))
  })
})

/**
 * spec map-layer-timeline-controls「Floating controls clear the control bar」：
 * `M11BottomControlBar` 自身 `bottom-4` + 固定 `h-16`（64px）占据 16–80px 这一带，
 * 三个浮层必须整体抬到它上面。
 */
describe('floating controls clear the bottom control bar', () => {
  it('lifts the legend from bottom-12 to bottom-24', () => {
    render(<M11FloatingLegend layer="discharge" layers={[dischargeLayer]} />)
    expectBottomOffset(screen.getByTestId('m11-floating-legend'), 'bottom-24', 'bottom-12')
  })

  it('lifts the back-to-overview button from bottom-4 to bottom-24', () => {
    render(<M11BackToOverviewButton onClick={vi.fn()} />)
    expectBottomOffset(screen.getByTestId('m11-back-to-overview'), 'bottom-24', 'bottom-4')
  })

  it('lifts the floating notice from bottom-20 to bottom-40', () => {
    render(<M11FloatingNotice testId="m11-offset-notice">降水提示</M11FloatingNotice>)
    expectBottomOffset(screen.getByTestId('m11-offset-notice'), 'bottom-40', 'bottom-20')
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
