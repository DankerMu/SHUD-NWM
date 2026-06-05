# Issue #290 (M24-3A) 动态动作流 + 进度工作日志

> 定制自 `.claude/skills/dual-end-issue-workflow`，按 #290 实际状态裁剪。
> 单一事实来源：本文件记录动作流定义 + 实时进度。每完成一步立即更新。

## 1. 目标与边界

- **Issue**: #290 M24-3A 并发 submit-and-return + durable 两阶段预留
- **分支**: `feat/issue-290-concurrent-reservation`
- **In scope**: 锁内写持久预留行（`pipeline_job` + `idempotency_key`）→ sbatch 前 reserve、提交后原子 bind `slurm_job_id`、跨重叠 pass / 提交崩溃窗口的双提交防护、reconcile-by-comment 崩溃恢复、grace-gate 防 slurmdbd 滞后误降级。
- **Out of scope**: 多 basin 实况（#291/§3B）、连续守护进程（#292/§4）、诊断退役（#293/§5）。

## 2. 角色分工（双端）

| 角色 | 职责 |
|------|------|
| **Claude Code**（本地编排） | 状态评估、修复计划、候选去重 + Phase 4.5 裁决、证据综合、git/PR、合并门、文档维护 |
| **fix subagent**（派发，leaf） | 不变式级代码/测试修复；可编辑、跑 ruff，不 commit |
| **review subagent**（派发，leaf，read-only） | 单 reviewer-pack lens，仅产候选 |
| **远端 node-22** | 真实 PostgreSQL/TimescaleDB/Slurm 测试 oracle |
| **GitHub CI** | 最终合并门（用户统一处理；本流程不等 CI 迭代） |

**执行约定（用户标准授权）**：「按顺序完成所有 open issues，审核通过后无需等待 CI 和人工 gate 直接 merge 然后继续，CI 我会在完成后统一处理」。即 **终评 CLEAN 后预授权直接合并**（绕过 CI/人工门），CI 由用户事后统一处理。

## 3. 状态机入口（动态裁剪，进入点 = Phase 6 修复）

| 阶段 | 状态 | 说明 |
|------|------|------|
| Phase 0/0.5 OpenSpec fixture + 风险分级 | ✅ | DB-backed state/schema/shared-root → 6-pack |
| Phase 1 实现（两阶段预留 + reconcile） | ✅ | 进入会话前已落地 `93b5ddc`..`4127611` |
| Phase 6 修复（多轮） | ✅ | 见 §4 候选/裁决 + §7 日志 |
| Phase 4/4.5 cross-review 多轮 | ✅ | round-1..4，逐轮收敛 |
| Phase 7 独立终检 | ✅ | 干净树 import sanity + 双端测试 |
| Phase 8 证据 + 中文总结 + 合并 | ⏳ | Evidence Floor 验证 + 合并 |

## 4. 候选/裁决账本（Phase 4.5，按轮）

### Round-1（grace 类）
- **C2(a)** confirmed-absent reconcile 在 slurmdbd 滞后时会把 in-flight 预留误降级 → 双提交。**CONFIRMED** → 修：grace-gate defer（`c4f58b0`）。

### Round-2（综合复审，grace 锚精修）
- **created_at-vs-updated_at 锚点**：grace 锚 `created_at`，而 reclaim 只刷 `updated_at` 不动 `created_at` → reclaim→重提→再崩窗口 grace 失效 → 双提交复发。**CONFIRMED** → 修：锚点改 `updated_at`（reserve/reclaim/bind 三路径刷新；absence_unconfirmed defer 不刷）（`77d8039`）。

### Round-3（综合复审，新类 ×2）
- **FINDING-1** 缓存 reconcile 会话 commit 无 rollback → 一次 commit 失败后会话中毒、被 best-effort 吞掉 → 整个 daemon crash-recovery 静默失效。**CONFIRMED / in-scope / merge-blocking** → 修：`_reset_reconcile_store_after_error`（两个 except 内 rollback；rollback 自身失败才丢弃重建）（`8410ef2`）。
- **FINDING-2** PR 给 array 提交盖 idempotency `--comment` 以支持崩溃恢复，但读回 querier 过滤掉所有含 `_` 的 JobID（即所有 `<master>_<task>` array 行）→ array 永不 reconcile → reservation_lost → 重提整个 array（双提交）。**CONFIRMED / in-scope** → 修：抽 `_parse_comment_sacct_rows`，保留 `.` 过滤（排除 `.batch/.extern`），array 行归一化 `split("_",1)[0]` 到裸 master id（过 `SLURM_JOB_ID_RE`）（`8410ef2`）。
- **B-LOW** created_at 兜底 young 分支无测试。**CONFIRMED LOW** → 补测试（`8410ef2`）。
- C2(a) grace 类经 Reviewer A 复核 = **清白**（无活性倒退、双提交全路径关闭）→ 非同类复发 → 不触发 Review Failure Retro。

### Round-5（node-22 验证抓出，非静态复审）
- **VERIFY-1** Evidence Floor 全套件在 node-22 跑时**挂死**：诊断发现 pytest worker 卡在 TCP **SYN-SENT 连 `169.254.1.1:5432`**(link-local，node-22 静默丢包)。根因：#290 把 `_run_restart_reconcile` 接到 pass 开头(scheduler.py:895)，经 `create_engine(database_url)` **无 connect_timeout** 连库，且在提交路径 DB-host preflight(scheduler.py:1023)**之前** → 误配/不可达 `database_url` 让 pass 开头无限挂死；正常网络下连接快速 refuse 被 best-effort 吞掉故 CI 不暴露，node-22 丢 link-local 包则永挂。触发挂死的是 master 既有测试 `test_slurm_preflight_blocks_..._before_submission`(parametrize 169.254.1.1)，但**回归是 #290 引入**(reconcile 连库早于 preflight 且无超时)。**CONFIRMED / in-scope / merge-blocking**(对 #292 连续 daemon：DB 瞬断会让 pass 开头无限挂死而非快速失败)。
  - 修：reconcile engine 加 `connect_args={"connect_timeout": RECONCILE_DB_CONNECT_TIMEOUT_SECONDS=5}`(对齐 readonly_db_validation 5s 约定)→ 不可达 DB 快速失败、被 best-effort except 吞掉、pass 推进到 preflight 干净阻断(`ee5253c`)。白盒回归测试锁定有界 connect_timeout。
  - **意义**：这是双端 workflow 跑**真实环境 oracle** 的价值——4 轮静态复审全部漏掉，node-22 验证抓出。
- **VERIFY-1 聚焦复审**(1 reviewer 对抗式)：connect_timeout 修复 sound，零 in-scope blocking(psycopg2 透传生效、lazy connect 命中查询连接点、best-effort except 兜住、白盒测试反事实有效、未动摇 round-4)。残留 **Q3 LOW**(reconcile engine 未设 statement_timeout，连上后长查询仍可能挂；`chain.py:5737` 同样无超时)→ follow-up **#300**，非阻断。

### Round-6（node-22 验证 2-failed 抓出 + 同类第 2 轮 → 不变式闭合）
- **VERIFY-2** Evidence Floor 在 node-22 跑出 `2 failed, 574 passed`：`test_slurm_preflight_blocks_..._before_submission` 的 `bad::host` / `[::1` 两个畸形 host 参数 fail。根因：`_restart_reconcile_store` 在 try/except **之外**构建 engine（`store = self._restart_reconcile_store()` 在 scheduler.py:1242，try 始于 1252），畸形 `database_url` 让 SQLAlchemy `make_url` 在 `create_engine` 处**同步抛解析异常** → 传播出 `_run_restart_reconcile` → 在提交路径 DB-host preflight **之前**打断 `run_once` → pass 中止而非干净 `preflight_blocked`。**CONFIRMED / in-scope / merge-blocking**。
  - **Review Failure Retro（pattern escalation）**：这是 reconcile-robustness **同类第 2 个 finding（共享面 `_run_restart_reconcile`）**——Round-5 修了「连接挂死」子例（connect_timeout），本轮又冒「建引擎抛异常」子例。逐例补丁会无限打地鼠（下一子例：连上后慢查询挂死）。故升级为**不变式闭合**：*reconcile 对任何 `database_url` 问题（不可达 / 畸形 / 建引擎失败 / 连上后慢查询）一律 best-effort，绝不传播、绝不挂死 pass*。一次性关闭整类——
    1. **建引擎失败/畸形**：`_restart_reconcile_store` 把 engine+Session+PipelineStore 构建包进 try/except → 任何异常返回 None（best-effort skip，preflight 照常）。零泄漏构造：只存 `type(error).__name__`（class 名，证明无密码），**绝不**存原始异常串（畸形-URL 异常消息内嵌完整 DSN 含口令）。
    2. **连上后慢查询**：connect_args 加 `options=-c statement_timeout=10000`（对齐 readonly_db_validation 10s 约定）→ 封掉「连上但长查询挂死」子例（此前列为 Q3 LOW→#300，本轮随类一并闭合，从 #300 移除）。
    3. `_run_restart_reconcile` 的 `store is None` 分支区分 build-failure（`reason=reconcile_store_build_failed` + `error_type`）与 absent-url。
  - 修：`c2ed6ba`。回归测试覆盖 `bad::host` / `[::1`：store 返 None 不抛、reconcile skip 不传播、`"secret" not in json.dumps(result)`。本地 `tests/test_gateway_reconcile.py` 40 passed、ruff clean。
  - **意义（再次印证）**：静态复审 4 轮 + Round-5 仍漏掉建引擎同步抛异常子例；node-22 真实 oracle 再次抓出。connect_timeout（运行期连接）与 make_url（解析期）是两条不同失败路径，唯有「整类 best-effort」不变式能覆盖。

### Round-4（综合复审，CLEAN）
- 所有攻击向量 REFUTED；两处新 fix 站住。
- **LOW-1**（out-of-scope）drop 分支旧 engine 未 dispose（仅 rollback 自身失败的死连接路径，GC 回收）→ follow-up **#300**。
- **LOW-2**（out-of-scope）`_parse_comment_sacct_rows` 对畸形 `_foo` 首行 short-circuit（sacct 现实不产此类行、无注入面）→ follow-up **#300**（归一化后加 `SLURM_JOB_ID_RE` 形态校验）。
- **F1**（in-scope docs）迁移 000029 部署顺序义务未记录 → 已记入 000029 头部 + 本 worklog §5 + PR body（见 §5）。
- **F2/C5**（REFUTED 为活 race）SQLAlchemy reclaim/bind 非原子但仅被串行 reconcile 调用；docstring 措辞夸大 → follow-up **#300**。
- **F3/C6**（REFUTED 为 bug）`upsert_pipeline_job` 省 idempotency_key/candidate_id 列但 reserve 行已存在走 DO UPDATE 保留 → 无需动作。
- **结论：零 in-scope CONFIRMED、零 merge-blocking PLAUSIBLE = CLEAN。** 有界循环 round-4 收敛，未触 5 轮硬门。

## 5. 部署顺序义务（F1，强制）

**迁移 `db/migrations/000029_pipeline_reservation.sql` 必须在新预留代码上线前 apply，且必须在 #292（§4 连续守护进程）go-live 前完成。** 该迁移纯 additive（列 NULLable + 部分唯一索引 `WHERE idempotency_key IS NOT NULL`），但 psycopg `reserve_pipeline_job` 引用新列；若列缺失则 reserve 抛 `UndefinedColumn`（被 submit 路径吞为 `submission_failed`，可恢复但退化）。node-22 prod DB 尚未 apply 000029 —— go-live 前置动作。

## 6. 验证门（Evidence Floor #290）

```bash
# 远端 node-22 执行（真实 DB，仅干净注入 DATABASE_URL，不 source 整个 compute.host.env）
uv run pytest -q tests/test_production_scheduler.py tests/test_orchestration_chain.py
uv run ruff check .
openspec validate m24-multibasin-continuous-daemon-live --strict --no-interactive  # 本地
```

- reservation/idempotency + crash-window 测试集 PASS（含 reconcile-by-comment、kill-after-submit-before-bind、overlapping passes、grace defer）。
- overlapping-submit 实况 receipt：并发重叠提交的 live-proof —— 真实 Slurm 提交，归属 §3A live 证据；若依赖缺失则记 BLOCKED（合法终态）。
- 注：之前全量套件 324-failed 实为 verify-harness 污染（source 整个 compute.host.env 注入生产 workspace/lock 路径致确定性测试 `lock_path must be under workspace_root`），非代码缺陷；本轮只干净注入 DATABASE_URL。

## 7. 进度日志（倒序，最新在上）

| 时间 | 阶段 | 动作 | 结果 |
|------|------|------|------|
| 2026-06-05 | Phase 8 | Evidence Floor 终态：node-22 真实 DB（c2ed6ba）跑 `test_production_scheduler.py + test_orchestration_chain.py` **576 passed, 0 failed**（先前 2-failed 畸形-URL 已闭合）；全量 ruff clean | ✅ |
| 2026-06-05 | Phase 6 修复 | Round-6 malformed-URL 不变式闭合（`c2ed6ba`）：`_restart_reconcile_store` build 包 try/except（零泄漏，只存异常类名）+ statement_timeout 封慢查询子例；本地 40 passed、ruff clean | ✅ |
| 2026-06-05 | Round-6 | node-22 验证抓出 2-failed（`bad::host`/`[::1` 建引擎同步抛异常传播打断 pass）→ 同类第 2 轮 → pattern escalation 不变式闭合（见 §4 Retro） | ✅ |
| 2026-06-04 | Phase 8 | Evidence Floor 首跑：node-22 报 2 failed, 574 passed（畸形-URL 建引擎异常未被 best-effort 兜住） | ⚠️ 已修 |
| 2026-06-04 | Phase 7 | 干净树终检：HEAD=origin=`8410ef2`、import sanity（reconcile 重构无断裂）、本地+远端 reconcile 35 passed | ✅ |
| 2026-06-04 | Round-4 | 2 reviewer（对抗式 + 全 diff 七维）：**0 merge-blocking**，全攻击向量 REFUTED；2 LOW + F1 docs 转 follow-up/记录 → **CLEAN** | ✅ `8410ef2` |
| 2026-06-04 | Phase 6 修复 | FINDING-1 session rollback + FINDING-2 array querier + B-LOW 测试（`8410ef2`）；本地+远端 35 passed，ruff ✅ | ✅ |
| 2026-06-04 | Round-3 | 6-pack→3-lens 综合复审：FINDING-1/2 + B-LOW（新类）；C2(a) 经复核清白 | ✅ |
| 2026-06-04 | Phase 6 修复 | grace 锚 created_at→updated_at + 回归/边界/malformed 测试（`77d8039`）；28 passed | ✅ |
| 2026-06-04 | Round-2 | 综合复审发现 created_at-vs-updated_at 锚点缺陷 | ✅ |
| (前序会话) | Phase 6 修复 | grace-gate defer（`c4f58b0`）+ 原子 takeover reclaim（`4127611`）+ reserve 吸收唯一冲突（`a3bb380`）+ reserve gate 接线（`93b5ddc`） | ✅ |
