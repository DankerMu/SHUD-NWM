# Tasks: candidate-projection-stage-attempt-retention

Fixture level: expanded
Upstream suggested level: absent (issue 早于 0.16.0 契约；`retry`/persisted-state/`scheduler` 强制触发词定 expanded)

## Risk packs considered (core)

- Public API / CLI / script entry: not selected — 私有投影函数签名不变。
- Config / project setup: not selected — `job_limit` 默认与语义不变。
- File IO / path safety / overwrite: not selected — 无文件面（纯内存投影）。
- Schema / columns / units / field names: not selected — 投影键集不变（`state_truncated`/`pipeline_jobs_total` 语义 D2 钉住）。
- Auth / permissions / secrets: not selected — 无认证面。
- Concurrency / shared state / ordering: **selected** — 截断选集与回排顺序契约、过滤式 fill 的窗口边界稳定性（D1.2）、保留并列规则（D1.1）；证据 E1/E2/E3。
- Resource limits / large input / discovery: **selected** — hard cap 不得突破（D1.4）+ 可见性两半面的诚实分析（D2）；证据 E4/E11。
- Legacy compatibility / examples: **selected** — 友好序逐元素一致 + :42219 不放宽（证据 E2）；DB 路径显式排除为 D0 裁决，无腿（by design）。
- Error handling / rollback / partial outputs: **selected** — 预算 blocked（E5）+ mint 行为变化两腿（E12）+ released 行钉住（E6）。
- Release / packaging / dependency compatibility: not selected — 无依赖变化；3.11 兼容自查（D6）。
- Documentation / migration notes: **selected** — `docs/runbooks/failed-basin-retry.md` 补一行（预算在逆序几何下现在真实绑定）。

Domain packs (NHMS profile): Slurm production lifecycle **selected**（L2 预算绑定 + E12a 恢复
一次真实提交路径影响生产自旋收敛；oracle 仍为本地 pytest，不改 sbatch/调度行为本身）。
其余 not selected——无地理/时序/数值/DB 面（DB 读路径显式排除，D0）。

## Required evidence

- E1 逆序几何主缺陷: `*_forecast_retry_N` 旧端 + `>= job_limit` 条更新其它 stage 行 →
  `_state_retry_attempt(state, stage="forecast") == N`（当前实测 0；issue 探针同形，
  `job_limit=5`, `N=87`）。
- E2 友好序不回归: :42219 原样通过；另加显式期望腿——保留行在窗内时最终选集 == 新鲜度序
  top-`job_limit` 的**显式 id 列表**（不与"pre-change 代码"比，与写死的期望列表比）。
- E3 保留确定性: 同 stage 同 attempt 并列 → truth timestamp 最新者保留；fill 为过滤式
  （D1.2），窗口边界完全并列时行为与现状一致。
- E4 退化边界: 保留集 > `job_limit` → 总数仍 == `job_limit`（truth timestamp 最新胜）；
  attempt==0 stage 不占名额。
- E5 预算绑定: 逆序 + `N >= retry_limit` → `("blocked", "strict_warm_start_retry_budget_exhausted")`。
- E6 released 行钉住: (a) **真实 reserve→release 序列**产出行 `status=="reservation_lost"`
  且 `error_code` 为空；(b) 该行 `should_auto_retry` 为假；不变量注释落 :1778/:1903/:2804/:2954
  四处。
- E7 消费面核对: `scheduler_state_failure.py` 四处 + `manual_retry:982` 逐一核对结论入
  PR body（:1917 除外——它有专属 E12 腿）。
- E10 alias 表钉: 窗外最大 attempt `copyback` 行被保留（canonical）；窗外 `download` 行
  不占保留名额（非 canonical）——防 `_STAGE_ALIASES` 误用（D1.1）。
- E11 挤出面交互: 用 **geometry B**（D3 配方：succeeded publish filler → 三键全空）使
  `_failed_stage` 行扫描可达，**硬断言前提** `assert _failed_stage(today_state) is None`
  （非注释——geometry A 下会 vacuous 绿），钉逆序几何下解析结果 `None → 'forecast'`。
- E12 manual-retry mint 行为变化（D3 修订引用链）: (a) 逆序 + adopted marker 不带显式
  attempt + `N < retry_limit` → `manual_retry.new_attempt == N+1`、mint `_retry_{N+1}`
  （今天静默 no-op 撞键）；(b) 逆序 + `N >= retry_limit` → manual 路径照常
  `allowed: True` / `new_attempt == N+1`——预算不门 manual retry（`manual=True` 解除
  permanent），钉既有语义在真值下的形状。
- E8 命令: `uv run pytest -q tests/test_production_scheduler.py -k "strict_warm_start or retry_attempt or truncat"`；
  `uv run pytest -q tests/test_production_scheduler.py tests/test_orchestration_chain.py
  tests/test_gateway_reconcile.py`；`uv run ruff check .`；
  `openspec validate candidate-projection-stage-attempt-retention --strict --no-interactive`。
- E9 receipt: PR body 引用 #1173 归档 receipt（archive/2026-07-27-…/tasks.md:39-40，
  已佐证 D4）；fresh node-22 只读计数可选，不可达不阻塞；归档文件不编辑。

## Review focus

1. D1.1 推导链同构 + **禁用本文件 `_STAGE_ALIASES`**（含 download 漏 copyback）——E10 是守门腿。
2. D1.2 过滤式 fill——窗口边界完全并列的稳定排序行为不得漂移。
3. D2 可见性变化两个半面——加入面在生产行形下被全部扫描者过滤（无腿，诚实收窄）；挤出面（条件可达，E11
   须自建可达性前提）；`latest_job` 已证安全无需腿。
4. D3/E12 是 **manual-retry 路径**的有意行为变化——`manual=True` 设计上解除 limit 门
   （retry.py:199），预算不门 manual retry；两腿钉住（可达性前提与 E11 共享几何），
   不得只写 prose，引用链按 D3 修订版。
5. E6a 必须驱动真实 reserve→release 序列——手搓行钉不住真实回归向量（:1778/:1903 的未来编辑）。
6. D0 范围裁决——spec 措辞显式排除 DB 路径；DB 缺口 issue 已路由（PR body 记编号）。
7. Python 3.11 兼容（#1566 教训）。

## Tasks

- [ ] 1.1 `candidate_state_from_rows` 截断块 stage-aware 保留（D1 五条规则，过滤式 fill）
- [ ] 2.1 回归测试 E1-E5、E10、E11（tests/test_production_scheduler.py；E11/E12 共享几何）
- [ ] 2.2 E12 两腿 + E6 真实序列钉 + 四处不变量注释
- [ ] 2.3 消费面核对 E7（PR body 落结论）
- [ ] 2.4 runbook 一行（failed-basin-retry.md）
- [ ] 3.1 E8 全绿；偏离记录 + E9 receipt 引用写入 PR body；DB 缺口 issue：**#1572**（已路由，
      depends on 本 change 的不变量定稿）
