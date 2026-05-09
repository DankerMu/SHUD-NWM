## Architecture

```
apps/frontend/                    ← Vite 项目根目录
├── index.html                    ← Vite 入口 (SPA)
├── vite.config.ts
├── tailwind.config.ts
├── tsconfig.json
├── package.json
├── src/
│   ├── main.tsx                  ← React 入口，Router 挂载
│   ├── App.tsx                   ← 根布局 + 路由定义
│   ├── api/
│   │   ├── client.ts             ← openapi-fetch 实例，baseUrl 配置
│   │   └── types.ts              ← openapi-typescript 自动生成
│   ├── stores/
│   │   ├── auth.ts               ← 用户角色状态 (Zustand)
│   │   ├── forecast.ts           ← 预报页状态
│   │   └── monitoring.ts         ← 监控页状态（stages/jobs/metrics/queue）
│   ├── hooks/
│   │   ├── usePolling.ts         ← 通用轮询 hook (interval + visibilitychange)
│   │   ├── usePagination.ts      ← 服务端分页 hook
│   │   └── useToast.ts           ← toast 通知 hook
│   ├── components/
│   │   ├── ui/                   ← shadcn/ui 组件（Button, Card, Table, Dialog, Badge, Select, Toast）
│   │   ├── layout/
│   │   │   ├── AppShell.tsx      ← 顶部导航 + 侧栏框架
│   │   │   ├── NavBar.tsx        ← 导航栏（预报 | 监控）
│   │   │   └── RBACGate.tsx      ← 角色守卫组件
│   │   ├── charts/
│   │   │   ├── QueueDonut.tsx    ← 队列深度环形图
│   │   │   ├── StageDurationBar.tsx ← 阶段耗时柱状图
│   │   │   ├── TrendLine.tsx     ← 通用折线图（耗时/成功率）
│   │   │   └── ForecastChart.tsx ← 预报曲线图（从 index.html 迁移）
│   │   ├── map/
│   │   │   ├── MapView.tsx       ← react-map-gl 地图容器
│   │   │   └── RiverLayer.tsx    ← 河段图层
│   │   ├── monitoring/
│   │   │   ├── SummaryBar.tsx    ← 顶部摘要条
│   │   │   ├── StageCard.tsx     ← 单个阶段卡片
│   │   │   ├── StageList.tsx     ← 7 阶段列表容器
│   │   │   ├── BasinFailures.tsx ← per-basin 失败展开
│   │   │   ├── JobsTable.tsx     ← 作业列表表格
│   │   │   ├── JobFilters.tsx    ← 过滤器栏
│   │   │   ├── LogModal.tsx      ← 日志查看 Dialog
│   │   │   └── TrendPanel.tsx    ← 右侧趋势面板
│   │   └── forecast/
│   │       ├── ForecastPanel.tsx ← 预报侧边栏（从 index.html 迁移）
│   │       └── SegmentInfo.tsx   ← 河段信息卡片
│   ├── pages/
│   │   ├── ForecastPage.tsx      ← / 路由
│   │   └── MonitoringPage.tsx    ← /monitoring 路由
│   └── lib/
│       ├── cn.ts                 ← Tailwind className merge 工具
│       ├── format.ts             ← 日期/时长格式化
│       └── constants.ts          ← 阶段名映射、状态颜色等常量
├── public/
│   └── favicon.ico
├── e2e/
│   ├── monitoring.spec.ts        ← Playwright: 监控页 E2E
│   └── forecast.spec.ts         ← Playwright: 预报页 E2E
└── dist/                         ← vite build 输出（git ignored）
```

## Design Token 迁移

将现有 CSS variables 映射到 Tailwind theme tokens：

```typescript
// tailwind.config.ts
export default {
  theme: {
    extend: {
      colors: {
        background: "var(--bg, #f7f9fb)",
        panel: "var(--panel, #ffffff)",
        foreground: "var(--text, #1f2937)",
        muted: "var(--muted, #64748b)",
        border: "var(--line, #d7dee8)",
        river: { DEFAULT: "var(--river, #0f8fbf)", strong: "var(--river-strong, #ef7d22)" },
        accent: "var(--accent, #2266cc)",
        danger: "var(--danger, #b42318)",
      },
      fontFamily: {
        sans: ['Inter', 'PingFang SC', 'Microsoft YaHei', 'Arial', 'sans-serif'],
      },
    },
  },
}
```

## 路由设计

```typescript
// App.tsx
<BrowserRouter>
  <AppShell>
    <Routes>
      <Route path="/" element={<ForecastPage />} />
      <Route path="/monitoring" element={
        <RBACGate roles={['operator', 'model_admin', 'sys_admin']}>
          <MonitoringPage />
        </RBACGate>
      } />
    </Routes>
  </AppShell>
</BrowserRouter>
```

SPA 路由通过 Vite 的 `appType: 'spa'` + FastAPI 的 fallback 配置实现。FastAPI 需要在 API 路由之后添加一个 catch-all：

```python
# apps/api/main.py
@app.get("/{full_path:path}")
async def spa_fallback(full_path: str):
    return FileResponse("apps/frontend/dist/index.html")
```

## API 类型生成

OpenAPI 路径使用完整前缀 `/api/v1/*`（与 `openapi/nhms.v1.yaml` 一致），`openapi-fetch` 客户端的 `baseUrl` 设为空字符串（路径已包含前缀）：

```bash
# 从 OpenAPI spec 生成 TypeScript 类型
npx openapi-typescript openapi/nhms.v1.yaml -o src/api/types.ts

# API 客户端使用
import createClient from 'openapi-fetch'
import type { paths } from './types'

const client = createClient<paths>({ baseUrl: '' })

// 类型安全的 API 调用——路径使用 OpenAPI 中的完整键
const { data, error } = await client.GET('/api/v1/pipeline/stages', {
  params: { query: { source: 'GFS', cycle_time: '2026-05-09T00:00:00Z' } }
})
// data 自动推导为 PipelineStagesResponse 类型
```

## 状态管理

```typescript
// stores/monitoring.ts
import { create } from 'zustand'

interface MonitoringState {
  source: string
  cycleTime: string
  stages: PipelineStage[]
  jobs: PipelineJob[]
  jobTotal: number
  queue: QueueDepth | null
  isPolling: boolean
  error: string | null

  setSource: (source: string) => void
  setCycleTime: (time: string) => void
  fetchAll: () => Promise<void>
  fetchJobs: (filters: JobFilters) => Promise<void>
}
```

## 轮询 Hook

```typescript
// hooks/usePolling.ts
function usePolling(callback: () => Promise<void>, intervalMs = 10_000) {
  useEffect(() => {
    let timer: number
    const tick = () => { callback().finally(() => { timer = window.setTimeout(tick, intervalMs) }) }

    const onVisibility = () => {
      if (document.hidden) { clearTimeout(timer) }
      else { tick() }
    }

    document.addEventListener('visibilitychange', onVisibility)
    tick()

    return () => {
      clearTimeout(timer)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [callback, intervalMs])
}
```

## Vite 开发代理

```typescript
// vite.config.ts (位于 apps/frontend/，root 为 '.' 即自身目录)
export default defineConfig({
  build: { outDir: 'dist', emptyOutDir: true },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true },
      '/health': { target: 'http://localhost:8000' },
    },
  },
})
```

## 构建与部署

```bash
# 开发
cd apps/frontend && pnpm dev     # → http://localhost:5173，API 代理到 :8000

# 构建
cd apps/frontend && pnpm build   # → apps/frontend/dist/

# 生产（FastAPI 挂载）
app.mount("/", StaticFiles(directory="apps/frontend/dist", html=True), name="frontend")
```

CI 新增步骤：
```yaml
- name: Build Frontend
  run: cd apps/frontend && pnpm install --frozen-lockfile && pnpm build && pnpm test
```

## 迁移策略

**逐页迁移，新旧并行**：

1. **Phase 1**：搭建 Vite + React + Tailwind 骨架，配置 shadcn/ui、路由、API 类型生成、Zustand store
2. **Phase 2**：迁移 monitoring.html → `MonitoringPage.tsx`（功能最复杂，收益最大）
3. **Phase 3**：迁移 index.html → `ForecastPage.tsx`（含 MapLibre 地图）
4. **Phase 4**：前端测试（Vitest 组件测试 + Playwright E2E）
5. **Phase 5**：删除旧 HTML 文件，更新 FastAPI 挂载路径

每个 Phase 完成后都是可部署状态。Phase 2 完成时旧 index.html 仍然可用；Phase 3 完成后所有页面都在新架构上。
