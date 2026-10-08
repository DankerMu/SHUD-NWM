import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { M11FloatingNotice } from '@/components/map/M11FloatingControls'
import { M11MapStatusOverlays } from '@/components/map/m11MapRuntime'

/**
 * 浮动提示与状态条的移动定位（openspec mobile-responsive-display task 3.4）在结构上的保证：
 * 四条状态条是同一个容器的子项、各带两行截断类，桌面形态的定位类原样保留。
 * 真实几何（不相交、不超过两行）由 `e2e/m11-notices.mobile.mocked.spec.ts` 在浏览器里量。
 */

const STATUS_CONTAINER = 'm11-map-status-overlays'
/** 四条状态条在容器内的 DOM 顺序（即移动形态自上而下的顺序）与各自的桌面 top 类。 */
const STATUS_ITEMS = [
  { testId: 'm11-basin-layer-unavailable', desktopTop: 'top-20', text: '当前没有可见流域边界。' },
  { testId: 'm11-map-unavailable', desktopTop: 'top-20', text: '地图暂不可用' },
  { testId: 'm11-selected-segment-map-unavailable', desktopTop: 'top-44', text: '选中河段不可用' },
  { testId: 'm11-map-source-error', desktopTop: 'top-32', text: '地图源错误' },
] as const
/** 改动前每条状态条的完整 className（`${top}` 为各条自己的 top 类）。 */
const desktopStatusClassName = (top: string) =>
  `absolute left-1/2 -translate-x-1/2 ${top} z-[90] max-w-[min(28rem,calc(100%-2.5rem))] rounded-md border border-warning/40 bg-white/95 px-3 py-2 text-sm text-neutral-800 shadow-md`
/** 改动前浮动提示自己的定位类（其后接 `GLASS_PANEL`）。 */
const DESKTOP_NOTICE_CLASS_NAME =
  'absolute left-1/2 bottom-[11.5rem] z-[110] max-w-[min(30rem,calc(100%-8rem))] -translate-x-1/2 px-3 py-2 text-xs text-neutral-800'
const CLAMP_CLASS = 'mobile:line-clamp-2'

function tokens(element: HTMLElement) {
  return element.className.split(/\s+/).filter(Boolean)
}

/** 截断在包着文本的唯一内层上（外层带内边距，直接截它会露出第三行的上半截）；它只有这一个类。 */
function clampWrapper(element: HTMLElement) {
  expect(element.children).toHaveLength(1)
  const wrapper = element.firstElementChild as HTMLElement
  expect(wrapper.className).toBe(CLAMP_CLASS)
  return wrapper
}

function renderAllFourStatusOverlays() {
  return render(
    <M11MapStatusOverlays
      loading={false}
      boundaryLoading={false}
      basinBoundaryOverlayEnabled
      basinCount={1}
      basinFeatureCount={0}
      skippedBasinGeometryCount={0}
      unavailableReason="地图暂不可用"
      selectedSegmentMapState="unavailable"
      selectedSegmentUnavailableReason="选中河段不可用"
      mapSourceError="地图源错误"
    />,
  )
}

describe('M11MapStatusOverlays mobile slotting', () => {
  it('renders all four status overlays as children of one container, in a fixed order', () => {
    renderAllFourStatusOverlays()

    const container = screen.getByTestId(STATUS_CONTAINER)
    expect(Array.from(container.children).map((child) => child.getAttribute('data-testid'))).toEqual(
      STATUS_ITEMS.map((item) => item.testId),
    )
    for (const item of STATUS_ITEMS) {
      const element = screen.getByTestId(item.testId)
      expect(element.parentElement).toBe(container)
      expect(element).toHaveAttribute('role', 'status')
      expect(element).toHaveTextContent(item.text)
    }
  })

  it('keeps the container layout-neutral in desktop form and click-through, bounded and clipping in mobile form', () => {
    renderAllFourStatusOverlays()

    const classes = tokens(screen.getByTestId(STATUS_CONTAINER))
    // 桌面形态：display: contents——容器不产生盒子，各条仍按自己的 absolute 定位。
    expect(classes).toContain('contents')
    // 容器在桌面形态没有任何无前缀的定位 / 布局类。
    expect(classes.filter((name) => !name.startsWith('mobile:'))).toEqual(['contents'])
    expect(classes).toEqual(
      expect.arrayContaining(['mobile:absolute', 'mobile:flex', 'mobile:flex-col', 'mobile:overflow-hidden', 'mobile:pointer-events-none']),
    )
    expect(classes.some((name) => /^mobile:bottom-/.test(name)), 'container needs a mobile bottom bound').toBe(true)
  })

  it('gives every status overlay the two-line clamp and leaves its desktop classes verbatim', () => {
    renderAllFourStatusOverlays()

    for (const item of STATUS_ITEMS) {
      const element = screen.getByTestId(item.testId)
      expect(clampWrapper(element)).toHaveTextContent(item.text)
      // 只做追加：改动前的 className 是新 className 的前缀，其后全部是 mobile: 变体。
      const desktop = desktopStatusClassName(item.desktopTop)
      expect(element.className.startsWith(`${desktop} `)).toBe(true)
      const appended = element.className.slice(desktop.length).split(/\s+/).filter(Boolean)
      expect(appended.length).toBeGreaterThan(0)
      expect(appended.every((name) => name.startsWith('mobile:'))).toBe(true)
    }
  })

  it('renders the container but no status overlay when nothing is wrong', () => {
    render(
      <M11MapStatusOverlays
        loading={false}
        boundaryLoading={false}
        basinBoundaryOverlayEnabled
        basinCount={1}
        basinFeatureCount={1}
        skippedBasinGeometryCount={0}
        unavailableReason={null}
        selectedSegmentMapState="idle"
        selectedSegmentUnavailableReason={null}
        mapSourceError={null}
      />,
    )

    expect(screen.getByTestId(STATUS_CONTAINER)).toBeEmptyDOMElement()
    expect(screen.queryAllByRole('status')).toHaveLength(0)
  })
})

describe('M11FloatingNotice mobile slot', () => {
  it('keeps its desktop classes verbatim and only appends mobile variants, including the two-line clamp', () => {
    render(<M11FloatingNotice testId="m11-met-station-status">暂无可渲染气象代站</M11FloatingNotice>)

    const notice = screen.getByTestId('m11-met-station-status')
    expect(notice).toHaveAttribute('role', 'status')
    expect(notice).toHaveTextContent('暂无可渲染气象代站')
    for (const name of DESKTOP_NOTICE_CLASS_NAME.split(' ')) expect(tokens(notice)).toContain(name)
    const mobileClasses = tokens(notice).filter((name) => name.startsWith('mobile:'))
    expect(clampWrapper(notice)).toHaveTextContent('暂无可渲染气象代站')
    // 移动形态离开桌面的“水平居中 + 底部偏移”：top 槽位、取消平移与 bottom。
    expect(mobileClasses).toEqual(expect.arrayContaining(['mobile:top-2', 'mobile:bottom-auto', 'mobile:translate-x-0']))
  })

  it('still renders nothing without content', () => {
    const { container } = render(<M11FloatingNotice testId="m11-met-station-status">{null}</M11FloatingNotice>)

    expect(container).toBeEmptyDOMElement()
  })
})
