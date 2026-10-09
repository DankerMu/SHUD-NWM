import { render } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ForecastChart } from '@/components/charts/ForecastChart'
import type { ForecastData } from '@/stores/forecast'

/**
 * `ForecastChart` 的缩放配置在缺省 prop 下不变（openspec mobile-responsive-display task 4.7，用例 (k)）。
 * 字面值抄自改动前的源码，不读产品常量：桌面形态与 `ForecastPanel` 的配置逐字不变由它钉住。
 */

const { coreProps } = vi.hoisted(() => ({ coreProps: vi.fn() }))

vi.mock('@/components/charts/echartsCore', () => ({
  echarts: {},
}))

vi.mock('echarts-for-react/lib/core', () => ({
  default: (props: Record<string, unknown>) => {
    coreProps(props)
    return <div data-testid="mock-echarts-core" />
  },
}))

/** 改动前的 inside dataZoom 字面值。 */
const DESKTOP_DATA_ZOOM = [{ type: 'inside', zoomOnMouseWheel: true, moveOnMouseMove: false, moveOnMouseWheel: false, filterMode: 'none' }]

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

function lastCoreProps() {
  const props = coreProps.mock.lastCall?.[0] as
    | { option: { dataZoom?: unknown }; onEvents?: unknown; onChartReady?: unknown }
    | undefined
  if (!props) throw new Error('echarts core was not rendered')
  return props
}

describe('ForecastChart 的缩放配置', () => {
  beforeEach(() => {
    coreProps.mockClear()
  })

  it('(k) zoomable、不传新 prop：dataZoom 与改动前的字面值深相等，不挂事件、不接 onChartReady', () => {
    render(<ForecastChart data={forecastData()} variant="compact" zoomable />)

    const props = lastCoreProps()
    expect(props.option.dataZoom).toStrictEqual(DESKTOP_DATA_ZOOM)
    expect(props.onEvents).toBeUndefined()
    expect(props.onChartReady).toBeUndefined()
  })

  it('(k) 不 zoomable：dataZoom 为 undefined，传不传 touchPan 都一样', () => {
    render(<ForecastChart data={forecastData()} />)
    expect(lastCoreProps().option.dataZoom).toBeUndefined()
    expect(lastCoreProps().onEvents).toBeUndefined()

    render(<ForecastChart data={forecastData()} touchPan />)
    expect(lastCoreProps().option.dataZoom).toBeUndefined()
  })

  it('(k) touchPan 显式为 false 等同缺省', () => {
    render(<ForecastChart data={forecastData()} variant="compact" zoomable touchPan={false} />)

    expect(lastCoreProps().option.dataZoom).toStrictEqual(DESKTOP_DATA_ZOOM)
  })

  it('zoomable + touchPan：只多出 moveOnMouseMove: true 与 preventDefaultMouseMove: false', () => {
    render(<ForecastChart data={forecastData()} variant="compact" zoomable touchPan />)

    expect(lastCoreProps().option.dataZoom).toStrictEqual([
      { ...DESKTOP_DATA_ZOOM[0], moveOnMouseMove: true, preventDefaultMouseMove: false },
    ])
  })
})
