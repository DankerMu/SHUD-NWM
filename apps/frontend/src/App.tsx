import { lazy, Suspense } from 'react'
import { BrowserRouter, Navigate, Route, Routes, useLocation, useParams } from 'react-router-dom'

import { AppShell } from '@/components/layout/AppShell'
import { RBACGate } from '@/components/layout/RBACGate'

const OverviewPage = lazy(() =>
  import('./pages/OverviewPage').then((module) => ({ default: module.OverviewPage })),
)
const MonitoringPage = lazy(() =>
  import('./pages/MonitoringPage').then((module) => ({ default: module.MonitoringPage })),
)
const ModelAssetsPage = lazy(() =>
  import('./pages/ModelAssetsPage').then((module) => ({ default: module.ModelAssetsPage })),
)

/**
 * 旧展示路由统一收敛到单页 `/`：
 * - replace 跳转，不污染历史回退栈；
 * - 保留原始 search query（深链状态不丢）；
 * - 附加语义参数时同名键以原始 search 的值为准（用户既有状态优先）。
 *
 * `param` 把路径参数（basinId/segmentId）映射为语义查询键；
 * `extraParams` 是静态语义参数（layer / overlay 等）。
 */
export function LegacyRedirect({
  param,
  extraParams,
}: {
  param?: { name: string; queryKey: string }
  extraParams?: Record<string, string>
}) {
  const location = useLocation()
  const params = useParams()
  const search = new URLSearchParams(location.search)

  const setIfAbsent = (key: string, value: string) => {
    if (!search.has(key)) search.set(key, value)
  }

  if (extraParams) {
    for (const [key, value] of Object.entries(extraParams)) setIfAbsent(key, value)
  }
  if (param) {
    const value = params[param.name]
    if (value) setIfAbsent(param.queryKey, value)
  }

  const query = search.toString()
  return <Navigate replace to={query ? `/?${query}` : '/'} />
}

export default function App() {
  return (
    <BrowserRouter>
      <AppShell>
        <Suspense fallback={<div>加载中...</div>}>
          <Routes>
            <Route path="/" element={<OverviewPage />} />
            <Route path="/overview" element={<LegacyRedirect />} />
            <Route path="/hydro-met" element={<LegacyRedirect />} />
            <Route path="/forecast" element={<LegacyRedirect />} />
            <Route
              path="/meteorology"
              element={<LegacyRedirect extraParams={{ metStations: '1' }} />}
            />
            <Route
              path="/flood-alerts"
              element={<LegacyRedirect />}
            />
            <Route
              path="/basins/:basinId"
              element={<LegacyRedirect param={{ name: 'basinId', queryKey: 'basinId' }} />}
            />
            <Route
              path="/segments/:segmentId"
              element={<LegacyRedirect param={{ name: 'segmentId', queryKey: 'segmentId' }} />}
            />
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
