# Tasks

## Risk packs

- [x] Public API / CLI / script entry — **selected**：`cohort_candidates` 的内容变了（去掉被取代的候选），字段不变。→ 2.1、2.2
- [x] Legacy compatibility — **selected**：#2584 的已有提示测试（4、5a–e、最新行）保持通过。→ 2.3
- [x] Operator alerting lanes / observer-observed predicate parity — **selected**：提示是值守观察面，本 bug 正是提示与真实恢复状态
      不一致。取代判定只用 journal 中 `state_save_qc` 行的状态与可证明覆盖，不引入第二套"已完成"判定；调度器侧的归属问题
      （#2603）不在本单。→ 2.1、2.2、2.4
- [x] Slurm production lifecycle / mock-vs-real parity — **selected**：人工标记后的重跑换 cohort run id（`_convert_` → `_full_`）是
      生产真实行为，测试样本按 node-22 journal 实际行形态构造。→ 2.1、3.4
- [x] Run manifest / QC provenance — **not selected**：只读行的 status/model_id/cohort_members，不读写 manifest 与 qc_result。
- [x] Geospatial / CRS / basin geometry — **not selected**：不读写几何与 CRS。
- [x] Hydro-met time series / forcing windows — **not selected**：cycle 身份只用于 journal 查询范围，不读写 forcing 序列或窗口。
- [x] SHUD numerical runtime / conservation / NaN — **not selected**：不涉及模型运行与数值。
- [x] PostGIS / TimescaleDB domain behavior — **not selected**：DB-free 文件 journal，只读，不连 DB。
- [x] External hydro-met providers / snapshot reproducibility — **not selected**：不访问上游数据源与快照。
- [x] Published NHMS artifacts / display identity — **not selected**：不改发布产物与展示身份；只影响值守看到的候选 id。
- [x] Config、File IO、Schema、Auth、Resource limits、Concurrency、Release — **not selected**：只读提示，不改状态、配置或文件格式，
      不持锁。

## Ordering

- 本 change 的 spec delta 是 MODIFIED，对象是 #2584 归档进主 spec 的 requirement；必须在 PR #2604（归档）合并后、本分支 rebase 到
  包含它的 master 上再 validate。

## Must preserve

- `_preview` 的决策、退出码、`--execute` 以及 `would_mark` / `run_active` / `journal_read_blocked` 的输出都不变。
- 提示仍然只列出成员关系可证明的行；`cohort_candidates_error` 的语义不变。

## 1. 实现

- [x] 1.1 `_cohort_candidates`：若存在一行 `cycle_<source>_<stamp>_*` cohort run 的 `state_save_qc` 行，`succeeded`、按
      `_file_retry_job_truth_sort_key` 排在失败 master 最新行之后、且**可证明**覆盖该模型（单模型行看 `model_id`；model-less 行只能经
      `_complete_cohort_members_by_run` 的完整成员证明），就不列出这个失败 master。覆盖判定与"是否列为候选"用同一条规则，
      不另写一套；证明不了覆盖的成功行不取代任何失败行。
- [x] 1.2 `docs/runbooks/node22-control-plane-manual-recovery.md`：在 cohort_candidates 的说明里补一句"已被更晚成功的 cohort 取代的 master 不列出"。

## 2. 测试（2.1 须在改动前变红）

- [x] 2.1 生产形态：旧 master `cycle_ifs_2026092212_convert_<model>` 的 convert/forcing/forecast 为 succeeded，`state_save_qc` 为
      `permanently_failed` / `SLURM_PARSE_ERROR`（04:31Z）；新 cohort `cycle_ifs_2026092212_full_<model>` 的各行为 succeeded，
      其中 `state_save_qc` 为 13:34Z；hydro run 的 forecast 行两次都成功。对 hydro run id 预览，结果为 `refused` / `no_retryable_failed_job`，
      且 `cohort_candidates == []`，没有 `warning`。
- [x] 2.2 反向：成功的 `_full_<model>` `state_save_qc` 行**更早**、失败的 `_convert_<model>` 行更晚（重跑后又失败）→ 列出
      `cycle_ifs_2026092212_convert_<model>`，`member_count == 1`，带 `warning`。
- [x] 2.4 多成员取代：失败的单模型 master（04:31Z），更晚（13:34Z）的 `cycle_ifs_2026092212_convert_cohort_<hash>` 的 forcing/forecast
      行 `cohort_members` 完整且含该模型、其 `state_save_qc` 行 model-less 且 `succeeded` → `cohort_candidates == []`。
- [x] 2.5 不可证明不取代（2.5 须在"粗粒度取代"实现下变红）：同 2.4，但该 cohort 的成员名单不完整（出现空 `model_id`，
      参照现有 `incomplete_members` 用例）→ 失败 master 仍列出，`member_count == 1`，带 `warning`。
- [x] 2.3 原有测试保持通过：`tests/test_node22_manual_retry_failed_runs.py` 全部通过。

## 3. 验证

- [x] 3.1 `uv run pytest -q tests/test_node22_manual_retry_failed_runs.py`，以及 `scripts/select_ci_tests.py` 对被改文件选出的集合（确认非空）。
- [x] 3.2 `uv run ruff check` 被改文件；markdownlint 检查该 runbook。
- [x] 3.3 `openspec validate cohort-hint-superseded-master --strict --no-interactive`。
- [x] 3.4 node-22 部署后只读预览（**禁止** `uv run`，维护窗口前会重建共享 `.venv`）：
      `cd /scratch/frd_muziyao/NWM && /scratch/frd_muziyao/NWM/.venv/bin/python scripts/node22_manual_retry_failed_runs.py
      --journal-root /scratch/frd_muziyao/nhms-prod/workspace/scheduler/journal --run-id fcst_ifs_2026092212_dg_8a34ed2ba8f8dd22f2716405569628a9
      --reason "#2605 post-deploy read-only smoke" --requested-by "<operator>"`（不带 `--execute`）→ `decision: refused`、
      `reason: no_retryable_failed_job`、`cohort_candidates == []`、无 `warning`、rc=0。

## Non-goals

- 重跑仍在进行、尚未 `succeeded` 时旧 master 仍会被列出（值守可能重复标记）：issue 只要求 succeeded 取代；另行评估。
- 选择器对旧 master id 的 `would_mark` 语义、#2600、#2603。

      Receipt (2026-09-24 02:41 CST, node-22 @ 86d89945, deployed 02:40 via pause/drain/deploy/resume, DEPLOY_OK, no service restart):
      `decision: refused`, `reason: no_retryable_failed_job`, `cohort_candidates: []`, no `warning`, rc=0.
