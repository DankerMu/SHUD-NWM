# Tasks — state-attempt-scope-discipline

## 1. Implementation

- [x] 1.1 `_job_is_cycle_scope_row` 下沉 `scheduler_state_rows.py`（语义逐字节不变），
      `scheduler_state_manual_retry.py` re-export 兼容
- [x] 1.2 (#1298) `_state_retry_attempt` 非 canonical 第三臂：原始 stage 名相等匹配 +
      cycle-scope 行排除 + `effective_retry_attempt` 同口径；`stage=None` 臂与
      canonical 臂逐字节不变；docstring 记录"非 canonical 臂无 floors 载带（窗口敏感）"
      边界
- [x] 1.3 (#1299) `_job_is_live_candidate_scope_failure` 行身份前置判据（无
      `job_id`/`pipeline_job_id` 的合成行 → False）；
      `_state_has_candidate_scope_failed_job` docstring 挂账段改写为修后事实，
      **含 delta 的 id-bearing 例外句**（顶层平铺带 job id 的形状仍按行读作活失败——
      #1299 AC-5 要求 docstring 与 spec 口径一致）
- [x] 1.4 (#1300) `_candidate_failed_stage` 新增（显式键同 `_failed_stage`；行扫描跳过
      repaired-evidence + cycle-scope 行）；**四个**消费点切换（policy 分类 stage 轴、
      cancelled 分支 `retry_policy.attempt`、manual fallback `previous_attempt`、
      `_fallback_previous_attempt` 内部 family-floor gate——对 #1300 声明边界的显式
      偏离，理由见 design D3，记入 PR 偏离记录）；`_failed_stage` 本体与其余调用点不变
- [x] 1.5 spec 三处挂账措辞随修复改写（经本 change delta；archive 时并入主 spec）

## 2. Evidence Floor（tests/test_production_scheduler.py，全部红先行或变异咬红）

- [x] E1 (#1298 主判别，红先行): 唯一活失败 = model-scoped `cancelled` 的
      `download_retry_4` 行、顶层 `retry_count=0`、无 `failed_stage` →
      `_manual_retry_new_attempt` 1→**5**；并按 attempt 来源参数化——id 后缀行之外
      再加一条无可解析后缀、`retry_count=4` 的 `<run_id>_retry_active` 生产形状行
      （手动重试首次铸造的 id，attempt 只存在于 recorded 计数上），两格同答 5
- [x] E2 (#1298 对照): 同形 canonical `forecast_retry_4` 仍 5；round-4 判别形
      （stage-blind 8 / 无 floor 1 / 正确 3）不回归（既有腿复跑确认未改）
- [x] E3 (#1298): `stage=None` 臂逐字节不变——evidence-owner / manual-retry 无 stage
      消费者既有腿全绿 + 非 canonical 行存在时 stage-less 读数不变的判别断言；同一
      flat-less 状态上并列钉 `stage="download"` 读 4（扁平键被剥离的子域：新臂窄扫描
      相对 master 的 stage-blind 跨行 max 是收窄，两臂在同一状态上分离）
- [x] E4 (#1298 scope 纪律，变异咬红): model-less `cycle_` run-id 的 cohort download 行
      `retry_count=7` 与候选 model-scoped `download_retry_4` 并存 → 新臂读 4 不读 7；
      变异（删 cycle-scope 排除）该腿红；并在状态副本上钉住 flat 合成项
      （`retry_count=6` → 读 6，变异"去 `max(flat or 0, …)`"咬红）
- [x] E5 (#1298 边界钉): 非 canonical 最大 attempt 行截出 `job_limit` 窗外 → 新臂读窗内
      值（floors 不覆盖非 canonical——现状钉，docstring 边界的活体证据）
- [x] E6 (#1299 主表格，红先行): 顶层 `pipeline_status` ∈ {cancelled, failed,
      permanently_failed} × `pipeline_jobs` ∈ {缺失, []} + marker `retry_count=5` →
      `_manual_retry_new_attempt` 全部 1→**5**；{succeeded, 缺失} 两行保持 5
- [x] E7 (#1299 回归): 携 `job_id` 的真实 `cancelled` 行仍判活失败（arm-2 拒钉不变，
      #1287 语义）
- [x] E8 (#1299 形状锚): single-mapping（内嵌 id）与顶层平铺**带 id** 形状的 arm-2 取值
      改前改后一致（fail-open 依赖钉）；无 id 合成行不再向 `_restarted_stage_family`
      贡献 stage（侧翼钉）
- [x] E9 (#1300 主判别，红先行): cohort `convert` 行 `retry_count=7` + 其上 marker
      `retry_count=8` + 候选 `cancelled` `..._forecast_retry_2` 行 + 无顶层
      `failed_stage` → `_candidate_state_decision(...).evidence["retry_policy"]["attempt"]
      == 3`（当前 8）；对 forcing / parse / state_save_qc / publish 参数化全 3
- [x] E10 (#1300 auto-retry 腿): 同形去 marker、`retry_limit=3` →
      `_failure_policy_payload(state)["limit_exhausted"] is False`、决策非
      `retry_limit_exhausted`
- [x] E11 (#1300 回归四联): download 列仍 3（#1287 AC）；候选有存活行时顶层 `stage`
      路径不变（`previous_attempt == 2`）；`_failed_stage` 其余消费点（重启路由至少
      一腿）行为不变；**gate 打开面回归钉**——`_candidate_failed_stage` 为 None 而
      `_failed_stage` 为 canonical 的几何下 family floor 打开且只抬值（方向安全钉）
- [x] E12 (#1300 可达形状): 顶层 stage 置 None + 行保留的形状并入 E9 参数化；可达枚举
      结论写入测试模块注释，**并点名 E9 头部形状的真实可达通道只有 identity filter 的
      top-level 剥离**（投影会把候选存活行 stage 写进顶层；repaired-stage-evidence 分支
      要求候选无行，其 nulling 块又恒写非空 `restart_stage`——故 `explicit_none` 轴是
      合成形状，钉"键缺失 ≡ 键为 None"的等价性）
- [x] E13 (#1298/#1300 交互): `_candidate_failed_stage` 解析出候选自身 model-scoped
      download 失败 → 分类/攻击面读到 download 真值（新臂）且不吃 cohort（E4 同几何
      对照）；按 `retry_limit` 参数化钉住新 attempt 的分类后果——9 仍 retryable、3 判
      `limit_exhausted`/`permanent`（`attempt == 4` 两格都保住），并加一格去 marker 的
      决策级断言 `action == "blocked"` / `reason == "retry_limit_exhausted"`
      （master 同形为 retry）
- [x] E14 (#1298 AC-4 活体证据): `scheduler_candidates` strict-warm-start 预算读点
      （常量 `"forecast"`，canonical）在非 canonical 行存在的几何下读数不变——新臂
      不可达该读点的回归钉

## 3. Verification & Delivery

- [ ] 3.1 命令全绿:
      `uv run pytest -q tests/test_production_scheduler.py -k "fallback_floor or non_canonical or restarted_stage_family or state_retry_attempt or cohort_stage or cycle_scope or live_failure or synthesized"`
      · 四套件全量 · `uv run ruff check .` ·
      `openspec validate state-attempt-scope-discipline --strict --no-interactive`
- [ ] 3.2 偏离记录 + spec 挂账销账确认（三处 "tracked as #129x" 全部消失）写入 PR body；
      PR `Closes #1298, closes #1299, closes #1300`
