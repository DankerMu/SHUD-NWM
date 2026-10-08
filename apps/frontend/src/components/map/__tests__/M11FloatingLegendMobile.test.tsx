import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { M11FloatingLegend } from '@/components/map/M11FloatingControls'
import type { LayerState } from '@/lib/m11/overviewDataContracts'

/**
 * 图例的形态分支（openspec mobile-responsive-display task 3.2，design.md D5）：
 * 移动形态 = 启动器 + 展开时才有的限高面板；桌面形态 = 常驻面板，与改动前相同。
 */
const dischargeLayer = {
  layerId: 'discharge',
  legend: [
    { label: '<1 m³/s', color: '#7FB8DC' },
    { label: '1-10 m³/s', color: '#4292C6' },
  ],
} as unknown as LayerState

const precipLegend = [
  { min: 0.1, max: 10, color: '#A6F28F', label: '0.1-10' },
  { min: 250, max: null, color: '#800040', label: '≥250' },
]

function legendTexts(container: HTMLElement) {
  return {
    discharge: Array.from(within(container).getByTestId('m11-floating-legend-entries').children).map((row) => row.textContent),
    precip: within(container)
      .getAllByTestId('m11-floating-legend-precip-row')
      .map((row) => row.textContent),
  }
}

describe('M11FloatingLegend by viewport form', () => {
  it('desktop form renders the always-on panel at its desktop offset and no launcher', () => {
    render(<M11FloatingLegend layer="discharge" layers={[dischargeLayer]} precipLegend={precipLegend} />)

    expect(screen.queryByTestId('m11-launcher-legend')).toBeNull()
    expect(screen.queryByTestId('m11-floating-legend-scroll')).toBeNull()
    const panel = screen.getByTestId('m11-floating-legend')
    expect(panel.className.split(/\s+/)).toEqual(expect.arrayContaining(['absolute', 'bottom-[7.5rem]', 'right-4', 'z-[120]']))
    expect(panel.getAttribute('style')).toBeNull()
  })

  it('desktop form ignores the expansion props', () => {
    const onToggle = vi.fn()
    render(<M11FloatingLegend layer="discharge" layers={[dischargeLayer]} expanded={false} onToggle={onToggle} panelMaxHeight={100} />)

    expect(screen.getByTestId('m11-floating-legend')).toBeInTheDocument()
    expect(screen.getByTestId('m11-floating-legend').getAttribute('style')).toBeNull()
    expect(screen.queryByTestId('m11-launcher-legend')).toBeNull()
  })

  it('mobile form collapsed renders only a named launcher that reports it is collapsed', () => {
    render(<M11FloatingLegend layer="discharge" layers={[dischargeLayer]} precipLegend={precipLegend} mobile />)

    const launcher = screen.getByRole('button', { name: '图例' })
    expect(launcher).toHaveAttribute('data-testid', 'm11-launcher-legend')
    expect(launcher).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByTestId('m11-floating-legend')).toBeNull()
  })

  it('mobile form reports each launcher activation to the shell', async () => {
    const onToggle = vi.fn()
    const user = userEvent.setup()
    render(<M11FloatingLegend layer="discharge" layers={[dischargeLayer]} mobile onToggle={onToggle} />)

    await user.click(screen.getByTestId('m11-launcher-legend'))
    expect(onToggle).toHaveBeenCalledTimes(1)
  })

  it('mobile form expanded renders the panel with a scroll container capped at the measured height', () => {
    render(
      <M11FloatingLegend layer="discharge" layers={[dischargeLayer]} precipLegend={precipLegend} mobile expanded panelMaxHeight={138} />,
    )

    expect(screen.getByTestId('m11-launcher-legend')).toHaveAttribute('aria-expanded', 'true')
    const panel = screen.getByTestId('m11-floating-legend')
    expect(panel.style.maxHeight).toBe('138px')
    // 面板不沿用桌面偏移。
    expect(panel.className.split(/\s+/)).not.toContain('bottom-[7.5rem]')
    const scroller = within(panel).getByTestId('m11-floating-legend-scroll')
    expect(scroller.className.split(/\s+/)).toContain('overflow-y-auto')
    expect(within(scroller).getByTestId('m11-floating-legend-entries')).toBeInTheDocument()
  })

  it('mobile form expanded carries the same legend content as the desktop panel', () => {
    const desktop = render(<M11FloatingLegend layer="discharge" layers={[dischargeLayer]} precipLegend={precipLegend} />)
    const desktopTexts = legendTexts(desktop.container)
    const desktopTitle = within(desktop.container).getByTestId('m11-floating-legend-precip').textContent
    desktop.unmount()

    const mobile = render(<M11FloatingLegend layer="discharge" layers={[dischargeLayer]} precipLegend={precipLegend} mobile expanded />)
    // 在移动面板的滚动容器里找：桌面面板没有这个容器。
    const scroller = within(mobile.container).getByTestId('m11-floating-legend-scroll')
    expect(legendTexts(scroller)).toEqual(desktopTexts)
    expect(within(scroller).getByTestId('m11-floating-legend-precip').textContent).toBe(desktopTitle)
    // 正对照：内容不是空的。
    expect(desktopTexts).toEqual({ discharge: ['<1 m³/s', '1-10 m³/s'], precip: ['0.1-10', '≥250'] })
    expect(desktopTitle).toContain('mm/24h')
  })
})
