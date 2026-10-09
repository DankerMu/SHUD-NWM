import { useCallback, useEffect, useRef } from 'react'
import type { EChartsInstance } from 'echarts-for-react/lib/types'

/**
 * 让 `height: 100%` 的图表画布跟随容器尺寸。封装库的自动重排会吞掉绑定后的第一次尺寸回调（与其后
 * 60ms 内的变化合并成同一次），容器恰在那时变尺寸（如刚加载完就旋转）画布会一直停在旧尺寸；这里自己观察容器。
 *
 * 返回值交给封装库的 `onChartReady`。`rendered` 为 false（调用方此刻不渲染图表）时断开观察。
 */
export function useChartFollowsContainer(rendered: boolean) {
  const containerObserver = useRef<ResizeObserver | null>(null)
  useEffect(() => () => containerObserver.current?.disconnect(), [])
  useEffect(() => { if (!rendered) containerObserver.current?.disconnect() }, [rendered])
  return useCallback((instance: EChartsInstance) => {
    const container = instance.getDom()
    containerObserver.current?.disconnect()
    containerObserver.current = new ResizeObserver(() => {
      if (instance.isDisposed()) return
      if (instance.getWidth() !== container.clientWidth || instance.getHeight() !== container.clientHeight) {
        // 实例是带显式宽高创建的，必须显式传 auto 才会重新读容器尺寸。
        instance.resize({ width: 'auto', height: 'auto' })
      }
    })
    containerObserver.current.observe(container)
  }, [])
}
