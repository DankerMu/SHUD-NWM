# Tasks

- [x] 1.1 `SiteHeader`：新增可选 prop（末端插槽，`ReactNode`，缺省 `undefined`）。传入时渲染为头部的一个 `shrink-0` flex 子项，位于头部最末（合作单位条之后，即头部右端）；未传入时输出的 DOM 与类名和现状逐字相同（不留空容器）。标题块已有 `mobile:min-w-0` + `mobile:truncate`，插槽占位后标题自行截断。
- [x] 1.2 `AppShell`：用 `useLocation()` 取 pathname（`AppShell` 恒在 Router 内：`App.tsx` 的 `BrowserRouter`、测试的 `MemoryRouter` / `BrowserRouter`）。把现有的 `<Select>…</Select>` 提成一个局部变量（只构造一次，`aria-label="Role"`、五个选项、`listbox` 结构不变），放置规则：
  - 桌面形态：现状（`main` 内 `absolute right-4 top-4 z-30`，触发器 `w-36`，弹层 `align=end side=bottom`），类名逐字保留。
  - 移动形态且 pathname 为 `/`：现状（`main` 内 `absolute left-0 top-1/2 z-[200] -translate-y-1/2`，触发器类与弹层方向逐字保留）。
  - 移动形态且其他路由：经 1.1 的插槽放进头部；触发器沿用移动形态的紧凑类（`w-14 …`），弹层开在触发器下方并向右对齐（`side=bottom align=end`；750×342 下头部下方约 298px，五项应放得下——判据是 2.1 在该视口“五个角色都点得到”，放不下则改方向并在报告里写明）。
  - 更新该处注释：D5 的左缘位置只适用于地图页，其余路由在头部（#2862）。
- [x] 1.3 vitest `AppShellRoleSelector.test.tsx`：既有四条按新结构保持（`/` 的移动类名断言、桌面类名断言、未开启时不渲染 ×2 形态）；新增：(a) 移动形态 + 非地图路由（`/ops`）时切换器在 `header` 元素内、不在 `main` 内，仍带紧凑触发器类，角色名仍在 DOM 里；(b) 桌面形态 + `/ops` 时仍在 `main` 内、桌面类名不变；(c) 未开启角色覆盖时，移动与桌面两种形态下 AppShell 渲染出的 `header` 恰有两个元素子节点（标题块 `div` 与 `img[alt="合作单位"]`）。另在 `SiteHeader` 自己的测试文件里（没有就新建 `SiteHeader.test.tsx`）钉死字面量：不传插槽时 `header` 恰两个元素子节点且 `header` 自身的 `className` 等于改动前的字符串；传插槽时为三个、插槽内容在最末。不要写成“与另一次渲染 `<SiteHeader />` 的输出比较”——两侧同源，空容器变异杀不死。测试的 Router 包装若需要指定初始路由，改用 `MemoryRouter initialEntries`。
- [x] 2.1 新移动 mocked spec（`e2e/m11-role-selector-scrolling-pages.mobile.mocked.spec.ts`，或并入 `e2e/m11-role-selector.mobile.mocked.spec.ts` 作新增用例——既有三条用例体不动）。三个移动 project 下各自的视口（390×664 / 750×342 / 844×390），另在用例内 `setViewportSize` 补 320×568：
  - `/ops` 与 `/monitoring`（operator）、`/system/model-assets`（`model_admin` 或 `sys_admin`）：触发器可见、包围盒在视口内；与页面滚动容器（`main` 的最后一个元素子节点，计算样式 `overflow-y` 为 `auto`——选择器先断言这一点再量）的包围盒不相交；在头部包围盒内，头部高仍为 48px；与徽标、标题元素的盒（带 `truncate` 的那个元素——截断后它的盒才是可见范围；`Range` 矩形不受 `overflow:hidden` 裁剪，320 下会越过触发器，不能用）互不相交；320×568 下另断言标题确实被截断（标题元素 `scrollWidth > clientWidth`），证明让位来自 flex 收缩。
  - 合作单位条：四个移动视口都窄于 `lg`，条带不可见。另加一条 `setViewportSize(1280×400)`（矮视口 -> 移动形态且条带可见）的 `/ops` 用例：先断言条带可见，再断言触发器在头部内、在条带右侧且与条带、标题元素盒互不相交。
  - 权限不足页（viewer 打开 `/ops`）：mock 须令 `display_readonly: false`（否则 viewer 被只读展示放行，见 `e2e/m11-role-selector.mobile.mocked.spec.ts` 的同类处理）；触发器同样在头部内、可点——这是从拒绝页切回有权限角色的唯一入口。
  - `/ops` 上（三个 project 各自的视口都跑，750×342 是头部下方最矮的一档，必跑）用 `setRole`（不带 `force`）依次切五个角色都成功（每次切换后 `getByLabel('Role')` 的文本含该角色名）；切到无权限角色后页面变为拒绝页属预期，继续切。
  - 路由切换：从 `/ops` 客户端导航到 `/`（或反向，取现成入口，如地图页的运维入口 / 浏览器后退）后，切换器回到各自的位置（`/` 上在 `main` 内左缘，`/ops` 上在头部内）。
- [x] 2.2 既有 spec 里因位置变化而失效的**注释或避让步骤**（如“先滚动避开角色切换器再点”）如实清点并在报告里列出；本 PR 只改与事实不符的注释措辞，不删避让步骤、不改断言（清理是否另行立单由编排者按清点结果决定）。

## 约定

- Risk pack「Legacy compatibility」selected：`/` 与桌面几何、`setRole` 契约是不可移动的参照 -> Must preserve 逐项不改通过；1.3 (c) 钉住生产头部 DOM 不变。
- 未选：Public API、Config、File IO、Schema、Auth（开启条件与角色语义不动）、Concurrency、Resource limits、Error handling、Release / dependency、Documentation。
- Must preserve（不改期望值通过）：`e2e/m11-role-selector.mobile.mocked.spec.ts` 既有三条、`e2e/m11-role-selector-desktop.mocked.spec.ts`、`e2e/ops-fallback-desktop.mocked.spec.ts`、`e2e/ops.mobile.mocked.spec.ts`、`e2e/model-assets.mobile.mocked.spec.ts`、`e2e/m11-ops-entry.mobile.mocked.spec.ts`、`e2e/m11-notices.mobile.mocked.spec.ts`、`e2e/m11-layer-basemap-launchers.mobile.mocked.spec.ts`、`e2e/m11-control-bar-portrait.mobile.mocked.spec.ts`、`e2e/m11-header.mobile.mocked.spec.ts`、`e2e/m11-header-desktop.mocked.spec.ts`；`e2e/support/setRole.ts`、`e2e/support/touchAudit.mocked.ts`、`e2e/support/opsFallback.mocked.ts` 不改；`e2e/live-display.spec.ts` 对切换器计数为 0 的断言语义不变。
- 止损：若既有 spec 断言了非地图路由上头部的子节点结构 / 标题宽度，或字号 / 触控审计在头部内的切换器上失败且豁免规则不覆盖——停下报告，不改审计规则、不改既有期望值。
- Non-goals：`/` 上的位置；桌面形态；`auth.ts`；运维页自身布局；`monitoring.mocked.spec.ts` 的 `force` 点击等测试质量问题（#2868）；给页面滚动容器加内边距让位。
- Evidence floor：
  - 先红后绿：2.1 的“不与页面滚动容器相交”“在头部内”在未改的 `src/` 上三个 project 都红；1.3 (a) 红；(b)(c) 为守卫（预期绿），如实报告。
  - 变异（按 sha256 还原）：路由分流写死为“恒在 `main` 左缘” -> 2.1 与 1.3 (a) 红；写死为“移动形态恒在头部” -> `m11-role-selector.mobile` 第一条（`/` 的几何）红；`SiteHeader` 未传插槽时也渲染空容器 -> `SiteHeader` 测试的字面量断言与 1.3 (c) 红；AppShell 在未开启角色覆盖时仍传插槽 -> 1.3 (c) 红。
  - 实测表：四个视口下头部、徽标、标题文字矩形、触发器、（可见时）合作单位条的包围盒，标题是否被截断。
  - `cd apps/frontend && pnpm typecheck && pnpm test && pnpm build`；Must preserve 的 spec；完整 mocked 车道；`src/__tests__/mobileSpecOracles.test.ts` 仍绿。
  - `openspec validate role-selector-off-scrolling-pages --strict --no-interactive`。
  - node-27（编排者执行，PR 构建对 live API，verify checkout + 预览端口）：生产构建不含切换器——证据脚本的桌面 oracle 与 `mobile-portrait` / `mobile-landscape` 预设 exit 0（头部 84 / 48px 等几何不变），并确认 `getByLabel('Role')` 计数为 0。这只是“生产头部 / 外壳不变”的守卫，不是切换器的 live 证据（issue 写明后者不适用）。切换器本身在 live 上不可达，只由 mocked 车道覆盖，如实记录。
