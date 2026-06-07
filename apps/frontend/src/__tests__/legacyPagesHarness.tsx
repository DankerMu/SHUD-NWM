import { lazy, Suspense } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router-dom'

import { AppShell } from '@/components/layout/AppShell'
import { RBACGate } from '@/components/layout/RBACGate'

/**
 * 测试夹具：保留 #337 之前的旧多页路由表，供深度页面行为测试继续覆盖
 * （stationSeries / riverForecast honest-display 不变量等）。
 *
 * 真实 App 已把这些路由收敛/重定向到单页 `/`，重定向矩阵由 `<App />` 单独覆盖；
 * 本夹具只承载"页面渲染后断言其内部行为"的测试，使用与 App 相同的全局 mock。
 * 复用新版 AppShell（无 NavBar），与生产外壳一致。
 */
const ForecastPage = lazy(() =>
  import('@/pages/ForecastPage').then((module) => ({ default: module.ForecastPage })),
)
const OverviewPage = lazy(() =>
  import('@/pages/OverviewPage').then((module) => ({ default: module.OverviewPage })),
)
const FloodAlertPage = lazy(() =>
  import('@/pages/FloodAlertPage').then((module) => ({ default: module.FloodAlertPage })),
)
const MeteorologyPage = lazy(() =>
  import('@/pages/meteorology/MeteorologyPage').then((module) => ({ default: module.MeteorologyPage })),
)
const HydroMetPage = lazy(() =>
  import('@/pages/hydroMet/HydroMetPage').then((module) => ({ default: module.HydroMetPage })),
)
const SegmentDetailPage = lazy(() =>
  import('@/pages/SegmentDetailPage').then((module) => ({ default: module.SegmentDetailPage })),
)
const MonitoringPage = lazy(() =>
  import('@/pages/MonitoringPage').then((module) => ({ default: module.MonitoringPage })),
)
const ModelAssetsPage = lazy(() =>
  import('@/pages/ModelAssetsPage').then((module) => ({ default: module.ModelAssetsPage })),
)

export function LegacyPagesHarness() {
  return (
    <BrowserRouter>
      <AppShell>
        <Suspense fallback={<div>加载中...</div>}>
          <Routes>
            <Route path="/" element={<OverviewPage />} />
            <Route path="/overview" element={<OverviewPage />} />
            <Route path="/hydro-met" element={<HydroMetPage />} />
            <Route path="/meteorology" element={<MeteorologyPage />} />
            <Route path="/forecast" element={<ForecastPage />} />
            <Route path="/flood-alerts" element={<FloodAlertPage />} />
            <Route path="/segments/:segmentId" element={<SegmentDetailPage />} />
            <Route
              path="/monitoring"
              element={
                <RBACGate roles={['operator', 'model_admin', 'sys_admin']}>
                  <MonitoringPage mode="monitoring" />
                </RBACGate>
              }
            />
            <Route
              path="/ops"
              element={
                <RBACGate roles={['operator', 'model_admin', 'sys_admin']}>
                  <MonitoringPage mode="ops" />
                </RBACGate>
              }
            />
            <Route
              path="/system/model-assets"
              element={
                <RBACGate roles={['model_admin', 'sys_admin']}>
                  <ModelAssetsPage />
                </RBACGate>
              }
            />
          </Routes>
        </Suspense>
      </AppShell>
    </BrowserRouter>
  )
}
