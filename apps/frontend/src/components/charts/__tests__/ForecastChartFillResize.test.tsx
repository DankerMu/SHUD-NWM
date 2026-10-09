import { render } from '@testing-library/react'
import type { EChartsInstance } from 'echarts-for-react/lib/types'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ForecastChart } from '@/components/charts/ForecastChart'
import type { ForecastData } from '@/stores/forecast'

const { coreProps } = vi.hoisted(() => ({ coreProps: vi.fn() }))

vi.mock('@/components/charts/echartsCore', () => ({
  echarts: {},
}))

vi.mock('echarts-for-react/lib/core', () => ({
  default: (props: { onChartReady?: (instance: EChartsInstance) => void }) => {
    coreProps(props)
    return <div data-testid="mock-echarts-core" />
  },
}))

/** 可控的 ResizeObserver：回调由测试同步触发，构造/断开顺序记入 events。 */
class ControllableResizeObserver implements ResizeObserver {
  static instances: ControllableResizeObserver[] = []
  static events: string[] = []

  readonly id: number
  readonly observe = vi.fn<(target: Element) => void>()
  readonly unobserve = vi.fn<(target: Element) => void>()
  readonly disconnect = vi.fn(() => {
    ControllableResizeObserver.events.push(`disconnect:${this.id}`)
  })

  constructor(private readonly callback: ResizeObserverCallback) {
    this.id = ControllableResizeObserver.instances.length
    ControllableResizeObserver.instances.push(this)
    ControllableResizeObserver.events.push(`construct:${this.id}`)
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
  const state = { disposed: false }
  const resize = vi.fn()
  const instance = {
    getDom: () => dom,
    getWidth: () => canvas.width,
    getHeight: () => canvas.height,
    isDisposed: () => state.disposed,
    resize,
  } as unknown as EChartsInstance
  return { dom, instance, resize, state }
}

function forecastData(): ForecastData {
  return {
    segmentId: 'seg-1',
    issueTime: '2026-05-03T00:00:00Z',
    unit: 'm3/s',
    sourceAttribution: 'GFS',
    cycleAttribution: 'GFS: 05-03 00Z',
    series: [
      {
        scenario: 'forecast_gfs_deterministic',
        source: 'GFS',
        isAnalysis: false,
        label: 'GFS 预报',
        color: '#ef7d22',
        cycleTime: '2026-05-03T00:00:00Z',
        availableLeadHours: 168,
        points: [
          { time: '2026-05-03T00:00:00Z', value: 1000 },
          { time: '2026-05-03T06:00:00Z', value: 1100 },
        ],
      },
    ],
  }
}

function capturedOnChartReady() {
  const props = coreProps.mock.lastCall?.[0] as { onChartReady?: (instance: EChartsInstance) => void } | undefined
  if (!props) throw new Error('echarts core was not rendered')
  return props.onChartReady
}

function readyFillChart(container: FakeSize, canvas: FakeSize) {
  const view = render(<ForecastChart data={forecastData()} variant="compact" fill />)
  const onChartReady = capturedOnChartReady()
  if (!onChartReady) throw new Error('fill chart did not pass onChartReady')
  const chart = fakeChart(container, canvas)
  onChartReady(chart.instance)
  return { ...view, ...chart, onChartReady, observer: ControllableResizeObserver.instances[0] }
}

describe('ForecastChart fill-mode container following', () => {
  beforeEach(() => {
    coreProps.mockClear()
    ControllableResizeObserver.instances = []
    ControllableResizeObserver.events = []
    vi.stubGlobal('ResizeObserver', ControllableResizeObserver)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('(a) re-reads the container size when the canvas size differs from it', () => {
    const container = { width: 640, height: 360 }
    const { observer, resize } = readyFillChart(container, { width: 320, height: 180 })
    expect(resize).not.toHaveBeenCalled()

    observer.trigger()
    expect(resize).toHaveBeenCalledTimes(1)
    expect(resize).toHaveBeenCalledWith({ width: 'auto', height: 'auto' })
  })

  it('(a) reacts to a width-only and to a height-only mismatch', () => {
    const container = { width: 640, height: 360 }
    const canvas = { width: 320, height: 360 }
    const { observer, resize } = readyFillChart(container, canvas)

    observer.trigger()
    expect(resize).toHaveBeenCalledTimes(1)

    canvas.width = 640
    canvas.height = 180
    observer.trigger()
    expect(resize).toHaveBeenCalledTimes(2)
    expect(resize).toHaveBeenLastCalledWith({ width: 'auto', height: 'auto' })
  })

  it('(b) leaves the chart alone while the canvas already matches the container', () => {
    const container = { width: 640, height: 360 }
    const { observer, resize } = readyFillChart(container, { width: 640, height: 360 })

    observer.trigger()
    expect(resize).not.toHaveBeenCalled()

    // 容器尺寸在回调时刻读取：之后变了才触发。
    container.width = 360
    observer.trigger()
    expect(resize).toHaveBeenCalledTimes(1)
  })

  it('(c) never resizes a disposed instance', () => {
    const { observer, resize, state } = readyFillChart({ width: 640, height: 360 }, { width: 320, height: 180 })
    state.disposed = true

    observer.trigger()
    expect(resize).not.toHaveBeenCalled()
  })

  it('(d) observes the chart element and disconnects on unmount', () => {
    const { dom, observer, unmount } = readyFillChart({ width: 640, height: 360 }, { width: 640, height: 360 })
    expect(ControllableResizeObserver.instances).toHaveLength(1)
    expect(observer.observe).toHaveBeenCalledTimes(1)
    expect(observer.observe).toHaveBeenCalledWith(dom)
    expect(observer.disconnect).not.toHaveBeenCalled()

    unmount()
    expect(observer.disconnect).toHaveBeenCalled()
  })

  it('(e) disconnects the previous observer before observing a re-created chart', () => {
    const { observer, onChartReady } = readyFillChart({ width: 640, height: 360 }, { width: 640, height: 360 })
    const next = fakeChart({ width: 640, height: 360 }, { width: 320, height: 180 })

    onChartReady(next.instance)
    expect(ControllableResizeObserver.events).toEqual(['construct:0', 'disconnect:0', 'construct:1'])
    expect(observer.disconnect).toHaveBeenCalledTimes(1)

    const replacement = ControllableResizeObserver.instances[1]
    expect(replacement.observe).toHaveBeenCalledWith(next.dom)
    expect(replacement.disconnect).not.toHaveBeenCalled()
    replacement.trigger()
    expect(next.resize).toHaveBeenCalledWith({ width: 'auto', height: 'auto' })
  })

  it('(f) does not follow the container without fill', () => {
    render(<ForecastChart data={forecastData()} variant="compact" />)

    expect(coreProps).toHaveBeenCalled()
    expect(capturedOnChartReady()).toBeUndefined()
    expect(ControllableResizeObserver.instances).toHaveLength(0)
  })

  it('(g) disconnects the observer when the mounted chart switches to the no-data state', () => {
    const { observer, rerender } = readyFillChart({ width: 640, height: 360 }, { width: 640, height: 360 })
    expect(observer.disconnect).not.toHaveBeenCalled()

    rerender(<ForecastChart data={null} variant="compact" fill />)
    expect(observer.disconnect).toHaveBeenCalled()
  })
})
