import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { M11FloatingBasemapSwitcher, M11FloatingLayerSwitcher, M11OpsLink } from '@/components/map/M11FloatingControls'

/**
 * 图层面板、底图切换器与运维入口的形态分支（openspec mobile-responsive-display task 3.3，design.md D5）：
 * 移动形态 = 启动器 + 展开时才有的限高面板，面板里的行 / 按钮与桌面是同一段渲染；桌面形态与改动前相同。
 */
const LAYER_DESKTOP_CLASSES = ['absolute', 'left-4', 'top-4', 'z-[120]', 'w-max', 'max-w-52', 'p-2']
const BASEMAP_DESKTOP_CLASSES = ['absolute', 'right-16', 'top-4', 'z-[120]', 'flex', 'items-center', 'gap-0.5', 'p-1']
const OPS_DESKTOP_CLASSES = ['absolute', 'right-4', 'top-28', 'z-[120]']

function classes(element: HTMLElement) {
  return element.className.split(/\s+/)
}

/** 面板里每个按钮的可访问事实：名字、按下态、禁用态、title。 */
function buttonFacts(container: HTMLElement) {
  return within(container)
    .getAllByRole('button')
    .filter((button) => !button.hasAttribute('aria-expanded'))
    .map((button) => ({
      text: button.textContent,
      label: button.getAttribute('aria-label'),
      pressed: button.getAttribute('aria-pressed'),
      disabled: (button as HTMLButtonElement).disabled,
      ariaDisabled: button.getAttribute('aria-disabled'),
      title: button.getAttribute('title'),
      testId: button.getAttribute('data-testid'),
    }))
}

describe('M11FloatingLayerSwitcher by viewport form', () => {
  it('desktop form renders the always-on card at its desktop offset and no launcher, ignoring the expansion props', () => {
    render(<M11FloatingLayerSwitcher layer="discharge" expanded={false} onToggle={vi.fn()} panelMaxHeight={100} />)

    expect(screen.queryByTestId('m11-launcher-layers')).toBeNull()
    expect(screen.queryByTestId('m11-floating-layer-switcher-scroll')).toBeNull()
    const card = screen.getByTestId('m11-floating-layer-switcher')
    expect(classes(card)).toEqual(expect.arrayContaining(LAYER_DESKTOP_CLASSES))
    expect(card.getAttribute('style')).toBeNull()
    // 桌面行不带移动形态的触控下限类。
    for (const button of within(card).getAllByRole('button')) expect(classes(button)).not.toContain('min-h-11')
  })

  it('mobile form collapsed renders only a named launcher that reports it is collapsed', () => {
    render(<M11FloatingLayerSwitcher layer="discharge" mobile />)

    const launcher = screen.getByRole('button', { name: '图层' })
    expect(launcher).toHaveAttribute('data-testid', 'm11-launcher-layers')
    expect(launcher).toHaveAttribute('aria-expanded', 'false')
    expect(classes(launcher)).toEqual(expect.arrayContaining(['h-11', 'w-11']))
    expect(screen.queryByTestId('m11-floating-layer-switcher')).toBeNull()
  })

  it('mobile form reports each launcher activation to the shell', async () => {
    const onToggle = vi.fn()
    const user = userEvent.setup()
    render(<M11FloatingLayerSwitcher layer="discharge" mobile onToggle={onToggle} />)

    await user.click(screen.getByTestId('m11-launcher-layers'))
    expect(onToggle).toHaveBeenCalledTimes(1)
  })

  it('mobile form expanded renders the panel with a scroll container capped at the measured height', () => {
    render(<M11FloatingLayerSwitcher layer="discharge" mobile expanded panelMaxHeight={138} />)

    expect(screen.getByTestId('m11-launcher-layers')).toHaveAttribute('aria-expanded', 'true')
    const panel = screen.getByTestId('m11-floating-layer-switcher')
    expect(panel).toHaveAttribute('aria-label', '地图图层切换')
    expect(panel.style.maxHeight).toBe('138px')
    // 面板不沿用桌面偏移，锚在启动器列左侧。
    expect(classes(panel)).not.toContain('left-4')
    expect(classes(panel)).toEqual(expect.arrayContaining(['absolute', 'right-full', 'top-0']))
    const scroller = within(panel).getByTestId('m11-floating-layer-switcher-scroll')
    expect(classes(scroller)).toContain('overflow-y-auto')
    expect(within(scroller).getByTestId('m11-layer-group-hydrology')).toBeInTheDocument()
    expect(within(scroller).getByTestId('m11-layer-group-meteorology')).toBeInTheDocument()
    // 每一行带 44px 的触控下限。
    const rows = within(scroller).getAllByRole('button')
    expect(rows).toHaveLength(3)
    for (const row of rows) expect(classes(row)).toContain('min-h-11')
  })

  it.each(['available', 'absent', 'unknown'] as const)(
    'mobile form expanded carries the same rows as the desktop card (precip catalogue %s)',
    (precipAvailability) => {
      const props = { layer: 'discharge', metStations: true, precip: true, precipAvailability } as const
      const desktop = render(<M11FloatingLayerSwitcher {...props} />)
      const desktopFacts = buttonFacts(desktop.container)
      const desktopGroups = within(desktop.container)
        .getAllByRole('group')
        .map((group) => group.getAttribute('aria-label'))
      desktop.unmount()

      const mobile = render(<M11FloatingLayerSwitcher {...props} mobile expanded />)
      const scroller = within(mobile.container).getByTestId('m11-floating-layer-switcher-scroll')
      expect(buttonFacts(scroller)).toEqual(desktopFacts)
      expect(
        within(scroller)
          .getAllByRole('group')
          .map((group) => group.getAttribute('aria-label')),
      ).toEqual(desktopGroups)
      // 正对照：内容不是空的，降水开关的禁用语义原样保留。
      expect(desktopGroups).toEqual(['水文', '气象'])
      expect(desktopFacts).toHaveLength(3)
      const precipToggle = within(scroller).getByTestId('m11-layer-toggle-precip') as HTMLButtonElement
      expect(precipToggle.disabled).toBe(precipAvailability !== 'available')
      expect(precipToggle).toHaveAttribute('aria-pressed', precipAvailability === 'available' ? 'true' : 'false')
    },
  )

  it('mobile form dispatches the same query patches as the desktop card', async () => {
    const user = userEvent.setup()
    const patches = async (mobile: boolean) => {
      const onQueryChange = vi.fn()
      const view = render(
        <M11FloatingLayerSwitcher
          layer="discharge"
          precipAvailability="available"
          onQueryChange={onQueryChange}
          {...(mobile ? { mobile: true, expanded: true } : {})}
        />,
      )
      const card = within(view.container).getByTestId('m11-floating-layer-switcher')
      await user.click(within(card).getByRole('button', { name: /气象代站/ }))
      await user.click(within(card).getByRole('button', { name: /累积降水/ }))
      await user.click(within(card).getByRole('button', { name: /流量/ }))
      view.unmount()
      return onQueryChange.mock.calls.map(([patch]) => patch)
    }

    const desktopPatches = await patches(false)
    expect(await patches(true)).toEqual(desktopPatches)
    expect(desktopPatches).toEqual([{ metStations: true }, { precip: true }, { layer: 'discharge' }])
  })
})

describe('M11FloatingBasemapSwitcher by viewport form', () => {
  it('desktop form renders the always-on segmented control at its desktop offset and no launcher', () => {
    render(<M11FloatingBasemapSwitcher basemap="vector" expanded={false} onToggle={vi.fn()} panelMaxHeight={100} />)

    expect(screen.queryByTestId('m11-launcher-basemap')).toBeNull()
    expect(screen.queryByTestId('m11-floating-basemap-switcher-scroll')).toBeNull()
    const control = screen.getByTestId('m11-floating-basemap-switcher')
    expect(classes(control)).toEqual(expect.arrayContaining(BASEMAP_DESKTOP_CLASSES))
    expect(control.getAttribute('style')).toBeNull()
    const buttons = within(control).getAllByRole('button')
    expect(buttons).toHaveLength(3)
    for (const button of buttons) {
      expect(classes(button)).toContain('h-8')
      expect(classes(button)).not.toContain('h-11')
      // 按钮直接挂在分段控件下，没有多出来的包裹层。
      expect(button.parentElement).toBe(control)
    }
  })

  it('mobile form collapsed renders only a named launcher that reports it is collapsed', () => {
    render(<M11FloatingBasemapSwitcher basemap="vector" mobile />)

    const launcher = screen.getByRole('button', { name: '底图' })
    expect(launcher).toHaveAttribute('data-testid', 'm11-launcher-basemap')
    expect(launcher).toHaveAttribute('aria-expanded', 'false')
    expect(classes(launcher)).toEqual(expect.arrayContaining(['h-11', 'w-11']))
    expect(screen.queryByTestId('m11-floating-basemap-switcher')).toBeNull()
  })

  it('mobile form reports each launcher activation to the shell', async () => {
    const onToggle = vi.fn()
    const user = userEvent.setup()
    render(<M11FloatingBasemapSwitcher basemap="vector" mobile onToggle={onToggle} />)

    await user.click(screen.getByTestId('m11-launcher-basemap'))
    expect(onToggle).toHaveBeenCalledTimes(1)
  })

  it('mobile form expanded renders the panel with a scroll container and 44px buttons', () => {
    render(<M11FloatingBasemapSwitcher basemap="satellite" mobile expanded panelMaxHeight={138} />)

    expect(screen.getByTestId('m11-launcher-basemap')).toHaveAttribute('aria-expanded', 'true')
    const panel = screen.getByTestId('m11-floating-basemap-switcher')
    expect(panel).toHaveAttribute('role', 'group')
    expect(panel).toHaveAttribute('aria-label', '底图切换')
    expect(panel.style.maxHeight).toBe('138px')
    expect(classes(panel)).not.toContain('right-16')
    expect(classes(panel)).toEqual(expect.arrayContaining(['absolute', 'right-full', 'top-0']))
    const scroller = within(panel).getByTestId('m11-floating-basemap-switcher-scroll')
    expect(classes(scroller)).toContain('overflow-y-auto')
    const buttons = within(scroller).getAllByRole('button')
    expect(buttons).toHaveLength(3)
    for (const button of buttons) {
      expect(classes(button)).toEqual(expect.arrayContaining(['h-11', 'min-w-11']))
      expect(classes(button)).not.toContain('h-8')
    }
  })

  it('mobile form expanded carries the same buttons and dispatches the same patches as the desktop control', async () => {
    const user = userEvent.setup()
    const run = async (mobile: boolean) => {
      const onQueryChange = vi.fn()
      const view = render(
        <M11FloatingBasemapSwitcher basemap="terrain" onQueryChange={onQueryChange} {...(mobile ? { mobile: true, expanded: true } : {})} />,
      )
      const control = within(view.container).getByTestId('m11-floating-basemap-switcher')
      const facts = buttonFacts(control)
      for (const name of ['卫星底图', '矢量底图', '地形底图']) await user.click(within(control).getByRole('button', { name }))
      view.unmount()
      return { facts, patches: onQueryChange.mock.calls.map(([patch]) => patch) }
    }

    const desktop = await run(false)
    expect(await run(true)).toEqual(desktop)
    expect(desktop.facts.map((fact) => [fact.label, fact.pressed])).toEqual([
      ['矢量底图', 'false'],
      ['卫星底图', 'false'],
      ['地形底图', 'true'],
    ])
    expect(desktop.patches).toEqual([{ basemap: 'satellite' }, { basemap: 'vector' }, { basemap: 'terrain' }])
  })
})

describe('M11OpsLink by viewport form', () => {
  function renderLink(mobile?: boolean) {
    return render(
      <MemoryRouter>
        <M11OpsLink visible {...(mobile === undefined ? {} : { mobile })} />
      </MemoryRouter>,
    )
  }

  it('desktop form keeps the absolute offset under the zoom control', () => {
    renderLink()
    const link = screen.getByTestId('m11-ops-link')
    expect(classes(link)).toEqual(expect.arrayContaining(OPS_DESKTOP_CLASSES))
    expect(classes(link)).not.toContain('order-last')
  })

  it('mobile form is an in-flow column item ordered after the launchers, with the same target and text', () => {
    renderLink(true)
    const link = screen.getByTestId('m11-ops-link')
    for (const token of OPS_DESKTOP_CLASSES) expect(classes(link)).not.toContain(token)
    expect(classes(link)).toContain('order-last')
    expect(link).toHaveAttribute('href', '/ops')
    expect(link).toHaveTextContent('运维')
  })

  it('mobile form still renders nothing for non-operator roles', () => {
    render(
      <MemoryRouter>
        <M11OpsLink visible={false} mobile />
      </MemoryRouter>,
    )
    expect(screen.queryByTestId('m11-ops-link')).toBeNull()
  })
})
