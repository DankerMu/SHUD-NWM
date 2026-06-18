## Context

NHMS 前端当前是两个独立的单体 HTML 文件（`index.html` 596 行 + `monitoring.html` 1533 行），全部采用 inline CSS + vanilla JS + CDN 依赖（ECharts、MapLibre GL）。没有构建工具、没有类型系统、没有组件化、没有测试框架。

这个技术选型在 M0-M1 的 MVP 阶段是合理的——快速验证端到端链路，避免前端工程化的前置投入。但随着 M2-M3 不断增加功能（analysis 曲线拼接、warm-start 选择、monitoring 监控页），问题已经显现：

- **2129 行单文件**：monitoring.html 单个 `<script>` 块 1200+ 行，无法拆分、无法复用
- **59 处 DOM 操作**：`innerHTML` + `escapeHtml` 手工拼模板，XSS 风险依赖人工审计
- **无类型安全**：API 响应结构变更不会触发编译错误，只能运行时发现
- **无前端测试**：E2E 测试只覆盖 API 层，前端渲染/交互逻辑零覆盖
- **重复代码**：两个 HTML 文件各自定义 CSS variables、API 调用、Toast 组件，无共享
- **CDN 依赖**：ECharts 无 SRI 校验，MapLibre 无版本锁定

M4 规划中还有多个前端需求（多源对比、实时预警、管理后台），继续在单文件上堆砌已经不可持续。

## Goals

- 将前端迁移到 **Vite + React + TypeScript + Tailwind CSS + shadcn/ui** 工程化技术栈
- 建立前端组件库和 API 类型契约，支撑 M4+ 快速迭代
- 引入前端自动化测试（Vitest 单元 + Playwright E2E）
- 保持 FastAPI 后端不变，前端仍通过 `/api/v1/*` 调用
- 保持 deployment 模式不变：构建产物仍由 FastAPI StaticFiles 挂载

## Non-Goals

- 不引入 SSR/Next.js —— 内部工具，SPA 足够
- 不引入独立前端服务器 —— 构建产物嵌入 FastAPI
- 不做后端 API 改造 —— 前端适配现有 API
- 不在本阶段引入国际化 i18n
- 不做移动端 APP（Web 响应式已足够）

## Decisions

### D1: 框架选择 — React 18 + TypeScript

**备选**：Vue 3、Svelte 5

**选择理由**：
- ECharts 有官方 React 封装（echarts-for-react）；MapLibre 有 react-map-gl
- shadcn/ui 基于 React + Radix + Tailwind，是当前最成熟的无头组件方案
- TypeScript 对 API 响应类型建模能力强，与后端 Pydantic model 可直接映射
- 团队（含 AI 辅助开发）React 生态经验最深

### D2: 构建工具 — Vite 6

**备选**：Webpack 5、Turbopack

**选择理由**：
- 开发服务器 HMR 毫秒级响应，Webpack 项目同等规模需要 2-5 秒
- 生产构建基于 Rollup，tree-shaking 成熟
- 对 TypeScript/JSX 零配置支持
- 社区活跃度和插件生态当前最强

### D3: UI 组件 — shadcn/ui + Tailwind CSS v4

**备选**：Ant Design、Material UI

**选择理由**：
- shadcn/ui 不是 npm 包而是代码模板，组件复制进项目后完全可控，无版本锁定风险
- 基于 Radix Primitives，accessibility 内建
- Tailwind v4 的 CSS-first 配置让 design token 管理更简洁
- 产物体积远小于 Ant Design（按需引入，无全量 CSS）
- 内部工具不需要 Ant Design 的企业级表单/表格复杂度

### D4: 图表 — ECharts + echarts-for-react

**备选**：Recharts、Nivo、D3

**选择理由**：
- 已有 ECharts 使用经验和配置积累（M1 预报曲线、M3 监控图表）
- echarts-for-react 提供声明式封装，与 React 状态管理自然融合
- 地理热力图、3D 地形等 M4 潜在需求 ECharts 原生支持
- 性能：canvas 渲染大数据量（万级数据点）ECharts 优于 SVG 方案

### D5: 地图 — react-map-gl + MapLibre GL JS

**备选**：Leaflet、OpenLayers

**选择理由**：
- 已有 MapLibre GL 使用（index.html），迁移成本最低
- react-map-gl 是 Uber 维护的 React 封装，declarative API
- WebGL 渲染，大量河段/流域 polygon 不卡顿
- 自定义 style 灵活度高

### D6: 状态管理 — Zustand

**备选**：Redux Toolkit、Jotai、React Context

**选择理由**：
- 极简 API（~10 行代码定义 store），学习成本低
- 无 Provider boilerplate，任意组件直接 `useStore()`
- TypeScript 推断完美
- 适合中等复杂度应用（5-15 个 store），不需要 Redux 的 action/reducer 仪式感

### D7: API 客户端 — 基于 OpenAPI 自动生成

**备选**：手写 fetch wrapper

**选择理由**：
- 项目已有 `openapi/nhms.v1.yaml`，用 openapi-typescript + openapi-fetch 自动生成类型安全客户端
- API 变更时重新生成即可，不再人工同步
- 请求/响应类型与后端 Pydantic model 保持一致
- 消除手写 fetch 的 typo 和遗漏风险

### D8: 测试 — Vitest + Playwright

**备选**：Jest + Cypress

**选择理由**：
- Vitest 与 Vite 共享配置和变换管线，零额外配置
- Playwright 比 Cypress 对 WebGL（MapLibre）支持更好
- 两者都是当前社区首选，文档和 AI 辅助都有很好的覆盖

### D9: 部署方式 — Vite build → FastAPI StaticFiles

**选择理由**：
- 保持当前部署模式：`vite build` 输出到 `apps/frontend/dist/`，FastAPI 挂载 `dist/` 目录
- 开发时用 Vite dev server（端口 5173）+ 代理到 FastAPI（端口 8000）
- CI 中 `npm run build` 作为构建步骤，输出 hash 文件名自动 cache-busting
- 不引入额外的前端部署基础设施

## Risks / Trade-offs

| 风险 | 缓解 |
|------|------|
| 迁移期间前端功能回归 | 逐页迁移（先 monitoring，再 index），每页迁移完成后 Playwright E2E 验证 |
| 构建产物体积增大 | Vite tree-shaking + 代码分割，ECharts 按需引入（仅 bar/line/pie/gauge），预算 <500KB gzip |
| MapLibre WebGL 与 React 生命周期冲突 | react-map-gl 已解决此问题，有成熟的 ref/callback 模式 |
| 新增 Node.js 构建依赖 | CI 已有 Node 环境（Markdown Lint 等），pnpm 替代 npm 减少磁盘占用 |
| shadcn/ui 组件样式与现有设计不一致 | 提取现有 CSS variables 作为 Tailwind theme tokens，保持视觉连续性 |
