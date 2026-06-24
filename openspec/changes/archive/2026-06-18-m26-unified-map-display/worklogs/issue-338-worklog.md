---
status: archived
current_authority:
  - openspec/specs/single-map-shell-routing/spec.md
  - openspec/specs/legacy-display-page-retirement/spec.md
  - openspec/specs/inplace-overview-basin-detail/spec.md
  - openspec/specs/map-feature-popups/spec.md
  - openspec/specs/met-station-cluster-layer/spec.md
  - docs/runbooks/display-readonly-live-mvt.md
  - docs/runbooks/two-node-deployment-overview.md
superseded_by:
  - openspec/specs/single-map-shell-routing/spec.md
  - openspec/specs/legacy-display-page-retirement/spec.md
  - openspec/specs/inplace-overview-basin-detail/spec.md
  - openspec/specs/map-feature-popups/spec.md
  - openspec/specs/met-station-cluster-layer/spec.md
status_since: 2026-06-24
archive_scope: whole-document
retained_for: "audit evidence for M26 inplace overview/basin detail implementation"
---

# issue-338 worklog — 总览↔详情就地化 + store 改造 (inplace-overview-basin-detail)【最高风险】

## 角色
- 编排/验证：Claude Code（本地）；实现：dispatched fix subagent（leaf，不 commit）
- 验证 oracle：本地 tsc/vitest/check:api-types/build（纯前端）
- 合并门：审核 clean 即 merge（用户授权，不等 CI）

## 关键基线 + 架构事实
- `queryState.ts`：`M11QueryState` 无 basinId；`normalizeM11Identifier`（白名单 `[A-Za-z0-9._:-]{1,96}`）；`serializeM11QueryState` 逐字段 set；`parseM11QueryState` 逐字段 normalize。
- `overviewData.ts`（1380 行）：`loadBasinDetail(basinId, query)` **签名已是 (basinId, query)**——basinId 本就是入参，store 几乎不用动。`basinSnapshotMatchesQuery(snapshot, basinId, query)` 已按 basinId 单独匹配（`requestScope.basinId === basinId`）。
- **R1 根因**：`requestScopeQueryKey`(L201)/`requestScopeDataKey`(L205) 都 `serializeM11QueryState({...query,...})`。basinId 必须进 serialize（URL 分享），否则会进这两个键 → 缓存键变动 → 闪烁/重取。
- `BasinDetailPage.tsx`(869)：`useParams().basinId` → `loadBasinDetail` → `M11Layout` + 面板：StateReadout / SegmentDiscoveryPanel / SelectedSegmentPanel / SelectedSegmentTrendPanel / SelectedSegmentComparisonTable / BasinUnavailableNotice / "返回全国总览"。
- `OverviewPage.tsx`(598)：`basinAnalysisHref(basin,state)=m11QueryHref('/basins/:id',...)`（L500）；"进入流域分析" BasinLink（L204）+ "进入分析"（L431/435）。
- `/basins/:basinId` 路由已在 #337 改为 LegacyRedirect→`/?basinId=`；BasinDetailPage 现仅由 LegacyPagesHarness 挂载跑深度测试（AppRoutes ~L2874-3991）。

## 决策
- D-338-1：`queryState` 加 `basinId: string|null`（default null；parse 用 normalizeM11Identifier；serialize 末尾 set basinId）。
- D-338-2（R1 缓解，核心）：`requestScopeQueryKey` + `requestScopeDataKey` 内 strip basinId（`{...query, basinId: null, ...}`）→ 既有 overview/basin 序列化键**字节不变**，零缓存churn；basinId 仍由 `requestScope.basinId === basinId` 单独匹配。store 其余内部逻辑不动。
- D-338-3：先补 overviewData store 单测护栏（basinId-from-query：不同 basinId 区分快照、加 basinId 后既有键不变），再改页面。
- D-338-4：OverviewPage→DisplayMapPage 双模式（state.basinId）；进入分析→handleQueryChange({basinId})+fitTo；返回总览→{basinId:null,segmentId:null}。pathname 恒 `/`。
- D-338-5：BasinDetail 面板抽到 `components/m11/BasinDetailPanels.tsx`（导出名保留）；删 BasinDetailPage.tsx + LegacyPagesHarness 的 /basins 条目；basin-detail 深度测试改打真 App `/?basinId=…`。

## 验证矩阵（编排者独立复验）
| 检查 | 状态 |
|---|---|
| tsc | ✅ EXIT=0 |
| vitest | ✅ 626 passed / 29 files（含补测）|
| check:api-types | ✅ EXIT=0 |
| build | ✅ built 2513 modules |

## 候选/裁决 ledger（4 路并行 review，零 critical/major）
| 候选 | reviewer | 裁决 | 处置 |
|---|---|---|---|
| 6 Scenario 全实现 + pathname 恒 / | Spec/Inplace | CONFIRMED 正向 | 无 |
| R1 零 churn 真伪 | R1/Store | CONFIRMED（serialize 过滤 falsy，basinId:null 不输出，字节一致；basin 区分靠 requestScope.basinId）| 无 |
| in-flight dedup 键含 basinId | R1/Store | minor pre-existing（请求去重键非持久缓存，含 basinId 反而正确）| 记录，不改 |
| 详情能力无遗漏 + hooks 子组件隔离 | Capability/Integration | CONFIRMED（逐行 diff 仅 3 处预期改造，无相机回环）| 无 |
| 删"返回水文预报"链接 | Capability/Integration | minor（/forecast 已重定向，链接无意义）| 按设计删除 |
| reserved-char basinId 编码边界覆盖下降 | Test-Integrity | minor（whitelist 实际拒绝 /?#%）| ✅ 已补 m11QueryState 边界测试（拒绝保留字符 + 单值往返）|

裁决：clean（0 in-scope CONFIRMED 缺陷，0 merge-blocking）。唯一 actionable minor（reserved-char 边界）已补测闭合。

## 动态阶段
- [x] Phase 0 评估 + 基线 + R1 根因
- [x] Phase 1 fix subagent
- [x] Phase 2 本地验证（4/4 绿）
- [x] Phase 3 commit + PR
- [x] Phase 4-6 review（4 路并行，1 轮 clean）
- [x] Phase 7 独立复核（编排者直读 R1/queryState diff + 补 reserved-char 边界测试 + 复验 626）
- [x] Phase 8 merge（用户授权，不等 CI）
