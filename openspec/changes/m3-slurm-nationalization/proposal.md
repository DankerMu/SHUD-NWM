## Why

M2 完成了单流域 analysis + warm-start 闭环验证，但当前 Slurm Gateway 仅有 mock 后端，无法实际提交 HPC 作业，也不支持多流域并行调度。M3 用真实 Slurm 替换 mock，实现全国化多流域并行调度，让系统具备生产级别的作业管理、失败隔离和运维监控能力。

## What Changes

- 新增 Real Slurm Backend：通过 sbatch/sacct/scancel 命令与 Slurm 集群交互，替换 mock backend
- 新增 Job Array 编排：支持 `--array=0-N%M` 多流域并行提交，per-model resource profile 配置
- 新增依赖链自动化：Orchestrator 按 download → canonical → forcing → forecast → parse → frequency → publish 顺序通过 `--dependency=afterok` 串联作业链
- 新增 Partial Success 支持：部分流域失败时 forecast_cycle 进入 `forcing_ready_partial` / `parsed_partial` 状态，不阻断成功流域的入库和发布
- 新增 pipeline_job 持久化：将 Slurm job_id、stage、status、log_uri、retry_count 等写入 `ops.pipeline_job` 表
- 新增运维监控 API：`/pipeline/stages`、`/jobs`、`/metrics` 等端点，支持流水线状态查询和性能指标
- 新增前端产品监控页：七阶段流水线卡片、作业列表表格、性能与成功率趋势图、失败流域展开详情
- 新增失败重试：`POST /runs/{run_id}/retry` 手动重试 + 自动重试（max_retries 配置）

## Capabilities

### New Capabilities

- `real-slurm-backend`: 真实 Slurm 后端实现——sbatch 提交、sacct 状态查询、scancel 取消、模板白名单、命令注入防护
- `job-array-orchestration`: Job array 编排与 resource profile——`--array=0-N%M` 多流域并行、per-model CPU/内存/时间配置、manifest index 映射
- `dependency-chain-automation`: 作业依赖链自动化——Orchestrator 按七阶段 DAG 通过 `--dependency=afterok` 串联、cycle 级全局编排
- `partial-success-handling`: Partial success 状态管理——部分流域失败时的 cycle 状态转换（`forcing_ready_partial`/`parsed_partial`）、成功流域继续下游
- `pipeline-job-persistence`: pipeline_job 表持久化——Slurm job_id 与 run_id/cycle_id 关联、stage 标记、状态同步、log_uri 记录、retry_count 跟踪
- `ops-monitoring-api`: 运维监控 API——`/pipeline/stages`、`/jobs`、`/jobs/{job_id}/logs`、`/metrics/stage-duration`、`/metrics/success-rate`、`/queue/depth`
- `pipeline-monitoring-frontend`: 前端产品监控页——七阶段卡片、进度条、作业列表表格、性能趋势图、成功率趋势图、失败流域展开、Slurm 队列深度环形图
- `job-retry-mechanism`: 失败重试机制——手动 `POST /runs/{run_id}/retry`、自动重试策略（max_retries + backoff）、重试审计记录

### Modified Capabilities

（无已有 spec 需要修改——M1/M2 的 Slurm Gateway 接口契约保持不变，M3 在内部扩展真实后端实现）

## Impact

- **数据库**：`ops.pipeline_job`（新增写入）、`ops.pipeline_event`（新增写入）、`met.forecast_cycle`（新增 `forcing_ready_partial`/`parsed_partial` 状态值）
- **API**：新增 §7 运维监控接口全部端点（`/pipeline/stages`、`/jobs`、`/metrics/*`、`/queue/depth`、`POST /runs/{run_id}/retry`、`POST /runs/{run_id}/cancel`）
- **HPC/Slurm**：从 mock 切换到真实 sbatch/sacct/scancel 命令；新增 sbatch 模板文件（`infra/sbatch/`）；新增 resource profile 配置
- **前端**：新增产品监控页面（效果图 8）；仅 operator 及以上角色可见
- **配置**：新增 `slurm_gateway.backend=slurm` 配置项、resource_profiles YAML、sbatch 模板路径
- **依赖**：无新增外部依赖（sbatch/sacct/scancel 为 HPC 环境自带）
