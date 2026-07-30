# Design: first-cycle-package-ic-consumption

## 风险分级

**expanded**(生产调度主路径 + 跨 5 层链路:gate 判定 → 纯 evaluator → cold-seed 运载(`chain_forecast_cycle`)→ manifest 组装 → runtime 消费;#1164 priority:high)。

## 机制与现状(实机验证,2026-07-30)

- 断点:`chain_manifests.py:446-460` `init_mode = 3 if basin.init_state_id or init_state_uri else 1`;首时次两条路径(`chain_forecast_state.py:190-200` 非 strict、`scheduler_generation.py:626-638` generation-aware `COLD_NEW_MODEL`)都产出空 selection → init_mode=1 → `runtime.py:842` 包内 IC 门 false → `:860` `_set_cold_start_initial_state` → cfg.para `INIT_MODE 1` → SHUD `MD_initialize.cpp:30-45` 全变量清零。包内 cfg.ic 在 `runtime.py:429` stage 后无人读、无 warning。
- 注意:非 strict 是**代码默认**(`chain_config.py:154` `default=False`);生产 env 显式 strict(`infra/env/compute.example:19,163` `NHMS_REQUIRE_FORECAST_WARM_START=true`)。契约必须两种模式都成立,不得依赖"gate 已在上游拦截"这类模式相关假设。
- 实机观察(待 4.1 审计 receipt 正式确立,不作为验收前提):6 个 070500 上线流域首 run manifest `cold_start_no_state`/`init_mode=1`;抽查 variant 包 cfg.ic 非零(128KB~4.3MB)。仓内无任何 fresh-start 声明字段(全仓 grep 零命中)——issue 的"显式 fresh-start 配置"表述不成立,审计判据必须证据驱动。

## 核心决策

### D1. 资格判定信号:gate 计算,纯 evaluator 注入(不加发布字段)

- **IO 落位**:`evaluate_transition_decision` 是纯函数(`scheduler_generation.py:546-555`,只收 `history`/`declaration`);所有 IO 在 gate(`scheduler_generation_gate.py:322` 已有 `load_cutover_declaration` 先例)。包 manifest 读取放 gate:经 registry `resource_profile.manifest_uri`(`publish_scheduler_file_registry.py:566-567` 已发布)读包 manifest,产出资格信号注入 evaluator 新参数。
- **信号取值**:`QUALIFIED(含 ic sha256)` / `UNQUALIFIED` / `UNREADABLE` / **`None`(registry 无 manifest 引用——legacy 场景)**。evaluator 新参数**可选、默认 `None`**——零重基线(must-preserve #10)依赖此默认值。判定:`included_files[]` 存在 `relative_path` 匹配 `*.cfg.ic` 的条目,且其 **`sha256`** ≠ 空文件摘要(`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`)且 **`size_bytes > 0`**(字段名以 `basins_package.py:289-299` 实际 schema 为准——是 `sha256`/`size_bytes`,不是 `checksum`)。manifest uri 存在但不可达/JSON 损坏 = `UNREADABLE`。
- 内容级校验(header 可解析、非全零)下沉到 runtime staging fail-closed——选择层零对象 IO,只读 manifest。

### D2. 首时次判定表落在 generation-aware 路径;信号缺席 = legacy 保持

`scheduler_generation.py:626-638` `not history.exists_any_generation` 分支改为按注入信号判定:

| 资格信号 | 决策 |
|---|---|
| `QUALIFIED` | **`PACKAGED_IC_BOOTSTRAP`**(新枚举;强制消费——issue 目标契约 1) |
| `UNQUALIFIED` / `UNREADABLE` | **block**(新 `BLOCK_FIRST_CYCLE_INITIAL_STATE_UNDECIDED` typed reason 进 `TRANSITION_DECISION_REASONS`,只增不改,`scheduler_generation.py:204-221`) |
| `None`(无注册包引用) | `COLD_NEW_MODEL` 原样(**显式 carve-out**,见下) |

- **审批载体砍掉(原 D5,YAGNI)**:registry import 强制包含 `<shud_input_name>.cfg.ic`(`basins_registry_import.py:2005-2018` `BASINS_REGISTRY_SOURCE_MISMATCH`),不合格实际只剩 0 字节/损坏一种形态——fail-closed block 即可,显式 cold-start 审批机制列为 follow-up(非目标),本变更不引入 `cold_start_approval.json`。
- **`None` carve-out 的边界(两个 gate bypass 具名)**:`scheduler_generation_gate.py:322-328`(无 `package_checksum` 且无 declaration → legacy)与 `:336-346`(state index 不可用 → legacy)。二者在 §8 transition 评估**之前**即 return legacy 证据形态(无 transition block、无 mode、无 cold_start_reason)——保持 pre-#1164 原样:strict 下 block,非 strict 下仍可达 legacy `cold_start_no_state` 选择回退(具名残余,见残余风险)。带 `no_prior_history` 的 labeled `COLD_NEW_MODEL` 只出现在第三种输入(registry 行无 `manifest_uri`)。生产 18 个模型全部注册且带 manifest 引用,首时次不会走 bypass;此 carve-out 换来:`tests/test_scheduler_generation.py:846-868` 直方图基线(13 WARM_CONTINUE / 6 COLD_NEW_MODEL)与全部无包 unit fixture **零重基线**。记入 spec 场景与残余风险。
- 非 strict 旧路径(`chain_forecast_state.py:190-245`)选择逻辑不改;首时次运载不依赖它(见 D3)。

### D3. 决策到 runtime 的运载:cold-seed 通道扩展(修 F1/F2 主缺口)

evidence mode → basin 的唯一非测试消费点是 `chain_forecast_cycle.py:230-234` `_cold_seed_admitted`(现只认 `{db_free_cold_new_model, db_free_cold_declared_cutover}`)。不扩它的后果:strict 下新 mode 落到 `_select_strict_forecast_initial_state` → `WARM_START_SUCCESSOR_CHECKPOINT_MISSING` 硬错;非 strict 下落到 `chain_forecast_state.py:190-200` → `cold_start_no_state` 静默清零。因此:

- cold-seed 集合扩入 `db_free_packaged_ic_bootstrap`(该集合扩展只作用于 strict 分支的 admitted 判定);**basin 标记的写入与 `strict_required` 无关**:凡 evidence mode 为 `db_free_packaged_ic_bootstrap` 的 basin,一律写 `packaged_ic_selected=True` + `packaged_ic_checksum`(gate 信号携带的 ic sha256)——写点必须提到 `chain_forecast_cycle.py:210` 的 `strict_required and _cold_seed_admitted` guard 之外、`:187-194` 非 strict 早退之前,否则非 strict 路径仍会在 `chain_forecast_state.py:197-200` 降级 `cold_start_no_state`。
- **写序**:`_apply_initial_state_selection_to_basin`(`chain_runtime_utils.py:569-575`)会覆写 `init_state_quality`——packaged 标记必须在其后生效或由 manifest 组装层最终裁决:`chain_manifests.py` init_mode 推导改 `3 if init_state_id or init_state_uri or packaged_ic_selected else 1`,quality 在组装时由 `packaged_ic_selected` 最终决定 `packaged_calibrated_state`(last-writer 确定性,strict/非 strict 同一条路)。
- manifest `initial_state` 块:`quality=packaged_calibrated_state`、`state_id=None`、`ic_file_uri=None`、新字段 `packaged_ic_checksum`。**`init_state_id` 保持 None**:不进 `canonical_forecast_cohort_init_state_identities`(`accepted_submit_identity.py:848-850` 跳过空 id——首时次 cohort 语义与现状一致,#1183 absence-tolerant verdict 不受扰动)。
- 两种模式各一条端到端测试:`NHMS_REQUIRE_FORECAST_WARM_START=true/false` 下 bootstrap 候选均产出 `init_mode=3` + `quality=packaged_calibrated_state`。

### D4. runtime 消费收紧:两个 packaged 分支统一 consume-or-raise

runtime 实际有**两个** packaged 分支:`runtime.py:841-862`(无 state_id 探测包内 IC)与 `:863-875`(手工 manifest 带 `state_id="qhh_packaged_calibrated_state"`、`checksum: None`,`create_qhh_shud_manifest.py:118-124`),后者 fall-through 会走到 `:899` 静默冷启动。收紧为:

- 门:manifest 声明 `quality=packaged_calibrated_state`(两种形态都算)→ 必须找到唯一非空 `*.cfg.ic` 并 consume-or-raise,**绝不 fall-through**。
- 校验:manifest 带 `packaged_ic_checksum` 时,文件 sha256 必须相等(端到端完整性);**不带**(legacy 手工 manifest,qhh 形态)时跳过 checksum 比对,仅验非空 + header 可解析,记 warning 进 run evidence——must-preserve #7(qhh 手工路径可用)以此成立。header 校验复用 `tests/test_runtime_ic_header.py` 所测工具。
- 任何失败(缺文件/0 字节/checksum 不符/header 不可解析)→ 新错误码 `PACKAGED_IC_CONSUMPTION_FAILED` fail-closed;**删除 packaged 分支内及其 fall-through 可达的静默 `_set_cold_start_initial_state` 降级**。负锁是**行为锁**:声明 packaged-IC 的 manifest(带/不带 state_id 两形态)staged IC 缺失时必须 raise,测试断言无 `INIT_MODE 1` 执行——不以 grep 为锁(`:899`/`:933` 的回退对 state-snapshot 来源运行仍保留)。
- 残差归一化**做**:从 `_materialize_ic_to_project_name` 抽出 `normalize_state_negative_residuals` 调用为可复用 helper(`runtime.py:1015`),packaged 路径直接调 helper;`_materialize_ic_to_project_name` 本体(含 `:1029` `_shift_cfg_ic_time`)对 warm 路径逐字节不变。packaged 文件本就是 canonical 名(`basins_registry_import.py:2005-2007`),无需改名 materialize。
- 有意不做:时间一致性校验(`_verify_ic_time_consistency` 是 warm-start state 的 valid_time 语义,包 IC 是无时刻标定产物)与 `_shift_cfg_ic_time`。此取舍记入 spec 场景。
- 未声明 packaged-IC 的路径行为逐字节不变(合法 cold-start 回退、warm 消费管道、exact_required 检查顺序)。

### D5. evidence 面

candidate `state_evidence.mode` 新增 `db_free_packaged_ic_bootstrap`(`scheduler_generation_gate.py`);journal `hydro_run` 行新增 quality 字段(`file_orchestration_journal.py`);M24 receipt quality 枚举追加 `packaged_calibrated_state`(`services/m24_live/receipt.py:59-64`,既有 4 值不动)。

### D6. 存量审计工具(新 `scripts/audit_first_cycle_initial_state.py`)

只读,数据源:registry manifest(18 行)→ 各 model 包 manifest(IC 资格,D1 判据,`sha256`/`size_bytes` 字段)→ 首个业务 run manifest(`{workspace}/runs/` + object-store `runs/`,取最早 cycle 的 `initial_state.quality`/`runtime.init_mode`)。输出 schema 化 receipt:每 model×source 一行 `{model_id, source, ic_qualified, first_cycle, first_run_quality, first_run_init_mode, verdict}`,verdict ∈ `{consumed_package_ic, cold_start_with_qualified_ic(缺陷), cold_start_no_ic, undetermined(证据缺失)}`(审批机制已砍,无 `approved_cold_start`)。复用 `scripts/node27_storage_inventory_audit.py` 的 no-follow + jsonschema receipt 模式。receipt 带 limits 字段(仅核对 manifest 记录的 sha256,不逐字节验对象,NFS IO 约束)。验收:列出 6 个存量流域(×2 source = 12 行 `cold_start_with_qualified_ic`),零写。

## Must-preserve(评审锚)

1. **有历史后的**合法 cold-start 回退不变:stale 阈值(`chain_forecast_state.py:208-214`)、lineage/QC 回退耗尽(`:223-245`)、state 对象损坏回退(`runtime.py:893-901/:930-935`,仅限 state-snapshot 来源运行——声明 packaged-IC 的运行不得到达)、`state_manager is None`(`chain_forecast_state.py:183-184`)。
2. **首时次基线的变化是显式的**:`tests/test_warm_start.py:139-148` 与 `test_warm_start_chaining.py:885-929` 是首时次测试(无包 manifest fixture)——落入 D2 的 `None` carve-out,期望值**不变**;若实现使其变红,即 carve-out 被破坏,按缺陷处理。新增合格包场景用**新测试**表达,不改旧基线语义。
3. `warm_continue` 全管道不变:三方时间一致性(`runtime.py:1031-1086`)、`_materialize_ic_to_project_name` 本体、`WARM_START_REQUIRED`/`WARM_START_TIME_MISMATCH` 错误码。
4. #982/#1081:state-clone 6 个 refusal scope 与 `state_clone_cold_start_approval_required` 错误码、provenance 字段、`COLD_DECLARED_CUTOVER` 路径逐字节不变。
5. cohort `init_state_identities`:空 `init_state_id` 跳过语义不变(D3 保证);journal `recorded_init_state_id` 匹配语义不变。
6. `TRANSITION_DECISION_REASONS` 只增不改;既有枚举值与 evidence `mode` 字符串不变(键已被 `tests/test_scheduler_generation.py:443` 锚定)。
7. `create_qhh_shud_manifest.py` 手工路径在新门下仍可用(D4 legacy 无 checksum 形态)。
8. M24 receipt 枚举扩展是**追加**,既有 4 值不动。
9. 包发布/discovery 格式不变(D1 只读既有字段);direct-grid fingerprint gate 不触碰;审计只读。
10. `tests/test_scheduler_generation.py:846-868` 直方图基线(13/6)不变(信号缺席 → legacy)。

## 判定表(首时次,exists_any_generation=False)

| 资格信号 | 决策 | evidence mode | init_mode | SHUD 行为 |
|---|---|---|---|---|
| QUALIFIED | PACKAGED_IC_BOOTSTRAP | db_free_packaged_ic_bootstrap | 3 | 读包 IC |
| QUALIFIED 但 runtime 校验失败(checksum 不符/header 坏/0 字节/缺文件) | (已提交后)run fail `PACKAGED_IC_CONSUMPTION_FAILED` | 同上 | 3 | fail-closed,无静默清零 |
| UNQUALIFIED(0 字节/缺条目) | BLOCK_FIRST_CYCLE_INITIAL_STATE_UNDECIDED | blocked | — | 不提交 |
| UNREADABLE(uri 不可达/JSON 损坏) | 同上(fail-closed) | blocked | — | 不提交 |
| None:registry 行无 manifest 引用 | COLD_NEW_MODEL 原样(labeled,`no_prior_history`) | db_free_cold_new_model | 1 | 清零(显式 carve-out) |
| None:两个 gate bypass | legacy 证据形态原样(无 transition block) | (legacy,无 mode) | strict block / 非 strict 1 | strict 不提交;非 strict 可达 legacy `cold_start_no_state`(具名残余) |

## Evidence mapping

- `tests/test_scheduler_generation.py`:判定表 5 行全覆盖(QUALIFIED/UNQUALIFIED/UNREADABLE/None carve-out/两 bypass 各一);既有直方图与 `WARM_CONTINUE`/`COLD_DECLARED_CUTOVER` 回归**零改动**通过(F9:不存在"有历史的 COLD_NEW_MODEL"——该枚举唯一发射点在无历史分支)。
- cold-seed 运载测试:strict=true/false 两种模式下 bootstrap 候选 → basin 标记 → manifest `init_mode=3`/`quality=packaged_calibrated_state`/`packaged_ic_checksum`(F1/F2)。
- `tests/test_shud_runtime.py`:两 packaged 形态(带/不带 state_id)消费主线(cfg.para `INIT_MODE 3` + 残差归一化 helper 被调);负测:缺文件/checksum 不符/0 字节/header 坏 → `PACKAGED_IC_CONSUMPTION_FAILED` 且断言无 `INIT_MODE 1` 执行(行为负锁);未声明路径回归。
- 审计工具测试(新文件):合成 registry + run 证据 → 缺陷行 verdict;undetermined 行为;零写断言。
- 红前证据:未改源上首时次 basin 包含合格 IC → 断言 `quality == packaged_calibrated_state` 失败(现值 cold_start_no_state)。
- 实机(merge 后,变更 2 前):审计工具在 node-22 跑出 12 行 `cold_start_with_qualified_ic` receipt,回贴 #1164(该 receipt 同时正式确立"18 包全带非零 IC"的现场观察)。

## 非目标

- 六流域历史回放与受控覆盖(变更 2,依赖本变更)。
- **显式 cold-start 审批机制**(原 D5,已砍;follow-up issue 承接——当前语义:不合格即 block,无审批逃生门)。
- 流域参数重标定、径流偏低成因。
- #982/#1081 语义修改;DB-mode 路径。
- 包发布格式演进(IC 语义字段进 manifest 属 follow-up)。

## 残余风险(具名)

- `None` carve-out:未注册(无 manifest 引用)模型的首时次仍是 labeled cold start——生产 18 模型全注册,不受影响;若未来出现未注册模型上线,将复现 #1164 形态。follow-up 与审批机制一并承接。
- **gate bypass 的非 strict 残余**:两个 bypass 路径在非 strict 模式下仍可达 legacy `cold_start_no_state` 选择回退(pre-#1164 原样,未新增)。生产为 strict(bypass 首时次会 block),且 18 模型均带 `package_checksum` + manifest 引用,正常不走 bypass;改 bypass 行为会破 must-preserve #1/#6/#10,故显式保留。
- 12 个老流域若未来因 cutover 重回首时次语义,将进入新契约——行为变化是本契约的目的,首个自然案例在变更 2 回放窗口外观察。
- 包 manifest sha256 与包内实际文件不符(发布期损坏)由 D4 端到端 checksum 兜住;审计工具对历史包只读 manifest,不逐字节验对象(记入 receipt limits)。

## 修订记录(fixture review round 1)

REVISE→修复:F1/F2(D3 cold-seed 运载 + 双模式契约)、F3(D1 gate/纯函数 IO 落位)、F4(must-preserve #1/#2 重写——原引基线实为首时次测试)、F5(`sha256`/`size_bytes`)、F6/F7(D4 双分支 + legacy 无 checksum 形态 + 行为负锁)、F8(残差 helper 抽取)、F9(回归目标改真实可达枚举)、F10(spec delta 补 MODIFIED 两条)、F11(砍审批载体)、F12(两 bypass 具名 carve-out)、F13(引文修正)、F14(NFS 观察降级为待审计确立)、F15(不变性场景列观测量)。
