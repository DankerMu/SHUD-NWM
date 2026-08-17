# Proposal: registry-retire-declaration-channel

## Why

Issue #1433（PR #1429 round-2 F1 verifier CONFIRMED，pre-existing）：已在
canonical registry 的 model 包变 invalid 后，bulk publish 的合法 skip 通道
（`publish_scheduler_file_registry.py:663-678`/`:749-757`/`:758-784`）让
prospective 比 previous 少一行；refresh 的 #1080 cutover gate 把差集无条件判
`removed` 并 refuse（`scheduler_file_provider_refresh.py:2852-2870`），在
canonical replace 前抛 `SCHEDULER_REGISTRY_REFRESH_PRECOMMIT_FAILED`。自动
refresh lane 无条件装该 gate 且无绕过开关——此后**每趟 daily refresh 都失败
且不自愈**，健康流域的新包一并进不了 registry，唯一出口是被 runbook 定性为
审计红旗的手动 `--allow-uncovered-cutover`。注释承诺的 "separate declared
workflow" 在仓库内不存在实现：declaration schema 的 entry 必填双 checksum、
`transition_mode` enum 只有 `"replace"`，结构上无法表达 removal。

## What Changes

按 issue 推荐方案（显式 retire 声明，最小扩已有通道）：

- **声明层（design D1）**：`schemas/scheduler_registry_package_cutover.schema.json`
  **v1 内加法扩展**——`transition_mode` enum 加 `"retire"`；`new_checksum`
  放宽为 hex 或 null，if/then 钉死「retire ⇔ `new_checksum: null`、replace ⇔
  hex」。`schema_version` const 不动：既有 replace-only 文件逐字仍有效；旧
  读者遇 retire 文件按既有 `registry_cutover_declaration_invalid` fail-closed
  （安全方向）。
- **判据层（design D2）**：`_classify_registry` 规则 1 为 retire entry 开
  membership 豁免（retire 的 model 本就不在 prospective；其 model 必须 ∈
  removed 差集，否则 declaration_invalid）；规则 4 重写为**两遍法**——
  第一遍判 retire checksum 不符（置污染位 + declaration_invalid 行，该
  removal 不另出 removal_refused 行），第二遍仅当 declaration 整体有效才
  把匹配行放进新桶 `declared_retirements`（污染时全部 refuse 不进桶、入桶
  与否不依赖 removed 行序）；无 entry 的 removal **逐字保持**既有
  `registry_cutover_removal_refused`。拒因优先级阶梯不变。
- **证据层（design D3）**：removal 拒因 entry 增补 skip-cause 判据——publish
  侧把「被 skip 的已注册 model」的 inventory 行（`status`/
  `missing_required_files`/`invalid_required_files`，键名与
  `publish_scheduler_file_registry.py:675` 一致）结构化透传给 gate；有行 ⇒
  「包变 invalid 被 skip」，无行 ⇒「目录被删」——判据层区分两形。
- **对账层（design D4）**：classification receipt 新桶
  `declared_retirements`（items ⊆ removed，且 **`retired_total <=
  removed_total` 显式不等式**堵截断伪造口）；`refused` 覆盖面改为「removed
  中未声明部分」；`unchanged + package_changed + removed == previous_count`
  不变（removed 仍是观察分类，retire 只影响拒绝不影响分类）；id-only/
  dry_run 模式该桶必为 0；legacy receipt 无桶按 0。receipt JSON Schema
  四处同步。
- **文档层（design D5）**：runbook `current-production-ops.md:508-510` 补
  「包变 invalid 也触发 removal」路径 + 完整恢复顺序（停 timer → retire
  declaration（首选）/ `--allow-uncovered-cutover`（遗留红旗）→ 复核
  receipt → 恢复 timer；#1104 并发禁令全程适用）。
- **测试层（design D6）**：端到端串联（bulk publish skip 已注册 model →
  gate 实际终态）双分支主锚 + declaration_invalid 变体格 + 对账伪造格 +
  schema 前后兼容格；既有 `:3198` removal 用例判据零改动。
- **spec delta**：`scheduler-registry-refresh` ADDED「declared retirement
  channel」requirement + MODIFIED id-only 对账 requirement（新桶入约束）。

## Risk Triage

- Fixture level: **expanded**。M 规模、fail-closed 安全门语义扩展（错一格
  = 注销 #1080 保证或死锁不解）、schema/receipt/spec/runbook 五载体联动、
  声明通道有 generation 绑定/过期/审计等既有机制须逐项继承。issue 无
  Suggested fixture level；divergence：无。
- Repair intensity: standard。
- Risk packs:
  - **security/fail-closed（decision-ladder 变体）: selected** —— 未声明
    removal 必须继续 fail-closed；retire 豁免不得泄漏到 replace/未知形；
    generation 绑定、checksum 匹配、过期窗口对 retire 逐项适用；伪造
    receipt（retired ⊄ removed、id-only 带 retired）必须被对账拒。
  - compatibility/regression: selected —— 既有 replace declaration 文件、
    `:3198` removal 用例、`cutover_gate` audit 块形状（#1132/R2-A1）、
    dry_run 提前返回语义全部零改动；receipt 对账对 legacy 无桶 receipt
    不回归。
  - spec-compliance: selected —— ADDED requirement 与实现逐句对读；
    MODIFIED id-only requirement 从 live 文本脚本再生（#1515 教训）。
  - deletion-safety: selected —— 本 change 的产出恰是「让一行合法消失」，
    受控删除语义本身是测试对象。
  - performance、security/auth(权限)、version-divergence: not selected ——
    无热路径/权限/解释器分叉面。
- Seams under test：`_classify_registry` 纯函数直测（规则 1/4 全形）+
  `_load_cutover_declaration`（schema+代码双层）+
  `_registry_precommit_gate`→`publish_all_basin_scheduler_registry` 端到端 +
  `_enforce_registry_classification_reconciliation` 对账。

## Non-Goals

- 不改 skip 通道门控判据（哪些 model 被 skip 不变；坏包拖垮整树是更坏方案）。
- 不放松 #1080 默认拒绝：无「自动接受 invalid 导致的 removal」隐式豁免。
- 不动 IC 头部形状门（#1197/PR #1429）。
- 不动拒因 payload details 口径本体（#1432 范围）；本 change 只在新增
  removal 证据里复用同组键名。
- 不动 #1104 并发禁令与 `cutover_gate` audit 块形状（#1132/R2-A1）。
- 不做独立 retire 声明文件格式（备选路线弃：复用既有 declaration 的
  generation 绑定/过期/审计机制是推荐方案的核心收益）。

## Impact

- `scripts/scheduler_file_provider_refresh.py`（`_classify_registry`、
  `_load_cutover_declaration`、`CUTOVER_TRANSITION_MODES`、
  `ClassificationResult`/`to_receipt`、
  `_enforce_registry_classification_reconciliation`、gate 透传参数）
- `scripts/publish_scheduler_file_registry.py`（skip 的已注册 model 结构化
  透出）
- `schemas/scheduler_registry_package_cutover.schema.json`（v1 加法扩展）+
  `schemas/examples/scheduler_registry_package_cutover.example.json`（就地
  扩 retire entry，CI 同名配对）
- `schemas/scheduler_file_provider_refresh_receipt.schema.json`（四处：
  classification 可选新桶、refused_group 三可选键、nullable retire
  `$defs`、cutover 组 hex 约束不动——design D4）
- `services/orchestrator/scheduler_generation.py`（**共享 declaration 的第
  二消费者**，fixture review F2：常量加值 + retire entry 容忍-跳过短路，
  design D2）
- `tests/test_scheduler_generation.py`（消费者格）
- `openspec/specs/scheduler-registry-refresh/spec.md`（archive 回写）
- `docs/runbooks/current-production-ops.md`
- `tests/test_scheduler_file_provider_refresh.py` ·
  `tests/test_publish_scheduler_file_registry.py`
