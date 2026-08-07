# Tasks: journal-cycle-row-model-scope

## Risk triage

- Issue type: bugfix (#1288, master 既有,db-free 独有)
- Project profile: NHMS
- Blast radius: medium-high（原评 medium,fixture 复审 P1-1 上调:候选
  seam 附近是驱动 4 个 gate surface 的共享谓词,错 seam 会放松
  `active_duplicate_pipeline` 去重闸且无既有测试守护;正确 seam 下爆破面
  收回候选态投影,但 gate surface 负向回归成为硬要求）
- Fixture level: expanded（domain trigger: orchestrator state/journal 读
  契约;issue 无 Suggested fixture level 字段——issue-scribe 产出缺省,
  divergence 记录:自评 expanded,与 #1287 同域同级）
- Repair intensity: medium（投影层行+事件同批过滤,一处组装点两条推导;
  事件面是显式约束非自动收敛——P1-1b 复活通道要 mutant 证红）
- Selected risk packs:
  - `oracle-discrimination`（selected:issue 端到端判别对——他模型 marker 5
    钉进 retry_count=0 候选 vs 修后 1;判别与 mutant 红能力在案,含
    "只滤行不滤事件"mutant）
  - `invariant-state`（selected:model-less cohort 可见性、自身行可见性、
    4 个 gate surface 行为不变三组负向不变量）
  - `spec-compliance`（selected::312 requirement 同穴修改,visibility 句
    限定必须与 #1287 已落措辞相容;candidate-state scope 限定与 gate
    例外必须如实）
  - `integration`（selected——复审 P2-4 翻转:同一读面跨
    `file_orchestration_journal` / `chain_forecast_trigger` /
    `scheduler_candidates` 驱动决策闸门,且复审实测无既有用例覆盖 gate
    surface;2.6 即该 pack 的落点）
  - `security/perf`（not selected:无此类面）
- Evidence floor: 见 §E

## 1. Implementation

- [x] 1.1 seam = `candidate_state` 投影（`file_orchestration_journal.py`）:
  在 `pipeline_jobs=` 推导（`:689-700`）与 `pipeline_events=` 推导
  （`:701-708`）处排除"他模型具名（`model_id` 非空且 ≠ 候选）+
  `run_id == cycle_run_id`"的行与解析到该行的 `pipeline_job` 事件——
  **行/事件同一步排除**(P1-1b)。判定复用/对齐
  `_is_model_less_cycle_scope_job`（`:8405-8413`）的空值口径
  （`in (None, "")`）。共享谓词 `_job_matches_candidate`、
  `_filter_cycle_rows_for_model` 及 4 个 gate surface（`:502`/`:531`/
  `:618`/`:4157`）**逐字不动**。
- [x] 1.2 代码注释显式对齐 DB 候选态谓词口径,指向
  `chain_repository_state.py:510-515`,并注明 gate 谓词
  （`chain_repository.py:74-79`/`:177-181`）有意保持宽形(issue AC 5 +
  复审 P1-1 反向依据)。

## 2. Tests（tests/test_file_orchestration_journal.py 为主;判别对走真实
file-journal 读路径 `repository.candidate_state()` 端到端;fixture 入口
纪律见 design D6——直录 `pipeline-jobs/*.json`,信封 model 与 payload 一致）

- [x] 2.1 判别对（读侧,AC 1）:他模型具名（`model_id=model_b`）+
  `run_id == cycle_<source>_<stamp>` + `retry_count=5` 的直录行,不出现在
  `model_a` 候选 `candidate_state()["pipeline_jobs"]`;指向它的
  `entity_type=pipeline_job` manual marker 事件**同批**不在事件表。
- [x] 2.2 判别对（钉值,AC 2）:同 fixture 下
  `_manual_retry_new_attempt(state, previous_attempt=0) == 1` 且
  `_manual_retry_payload(state)` 不含 `new_attempt`（修前 red:5 /
  含 new_attempt=5）。
- [x] 2.2b P1-1b mutant 证红:构造"只滤行、不滤事件"的 mutant(投影处
  仅移除行),2.1 的事件断言与 2.2 的钉值断言至少一条 red——证明落单
  marker 经 `_unresolvable_marker_entity_pins_attempt` 可解析 cycle-scope
  语法臂复活钉值的通道被测试封死。
- [x] 2.3 负向（cohort 可见性,AC 3）:model-less + `run_id == cycle_run_id`
  及 `cycle_run_id_<suffix>` 行仍对全体候选可见;#1205 刀 2 既有用例全绿。
- [x] 2.4 负向（自身行,AC 4）:候选自身 model 具名 + `run_id == cycle_run_id`
  行仍可见,指向它的 marker 仍正常采信/钉值;
  `tests/test_file_orchestration_journal.py:214-228` `_active_job` 形保持绿。
- [x] 2.5 红证明:2.1/2.2 在 pre-change 源上 red（stash 法或 archive 副本),
  实现后 green;输出入 implementer 报告。
- [x] 2.6 负向（gate surface,复审 P1-1):
  (a) 他模型具名 + `run_id == cycle_run_id` 的 `queued` 直录行在场时,
      `has_active_pipeline(model_a)` / `active_slurm_jobs(model_a)`
      返回值与修改前一致（DB 对照 `chain_repository.py:74-79`/`:177-181`
      的无条件 cycle-run 子句);
  (b) 另用一条 `status=succeeded` 且 `stage` 属完成阶段（`state_save_qc`
      或 `publish`）的他模型具名 cycle-run 直录行,断言
      `has_completed_pipeline(model_a)` 与修改前一致（复审实测该形状现
      返回 True;`forecast` stage 为 False,不具判别力——queued 形状对该
      函数是空断言,故拆 (b)）。
- [x] 2.7 DB 口径对齐以 code-reading 证据交付(design D5):代码注释引用
  `chain_repository_state.py:510-515`(1.2),不声称自动化 DB 断言;仓内
  real-DB 套件无 `candidate_state` 覆盖为复审核实事实。

## E. Evidence floor

- [x] E1 `uv run pytest -q tests/test_file_orchestration_journal.py
  tests/test_file_orchestration_migration.py tests/test_production_scheduler.py`
  结果(implementer 与 orchestrator 各自实测一致):**1353 passed /
  1 failed**——唯一失败 `test_db_free_slurm_storage_root_check_masks_symlink_loop_path`
  为 macOS 既有环境失败(复审无补丁基线 1347/1 同 red),与本 change 无关;
  +6 = 六个新测试。
- [x] E2 `uv run ruff check .`
- [x] E3 `openspec validate journal-cycle-row-model-scope --strict
  --no-interactive`
- [x] E4 Surface check:生产 diff 仅
  `services/orchestrator/file_orchestration_journal.py`;共享谓词与 gate
  surface 函数零 diff;spec 措辞仅经本 change delta。
- [ ] E5 CI `Unit Tests` green on PR head。
