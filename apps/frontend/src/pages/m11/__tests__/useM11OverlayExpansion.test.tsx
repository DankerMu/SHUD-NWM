import { act, fireEvent, render, renderHook, screen } from '@testing-library/react'
import { useRef } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  toggleM11OverlayExpansion,
  useM11MobilePanelMaxHeight,
  useM11OverlayExpansion,
  type M11OverlayPanel,
} from '@/pages/m11/useM11OverlayExpansion'

/**
 * 浮层展开值状态机（openspec mobile-responsive-display task 3.2，design.md D5）。
 * `'layers'` / `'basemap'` 在本 task 还没有消费者，互斥在这里钉住。
 */
const PANELS: M11OverlayPanel[] = ['layers', 'basemap', 'legend']

function keydownListeners(spy: { mock: { calls: unknown[][] } }) {
  return spy.mock.calls.filter((call) => call[0] === 'keydown').length
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('toggleM11OverlayExpansion', () => {
  it.each(PANELS)('expands %s from the collapsed state and collapses it when toggled again', (panel) => {
    expect(toggleM11OverlayExpansion(null, panel)).toBe(panel)
    expect(toggleM11OverlayExpansion(panel, panel)).toBeNull()
  })

  it('replaces the expanded panel with the other one for every ordered pair', () => {
    for (const from of PANELS) {
      for (const to of PANELS) {
        if (from === to) continue
        expect(toggleM11OverlayExpansion(from, to), `${from} -> ${to}`).toBe(to)
      }
    }
  })
})

describe('useM11OverlayExpansion', () => {
  it('starts collapsed and keeps at most one panel expanded', () => {
    const { result } = renderHook(() => useM11OverlayExpansion(true))
    expect(result.current.expanded).toBeNull()

    act(() => result.current.toggle('legend'))
    expect(result.current.expanded).toBe('legend')
    act(() => result.current.toggle('layers'))
    expect(result.current.expanded).toBe('layers')
    act(() => result.current.toggle('basemap'))
    expect(result.current.expanded).toBe('basemap')
    act(() => result.current.toggle('basemap'))
    expect(result.current.expanded).toBeNull()
  })

  it('collapses through the reset entry', () => {
    const { result } = renderHook(() => useM11OverlayExpansion(true))
    act(() => result.current.toggle('legend'))
    act(() => result.current.collapse())
    expect(result.current.expanded).toBeNull()
  })

  it('collapses on Escape and ignores other keys', () => {
    const { result } = renderHook(() => useM11OverlayExpansion(true))
    act(() => result.current.toggle('legend'))

    fireEvent.keyDown(document, { key: 'Enter' })
    expect(result.current.expanded).toBe('legend')
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(result.current.expanded).toBeNull()
  })

  it('listens for keys only while a panel is expanded in mobile form', () => {
    const add = vi.spyOn(document, 'addEventListener')
    const remove = vi.spyOn(document, 'removeEventListener')
    const { result } = renderHook(() => useM11OverlayExpansion(true))
    expect(keydownListeners(add)).toBe(0)

    act(() => result.current.toggle('legend'))
    expect(keydownListeners(add)).toBe(1)
    expect(keydownListeners(remove)).toBe(0)

    act(() => result.current.collapse())
    expect(keydownListeners(remove)).toBe(1)
    expect(keydownListeners(add)).toBe(1)
  })

  it('removes the key listener on unmount', () => {
    const add = vi.spyOn(document, 'addEventListener')
    const remove = vi.spyOn(document, 'removeEventListener')
    const { result, unmount } = renderHook(() => useM11OverlayExpansion(true))
    act(() => result.current.toggle('legend'))
    const listener = add.mock.calls.find((call) => call[0] === 'keydown')?.[1]
    expect(listener).toBeTypeOf('function')

    unmount()
    expect(remove).toHaveBeenCalledWith('keydown', listener)
  })

  it('resets when the viewport leaves mobile form, removes the key listener and stays collapsed on re-entry', () => {
    const add = vi.spyOn(document, 'addEventListener')
    const remove = vi.spyOn(document, 'removeEventListener')
    const { result, rerender } = renderHook(({ mobile }) => useM11OverlayExpansion(mobile), {
      initialProps: { mobile: true },
    })
    act(() => result.current.toggle('legend'))
    expect(result.current.expanded).toBe('legend')

    rerender({ mobile: false })
    expect(result.current.expanded).toBeNull()
    expect(keydownListeners(remove)).toBe(keydownListeners(add))

    rerender({ mobile: true })
    expect(result.current.expanded).toBeNull()
  })

  it('reads as collapsed in desktop form and attaches no key listener', () => {
    const add = vi.spyOn(document, 'addEventListener')
    const { result } = renderHook(() => useM11OverlayExpansion(false))

    act(() => result.current.toggle('legend'))
    expect(result.current.expanded).toBeNull()
    expect(keydownListeners(add)).toBe(0)
  })
})

/** jsdom 不做布局：给三个元素各装一个固定的包围盒，验证的是“限高 = 实测几何之差”这条算式。 */
function rect(top: number, bottom: number): DOMRect {
  return { top, bottom, left: 0, right: 0, x: 0, y: top, width: 0, height: bottom - top, toJSON: () => ({}) } as DOMRect
}

function MaxHeightProbe({ active, withFloor }: { active: boolean; withFloor: boolean }) {
  const regionRef = useRef<HTMLElement | null>(null)
  const columnRef = useRef<HTMLDivElement | null>(null)
  const maxHeight = useM11MobilePanelMaxHeight({ active, regionRef, columnRef, floorSelector: '[data-floor]' })
  return (
    <section
      ref={(node) => {
        regionRef.current = node
        // 地图区：视口 y 84–342（750×342、头部 84px）。
        if (node) node.getBoundingClientRect = () => rect(84, 342)
      }}
    >
      <div
        ref={(node) => {
          columnRef.current = node
          if (node) node.getBoundingClientRect = () => rect(92, 136)
        }}
      />
      {withFloor ? (
        <div
          data-floor
          ref={(node) => {
            // 控制条：底边距地图区底 40px、高 64px。
            if (node) node.getBoundingClientRect = () => rect(238, 302)
          }}
        />
      ) : null}
      <output data-testid="max-height">{String(maxHeight)}</output>
    </section>
  )
}

describe('useM11MobilePanelMaxHeight', () => {
  it('limits the panel to the space between the launcher column top and the control bar top, minus the gap', () => {
    render(<MaxHeightProbe active withFloor />)
    // 238（控制条顶）− 92（列顶）− 8（间隙）
    expect(screen.getByTestId('max-height').textContent).toBe('138')
  })

  it('falls back to the attribution band above the map bottom when there is no control bar', () => {
    render(<MaxHeightProbe active withFloor={false} />)
    // 342（地图区底）− 40（attribution 带）− 92（列顶）− 8（间隙）
    expect(screen.getByTestId('max-height').textContent).toBe('202')
  })

  it('measures nothing while no panel is expanded', () => {
    render(<MaxHeightProbe active={false} withFloor />)
    expect(screen.getByTestId('max-height').textContent).toBe('null')
  })
})
