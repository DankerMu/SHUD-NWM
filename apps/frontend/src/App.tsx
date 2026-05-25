import { lazy, Suspense } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router-dom'

import { AppShell } from '@/components/layout/AppShell'
import { RBACGate } from '@/components/layout/RBACGate'

const ForecastPage = lazy(() =>
  import('./pages/ForecastPage').then((module) => ({ default: module.ForecastPage })),
)
const OverviewPage = lazy(() =>
  import('./pages/OverviewPage').then((module) => ({ default: module.OverviewPage })),
)
const BasinDetailPage = lazy(() =>
  import('./pages/BasinDetailPage').then((module) => ({ default: module.BasinDetailPage })),
)
const FloodAlertPage = lazy(() =>
  import('./pages/FloodAlertPage').then((module) => ({ default: module.FloodAlertPage })),
)
const MeteorologyPage = lazy(() =>
  import('./pages/meteorology/MeteorologyPage').then((module) => ({ default: module.MeteorologyPage })),
)
const HydroMetPage = lazy(() =>
  import('./pages/hydroMet/HydroMetPage').then((module) => ({ default: module.HydroMetPage })),
)
const SegmentDetailPage = lazy(() =>
  import('./pages/SegmentDetailPage').then((module) => ({ default: module.SegmentDetailPage })),
)
const MonitoringPage = lazy(() =>
  import('./pages/MonitoringPage').then((module) => ({ default: module.MonitoringPage })),
)
const ModelAssetsPage = lazy(() =>
  import('./pages/ModelAssetsPage').then((module) => ({ default: module.ModelAssetsPage })),
)

export default function App() {
  return (
    <BrowserRouter>
      <AppShell>
        <Suspense fallback={<div>加载中...</div>}>
          <Routes>
            <Route path="/" element={<OverviewPage />} />
            <Route path="/overview" element={<OverviewPage />} />
            <Route path="/basins/:basinId" element={<BasinDetailPage />} />
            <Route path="/hydro-met" element={<HydroMetPage />} />
            <Route path="/meteorology" element={<MeteorologyPage />} />
            <Route path="/forecast" element={<ForecastPage />} />
            <Route path="/flood-alerts" element={<FloodAlertPage />} />
            <Route path="/segments/:segmentId" element={<SegmentDetailPage />} />
            <Route
              path="/monitoring"
              element={
                <RBACGate roles={['operator', 'model_admin', 'sys_admin']}>
                  <MonitoringPage />
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
