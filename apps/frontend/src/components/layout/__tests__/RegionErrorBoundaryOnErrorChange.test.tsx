import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'

import { RegionErrorBoundary } from '@/components/layout/RegionErrorBoundary'

/**
 * `RegionErrorBoundary` 的可选错误回调 `onErrorChange`（openspec mobile-responsive-display task 4.6，
 * design.md D11）：捕获时以该错误调用，错误被清除（「重试」或 `resetKeys` 变化）时以 `null` 调用；
 * 不传回调时渲染结果与以往相同。页面据它得知曲线区域是否在兜底。
 */
const ERROR_MESSAGE = 'curve panel blew up while rendering'

// 抛错由 prop 控制（同 `RegionErrorBoundary.test.tsx`）：自翻旗的子组件会在 React 的同步重试里成功。
function Thrower({ shouldThrow }: { shouldThrow: boolean }) {
  if (shouldThrow) throw new Error(ERROR_MESSAGE)
  return <div data-testid="region-child">区域内容</div>
}

function Harness({
  shouldThrow,
  resetKeys,
  onErrorChange,
}: {
  shouldThrow: boolean
  resetKeys?: readonly unknown[]
  onErrorChange?: (error: Error | null) => void
}) {
  return (
    <div data-testid="harness-root">
      <RegionErrorBoundary region="预报面板" testId="region-error-map-panels" resetKeys={resetKeys} onErrorChange={onErrorChange}>
        <Thrower shouldThrow={shouldThrow} />
      </RegionErrorBoundary>
    </div>
  )
}

let consoleError: MockInstance<typeof console.error>

beforeEach(() => {
  consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
})

afterEach(() => {
  consoleError.mockRestore()
})

describe('RegionErrorBoundary onErrorChange', () => {
  it('reports the caught error exactly once when the subtree throws', () => {
    const onErrorChange = vi.fn()
    render(<Harness shouldThrow onErrorChange={onErrorChange} />)

    expect(screen.getByTestId('region-error-map-panels')).toBeInTheDocument()
    expect(onErrorChange).toHaveBeenCalledTimes(1)
    const reported = onErrorChange.mock.calls[0][0]
    expect(reported).toBeInstanceOf(Error)
    expect((reported as Error).message).toBe(ERROR_MESSAGE)
  })

  it('reports null after a retry that succeeds', () => {
    const onErrorChange = vi.fn()
    const { rerender } = render(<Harness shouldThrow resetKeys={['a']} onErrorChange={onErrorChange} />)
    rerender(<Harness shouldThrow={false} resetKeys={['a']} onErrorChange={onErrorChange} />)
    // 原因已消失但没人清错：仍是兜底，也没有新的回调。
    expect(screen.getByTestId('region-error-map-panels')).toBeInTheDocument()
    expect(onErrorChange).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: '重试' }))

    expect(screen.getByTestId('region-child')).toBeInTheDocument()
    expect(onErrorChange).toHaveBeenCalledTimes(2)
    expect(onErrorChange).toHaveBeenLastCalledWith(null)
  })

  it('reports null after resetKeys change clears the error', () => {
    const onErrorChange = vi.fn()
    const { rerender } = render(<Harness shouldThrow resetKeys={['a']} onErrorChange={onErrorChange} />)
    expect(onErrorChange).toHaveBeenCalledTimes(1)

    rerender(<Harness shouldThrow={false} resetKeys={['b']} onErrorChange={onErrorChange} />)

    expect(screen.getByTestId('region-child')).toBeInTheDocument()
    expect(onErrorChange).toHaveBeenCalledTimes(2)
    expect(onErrorChange).toHaveBeenLastCalledWith(null)
  })

  it('never reports null while the region is still in fallback: a retry that throws again reports the error again', () => {
    const onErrorChange = vi.fn()
    render(<Harness shouldThrow onErrorChange={onErrorChange} />)
    expect(onErrorChange).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: '重试' }))

    expect(screen.getByTestId('region-error-map-panels')).toBeInTheDocument()
    expect(onErrorChange.mock.calls.length).toBeGreaterThanOrEqual(2)
    // 终态是兜底：最后一次回调带的是错误，中途没有任何一次以 null 调用。
    for (const [reported] of onErrorChange.mock.calls) expect(reported).toBeInstanceOf(Error)
  })

  it('does not call back on ordinary re-renders without an error', () => {
    const onErrorChange = vi.fn()
    const { rerender } = render(<Harness shouldThrow={false} resetKeys={['a']} onErrorChange={onErrorChange} />)
    rerender(<Harness shouldThrow={false} resetKeys={['a']} onErrorChange={onErrorChange} />)
    rerender(<Harness shouldThrow={false} resetKeys={['b']} onErrorChange={onErrorChange} />)

    expect(screen.getByTestId('region-child')).toBeInTheDocument()
    expect(onErrorChange).not.toHaveBeenCalled()
  })

  it('without the callback: throw, retry and resetKeys reset render exactly what the same sequence renders with it', () => {
    // 同一串操作各跑一遍（带回调 / 不带回调），每一步的 DOM 都逐字相同；不带回调时不抛异常。
    function run(onErrorChange?: (error: Error | null) => void) {
      const steps: string[] = []
      const view = render(<Harness shouldThrow resetKeys={['a']} onErrorChange={onErrorChange} />)
      const snapshot = () => steps.push(view.container.innerHTML)
      snapshot()
      // 「重试」但原因仍在：再次兜底。
      fireEvent.click(screen.getByRole('button', { name: '重试' }))
      snapshot()
      // `resetKeys` 变化且原因已消失：复位。
      view.rerender(<Harness shouldThrow={false} resetKeys={['b']} onErrorChange={onErrorChange} />)
      snapshot()
      // 再抛错，再「重试」且原因已消失：恢复。
      view.rerender(<Harness shouldThrow resetKeys={['b']} onErrorChange={onErrorChange} />)
      snapshot()
      view.rerender(<Harness shouldThrow={false} resetKeys={['b']} onErrorChange={onErrorChange} />)
      fireEvent.click(screen.getByRole('button', { name: '重试' }))
      snapshot()
      view.unmount()
      return steps
    }

    const withoutCallback = run()
    const withCallback = run(vi.fn())

    expect(withoutCallback).toEqual(withCallback)
    expect(withoutCallback).toHaveLength(5)
    expect(withoutCallback[0]).toContain('data-testid="region-error-map-panels"')
    expect(withoutCallback[1]).toContain('data-testid="region-error-map-panels"')
    expect(withoutCallback[2]).toBe('<div data-testid="harness-root"><div data-testid="region-child">区域内容</div></div>')
    expect(withoutCallback[3]).toContain('data-testid="region-error-map-panels"')
    expect(withoutCallback[4]).toBe(withoutCallback[2])
  })
})
