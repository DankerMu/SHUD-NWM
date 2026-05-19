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
const SegmentDetailPage = lazy(() =>
  import('./pages/SegmentDetailPage').then((module) => ({ default: module.SegmentDetailPage })),
)
const MonitoringPage = lazy(() =>
  import('./pages/MonitoringPage').then((module) => ({ default: module.MonitoringPage })),
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
          </Routes>
        </Suspense>
      </AppShell>
    </BrowserRouter>
  )
}
