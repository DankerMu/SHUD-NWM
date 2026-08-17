# Design: registry-retire-declaration-channel

## D1 — 声明层：v1 加法扩展（裁决：不升 v2、不建独立文件）

`schemas/scheduler_registry_package_cutover.schema.json`：

- `entries.items.properties.transition_mode.enum` → `["replace", "retire"]`。
- `entries.items.properties.new_checksum` →
  `{"anyOf": [{"type": "string", "pattern": "^[0-9a-f]{64}$"}, {"type": "null"}]}`。
- `entries.items` 增条件（2020-12，两条 if/then 必须包在 **`allOf`** 里——
  同一对象只能有一个裸 `if`，fixture review Note 实测本仓 jsonschema 4.26.0
  Draft202012Validator 正确执行且与 `additionalProperties: false` 无冲突）：
  `if transition_mode=="retire" then new_checksum type null`；
  `if transition_mode=="replace" then new_checksum pattern hex`。
  `required` 不变（`new_checksum` 仍必填，值可为 null——显式 null 是有意
  设计：retire 声明必须写明「没有新包」，缺键与 null 语义不同）。
- `schemas/examples/scheduler_registry_package_cutover.example.json` **就地
  扩展**加一条 retire entry（CI json-schema-validate 按 `<base>.example.json`
  ↔ `<base>.schema.json` 同名配对，新增第二个示例文件不会被校验——必须改
  既有示例；本地等价命令 `check-jsonschema --check-metaschema` +
  `--schemafile`，tasks 3.4 结论化）。
- `schema_version` const、`additionalProperties: false`、其余字段全部不动。

裁决理由（v2 弃）：加法扩展下**所有既有 v1 文件逐字仍有效**；旧代码读到
retire 文件时 `CUTOVER_TRANSITION_MODES` 校验（`:2541`）与 schema 双双
fail-closed 成 `registry_cutover_declaration_invalid`——错误方向安全。升 v2
则既有文件/写作流程全部作废，收益为零。独立 retire 文件弃：会复制
generation 绑定/过期窗口/字节上限/审计整套机制。

`_load_cutover_declaration`（`scheduler_file_provider_refresh.py:2481-2545`）
同步：`CUTOVER_TRANSITION_MODES = frozenset({"replace", "retire"})`（`:108`）；
per-entry 代码层镜像 schema 条件（retire ⇒ `entry["new_checksum"] is None`，
replace ⇒ hex str；不匹配 ⇒ `registry_cutover_declaration_invalid`）。

**receipt 侧 transition_mode 按桶收紧（fixture review Note）**：`:2301-2303`
现共用该常量——加值后 `declared_cutovers` 里伪造 `"retire"` 行会被放行。
改为按桶校验：`declared_cutovers` entry 只许 `"replace"`、
`declared_retirements` entry 只许 `"retire"`；receipt JSON Schema 的对应
`$defs`（`:327` 区）同理各自钉死。

**共享 schema 的第二消费者（F2，必改）**：
`services/orchestrator/scheduler_generation.py` 与 refresh gate 共用同一
env（`NHMS_REGISTRY_CUTOVER_DECLARATION_PATH`）与 schema 文件（`:167`
加载），自带一份 `CUTOVER_TRANSITION_MODES = frozenset({"replace"})`
（`:143`）。不改它则 retire 声明在位期间：schema 放行 → `:831-836` 消费者
常量判 `declaration_entry_transition_mode_invalid` → **整个文件** `_load_error`
→ `match_declaration_entry` 恒 None → 并存的 replace 声明也失效、候选落
`block_declaration_missing/stale`。裁决：**消费侧容忍并跳过 retire entry**——
其常量加 `"retire"`，`match_declaration_entry`（或其调用链）对
`transition_mode=="retire"` 的 entry 直接 skip（retire 无 new_checksum，
与 generation 派生无关）；短路精确落点（R2-4）：`:814`
（`seen_model_ids.add`）**之后**、`:815-816` checksum 归一化**之前**——
放太早会连带丢掉重复 model_id 检测
（`declaration_entry_model_id_invalid` 分支）；放晚则 None 变字符串
`"None"` 流入 `:1109` 比较与 `:1125` `derive_generation`。跳过的 retire
entry 不进 `normalized_entries`，`_declaration_load_evidence`
（`:930-940`）的 `entry_count`/`entry_model_ids` 比文件少——可接受偏差，
实现知情。测试补：retire entry 在位时文件正常加载、replace entry
照常匹配、retire entry 永不匹配任何候选、无 `"None"` 字符串产物
（`tests/test_scheduler_generation.py:199` 现有 `"rebase"` 用例覆盖不到
这条新可达分支）。

## D2 — 判据层：`_classify_registry` 规则重写（`:2768-2872`）

分类段（unchanged/added/package_changed/removed 计算）**逐字不动**——
removed 仍是纯观察差集；retire 只改「拒不拒」，不改「算不算 removed」。
dry_run 提前返回（`:2745-2747`）不动：dry_run 不评估 removal，也不评估
retirement。

- **规则 1 改造**（unknown declaration ids）：现行「entry 的 model_id 不在
  prospective ⇒ invalid」对 retire 语义不成立（retire 的 model 定义上不在
  prospective）。新判据：
  - replace entry：不在 prospective ⇒ invalid（原语义逐字保留）。
  - retire entry：必须 ∈ `result.removed`（即 in previous ∧ not in
    prospective）。retire 的 model 仍在 prospective ⇒ invalid（不能退役
    一个还在发布的 model）；retire 的 model 不在 previous ⇒ invalid
    （退役一个不存在的行）。两形都走既有 `refuse_declaration` 通道。
- **规则 4 重写**（removals），**两遍法**（R2-1 裁决选 (a)），含污染语义
  （F5）：
  - **第一遍**（判定）：扫全部 removed，对匹配到 retire entry 但
    `old_checksum` 不符的形置 `declaration_invalid` 并出
    `refuse_declaration` 行——至此污染位终值确定（规则 1/2 的贡献在规则
    4 之前已完成）。
  - **第二遍**（入桶）：仅当污染位终值为 False，匹配的 retire entry
    （`transition_mode=="retire"` ∧ `old_checksum == previous 行
    package_checksum`）⇒ 进 `result.declared_retirements`（entry 形：
    `model_id`、`old_checksum`、`new_checksum: None`、
    `effective_cycle_utc`、`transition_mode`——与 `declared_cutovers`
    entry 同构）；**不** refuse。污染位为 True ⇒ 全部匹配 entry 走
    `refuse_declaration`、不进桶。
  - **为何比规则 2 严（有意不同形，写明防再生）**：规则 2 是单遍边判边
    污染（`:2818-2838`），先出现的合法行会先入桶、receipt 可出现
    「cutovers 非空 + declaration 无效」——replace 只换 checksum，可容忍；
    retirement 删 canonical 行是破坏性动作，入桶与否**不得**依赖
    previous canonical 的行序。两遍法交付 requirement 的 "under a
    declaration that is valid as a whole"，且结果确定。规则 2 现行为
    **零改动**（out of scope）。
  - 匹配到 retire entry 但 `old_checksum` 不符 ⇒ `declaration_invalid`
    （走 `refuse_declaration` 带 entry，置污染位——与规则 2 的 checksum
    不符同语义）；**该 removal 不再额外产出 `removal_refused` 行**（一个
    removal 事件恰一条 refusal 行，镜像规则 2 中 mismatched entry 不另出
    undeclared 行；F7 定死）。
  - 无 entry / entry 是 replace ⇒ 既有 `registry_cutover_removal_refused`
    refusal **逐字保持**（replace entry 命中 removed 的形：该 entry 已被
    规则 1 判 invalid——model 不在 prospective——此时 removal 行的拒因仍是
    `removal_refused`，refusal 行数 = 规则 1 的 entry 行 + removal 行，
    D6 格 4 钉双行并存）。
- **拒因阶梯**（`:2866-2872`）语义不变：`declaration_invalid` >
  `removal_refused`（仅当存在**未声明** removal）> `undeclared`。全部
  removed 都被声明时不再因 removal 拒。
- **generation 绑定**（`:2779-2785`）对 retire 逐字适用：declaration 的
  generation 必须等于本趟 prospective generation——prospective 少了 bravo
  行，generation 内容哈希随之变化，运维须按新 generation 写声明（与
  drift cutover 流程同形，dry_run 先取 generation 再写 declaration）。
  过期窗口/cycle 对齐（`:2528-2540`）同样共用，零新代码。

## D3 — 证据层：removal 拒因的 skip-cause 判据

- publish 侧：`_select_publishable_models` / `_repair_missing_radiation_contexts`
  的三处 skip `continue` 不改判据，但被 skip 的 inventory 行
  （`{model_id: {"status": ..., "missing_required_files": [...],
  "invalid_required_files": [...]}}`，键名与 `:675` details 一致）需结构化
  透出。**透传路线（F1 裁决）**：skip 信息产生在 publisher 内部，gate 是
  3 位置参数钉死的回调（类型声明
  `publish_scheduler_file_registry.py:162-165`、调用点 `:357`、refresh 闭包
  `scheduler_file_provider_refresh.py:769-786`、测试 double
  `tests/test_publish_scheduler_file_registry.py:530-534`）——给回调加第 4
  参会破全部 4 处。采用 **out-sink 模式**（与 `cutover_gate` /
  `registry_commit_observer` 同形）：`publish_all_basin_scheduler_registry`
  加一个加法可选 sink 参数（默认 None，既有调用方零改动），publisher 在
  skip 时点把行喂给 sink；refresh 侧闭包用 nonlocal 承接、再传进自己构造
  的 `_registry_precommit_gate` 调用（gate 本体加加法关键字参数默认
  None——gate 不是那个 3 参回调本身，闭包是；闭包内部调用 gate 时带上）。
  回调契约与既有测试 double 逐字不动。透出发生在 skip 时点，无需 publish
  侧知道「已注册」——全部被 skip 行都透出，gate 侧只对 removed ∩ skipped
  使用。
- gate 侧：removal refusal entry（`:2853-2861`）增补三键（有 inventory 行
  时）：`status` / `missing_required_files` / `invalid_required_files`。
  无行（真·目录被删）⇒ 三键缺省——两形由此可辨。`declared_retirements`
  entry 不带这三键（声明放行无需归因）。
- 上限纪律（F3-3 修正）：refusal entry 的文件名列表**没有现成对应截断
  常量**（`_validate_value_bounds:2322-2336` 只兜 512/256 字符串底）——
  为三键中的两个列表键定显式上限（复用 `MAX_COLLECTION_ITEMS` 或在其旁
  新增一个语义命名常量，实现时二选一并记录），写入与校验两侧同值。

## D4 — 对账层：receipt 与 reconciliation（F3 全枚举 + F4 收紧）

- `ClassificationResult` 增 `declared_retirements: list`；`to_receipt()`
  增桶（`{"total": n, "items": [...]}`，与其它桶同构）。
- **classification 顶层键校验（F3-1）**：`:1894-1912` 严格差集——新桶必须
  进 `_CLASSIFICATION_OPTIONAL_KEYS`（`:1877`），**绝不进 required**：
  `reconstruct_primary_receipt`/`validate_current_receipt` 要读 pre-change
  磁盘 receipt（`:1873-1875` 注释），进 required 即作废 I7。
- **容缺访问（F3-2）**：既有 `_total()`/`_items()` 对缺失组直接 raise，
  「legacy 无桶按 0」需新增容缺访问器（缺键 ⇒ (0, [])），只用于新桶。
- `_enforce_registry_classification_reconciliation`（`:2216-2260` 区）：
  - `unchanged + package_changed + removed == previous_count` **不变**
    （removed 计入被声明退役的行）。
  - `declared_retirements` 的 model_id 集合 ⊆ `removed` items 集合；违反
    ⇒ `receipt_classification_invalid`。
  - **显式拒 `retired_total > removed_total`（F4，只紧不松）**：⊆ 按
    items 做而 items 有 `MAX_COLLECTION_ITEMS=256` 截断（`:2619-2627`），
    空/截断 items 下「⊆ 恒真 + 灌大 total 压低 refused 下界」是真实伪造
    口——total 级不等式与 items 级 ⊆ 双钉。
  - `expected_min_refused = (removed_total - retired_total) +
    max(package_changed_total - declared_total, 0)`（前项经上一条保证
    ≥0，无需再钳）。
  - id-only 模式（mode="id_only" 或 legacy dry_run 键控）：
    `declared_retirements.total` 必为 0（removed 恒 0 的推论，显式钉）。
  - legacy receipt 无 `declared_retirements` 键 ⇒ 按 total=0 处理。
- **refusal entry 对象组校验（F3-3）**：`_validate_object_group`
  （`:2263-2307`）同为严格差集——D3 三键必须扩进 `:1964-1970` 调用处的
  `optional_keys`；列表上限见 D3。
- **receipt JSON Schema（F3-4，结论：要改，四处）**：
  `schemas/scheduler_file_provider_refresh_receipt.schema.json`——
  `registry_classification` `additionalProperties:false`+required
  （`:178-192`）加**可选**新桶；`refused_group.items` 属性写死
  （`:271-296`）加三个可选键；`declared_cutover_group.new_checksum` 是纯
  hex `$ref` 不接受 null（`:325`）——retire 桶**新建** nullable `$defs`
  （不改 cutover 组的 hex 约束，按桶收紧见 D1/D2 Note）；
  `tests/test_scheduler_file_provider_refresh.py:1788`/`:3651` 拿该 schema
  校 receipt，漏改即红。
- `cutover_gate` audit 块（`_CUTOVER_GATE_KEYS` `:1826`）**零改动**。

## D5 — 文档层：runbook

`docs/runbooks/current-production-ops.md:508-510` 扩写：

- 触发面补全：removal 不只来自「动了 `NHMS_BASINS_ROOT` 目录」——已注册
  model 的包变 invalid（如 `*.cfg.ic` 头部畸形、缺 `*.tsd.rl` 无模板）被
  publish skip 同样触发，且 `--dry-run` 预览看不到该拒绝。
- 恢复顺序（首选，retire declaration）：停 timer（#1104 并发禁令）→
  `--dry-run` 取 prospective generation → 写 retire declaration（entry:
  model_id + old_checksum=previous canonical 行 checksum +
  `new_checksum: null` + `transition_mode: "retire"` + 对齐 cycle）→
  跑一趟 refresh（受 gate 审计，receipt 出 `declared_retirements` 行）→
  核对 receipt → 删 declaration → 恢复 timer。
- 遗留路径降级标注：`--allow-uncovered-cutover` 手动退役仍可用但保持
  审计红旗定性（bypass 理由 + 双端 SHA-256 + declaration 复位），标注
  「仅当 declaration 通道不可用时」。
- `:500-504` 的 classification 桶枚举同步加 `declared_retirements`
  （fixture review Note）。
- 消费侧提示（F2）：retire declaration 在位期间 scheduler 侧
  （scheduler_generation）读同一文件——容忍-跳过语义落地后 replace entry
  照常生效；恢复顺序无需为 scheduler 侧加步骤，但 runbook 记一句该共享。

## D6 — 测试计划

主锚（端到端，`tests/test_scheduler_file_provider_refresh.py`，以 `:3198`
`_run_gate` 族与 publish 侧既有 e2e 夹具为骨架）：

- **红/现状分支**（issue 复现形，先红后绿的「红」即现状锚定）：previous
  canonical = {alpha, bravo}；bravo 包变 invalid（如删一个 required file）
  被 bulk publish skip；无 declaration ⇒
  `provider_reason == "registry_cutover_removal_refused"`，refusal entry
  带 `invalid_required_files`/`missing_required_files`/`status`（D3 绿
  后）；previous canonical 字节不变、零发布。**此分支修后仍成立**（未
  声明 fail-closed），红-绿差分在下一格。
- **绿/声明分支**：同底 + 合法 retire declaration（generation 绑定本趟）⇒
  refresh 成功、canonical 少 bravo 行、receipt
  `declared_retirements == [bravo]`、`refused` 不含 bravo、alpha 正常
  发布。**接线前红分两段（F6 修正归因）**：tasks 1.1/1.2 落地前，红在
  **加载层**——schema enum 拒 retire → `_load_cutover_declaration` 抛 →
  gate `:2929-2932` 捕获后 `declaration=None`、规则 1 根本不执行，终态
  refused 形是 `__declaration__` 合成行 + bravo 的 `removal_refused` 行
  （`:2943-2954` 覆写 reason 为 `declaration_invalid`）；1.1/1.2 落地、
  1.3 未落地时，红才在**规则 1**（membership 判 invalid，refused 行带
  entry checksum）。红证按两段分别记录，断言形状勿混。

判定表（`_classify_registry` 直测）：

| # | 形 | 期望 |
|---|---|---|
| 1 | removed + 匹配 retire entry | declared_retirements，放行 |
| 2 | removed + retire entry checksum 不符 | declaration_invalid |
| 3 | removed + 无 entry | removal_refused（逐字既有） |
| 4 | removed + replace entry（model 不在 prospective） | 规则 1 invalid ∧ removal_refused 并存 |
| 5 | retire entry 但 model 仍在 prospective | declaration_invalid |
| 6 | retire entry 但 model 不在 previous | declaration_invalid |
| 7 | retire + generation 不符 | declaration_invalid（既有绑定） |
| 8 | 两 removed，一声明一未声明 | 一进桶一 refuse，拒因 removal_refused |
| 9 | dry_run + retire declaration | 提前返回，桶空（id-only 不评估） |
| 10 | 污染形（来源=规则 1/2：generation 不符 / unknown id / package_changed checksum 错）+ 合法 retire entry | retire 也 refuse_declaration、不进桶（F5） |
| 11 | 污染形（来源=**另一条 retire entry** checksum 错）+ 合法 retire entry，removed 两种行序各跑一次 | 两序同结果：两条都 refuse、桶空（R2-1 两遍法的确定性钉测） |

schema/加载层格：retire entry `new_checksum: null` 通过；retire + hex 拒；
replace + null 拒；既有纯 replace v1 文件逐字通过（前向兼容锚）；
`CUTOVER_TRANSITION_MODES` 相等钉测（schema enum 与代码常量一致）。

对账层格：带 `declared_retirements` 的诚实 receipt 通过；伪造
retired ⊄ removed 拒；id-only 带非零 retired 拒；legacy 无桶 receipt
通过（零回归）；refused 下界公式扣减 retired 后仍钉住伪造下溢。

既有锚零改动：`:3198` removal 用例、`registry_cutover_undeclared` 族、
audit 块族、`test_publish_scheduler_file_registry.py` 既有断言。

## D7 — Invariant Matrix

- I1 未声明 removal fail-closed 逐字不变（`:3198` 判据零改动）。
- I2 skip 通道门控判据零改动（publish 侧只加结构化透出，不改 continue）。
- I3 `cutover_gate` audit 块形状（#1132/R2-A1）与 #1104 并发禁令零改动。
- I4 generation 绑定/过期窗口/cycle 对齐/字节上限对 retire 逐项适用
  （共用同一实现，禁止旁路）。
- I5 对账公式扩展只紧不松：previous-side 等式不变、items ⊆ 约束 +
  `retired_total <= removed_total` 显式不等式双钉（前项因此 ≥0 无需钳位）、
  id-only 新桶恒 0。
- I9 两遍法顺序无关性：matched retirement 入桶与否不依赖 removed 迭代序
  （requirement 明句 + D6 格 11 钉测）。
- I6 dry_run 语义不变：不评估 removal 亦不评估 retirement。
- I7 既有 replace declaration 文件与 legacy receipt 前向零回归（新桶
  optional-not-required 是其载体，D4）。
- I8 共享 declaration 消费者（scheduler_generation）在 retire entry 在位
  时不失能：replace entry 照常匹配、retire entry 永不匹配、无 `"None"`
  字符串产物（F2）。

## D8 — 已知残余（记录，不在本 change 修）

一行合法退役后的下游：readiness 由 `derive_catalog_bound_readiness_entries`
从 registry_models 重算（自然消失）；但 state snapshot index 走
`validated_entries_for_renewal()` 独立续期（`:720-725`）不与 registry 交叉
校验——退役 model 的存量 state 行成孤儿；orchestrator 读不到该 registry 行
的行为未评估。按残余记 PR body；若实现期证据表明为真实缺陷，路由
issue-scribe 另立。
