# Tasks: scheduler-completion-verdict-absence-tolerance

## 1. 实现

- [x] 1.1 **统一比对 helper**:抽出单一 `terminal_init_state_match(...) -> {"match","absent","conflict"}`,语义 = 逐在场字段比对(见 design 修订:absent=零身份字段;在场字段全合=match;任一在场字段矛盾=conflict;**禁止**部分字段即 conflict);helper 为**纯字段比对**,candidate 侧 `candidate_state` 分支与 `COLD_START_QUARANTINED` 逃生门**留在 candidate wrapper 内、先于 helper 短路返回 match,不进 helper、不上提 verdict 路径**;verdict 侧(`scheduler_discovery.py:328-340`)消费 helper;candidate 侧 wrapper 的 `hydro_run` 腿(`scheduler_candidates.py:1842-1868`)**保留 `_warm_state_record_matches` selected-驱动比对,逐字节不变**(cross-review C1:换向会绕过 #1173 预算路由);legacy hydro_run 行(仅 `init_state_id`)在 verdict 侧匹配行为逐字节不变。
- [x] 1.2 **verdict 容缺**:`_cycle_completion_verdict` 中,终态成功 + `successor_state.ready` + helper 返回 `absent` → `complete`;`conflict` → `gap`(不回退);其余分支不动。附判定表单测(含 `successor_evidence is None` 第三态 → gap)+ 0718/0719 旧 per-basin 行为回归锁 + §8.7 journal predecessor identity 门行为不变的回归锁(F5)。
- [x] 1.3 **cohort 行前向补账(预约期,master 行字段)**:在预约写入点(`chain_forecast_orchestrator_cycle.py:509-527`)把身份落账为 **master 行新字段:按 `array_task_id`/`model_id` 键控的逐 model 身份映射**(标量禁止——N7;不经 member 传播——N1);终态逐 model 行构造从 `existing` master 行按本行 `array_task_id` 取对应项(`file_orchestration_journal.py:2806-2824`);新字段**加入冻结表且值自预约起不变**(与 design must-preserve #5 口径统一,N9);`_pipeline_job_row` 43 键构造器显式加字段(否则静默丢值 + 冻结校验抛错);`_CYCLE_SCOPE_JOB_PROJECTION_KEYS` 可见性显式取舍并测试锁定;**不动** `ORDINARY_UPSERT_FIELDS` 冻结语义、**不进** `_MEMBER_FIELDS`/digest 输入集(历史行 digest 校验零变化,负测锁定);不变量闸负测(非法/部分身份被拒);旧行 absent-tolerant 读取,零 migration。
- [x] 1.4 **runbook**:`docs/runbooks/failed-basin-retry.md` 增"缺账 vs 错账"判读节 + 072000 处置结论(修复部署后自动收敛,无手工干预;引用本 change)。

## 2. 验证(本地/CI)

- [x] 2.1 四文件门:`uv run pytest -q tests/test_production_scheduler.py tests/test_gateway_reconcile.py tests/test_warm_start_chaining.py tests/test_file_orchestration_journal.py`(净化 `__pycache__` + `PYTHONDONTWRITEBYTECODE=1`;journal 文件覆盖不变量闸与 `completed_pipeline_init_state_id` 既有断言)。
- [x] 2.2 `uv run ruff check .`;`openspec validate scheduler-completion-verdict-absence-tolerance --strict --no-interactive`。
- [x] 2.3 红前证据:改动前对"absence→complete"三形态至少一条能红(现状 absence 判 gap)。

## 3. 评审

- [ ] 3.1 fixture review(只读)→ 修复 → validate。
- [ ] 3.2 实现后 risk-adaptive cross-review(≥2 lane)+ verifier 批次;round ledger 记账。

## 4. Evidence Floor(实机 oracle,merge 后)

- [ ] 4.1 node-22 部署后 ≥1 自然 pass:evidence 中 072000 不再是 oldest gap(verdict complete),072012 候选生成、通过 warm 准入并**提交**(或其真实 Slurm 终态);随后链逐格推进(≥2 个后继 cycle 依次进入候选)。receipt(pass 文件名 + 关键字段)回贴 issue #1183 与 PR。若观测偏离,先读数再分支,严禁放宽判定。
