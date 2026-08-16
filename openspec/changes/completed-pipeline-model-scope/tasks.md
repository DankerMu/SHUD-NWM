# Tasks: completed-pipeline-model-scope

## 1. 实现

- [x] 1.1 `has_completed_pipeline`（`file_orchestration_journal.py:558-563`
      合取项）追加 `and not _is_foreign_model_cycle_scope_job(...)`
      （design D1；复用 #1288 谓词，禁复制口径、禁动共享谓词
      `_job_matches_candidate`）。
- [x] 1.2 改写投影处口径注释（`file_orchestration_journal.py:721-729`）：
      排除逻辑两层（投影面 #1288 / completion gate 本地合取 #1302），
      completion gate 与 DB `chain_repository.py:97-111` 方向对齐，
      duplicate-submission 两 gate 仍宽（design D3.2）。
- [x] 1.3 gate 内（或谓词 docstring）注释显式指向 DB 对照
      `chain_repository.py:97-111`（issue AC「journal/DB 口径显式对齐」）。

## 2. 测试（先红后绿；真实 `FileOrchestrationJournalRepository` 直录 fixture）

- [x] 2.1 判别主锚：他模型具名（model_id 非空 ≠ 候选）+
      `run_id == cycle_<source>_<stamp>` + `status=succeeded`，stage 参数化
      `state_save_qc` / `publish` / `parse` → `has_completed_pipeline(候选)`
      均 **False**（修前 True，需 red-proof）。
- [x] 2.2 终态口径开关形：`NHMS_ORCHESTRATOR_TERMINAL_STAGE=forecast_state_save_qc`
      下他模型 stage=state_save_qc 行 → **False**（修前 True，需
      red-proof）；同口径下他模型 stage ∈ {parse, publish} 行 → False
      （修前修后同为 False，D5 第 3 行不回归钉）。env-flip 直接
      `monkeypatch.setenv`（`_compute_state_save_qc_terminal_enabled`
      无缓存）。
- [x] 2.3 自身失败形：候选自身 hydro_run.status 参数化
      {failed, cancelled, created} + 自身 forecast 行 failed + 他模型
      state_save_qc 成功行在场 → **False**（修前 True，需 red-proof）。
- [x] 2.4 不回归（cohort 可见性，**默认口径**）：model-less cycle-scope
      cohort 完成行（`run_id == cycle_run_id` 与 `cycle_run_id_<suffix>`
      两形）仍让全体候选判 True。（生产口径下 cohort publish/parse 形
      本就 False，不按 env 参数化——见 D5 第 5 行括注与 delta scenario
      的「under the active contract」限定。）
- [x] 2.5 不回归（候选自身，**默认口径**）：候选自身具名 cycle-run
      完成行、自身 `fcst_...` run_id 完成行、自身 hydro 完成，均仍
      True（hydro 完成臂仅默认口径可达，:564-565）；他模型
      stage=forecast（非终态）行仍 False（真值表 D5 第 6/7 行钉住）。
- [x] 2.6 消费面判别锚（trigger 面）：他模型完成行在场 → 候选不再走
      `chain_forecast_trigger.py:247-251` 的 completed-skip 腿（修前走，
      需 red-proof）。**构造口径（fixture review 裁定，防绕远路）**：
      沿用 `tests/test_orchestration_chain.py` 既有 fake
      `ReadyForecastRepository` harness（`test_trigger_ready_forecasts_*`
      族），仅把 fake 的 `has_completed_pipeline` 委托到一个铺了 foreign
      行的真实 `FileOrchestrationJournalRepository`；readiness /
      `list_canonical_ready_cycles` / 提交路径仍走既有 fake。
- [x] 2.7 不回归（相邻 gate 面）：同一 fixture 下 `has_active_pipeline` /
      `active_slurm_jobs` 返回值逐字不变（`_job_matches_candidate` 未动
      的行为证明）；既有
      `test_foreign_model_cycle_run_row_stays_visible_to_the_duplicate_submission_gates`
      保持绿且不改。
- [x] 2.8 解冻 #1288 冻结断言：
      `test_foreign_model_cycle_run_row_stays_visible_to_the_completion_gate`
      （`tests/test_file_orchestration_journal.py:1589-1610`）True→False、
      更名 + docstring 记「#1288 冻结、#1302 解冻、对齐 DB hydro-run
      三元限定」（design D3.1）。issue AC6 字面所指的 #1288 tasks 2.6(b)
      已归档（archive 是历史账本，不回改）；freeze→unfreeze 记录以本
      change 文档 + 测试 docstring + spec delta 三处为准（proposal AC6
      处置声明）。

## 3. 验证（Evidence Floor）

- [x] 3.1 `uv run pytest -q tests/test_file_orchestration_journal.py` 通过。
- [x] 3.2 `uv run pytest -q tests/test_production_scheduler.py
      tests/test_orchestration_chain.py` 通过。
- [x] 3.3 `uv run ruff check .` 通过。
- [x] 3.4 spec-compliance 人工证据：delta 契约句 + 新 scenario 与最终实现
      逐句对读一致；D3 解冻三处（测试/注释/spec）同步核对；
      `openspec validate completed-pipeline-model-scope --strict
      --no-interactive` 通过。
