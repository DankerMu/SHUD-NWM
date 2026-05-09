import { BrowserRouter, Route, Routes } from 'react-router-dom'

import { AppShell } from '@/components/layout/AppShell'
import { RBACGate } from '@/components/layout/RBACGate'
import { ForecastPage } from '@/pages/ForecastPage'
import { MonitoringPage } from '@/pages/MonitoringPage'

export default function App() {
  return (
    <BrowserRouter>
      <AppShell>
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
      </AppShell>
    </BrowserRouter>
  )
}
