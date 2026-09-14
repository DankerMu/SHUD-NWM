import { Component, type ErrorInfo, type ReactNode } from 'react'

import { cn } from '@/lib/cn'

interface RegionErrorBoundaryProps {
  /** 中文区域名：进日志与 `aria-label`。 */
  region: string
  /** fallback 的 `data-testid`。 */
  testId: string
  /** 任一元素变化（`Object.is`）即清掉已捕获的错误。 */
  resetKeys?: readonly unknown[]
  /** `region` = 区域卡片；`page` = 整页 fallback（多一个「刷新页面」）。 */
  variant?: 'region' | 'page'
  /** 浮层区域的定位类：fallback 落在原区域的位置上。 */
  className?: string
  children?: ReactNode
}

interface RegionErrorBoundaryState {
  error: Error | null
}

function resetKeysChanged(previous: readonly unknown[] = [], next: readonly unknown[] = []) {
  return previous.length !== next.length || previous.some((key, index) => !Object.is(key, next[index]))
}

/**
 * 渲染期抛错的最小爆炸半径（#2347）：React 仍要求边界是 class，不引入 `react-error-boundary`。
 * 只接 render / 生命周期里的抛错；事件回调、effect、MapLibre 回调、异步拒绝不经过这里（design D5）。
 * 非错误态直接返回 children，不加任何包裹元素（happy-path DOM 逐字不变）。
 */
export class RegionErrorBoundary extends Component<RegionErrorBoundaryProps, RegionErrorBoundaryState> {
  state: RegionErrorBoundaryState = { error: null }

  static getDerivedStateFromError(error: Error): RegionErrorBoundaryState {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // 目前没有远端上报通道；原始错误只进日志，不给用户看。
    console.error('[RegionErrorBoundary]', this.props.region, error, info.componentStack)
  }

  componentDidUpdate(prevProps: RegionErrorBoundaryProps, prevState: RegionErrorBoundaryState) {
    // 守卫 `prevState.error !== null`：key 变化与抛错落在同一次提交时，不能把刚捕获的错误当成
    // 「key 变了」立刻清掉——那会让子树马上再抛一次（react-error-boundary 同一语义）。
    if (
      this.state.error !== null &&
      prevState.error !== null &&
      resetKeysChanged(prevProps.resetKeys, this.props.resetKeys)
    ) {
      this.setState({ error: null })
    }
  }

  private readonly retry = () => {
    this.setState({ error: null })
  }

  render() {
    if (this.state.error === null) return this.props.children

    const { region, testId, variant = 'region', className } = this.props
    const isPage = variant === 'page'
    return (
      <div
        role="alert"
        aria-label={`${region}加载失败`}
        data-testid={testId}
        className={cn(
          'rounded-md border border-border bg-panel text-sm text-foreground shadow-sm',
          isPage ? 'm-4 max-w-lg space-y-3 p-4' : 'flex items-center gap-3 px-3 py-2',
          className,
        )}
      >
        <p className={cn(isPage && 'font-semibold')}>{isPage ? '页面加载失败' : '此区域加载失败'}</p>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={this.retry}
            className="rounded-md border border-border px-2.5 py-1 text-xs font-medium hover:bg-background"
          >
            重试
          </button>
          {isPage ? (
            <button
              type="button"
              onClick={() => window.location.reload()}
              className="rounded-md border border-border px-2.5 py-1 text-xs font-medium hover:bg-background"
            >
              刷新页面
            </button>
          ) : null}
        </div>
      </div>
    )
  }
}
