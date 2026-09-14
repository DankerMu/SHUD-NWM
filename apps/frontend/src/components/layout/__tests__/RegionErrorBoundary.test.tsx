import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'

import { RegionErrorBoundary } from '@/components/layout/RegionErrorBoundary'

const RAW_ERROR_MESSAGE = 'cycles[0] is null: secret stack detail'

// 抛错由 prop 控制，不用「抛一次就翻旗」：React 18 createRoot 在 render 抛错后会同步重试一次，
// 自翻旗的子组件会在重试里成功，边界永远看不到错误。
function Thrower({ shouldThrow }: { shouldThrow: boolean }) {
  if (shouldThrow) throw new Error(RAW_ERROR_MESSAGE)
  return <div data-testid="region-child">区域内容</div>
}

function Harness({ shouldThrow, resetKeys }: { shouldThrow: boolean; resetKeys?: readonly unknown[] }) {
  return (
    <div>
      <div data-testid="outside-sibling">边界外兄弟</div>
      <RegionErrorBoundary region="地图" testId="region-error-map" resetKeys={resetKeys}>
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

describe('RegionErrorBoundary', () => {
  it('contains a render throw to the region fallback without exposing the raw error', () => {
    render(<Harness shouldThrow />)

    const fallback = screen.getByTestId('region-error-map')
    expect(fallback).toHaveAttribute('role', 'alert')
    expect(fallback).toHaveTextContent('此区域加载失败')
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument()
    expect(screen.queryByText(RAW_ERROR_MESSAGE, { exact: false })).toBeNull()
    expect(document.body.textContent).not.toContain(RAW_ERROR_MESSAGE)
    expect(screen.getByTestId('outside-sibling')).toBeInTheDocument()
    expect(screen.queryByTestId('region-child')).toBeNull()
    expect(
      consoleError.mock.calls.some((call) => call[0] === '[RegionErrorBoundary]' && call[1] === '地图'),
    ).toBe(true)
  })

  it('renders children directly with no fallback when nothing throws', () => {
    render(<Harness shouldThrow={false} />)

    expect(screen.getByTestId('region-child')).toBeInTheDocument()
    expect(screen.queryByTestId('region-error-map')).toBeNull()
  })

  it('shows the page variant with retry and reload actions', () => {
    render(
      <RegionErrorBoundary region="页面" testId="route-error-fallback" variant="page">
        <Thrower shouldThrow />
      </RegionErrorBoundary>,
    )

    const fallback = screen.getByTestId('route-error-fallback')
    expect(fallback).toHaveAttribute('role', 'alert')
    expect(fallback).toHaveTextContent('页面加载失败')
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '刷新页面' })).toBeInTheDocument()
  })

  it('re-renders the region content when retry is clicked after the cause is gone', () => {
    const { rerender } = render(<Harness shouldThrow resetKeys={['a']} />)
    expect(screen.getByTestId('region-error-map')).toBeInTheDocument()

    // 同一组 key：错误态保持，fallback 不会自己消失。
    rerender(<Harness shouldThrow={false} resetKeys={['a']} />)
    expect(screen.getByTestId('region-error-map')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(screen.getByTestId('region-child')).toBeInTheDocument()
    expect(screen.queryByTestId('region-error-map')).toBeNull()
  })

  it('resets without clicking when resetKeys change after the error', () => {
    const { rerender } = render(<Harness shouldThrow resetKeys={['a']} />)
    expect(screen.getByTestId('region-error-map')).toBeInTheDocument()

    rerender(<Harness shouldThrow={false} resetKeys={['b']} />)
    expect(screen.getByTestId('region-child')).toBeInTheDocument()
    expect(screen.queryByTestId('region-error-map')).toBeNull()
  })

  it('keeps the fallback when resetKeys change in the same render that throws', () => {
    const { rerender } = render(<Harness shouldThrow={false} resetKeys={['a']} />)
    expect(screen.getByTestId('region-child')).toBeInTheDocument()

    // key 变化与抛错同一次提交：守卫（prevState.error !== null）不得把这次错误当成「key 变了」立刻清掉。
    // 无守卫时边界会立刻复位、子组件再抛一次、再捕获一次——fallback 依旧可见，
    // 所以「只捕获一次」（componentDidCatch 日志恰好一条）才是能区分两者的 oracle。
    consoleError.mockClear()
    rerender(<Harness shouldThrow resetKeys={['b']} />)
    expect(screen.getByTestId('region-error-map')).toBeInTheDocument()
    expect(consoleError.mock.calls.filter((call) => call[0] === '[RegionErrorBoundary]')).toHaveLength(1)

    rerender(<Harness shouldThrow={false} resetKeys={['c']} />)
    expect(screen.getByTestId('region-child')).toBeInTheDocument()
  })
})
