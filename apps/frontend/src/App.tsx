import { lazy, Suspense } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router-dom'

import { AppShell } from '@/components/layout/AppShell'
import { RBACGate } from '@/components/layout/RBACGate'

const ForecastPage = lazy(() =>
  import('./pages/ForecastPage').then((module) => ({ default: module.ForecastPage })),
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
            <Route path="/" element={<ForecastPage />} />
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
