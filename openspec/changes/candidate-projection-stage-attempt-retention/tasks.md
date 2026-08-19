# Tasks: candidate-projection-stage-attempt-retention

Fixture level: expanded
Upstream suggested level: absent (issue 早于 0.16.0 契约；`retry`/persisted-state/`scheduler` 强制触发词定 expanded)

> v2（round-1 cross-review 后机制修订）：行保留 → attempt-floor 载带；选集逐字节回到现状。
> 初版 E 腿中依赖行可见性的（旧 E3/E11/E12a 行为变化）已按 v2 重定义；round-1 verified
> findings S1-S4 / C1-C4 全部映射到 v2 腿或文档修正。

## Risk packs considered (core)

- Public API / CLI / script entry: not selected — 私有投影函数签名不变（state 增新键，Mapping 惰性）。
- Config / project setup: not selected — `job_limit` 默认与语义不变。
- File IO / path safety / overwrite: not selected — 无文件面（纯内存投影）。
- Schema / columns / units / field names: **selected** — state 新键 `stage_retry_attempt_floors`
  的形状与惰性（D1.3）；证据 E-v2-sel。
- Auth / permissions / secrets: not selected — 无认证面。
- Concurrency / shared state / ordering: **selected** — 选集逐字节不变是承重墙（D2）；证据
  E2'/E-v2-sel。
- Resource limits / large input / discovery: **selected** — floors 不加行不删行、hard cap 归
  于现状截断；证据 E4'。
- Legacy compatibility / examples: **selected** — 三个证伪几何回归钉（S1/S2/S3/S4）+ :42219
  不放宽 + stage-less flat-first 不变（E12''）。
- Error handling / rollback / partial outputs: **selected** — 预算 blocked（E5）+ geometry-B
  边界现状钉（E11-v2/E12-v2）+ released 行钉（E6a/b/c）。
- Release / packaging / dependency compatibility: not selected — 无依赖变化；3.11 兼容自查（D6）。
- Documentation / migration notes: **selected** — runbook 机制描述更新 + D0 共享函数事实澄清
  + :2807 注释归属修正（C4）。

Domain packs (NHMS profile): Slurm production lifecycle **selected**（L2 预算绑定影响生产自旋
收敛；oracle 仍为本地 pytest）。其余 not selected——无地理/时序/数值/DB 面（DB 读路径
guarantee 排除，D0；共享函数数值面已澄清）。

## Required evidence (v2)

- E1 逆序几何主缺陷: `*_forecast_retry_N` 旧端 + `>= job_limit` 条其它 stage 更新行 →
  `_state_retry_attempt(state, stage="forecast") == N`（floors 载带；红证据 = 去掉 floors
  并入）。
- E2' 选集逐字节不变: 逆序几何下 `pipeline_jobs` == 纯新鲜度 top-`job_limit` 的**显式 id
  列表**（写死期望）；既有 :42219 原样通过。
- E4' floors 不动行群体: 输入 > `job_limit` 时投影恰 `job_limit` 行（最新序）；floors 非空
  不改变行数与成员。
- E5 预算绑定: 逆序 + `N >= retry_limit` → `("blocked", "strict_warm_start_retry_budget_exhausted")`。
- E6 released 行钉: (a) 真实 reserve→release 行 `status=="reservation_lost"` 且 `error_code`
  空; (b) `should_auto_retry` 为假; **(c) reserve→permit(absence_retry_permitted)→reclaim→
  release 全链后同断言**（:1911 塞瞬时 code 变异必须咬红）。不变量注释四处 + :2807 措辞按
  D4 修正。
- E7 消费面核对: D3 v2 清单逐一核对结论入 PR body。
- E10' floor 推导链同构: 窗外最大 attempt `copyback` 行入 floor；`download` 行不入；
  **attempt 只在持久化 `retry_count`（无 `_retry_` 后缀，如 `_retry_active`）的行入 floor**；
  **stage 空只有 `job_type` 的行入 floor**——C1 两个变异（丢 job_type 回退 / 纯后缀解析）
  必须各自咬红。
- E11-v2 geometry-B 边界钉: succeeded publish filler + 窗外 failed retry 行 →
  **硬断言** `_failed_stage(state) is None`（选集不变，行不可见）；同 state 上
  `_state_retry_attempt(state, stage="forecast") == N`（floors 与行可见性解耦的直接证明）。
- E12-v2 manual mint 现状边界钉: geometry B + adopted marker 无显式 attempt →
  `new_attempt == 1`（现状撞键 no-op 维持，follow-up issue 编号入注释与 PR body）。
- E12'' stage-less flat-first 不变: floors 非空的投影 state 上 `_state_retry_attempt(state)`
  （无 stage）仍 flat-first 返回顶层 `retry_count`（floors 渗入 stage-less 的变异必须红）。
- E-v2-S1 再入几何回归钉: candidate-scoped retry 行 + 更新 run 成功行全窗外 + cycle-scope
  filler → `pipeline_status`/`failed_stage` 与改前全同（None/None），且 stage-scoped attempt
  读出真值。
- E-v2-S2 completed-stage 几何回归钉: 窗外 wedge + 窗内 convert succeeded + forcing failed →
  `restart_stage == 'forcing'`、completed-stage 证据保持。
- E-v2-S3 ACTIVE 几何回归钉: 窗外 running + slurm_job_id 行 → `_state_active_jobs` 为空。
- E-v2-S4 flat 载体几何回归钉: retry_count=4 载体行在窗内 → `state["retry_count"] == 4`。
- E-v2-sel 新键形状: 投影 state 恒含 `stage_retry_attempt_floors` dict（可空）；非投影
  state（无该键）读取行为不变。
- E13 identity 收窄（round-2 R2-A）: (a) **真实 journal 端到端腿**——model-less 带后缀
  cohort 行（retry_count=N）+ 候选自己首次失败行，无截断：filtered decision 为
  ('retry','retry_failed_candidate')、payload attempt==0（floors 被收窄；红证据 = 当前
  head 读出 N/permanent）；(b) shared-cycle-aggregate 臂（identity_filter:284-343）同断言——
  该臂上 floors 由 **strip 半边**（:328 无条件 strip + strip 列表含 floors 键）兜住，不是收窄
  调用（design D1.6 口径；测试名与 docstring 已随 round-3 改为 "erased on the ... arm"）；
  (c) E1/E5 wedge 行（裸 cycle run id）floor 存活——收窄不误伤；(d) strip 列表含 floors 键。
- E14 payload 决策面钉（round-2 R2-B）: 逆序几何 + 候选自己窗内 transient 失败行 →
  `_failure_policy_payload` {attempt: N, retryable: False, permanent: True} +
  `_permanent_failure_evidence` 非 None（去 floor 并入变异必须红）。
- E15 nameable mint 钉（round-2 R2-D）: 窗内候选权威 failed `_retry_2` 行 + 窗外
  `_retry_87` → mint `previous_attempt==87` / `new_attempt==88`（改前铸 `_retry_3`；去
  floor 并入变异必须红）。
- E16 预算读点收窄（round-3 R3-A，修法 1a）: cohort 行（`cycle_..._forecast_cohort_<digest>`
  形状）出窗 + `retry_count >= retry_limit` → strict-warm-start 决策为
  `('retry','strict_warm_start_terminal_init_state_mismatch')` 非 blocked（红证据 = 当前
  head 读出 blocked）；E5 wedge 几何（裸 cycle run id）必须继续 blocked——**修法 1b
  （读点前跑完整 `_candidate_state_decision_state`）明令禁止**（aggregate 臂 strip 会打死
  E5，verifier (e) 实测）。
- E13e blocker 子分支腿（round-3 R3-B）: `top_level_source_cycle_blocker` 为真使 :312-313
  strip 被跳过的几何 → 非 candidate-scoped 贡献行的 floor 被 :316 收窄删除（删 :316-320
  变异必须红）。**实测偏离**：`_top_level_source_cycle_download_blocker` 比对的是 state 顶层
  `run_id`，任何 producer 写的都是候选自己的 run_id ⇒ **当前无投影可产出带 floors 进入该
  分支的 state**（原 verifier "生产可达" 只成立到"分支进入"，不到"floors 活着到 :316"）。
  腿改为 guard 钉：真实投影 + 只替换该字段，docstring 显式声明这一点；收窄保留而非删除
  （谓词是 master 既有活代码，删了就是不变量缺口）。
- E13f inconclusive 臂腿（round-3 R3-B）: `_inconclusive_source_cycle_decision_state` 路径
  上 state 真带 floors、贡献行引用 unresolved job id → floor 被收窄（删 :143 收窄调用变异
  必须红）。几何全部由真实投影产出（两条未修复 download 失败 + 更新的成功 download +
  截断 ⇒ `inconclusive_truncated`）；**唯一显式写入的是贡献行的 `previous_job_id` 指向
  unresolved download job**——该字段是真实 pipeline-job 字段（`retry.py` 每次铸 retry 都
  写），但无生产写点把 forecast 行链到 source-cycle download 行，故此腿钉的是该臂契约而非
  实测几何；对照半边（同几何去掉该链接）floor 保留，证明断言只咬这条引用。
- E17 并列贡献行腿（round-3 R3-C）: 同 stage 两条并列 max 贡献行都出窗——foreign 行
  **必须比候选权威行更新鲜**（floors 建在倒序列表上，"只记第一条"记最新，否则腿是假的）
  → 过滤后 floor 仍为 N（"只记第一条、平手不入"变异必须红）。
- E18 消费者矩阵（retro 纠正动作核心）: grep 穷尽 `_state_retry_attempt(` 与
  `STAGE_RETRY_ATTEMPT_FLOORS_KEY` 全部读点，逐点标注"经过收窄 / 不经过 / 为什么安全"，
  矩阵落 design D3——下一轮 review 校验矩阵而非重新考古。
- E8 命令: `uv run pytest -q tests/test_production_scheduler.py -k "strict_warm_start or retry_attempt or truncat or retention or floor or mint"`
  （`or mint` 是 round-4 补的——E15 此前不被该定向命令选中）；
  `uv run pytest -q tests/test_production_scheduler.py tests/test_orchestration_chain.py
  tests/test_gateway_reconcile.py`；`uv run ruff check .`；
  `openspec validate candidate-projection-stage-attempt-retention --strict --no-interactive`。
- E9 receipt: PR body 引用 #1173 归档 receipt（archive/2026-07-27-…/tasks.md:39-40；归档
  文件不编辑）+ **issue AC-6 行数**（node-22 只读实测，2026-08-18）：cycle 2026072000
  pipeline-jobs 行数 gfs=94（其中 87 条 `_forecast_retry_*`）/ ifs=124——ifs 超过默认
  job_limit=100，逆序截断几何在生产真实存在。

## Review focus (v2)

1. 选集逐字节不变是承重墙——四个证伪几何回归钉（E-v2-S1..S4）+ E2' 显式 id 列表；任何
   选集差异都是 P1。
2. floors 推导链同构（builder 与消费者同模块同函数）——E10' 的 C1 两变异是守门腿。
3. stage-less flat-first 语义不得被 floors 污染（E12''）。
4. geometry-B 边界是**显式接受的现状**（E11-v2/E12-v2 + follow-up issue），不是回归——
   review 不得再以行不可见为由要求恢复行保留。
5. E6c 必须驱动真实 reclaim 链（:1835-1839 前置条件形状），:1911 变异必须咬红。
6. D0 v2 澄清——DB 路径 floors 数值面顺向变化已落字；选集在两路径都不变。
7. Python 3.11 兼容（#1566 教训）。
8. （round-2 增补）D1.6 identity 收窄——E13 四子腿是守门；修法必须是收窄式（贡献行元数据
   + 同谓词判定），**禁止**从过滤后行集重算 floors（会打死 E1/E5，verifier (e) 裁定）。
9. （round-2 增补）D3 两个有意决策级变化（E14 payload / E15 nameable mint）必须有腿且
   design 声明到位；flat 分量边界（D1.7）措辞收窄落 spec/design/docstring/runbook 四处。

## Tasks

- [x] 1.1 (v1) `candidate_state_from_rows` 截断块 stage-aware 保留 —— **v2 中回退**
- [x] 1.2 v2 机制：选集回退纯新鲜度 + floors builder（scheduler_state_rows）+ state 新键 +
      `_state_job_retry_attempt` 并入 floor
- [x] 2.1 v2 测试：E1/E2'/E4'/E5/E10'/E11-v2/E12-v2/E12''/E-v2-S1..S4/E-v2-sel
- [x] 2.2 E6a/b/c + 不变量注释四处（含 :2807 按 D4 修正措辞）
- [x] 2.3 消费面核对 E7（PR body 落结论）
- [x] 2.4 runbook 机制描述更新（failed-basin-retry.md）
- [x] 1.3 (round-2) D1.6 identity 收窄实现（贡献行元数据 + 三处 filter 收窄 + strip 键）
- [x] 2.5 (round-2) E13/E14/E15 腿 + D1.7 措辞收窄（rows.py 两处 docstring + runbook 句）
      + rows.py:36-39 "always present" 注释限定
- [x] 1.4 (round-3/retro) 修法 1a：scheduler_candidates.py:2225 读点前收窄 + 删 :333 死代码
- [x] 2.6 (round-3/retro) E16/E13e/E13f/E17 腿 + E14/E15 改钉 decision_state + E18 消费者
      矩阵落 design D3 + tasks E13(b)/docstring 口径对齐 + download 逃生门注释 +
      rows.py:46-51 注释收窄
- [ ] 3.1 E8 全绿；偏离记录 + E9 receipt（含 AC-6 行数）+ #1572/#1577 + 串味 follow-up
      编号（flat 通道 #1579 / 行扫描通道 #1586）写入 PR body
