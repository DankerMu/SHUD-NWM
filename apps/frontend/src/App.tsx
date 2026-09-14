import { lazy, Suspense, type ReactNode } from 'react'
import { BrowserRouter, Navigate, Route, Routes, useLocation, useParams } from 'react-router-dom'

import { AppShell } from '@/components/layout/AppShell'
import { RBACGate } from '@/components/layout/RBACGate'
import { RegionErrorBoundary } from '@/components/layout/RegionErrorBoundary'

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
 * `param` 把路径参数映射为语义查询键（今天只剩 `segmentId`；`/basins/:basinId` 已随流域详情
 * 通道下线，#2109 裁决 B，不再映射路径参数）；`extraParams` 是静态语义参数（layer / overlay 等）。
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

/** 路由表：由 `App` 挂在 `BrowserRouter` 内；单独导出以便在 `MemoryRouter` 中钉住路由声明。 */
export function AppRoutes() {
  return (
    <Routes>
      <Route path="/" element={<OverviewPage />} />
      <Route path="/overview" element={<LegacyRedirect />} />
      <Route path="/hydro-met" element={<LegacyRedirect />} />
      <Route path="/forecast" element={<LegacyRedirect />} />
      <Route
        path="/meteorology"
        element={<LegacyRedirect extraParams={{ metStations: '1' }} />}
      />
      {/* 旧流域详情书签：落到全国总览，丢弃路径参数、保留其余 query（#2109 裁决 B）。 */}
      <Route path="/basins/:basinId" element={<LegacyRedirect />} />
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
          <RBACGate roles={['operator', 'model_admin', 'sys_admin']} allowDisplayReadonly>
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
  )
}

/**
 * 路由级边界（#2347 D2）：位于 `Suspense` **外**，懒加载 chunk 被拒时 React 把拒绝抛到这里；
 * 位于 `AppShell` **内**，站点页头不随页面崩溃卸载。按 pathname + search 复位：换路由或改 query 即重试。
 * 注意 `React.lazy` 缓存被拒的 import：chunk 失败时「重试」/回到同一路由会再抛，只有「刷新页面」能恢复。
 */
function RouteErrorBoundary({ children }: { children: ReactNode }) {
  const location = useLocation()
  return (
    <RegionErrorBoundary
      variant="page"
      testId="route-error-fallback"
      region="页面"
      resetKeys={[location.pathname, location.search]}
    >
      {children}
    </RegionErrorBoundary>
  )
}

/** 与路由器无关的生产组合：`App` 挂在 `BrowserRouter` 内，测试挂在 `MemoryRouter` 内。 */
export function AppFrame() {
  return (
    <AppShell>
      <RouteErrorBoundary>
        <Suspense fallback={<div>加载中...</div>}>
          <AppRoutes />
        </Suspense>
      </RouteErrorBoundary>
    </AppShell>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <AppFrame />
    </BrowserRouter>
  )
}
