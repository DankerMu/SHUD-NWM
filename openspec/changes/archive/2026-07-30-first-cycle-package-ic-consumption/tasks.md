# Tasks: first-cycle-package-ic-consumption

## 1. 选择层:资格信号 + 首时次判定(design D1/D2)

- [x] 1.1 红前证据:在未改源上写 generation-aware 首时次测试(注入合格资格信号/包 manifest 含合格 cfg.ic),断言决策为 `PACKAGED_IC_BOOTSTRAP` / manifest `quality=packaged_calibrated_state`——必须先红(现值 `COLD_NEW_MODEL` / `cold_start_no_state`),红输出存档进 PR 证据。
- [x] 1.2 资格判定 IO 落 gate(D1):`scheduler_generation_gate.py` 经 registry `resource_profile.manifest_uri` 读包 manifest,产出信号 `QUALIFIED(含 ic sha256)/UNQUALIFIED/UNREADABLE/None`;判据用 `included_files[].sha256`(≠ 空文件摘要)+ `size_bytes>0`(字段名以 `basins_package.py` 实际 schema 为准);uri 存在但不可达/JSON 损坏 = `UNREADABLE`。选择层零对象 IO。
- [x] 1.3 纯 evaluator 扩参(D2):`evaluate_transition_decision` 新增资格信号参数(**可选,默认 `None`**——直方图零重基线依赖此默认),`exists_any_generation=False` 分支按判定表:`QUALIFIED` → 新枚举 `PACKAGED_IC_BOOTSTRAP`;`UNQUALIFIED`/`UNREADABLE` → 新 `BLOCK_FIRST_CYCLE_INITIAL_STATE_UNDECIDED`(typed reason 进 `TRANSITION_DECISION_REASONS`,只增不改);`None` → `COLD_NEW_MODEL` 原样(carve-out,含两个 gate bypass:`scheduler_generation_gate.py:322-328`/`:336-346`)。
- [x] 1.4 evidence 面(D5):candidate `state_evidence.mode` 新增 `db_free_packaged_ic_bootstrap`;block 行 typed reason 可见。
- [x] 1.5 `tests/test_scheduler_generation.py`:判定表全行(QUALIFIED / UNQUALIFIED block / UNREADABLE block / None carve-out 保持 COLD_NEW_MODEL / 两 bypass 各一条)+ **零改动**通过既有直方图基线(13 WARM_CONTINUE / 6 COLD_NEW_MODEL,`:846-868`)与 `WARM_CONTINUE`/`COLD_DECLARED_CUTOVER` 回归。

## 2. 运载:cold-seed 通道 + manifest 组装(design D3)

- [x] 2.1 `chain_forecast_cycle.py`:`_cold_seed_admitted` 集合扩入 `db_free_packaged_ic_bootstrap`(仅 strict 分支判定用);**basin 标记写入独立于 `strict_required`**——写点在 `:210` guard 之外、`:187-194` 非 strict 早退之前:凡 evidence mode 为 `db_free_packaged_ic_bootstrap` 一律写 `packaged_ic_selected=True` + `packaged_ic_checksum`(信号携带的 sha256)。
- [x] 2.2 `chain_manifests.py`:init_mode 推导改 `3 if init_state_id or init_state_uri or packaged_ic_selected else 1`;quality 由 `packaged_ic_selected` 最终裁决为 `packaged_calibrated_state`(last-writer,压过 `_apply_initial_state_selection_to_basin` 的覆写);manifest `initial_state` 块:`state_id=None`、`ic_file_uri=None`、`packaged_ic_checksum`。`init_state_id` 保持 None(cohort identity map 语义不变)。
- [x] 2.3 双模式端到端测试:`NHMS_REQUIRE_FORECAST_WARM_START=true` 与 `=false` 下 bootstrap 候选均产出 `init_mode=3` + `quality=packaged_calibrated_state`(strict 不硬错、非 strict 不降级 `cold_start_no_state`)。
- [x] 2.4 journal `hydro_run` 行新增 quality 字段(`file_orchestration_journal.py`);M24 receipt quality 枚举追加 `packaged_calibrated_state`(既有 4 值不动)。

## 3. runtime 消费收紧(design D4)

- [x] 3.1 `runtime.py` 两个 packaged 分支(`:841-862` 无 state_id、`:863-875` legacy 手工带 state_id)统一 consume-or-raise:门 = manifest 声明 `quality=packaged_calibrated_state`;必须找到唯一非空 `*.cfg.ic`;带 `packaged_ic_checksum` 时文件 sha256 必须相等,不带时仅验非空 + header 可解析并记 warning;header 校验复用既有工具;应用残差归一化;任何失败 → 新错误码 `PACKAGED_IC_CONSUMPTION_FAILED`,分支**绝不 fall-through** 到 `:899`/`:933` 静默冷启动。
- [x] 3.2 残差归一化 helper 抽取:`normalize_state_negative_residuals` 调用从 `_materialize_ic_to_project_name`(`:1015`)抽为可复用 helper,packaged 路径直接调;`_materialize_ic_to_project_name` 本体(含 `:1029` `_shift_cfg_ic_time`)对 warm 路径逐字节不变。不做时间一致性校验/时间平移(包 IC 无时刻,记测试注释锚)。
- [x] 3.3 `tests/test_shud_runtime.py`:两形态消费主线(cfg.para `INIT_MODE 3` + 残差 helper 被调 + legacy 形态 warning);**行为负锁**四连:缺文件/0 字节/checksum 不符/header 坏 → `PACKAGED_IC_CONSUMPTION_FAILED` 且断言无 `INIT_MODE 1` 执行(两形态都测缺文件);未声明 packaged-IC 路径回归(warm/cold 不变)。
- [x] 3.4 must-preserve 基线:`tests/test_warm_start.py:139-148`、`test_warm_start_chaining.py:885-929`(均为首时次无包 fixture,落 None carve-out)**期望值不变**通过;state-clone 6 refusal scope 与错误码零改动;`create_qhh_shud_manifest.py` 手工路径(legacy 无 checksum 形态)回归确认。

## 4. 存量审计工具(design D6)

- [x] 4.1 新 `scripts/audit_first_cycle_initial_state.py`:只读,registry manifest → 包 manifest IC 资格(D1 判据)→ 首个业务 run manifest(`{workspace}/runs/` + object-store `runs/` 最早 cycle 的 `initial_state.quality`/`runtime.init_mode`)→ schema 化 receipt,verdict ∈ `{consumed_package_ic, cold_start_with_qualified_ic, cold_start_no_ic, undetermined}`;receipt 带 limits 字段(manifest-only 摘要核对);no-follow + jsonschema receipt 模式。
- [x] 4.2 审计工具测试(新测试文件):合成 registry + run 证据 → 缺陷行 verdict 正确;undetermined(证据缺失)行为;零写断言(receipt root 之外无文件产生)。

## 5. 实机验收(merge 后,node-22;变更 2 前置)

- [x] 5.1 部署后在 node-22 只读运行审计工具,产出 receipt:6 个 070500 流域 × GFS/IFS = 12 行 `cold_start_with_qualified_ic`(同时正式确立"18 包全带非零 IC"的现场观察);receipt 回贴 #1164。
- [x] 5.2 确认 timer 保持停止(用户指令);本变更不触发任何生产 run——首时次新路径的实弹验证由变更 2(六流域回放)承担。

## Evidence Floor

- `uv run pytest -q tests/test_scheduler_generation.py tests/test_shud_runtime.py tests/test_warm_start.py tests/test_warm_start_chaining.py` + 审计工具测试文件 全绿
- 1.1 红前证据(红输出)在 PR 内
- 3.3 行为负锁证据:声明 packaged-IC 的 manifest(两形态)失败必 raise,无 `INIT_MODE 1` 执行
- 1.5 直方图基线与 3.4 首时次基线**零改动**通过(carve-out 完整性)
- `uv run ruff check .`
- `openspec validate first-cycle-package-ic-consumption --strict --no-interactive`
- 5.1 node-22 审计 receipt(12 行缺陷复现)回贴 #1164
