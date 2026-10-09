import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'

import { M11DraggableCurveWindow } from '@/components/map/M11DraggableCurveWindow'
import { installMobileFormMatchMedia } from '@/test/mobileFormMatchMedia'

/**
 * 曲线窗容器的移动形态（openspec mobile-responsive-display task 4.2，design.md D9）：
 * 抽屉不可拖——不挂拖拽监听、没有内联定位；形态切换时清掉进行中的拖拽，且不重挂子树。
 * 用例 (m)–(q) 对应 tasks.md 里 #2803 的 Triage。抽屉的几何由 e2e 断言（jsdom 无布局）。
 */

const WINDOW_TEST_ID = 'curve-window'
const HANDLE_TEST_ID = `${WINDOW_TEST_ID}-drag-handle`
const BODY_TEST_ID = `${WINDOW_TEST_ID}-body`
const DRAG_EVENTS = ['pointermove', 'pointerup', 'pointercancel'] as const

let form: ReturnType<typeof installMobileFormMatchMedia>
let addSpy: MockInstance<typeof window.addEventListener>
let removeSpy: MockInstance<typeof window.removeEventListener>

/** `window` 上以三个拖拽事件名注册 / 移除监听的调用（resize 等其他事件不算）。 */
function dragEventCalls(spy: MockInstance<typeof window.addEventListener> | MockInstance<typeof window.removeEventListener>) {
  return spy.mock.calls.map(([type]) => type).filter((type) => (DRAG_EVENTS as readonly string[]).includes(type)).sort()
}

function renderWindow(options: { mobile: boolean; landscape?: boolean; onActivate?: () => void }) {
  form = installMobileFormMatchMedia(options.mobile, options.landscape ?? false)
  addSpy = vi.spyOn(window, 'addEventListener')
  removeSpy = vi.spyOn(window, 'removeEventListener')
  return render(
    <M11DraggableCurveWindow
      kind="river"
      testId={WINDOW_TEST_ID}
      onActivate={options.onActivate}
      header={<div data-testid="curve-window-title">标题</div>}
    >
      <div data-testid="curve-window-child">曲线</div>
    </M11DraggableCurveWindow>,
  )
}

function pressHandle() {
  fireEvent.pointerDown(screen.getByTestId(HANDLE_TEST_ID), { button: 0, clientX: 40, clientY: 40 })
}

beforeEach(() => {
  vi.restoreAllMocks()
})

afterEach(() => {
  addSpy?.mockRestore()
  removeSpy?.mockRestore()
  form?.restore()
})

describe('M11DraggableCurveWindow 的移动形态', () => {
  for (const landscape of [false, true]) {
    const formName = landscape ? '矮视口横屏（右侧抽屉）' : '非矮视口横屏（底部抽屉）'

    it(`(m) ${formName}：在抓手上按下不注册拖拽监听，窗没有内联定位，抓手不显示拖拽光标`, () => {
      renderWindow({ mobile: true, landscape })
      const frame = screen.getByTestId(WINDOW_TEST_ID)
      const handle = screen.getByTestId(HANDLE_TEST_ID)

      pressHandle()
      fireEvent.pointerMove(window, { clientX: 400, clientY: 400 })

      expect(dragEventCalls(addSpy)).toEqual([])
      expect(frame.style.left).toBe('')
      expect(frame.style.top).toBe('')
      expect(frame.style.visibility).toBe('')
      expect(handle).not.toHaveClass('cursor-grab')
      expect(handle).not.toHaveClass('cursor-grabbing')
      expect(handle).not.toHaveClass('touch-none')
      // 层级两种形态都保留。
      expect(frame.style.zIndex).toBe('142')
      expect(frame).not.toHaveClass('aspect-video')
    })
  }

  it('(n) 桌面形态（对照）：在抓手上按下会注册三个拖拽监听，窗带内联定位', () => {
    renderWindow({ mobile: false })
    const frame = screen.getByTestId(WINDOW_TEST_ID)
    const handle = screen.getByTestId(HANDLE_TEST_ID)
    expect(handle).toHaveClass('cursor-grab', 'touch-none')

    pressHandle()

    expect(dragEventCalls(addSpy)).toEqual([...DRAG_EVENTS].sort())
    expect(handle).toHaveClass('cursor-grabbing', 'touch-none')
    expect(frame.style.left).not.toBe('')
    expect(frame.style.top).not.toBe('')
    expect(frame).toHaveClass('aspect-video')
  })

  it('(o) 桌面拖拽进行中切到移动形态：三个拖拽监听被移除，之后的指针移动不再给窗定位', () => {
    renderWindow({ mobile: false })
    const frame = screen.getByTestId(WINDOW_TEST_ID)
    pressHandle()
    expect(dragEventCalls(addSpy)).toEqual([...DRAG_EVENTS].sort())
    expect(dragEventCalls(removeSpy)).toEqual([])

    act(() => form.setForm({ mobile: true, landscape: false }))

    expect(dragEventCalls(removeSpy)).toEqual([...DRAG_EVENTS].sort())
    // 移除的正是注册时的那三个回调。
    for (const type of DRAG_EVENTS) {
      const added = addSpy.mock.calls.find(([name]) => name === type)?.[1]
      const removed = removeSpy.mock.calls.find(([name]) => name === type)?.[1]
      expect(removed).toBe(added)
    }
    fireEvent.pointerMove(window, { clientX: 300, clientY: 300 })
    expect(frame.style.left).toBe('')
    expect(frame.style.top).toBe('')
    expect(frame.style.visibility).toBe('')
    expect(screen.getByTestId(HANDLE_TEST_ID)).not.toHaveClass('cursor-grabbing')
  })

  it('(o) 回到桌面形态：窗重新带内联定位，上一次未结束的拖拽不会复活', () => {
    renderWindow({ mobile: false })
    const frame = screen.getByTestId(WINDOW_TEST_ID)
    const initial = { left: frame.style.left, top: frame.style.top }
    pressHandle()
    fireEvent.pointerMove(window, { clientX: 300, clientY: 260 })
    expect({ left: frame.style.left, top: frame.style.top }).not.toEqual(initial)
    expect(screen.getByTestId(HANDLE_TEST_ID)).toHaveClass('cursor-grabbing')

    act(() => form.setForm({ mobile: true, landscape: false }))
    act(() => form.setForm({ mobile: false, landscape: false }))

    // `dragging` 已归零：移动形态的抓手本来就不带拖拽光标类，只有回到桌面后才看得出来。
    const handle = screen.getByTestId(HANDLE_TEST_ID)
    expect(handle).not.toHaveClass('cursor-grabbing')
    expect(handle).toHaveClass('cursor-grab')
    // 默认位置，而不是进入移动形态前拖到的位置。
    expect({ left: frame.style.left, top: frame.style.top }).toEqual(initial)
    expect(frame.style.visibility).toBe('')
    fireEvent.pointerMove(window, { clientX: 500, clientY: 500 })
    expect({ left: frame.style.left, top: frame.style.top }).toEqual(initial)
  })

  it('(p) 两种形态下主体容器都在，形态切换不重挂子树；头部在主体容器之外', () => {
    renderWindow({ mobile: false })
    const frame = screen.getByTestId(WINDOW_TEST_ID)
    const body = screen.getByTestId(BODY_TEST_ID)
    const child = screen.getByTestId('curve-window-child')
    expect(body).toHaveClass('contents')
    expect(body).toContainElement(child)
    expect(body).not.toContainElement(screen.getByTestId(HANDLE_TEST_ID))

    for (const next of [
      { mobile: true, landscape: false },
      { mobile: true, landscape: true },
      { mobile: false, landscape: false },
    ]) {
      act(() => form.setForm(next))
      expect(screen.getByTestId(WINDOW_TEST_ID)).toBe(frame)
      expect(screen.getByTestId(BODY_TEST_ID)).toBe(body)
      expect(screen.getByTestId('curve-window-child')).toBe(child)
      expect(body).not.toContainElement(screen.getByTestId(HANDLE_TEST_ID))
      if (next.mobile) {
        expect(body).toHaveClass('overflow-y-auto', 'flex-1', 'min-h-0')
        expect(body).not.toHaveClass('contents')
      } else {
        expect(body).toHaveClass('contents')
      }
    }
  })

  it('(q) 移动形态：在抓手上按下仍触发 onActivate', () => {
    const onActivate = vi.fn()
    renderWindow({ mobile: true, onActivate })

    pressHandle()

    expect(onActivate).toHaveBeenCalled()
  })
})
