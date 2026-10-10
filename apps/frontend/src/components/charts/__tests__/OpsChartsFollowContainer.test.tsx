import { render, screen } from '@testing-library/react'
import type { EChartsInstance } from 'echarts-for-react/lib/types'
import type { ReactElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { QueueDonut } from '@/components/charts/QueueDonut'
import { StageDurationBar } from '@/components/charts/StageDurationBar'
import { TrendLine } from '@/components/charts/TrendLine'
import type { PipelineStage } from '@/stores/monitoring'

/**
 * 运维页三类图表的“画布跟随容器”接线（openspec ops-charts-follow-container task 1.2，#2860）。
 * 只钉接线：每个组件把 `onChartReady` 交给封装库，就绪后观察实例的 dom、尺寸不一致时重排、卸载时断开；
 * `TrendLine` / `StageDurationBar` 变为空数据时不再渲染图表且观察器断开。
 * 观察器的其余行为由 `ForecastChartFillResize.test.tsx` 经同一个 hook 钉住。
 */

const { coreProps } = vi.hoisted(() => ({ coreProps: vi.fn() }))

vi.mock('@/components/charts/echartsCore', () => ({ echarts: {} }))

vi.mock('echarts-for-react/lib/core', () => ({
  default: (props: { onChartReady?: (instance: EChartsInstance) => void }) => {
    coreProps(props)
    return <div data-testid="mock-echarts-core" />
  },
}))

/** 可控的 ResizeObserver：回调由测试同步触发。 */
class ControllableResizeObserver implements ResizeObserver {
  static instances: ControllableResizeObserver[] = []

  readonly observe = vi.fn<(target: Element) => void>()
  readonly unobserve = vi.fn<(target: Element) => void>()
  readonly disconnect = vi.fn()

  constructor(private readonly callback: ResizeObserverCallback) {
    ControllableResizeObserver.instances.push(this)
  }

  trigger() {
    this.callback([], this)
  }
}

interface FakeSize {
  width: number
  height: number
}

/** 假 ECharts 实例：容器尺寸与实例尺寸都由测试直接改写。 */
function fakeChart(container: FakeSize, canvas: FakeSize) {
  const dom = document.createElement('div')
  Object.defineProperty(dom, 'clientWidth', { configurable: true, get: () => container.width })
  Object.defineProperty(dom, 'clientHeight', { configurable: true, get: () => container.height })
  const resize = vi.fn()
  const instance = {
    getDom: () => dom,
    getWidth: () => canvas.width,
    getHeight: () => canvas.height,
    isDisposed: () => false,
    resize,
  } as unknown as EChartsInstance
  return { dom, instance, resize }
}

/** 渲染组件、取它交给封装库的 `onChartReady` 并用假实例调用一次。 */
function readyChart(element: ReactElement, container: FakeSize, canvas: FakeSize) {
  const view = render(element)
  const props = coreProps.mock.lastCall?.[0] as { onChartReady?: unknown } | undefined
  if (!props) throw new Error('echarts core was not rendered')
  expect(props.onChartReady, '组件应把函数型 onChartReady 交给封装库').toBeTypeOf('function')
  const chart = fakeChart(container, canvas)
  ;(props.onChartReady as (instance: EChartsInstance) => void)(chart.instance)
  expect(ControllableResizeObserver.instances).toHaveLength(1)
  return { ...view, ...chart, observer: ControllableResizeObserver.instances[0] }
}

function stage(name: string, durationSeconds: number): PipelineStage {
  return {
    stage: name,
    display_status: 'succeeded',
    duration_seconds: durationSeconds,
    basin_progress: { completed: 1, total: 1, failed: 0 },
    basin_results_limit: 50,
    basin_results_total: 0,
    basin_results_returned: 0,
    basin_results_truncated: false,
    basin_results: [],
  }
}

const TREND_DATES = ['2026-05-01', '2026-05-02']
const TREND_SERIES = [{ name: '成功率', data: [98, 100] }]
const STAGES = [stage('download', 120), stage('forecast', 900)]

/** 三个组件：固定像素高度（与组件 style 一致）、有数据时的元素；`empty` 是同一组件的空数据形态与占位文案。 */
const CHARTS: Array<{
  name: string
  height: number
  element: ReactElement
  empty?: { element: ReactElement; placeholder: string }
}> = [
  {
    name: 'QueueDonut',
    height: 180,
    element: <QueueDonut queue={{ running: 2, pending: 3, idle: 5 }} />,
  },
  {
    name: 'TrendLine',
    height: 280,
    element: <TrendLine title="每周期成功率" dates={TREND_DATES} series={TREND_SERIES} unit="percent" />,
    empty: {
      element: <TrendLine title="每周期成功率" dates={[]} series={[]} unit="percent" />,
      placeholder: '暂无趋势数据',
    },
  },
  {
    name: 'StageDurationBar',
    height: 260,
    element: <StageDurationBar stages={STAGES} />,
    empty: { element: <StageDurationBar stages={[]} />, placeholder: '暂无耗时数据' },
  },
]

describe.each(CHARTS)('$name container following', ({ height, element, empty }) => {
  beforeEach(() => {
    coreProps.mockClear()
    ControllableResizeObserver.instances = []
    vi.stubGlobal('ResizeObserver', ControllableResizeObserver)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('re-reads the container size when the canvas width differs from the container width', () => {
    const { dom, observer, resize } = readyChart(element, { width: 324, height }, { width: 684, height })
    expect(observer.observe).toHaveBeenCalledTimes(1)
    expect(observer.observe).toHaveBeenCalledWith(dom)
    expect(resize).not.toHaveBeenCalled()

    observer.trigger()
    expect(resize).toHaveBeenCalledTimes(1)
    expect(resize).toHaveBeenCalledWith({ width: 'auto', height: 'auto' })
  })

  it('leaves the chart alone while the canvas already matches the container', () => {
    const { observer, resize } = readyChart(element, { width: 684, height }, { width: 684, height })

    observer.trigger()
    expect(resize).not.toHaveBeenCalled()
  })

  it('disconnects the observer on unmount', () => {
    const { observer, unmount } = readyChart(element, { width: 684, height }, { width: 684, height })
    expect(observer.disconnect).not.toHaveBeenCalled()

    unmount()
    expect(observer.disconnect).toHaveBeenCalled()
  })

  // QueueDonut 恒渲染，没有空数据形态。
  if (!empty) return

  it('stops rendering the chart and disconnects the observer when its data becomes empty', () => {
    const { observer, rerender } = readyChart(element, { width: 684, height }, { width: 684, height })
    expect(screen.getByTestId('mock-echarts-core')).toBeInTheDocument()
    expect(observer.disconnect).not.toHaveBeenCalled()

    // rerender 到空数据而不是 unmount：卸载时的断开由上一条钉住，这里钉的是“仍挂载但不再渲染图表”。
    rerender(empty.element)
    expect(screen.queryByTestId('mock-echarts-core')).not.toBeInTheDocument()
    expect(screen.getByText(empty.placeholder)).toBeInTheDocument()
    expect(observer.disconnect).toHaveBeenCalled()
  })
})
