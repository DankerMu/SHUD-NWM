## 1. 工程骨架搭建

- [ ] 1.1 在 `apps/frontend/` 下初始化 Vite + React + TypeScript 项目（`pnpm create vite`），配置 `vite.config.ts`（root、build outDir、dev proxy `/api` → `:8000`）
- [ ] 1.2 安装并配置 Tailwind CSS v4 + PostCSS，提取现有 CSS variables 到 `tailwind.config.ts` theme tokens（background/panel/foreground/muted/border/accent/danger/river 共 9 个）
- [ ] 1.3 初始化 shadcn/ui（`npx shadcn@latest init`），安装基础组件：Button, Card, Table, Dialog, Badge, Select, Toast, Tabs, DropdownMenu
- [ ] 1.4 配置 openapi-typescript：从 `openapi/nhms.v1.yaml` 生成 `src/api/types.ts`；创建 `src/api/client.ts`（openapi-fetch 实例，baseUrl 为空——路径已含 `/api/v1` 前缀）
- [ ] 1.5 配置 Zustand：创建 `stores/auth.ts`（角色状态 skeleton）、`stores/monitoring.ts`（状态 skeleton，API 方法在 Group 2 实现）、`stores/forecast.ts`（状态 skeleton）
- [ ] 1.6 创建 `AppShell.tsx` 布局组件 + `NavBar.tsx` 导航栏（预报 | 监控）
- [ ] 1.7 创建 `RBACGate.tsx` 角色守卫组件，基于 auth store 的 role 判断
- [ ] 1.8 配置路由：`react-router-dom` v7，`App.tsx` 定义 `/` 和 `/monitoring` 路由（依赖 AppShell + RBACGate）
- [ ] 1.9 创建通用 hooks：`usePolling.ts`（interval + visibilitychange）、`usePagination.ts`（服务端分页）、`useToast.ts`
- [ ] 1.10 创建工具函数：`lib/format.ts`（日期/时长格式化）、`lib/cn.ts`（className merge）、`lib/constants.ts`（阶段名/状态色）
- [ ] 1.11 配置 `.gitignore` 排除 `node_modules/`、`dist/`（CI 步骤统一在 5.3 添加）
- [ ] 1.12 验证：`pnpm dev` 启动开发服务器，空白 SPA 正常加载，API 代理正常，`pnpm build` 产物可被 FastAPI 挂载

## 2. 监控页迁移

- [ ] 2.1 创建 `MonitoringPage.tsx`：三栏响应式布局（stages | jobs | trends），复用现有 `monitoring.html` 的布局逻辑
- [ ] 2.2 实现 `SummaryBar.tsx`：周期信息 + 4 个作业计数 Badge（成功绿/失败红/运行蓝/等待灰）+ ECharts 队列深度环形图（`QueueDonut.tsx`）
- [ ] 2.3 实现 `StageList.tsx` + `StageCard.tsx`：7 阶段纵向排列，箭头连接，状态图标（✓/✗/◉/○）、耗时、完成率
- [ ] 2.4 实现 `BasinFailures.tsx`：失败/部分失败阶段点击展开 per-basin 列表（model_id/error_code/error_message）
- [ ] 2.5 实现 `StageDurationBar.tsx`：阶段耗时横向柱状图（ECharts）
- [ ] 2.6 实现 `JobsTable.tsx` + `JobFilters.tsx`：shadcn Table 组件，status/run_type/scenario 过滤，submitted_at/duration 排序，服务端分页（usePagination hook）
- [ ] 2.7 实现 `LogModal.tsx`：shadcn Dialog 组件，调用 `/jobs/{job_id}/logs`，等宽字体可滚动，错误处理
- [ ] 2.8 实现重试/取消按钮：按状态条件渲染，调用 API 带实际角色 header，shadcn Toast 提示
- [ ] 2.9 实现 `TrendPanel.tsx`：7 天阶段平均耗时折线图 + 成功率折线图（ECharts `TrendLine.tsx`）
- [ ] 2.10 集成 `usePolling` hook：10s 自动刷新 status + stages + jobs，visibilitychange 暂停/恢复，手动刷新按钮
- [ ] 2.11 响应式断点验证：>1200px 三栏、800-1200px 两栏（trends 下移）、<800px 单栏
- [ ] 2.12 对比测试：新旧监控页在相同 mock 数据下的渲染结果应一致

## 3. 预报页迁移

- [ ] 3.1 创建 `ForecastPage.tsx`：左侧地图 + 右侧面板的两栏布局
- [ ] 3.2 状态管理：forecast store 完善 API 方法（选中河段、预报数据、加载/错误状态），依赖 1.4 生成的 API client
- [ ] 3.3 实现 `MapView.tsx`：react-map-gl + MapLibre GL，移植现有 map style、导航/比例尺控件、base layer 配置
- [ ] 3.4 实现 `RiverLayer.tsx`：GeoJSON 河段图层，移植 hover 高亮、tooltip、click-to-select、点击空白清空选择、河网加载失败 banner
- [ ] 3.5 实现 `ForecastPanel.tsx` + `SegmentInfo.tsx`：河段信息卡片 + 预报曲线 + analysis/forecast 双色区分 + 起报时间 markLine + 数据源信息 + 关闭按钮重置 + 错误状态含重试按钮
- [ ] 3.6 实现 `ForecastChart.tsx`：ECharts 预报曲线图（echarts-for-react），移植 include_analysis、segments/series 解析、空数据处理
- [ ] 3.7 对比测试：新旧预报页功能一致性验证（地图交互、曲线渲染、错误处理）

## 4. 前端测试

- [ ] 4.1 配置 Vitest：`vitest.config.ts` 继承 Vite 配置，jsdom 环境，React Testing Library
- [ ] 4.2 组件单元测试：StageCard（状态图标映射）、JobFilters（过滤逻辑）、RBACGate（角色守卫）、format 工具函数
- [ ] 4.3 Store 测试：monitoring store 的 fetchAll/fetchJobs 行为，mock API client
- [ ] 4.4 配置 Playwright：`playwright.config.ts`，baseURL 指向 Vite dev server
- [ ] 4.5 Playwright E2E — 监控页：加载 → stages 渲染 → 失败展开 → jobs 过滤 → log modal → 重试操作 → 权限拒绝
- [ ] 4.6 Playwright E2E — 预报页：加载地图 → 点击河段 → 预报曲线渲染

## 5. 清理与交付

- [ ] 5.1 删除旧的独立 HTML 文件：`apps/frontend/monitoring.html`；将 legacy `apps/frontend/index.html` 内容已完全迁移到 React 组件后删除（注意：Vite SPA 入口 `apps/frontend/index.html` 是新建的 Vite 模板文件，不可删除）
- [ ] 5.2 更新 `apps/api/main.py`：StaticFiles 挂载 `apps/frontend/dist`，添加 SPA fallback catch-all
- [ ] 5.3 更新 CI：添加 `pnpm install && pnpm build && pnpm test` 步骤到 GitHub Actions
- [ ] 5.4 更新 `package.json` scripts：`dev`（vite dev）、`build`（vite build）、`test`（vitest run）、`test:e2e`（playwright test）、`generate:api`（openapi-typescript）
- [ ] 5.5 添加 `apps/frontend/.env.example`（`VITE_API_BASE_URL=/api/v1`）
- [ ] 5.6 添加 bundle size check 脚本：`pnpm build` 后检查 `dist/assets/` 中 JS+CSS gzip 总大小 < 500KB（排除 MapLibre GL），CI 中作为 gate
- [ ] 5.7 更新项目 README 前端开发说明

## 验收标准

- `pnpm dev` 启动后所有页面功能与旧版一致
- `pnpm build` 产物 < 500KB gzip（不含 MapLibre GL）
- `pnpm test` 单元测试 > 80% 覆盖率
- `pnpm test:e2e` Playwright E2E 全绿
- 旧 HTML 文件已删除
- CI 前端构建步骤通过
