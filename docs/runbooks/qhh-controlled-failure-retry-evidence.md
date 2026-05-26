# QHH controlled failure and retry evidence

最后更新：2026-05-26

## 范围

本 runbook 对应 GitHub issue #213 / M21-10。目标是证明一个受控的 QHH-like failed run identity 能从正式 pipeline persistence 贯穿到公开 ops API、`/ops` UI、authorized retry、retry job/event 记录和 job/stage 终态结果。

这条证据不读取 `.nhms-runs/qhh-continuous` 诊断 state JSON，不伪造 live Slurm/QHH proof，也不声明 final production readiness。

## deterministic evidence

deterministic 模式只使用测试数据库、mocked Slurm gateway fixture 和 mocked browser API response。证据命令：

```bash
uv run pytest -q tests/test_monitoring_api.py tests/test_retry_cancel_consistency.py

cd apps/frontend
corepack pnpm test:e2e -- monitoring.spec.ts --project=chromium --workers=1
```

重点覆盖：

- `tests/test_monitoring_api.py::test_qhh_like_controlled_failure_retry_evidence_propagates_one_formal_identity`
- `apps/frontend/e2e/monitoring.spec.ts` 中 `/ops` controlled failure lifecycle 测试

deterministic 证据证明：

- `/api/v1/pipeline/status`、`/api/v1/pipeline/stages`、`/api/v1/jobs` 和 `/api/v1/jobs/{job_id}/logs` 暴露同一个 failed `run_id/job_id/stage/cycle_id`。
- sibling cycle job 不进入选中 source/cycle 的 jobs/stages 结果。
- viewer/non-operator retry 被拒绝且不创建 job/event。
- operator retry 调用 `POST /api/v1/runs/{run_id}/retry`，带 operator role header。
- retry 创建新 pipeline job，记录 retry/submission event、`previous_job_id`、`retry_count`、`stage`、`run_id` 和 fixture Slurm metadata。
- retry job lifecycle 在 deterministic fixture 中推进到 `running` 后 `succeeded`，并记录 explicit job/stage terminal outcome。
- post-retry `/api/v1/pipeline/status` 的 `job_counts` 反映新增的 succeeded retry job 和历史 failed job；`current_state` 仍来自 `met.forecast_cycle.current_state` 的持久化值，除非正式 scheduler/orchestrator producer 另行更新，不因 retry job 成功而自动变为 cycle `complete`。
- `/ops` 能看到 failed row、backend logs route 内容、retry request、刷新后的 retry job row 和 succeeded job/stage terminal outcome；cycle current_state 仍保持后端持久化状态。

## artifact root

如本地保存截图、trace、pytest output、Playwright report 或人工记录，使用非提交目录：

```text
.codex/evidence/issue-213/
```

建议文件名：

```text
.codex/evidence/issue-213/backend-pytest.log
.codex/evidence/issue-213/frontend-e2e.log
.codex/evidence/issue-213/playwright-report/
.codex/evidence/issue-213/manifest.md
```

这些 artifact 是本地证据，不应提交二进制截图、trace zip 或生成报告。提交内容只保留测试、runbook 和必要进度说明。

## live evidence labels

live 模式只有在真实依赖实际执行并记录 receipt 后才可标注为 live。缺任一依赖时，证据必须保持 deterministic 或 blocked，不得写成 live-ready。

当前 deterministic #213 lane 明确跳过这些 live dependency categories：

- live Slurm submit/accounting：未执行真实 `sbatch`、`squeue`、`sacct`、`scancel`。
- live QHH SHUD runtime：未执行真实 `SHUD/shud`。
- live GFS/IFS download：未连接外部 GFS/IFS source 拉取新周期。
- live canonical/forcing/parse/publish chain：未写真实 `met.*`、`hydro.*` display product 结果。
- live PostgreSQL/Timescale/PostGIS target database：未使用目标生产数据库。
- live object store/shared log storage：未验证真实对象存储或共享 Slurm log root。
- live IdP/operator identity provider：只使用 documented dev/test role override 或 mocked header。
- live final readiness receipts：未验证 live alert sink、rollback、nationwide scale 或 final production readiness。

## live execution checklist

如果目标环境具备 live 依赖，另行采集 live proof，并把 deterministic 与 live receipt 分开记录：

1. 使用 `nhms-pipeline plan-production --plan` 作为正式 scheduler/orchestrator 入口。
2. 记录真实 `source_id`、`cycle_time`、`cycle_id`、`run_id`、failed `job_id`、`stage`、`slurm_job_id` 和 bounded `log_uri`。
3. 通过公开 API 验证 status/stages/jobs/logs，不读取 qhh diagnostic state JSON。
4. 用授权 operator 身份调用 retry，并记录 retry job/event、Slurm metadata、job/stage 终态和 cycle current_state 是否由正式 producer 更新。
5. 如果 live Slurm/QHH 任一步 unavailable，记录具体 unavailable reason；不要把 deterministic 结果提升为 live readiness。
