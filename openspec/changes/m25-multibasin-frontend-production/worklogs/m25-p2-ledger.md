# M25 P2 Carry-Forward Ledger

> 评审遗留的**非阻塞 P2**条目，供后续优化批次处理；**不在 #318（集成验收）内修**。
> 严重度统一 P2（UX 不一致 / 测试完备性 / 契约表达），均不破坏后端、不阻塞上线。
> 来源 commit 见各条；行号以合并时 master 为准，后续可能漂移。
> 记录日期：2026-06-07（#318 capstone 收尾）。

| id | 来源 | 文件:行 | 问题 | 建议 | 严重度 | 状态 |
|---|---|---|---|---|---|---|
| #315-P2-a | `ed60f80` (m25-315) | `apps/frontend/src/pages/hydroMet/HydroMetPage.tsx`（河段 fetch effect，~:471–:482，依赖 `debouncedMin/debouncedMax`） | stream_order 控件按字段可用性隐藏时，残留的 min/max state 仍随 debounce 发往后端（fetch effect 不按 availability 门控） | fetch effect 在拼 query 前按 stream_order 字段可用性门控 min/max，控件隐藏即不发该参数 | P2 | open |
| #315-P2-b | `ed60f80` (m25-315) / `dce266f` (m25-313) | `openapi/nhms.v1.yaml`（river-segments `stream_order`）+ `apps/frontend/src/pages/hydroMet/__tests__/ListProduction.test.tsx`（`as number` 逃逸） | 契约 `stream_order: number` 必填，前端"字段缺失→标注不可用"分支在当前类型下不可达；测试用 `as number` 逃逸构造缺失态 | 二选一：后端契约改 `number \| null`（真实反映可缺失），或在 spec 注明该分支为前端防御性兜底、契约保持必填 | P2 | open |
| #315-P2-c | `ed60f80` (m25-315) | `apps/frontend/src/pages/hydroMet/__tests__/ListProduction.test.tsx` | debounce 合并请求无显式断言（连续输入只发一次后端请求未被测） | 补一条 fake-timer 测试：连续输入多次、推进 debounce 后断言后端只被调用一次 | P2 | open |
| #315-P2-d | `ed60f80` (m25-315) | `apps/frontend/src/pages/hydroMet/__tests__/ListProduction.test.tsx` | 翻页只测 next，未测 prev / filter 变更后 offset 复位 | 补 prev 翻页断言 + search/variable 变更后 offset 归零断言 | P2 | open |
| #314-P2 | `5fb209b` (m25-314) | `apps/frontend/src/pages/hydroMet/HydroMetPage.tsx`（流域切换 → latest-product 重拉链路） | 流域切换时 stale basin_id select 边界：旧产品 basin_id 在新产品到达前短暂残留 | 切换瞬间清空/置 loading 旧产品身份，或以 selectedBasinId 为唯一来源派生展示，避免旧 basin_id 闪现 | P2 | open |
| #316-P2 | `d93c979` (m25-316) / `bd6e491` (m25-312) | `apps/frontend/src/pages/hydroMet/ReturnPeriodSection.tsx` + `__tests__/ReturnPeriodSection.test.tsx` + AppRoutes fixture（`Record<string, unknown>` 类型逃逸） | q_down degraded 分支无测试；缺页面级集成断言；AppRoutes fixture 用 `Record<string, unknown>` 掩盖 `return_period` 字段漂移 | 补 q_down degraded 分支单测 + 页面级 return_period_status 三态集成断言；fixture 换强类型（`components['schemas'][...]`）暴露字段漂移 | P2 | open |
