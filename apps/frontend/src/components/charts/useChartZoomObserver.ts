import { useEffect, useMemo, useRef } from 'react'
import type { EChartsInstance } from 'echarts-for-react/lib/types'

/** 时间轴缩放窗口：占全范围的百分比（0–100）。 */
export interface ChartZoomWindow {
  start: number
  end: number
}

/** 图表实例当前的缩放窗口；实例没有 dataZoom 时为 null。 */
function readZoomWindow(instance: EChartsInstance): ChartZoomWindow | null {
  const zoom = (instance.getOption() as { dataZoom?: Array<{ start?: unknown; end?: unknown }> } | undefined)?.dataZoom?.[0]
  return typeof zoom?.start === 'number' && typeof zoom.end === 'number' ? { start: zoom.start, end: zoom.end } : null
}

/**
 * 观察图表的缩放窗口与 tooltip 可见性：缩放 / 平移，或配置重设后回到全范围时，回报实例当前的窗口；
 * tooltip 显示 / 隐藏时回报。必须在渲染封装库组件的那个组件里调用，`option` 是交给封装库的同一个对象。
 *
 * 返回值交给封装库的 `onEvents`；两个回调都不传时为 `undefined`（不挂任何事件）。
 */
export function useChartZoomObserver(
  option: unknown,
  onZoomWindowChange?: (zoomWindow: ChartZoomWindow) => void,
  onTooltipVisibleChange?: (visible: boolean) => void,
) {
  // 回调放进 ref：交给封装库的 onEvents 必须是同一个对象，引用一变它就整组解绑重绑。
  const zoomWindowCallback = useRef(onZoomWindowChange)
  const tooltipVisibleCallback = useRef(onTooltipVisibleChange)
  useEffect(() => {
    zoomWindowCallback.current = onZoomWindowChange
    tooltipVisibleCallback.current = onTooltipVisibleChange
  })
  /** 发过 datazoom 的那个实例；没发过就没缩放过，窗口必为初值。 */
  const zoomedInstance = useRef<EChartsInstance | null>(null)
  const observed = Boolean(onZoomWindowChange || onTooltipVisibleChange)
  // 事件名按图表库的口径全小写。封装库以 (事件参数, 实例) 调用处理函数：窗口读实例，不信事件载荷。
  const onEvents = useMemo(() => {
    if (!observed) return undefined
    return {
      datazoom: (_event: unknown, instance: EChartsInstance) => {
        zoomedInstance.current = instance
        const zoomWindow = readZoomWindow(instance)
        if (zoomWindow) zoomWindowCallback.current?.(zoomWindow)
      },
      showtip: () => tooltipVisibleCallback.current?.(true),
      hidetip: () => tooltipVisibleCallback.current?.(false),
    }
  }, [observed])
  // 配置整体重设（notMerge）会把缩放窗口打回全范围而不发 datazoom：封装库在本 effect 之前已把新配置
  // 交给实例，这里重读一次。
  useEffect(() => {
    const instance = zoomedInstance.current
    if (!instance || instance.isDisposed()) return
    const zoomWindow = readZoomWindow(instance)
    if (zoomWindow) zoomWindowCallback.current?.(zoomWindow)
  }, [option])

  return onEvents
}
