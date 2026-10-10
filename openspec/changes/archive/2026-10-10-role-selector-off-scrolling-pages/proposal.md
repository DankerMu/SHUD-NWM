# role-selector-off-scrolling-pages

## Why

开启角色覆盖的开发 / mocked 测试构建里，移动形态下角色切换器是固定在 `main` 左缘中部、`z-[200]` 的 56×40 浮层（#2793 / D5，只按地图页设计）。在 `/ops`、`/monitoring`、`/system/model-assets` 这类整页滚动的页面上它压在内容之上：390×664 下盖住卡片标题一部分，750×342 下盖住 `Source` 筛选触发器左侧约 31px，滚到它下面的控件在那一块点不到（#2862）。生产构建不渲染它；代价是本地手机视口调试被遮挡，以及 mocked 移动 spec 的一个长期脆弱点。

## What Changes

- `apps/frontend/src/components/layout/AppShell.tsx`：移动形态下按路由分流——`/` 保持现状（左缘垂直居中，类名逐字不变）；其他路由把同一个切换器放进头部右端。桌面形态不变。
- `apps/frontend/src/components/layout/SiteHeader.tsx`：新增一个可选的末端插槽 prop（缺省不渲染任何东西）；切换器作为头部 flex 子项参与布局，标题靠已有的 `min-w-0` + `truncate` 收缩，而不是被绝对定位的浮层盖住。未传插槽时头部的 DOM 与类名与现状完全相同。
- vitest `AppShellRoleSelector.test.tsx` 按路由补用例；新增一个移动 mocked spec 断言非地图路由上的位置。

不改 `auth.ts`（开启条件）、`e2e/support/setRole.ts` 的签名、运维页组件、触控 / 字号审计的豁免。design.md 省略（compact）。

## Triage

```text
Issue type: bugfix
Fixture level: compact
Upstream suggested level: compact (agree；上游提示“若必须改 SiteHeader 的生产可见布局则升 expanded”——本方案给 SiteHeader 加的是缺省为空的插槽，生产构建恒不传，生产可见布局不变，故维持 compact；由 `SiteHeader` 测试里“未传插槽时子节点与 className 为字面量”的断言钉住)
Blast radius: 全部路由共用的外壳与头部；改坏时生产头部布局变化，或 mocked 车道所有靠 getByLabel('Role') 切角色的 spec 失效
Selected risk packs: Legacy compatibility（`/` 与桌面几何、setRole 契约不得移动）
Evidence floor: 新 mocked 断言先红后绿（三个移动 project）；`/` 与桌面的既有 spec 不改通过；未开启角色覆盖时头部 DOM 不变的 vitest；node-27 上 PR 构建的桌面 oracle 与移动预设（证明生产构建头部 / 外壳无变化）
```

## Impact

- 受影响文件：`AppShell.tsx`、`SiteHeader.tsx`、`src/components/layout/__tests__/AppShellRoleSelector.test.tsx`、一个新 e2e spec（或 `e2e/m11-role-selector.mobile.mocked.spec.ts` 内新增用例）。
- 受影响规格：`mobile-viewport-shell`。
