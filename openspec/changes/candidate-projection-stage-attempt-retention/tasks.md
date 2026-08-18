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
- E8 命令: `uv run pytest -q tests/test_production_scheduler.py -k "strict_warm_start or retry_attempt or truncat or retention or floor"`；
  `uv run pytest -q tests/test_production_scheduler.py tests/test_orchestration_chain.py
  tests/test_gateway_reconcile.py`；`uv run ruff check .`；
  `openspec validate candidate-projection-stage-attempt-retention --strict --no-interactive`。
- E9 receipt: PR body 引用 #1173 归档 receipt（archive/2026-07-27-…/tasks.md:39-40）；
  归档文件不编辑。

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

## Tasks

- [x] 1.1 (v1) `candidate_state_from_rows` 截断块 stage-aware 保留 —— **v2 中回退**
- [x] 1.2 v2 机制：选集回退纯新鲜度 + floors builder（scheduler_state_rows）+ state 新键 +
      `_state_job_retry_attempt` 并入 floor
- [x] 2.1 v2 测试：E1/E2'/E4'/E5/E10'/E11-v2/E12-v2/E12''/E-v2-S1..S4/E-v2-sel
- [x] 2.2 E6a/b/c + 不变量注释四处（含 :2807 按 D4 修正措辞）
- [x] 2.3 消费面核对 E7（PR body 落结论）
- [x] 2.4 runbook 机制描述更新（failed-basin-retry.md）
- [ ] 3.1 E8 全绿；偏离记录 + E9 receipt + follow-up issue 编号（geometry-B manual mint）
      写入 PR body；DB 缺口 issue：**#1572**
