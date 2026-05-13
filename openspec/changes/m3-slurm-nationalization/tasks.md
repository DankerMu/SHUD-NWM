## 0. 基础设施与配置

- [x] 0.1 编写 `ops.pipeline_job` 和 `ops.pipeline_event` 数据库 migration SQL，严格对齐上游 schema。Evidence: `db/migrations/000009_ops.sql`, `db/migrations/000011_pipeline_job_model_id.sql`, `db/migrations/000012_pipeline_job_array_task.sql`, `services/orchestrator/persistence.py`, `tests/test_pipeline_persistence.py`.
- [x] 0.2 在 `met.cycle_status` ENUM 中确认 `forcing_ready_partial` 和 `parsed_partial` 状态值存在。Evidence: `db/migrations/000003_enums.sql`, `tests/test_migrations.py`.
- [x] 0.3 创建 resource profile YAML 配置。Evidence: `config/resource_profiles.yaml`, `services/slurm_gateway/real_backend.py`, `tests/test_job_array.py`.
- [x] 0.4 创建 `infra/sbatch/` 目录，编写七阶段 sbatch 模板文件。Evidence: `infra/sbatch/`, `tests/test_slurm_array_contract.py`.
- [x] 0.5 扩展 `SlurmGatewaySettings` 配置模型。Evidence: `services/slurm_gateway/config.py`, `tests/test_real_slurm_gateway.py`.
- [x] 0.6 创建 job_type → sbatch 模板映射配置。Evidence: `config/job_type_templates.yaml`, `services/slurm_gateway/config.py`.

## 1. Real Slurm Backend

- [x] 1.1 实现 `RealSlurmGateway.submit_job`：模板查找、渲染、sbatch 调用、job_id 解析。Evidence: `services/slurm_gateway/real_backend.py`, `tests/test_real_slurm_gateway.py`.
- [x] 1.2 实现 `get_job_status`：调用 sacct 并映射到 `SlurmJobStatus`。Evidence: `services/slurm_gateway/real_backend.py`, `tests/test_real_slurm_gateway.py`.
- [x] 1.3 实现 `cancel_job`：调用 scancel 并更新状态。Evidence: `services/slurm_gateway/real_backend.py`, `tests/test_real_slurm_gateway.py`.
- [x] 1.4 实现 `list_jobs`：调用 sacct with filters 并分页返回。Evidence: `services/slurm_gateway/real_backend.py`, `tests/test_real_slurm_gateway.py`.
- [x] 1.5 实现 `fetch_logs`：从 workspace/log root 读取 stdout/stderr 日志文件。Evidence: `services/slurm_gateway/real_backend.py`, `tests/test_real_slurm_gateway.py`, `tests/test_object_store_roots.py`.
- [x] 1.6 实现 `health`：调用 `sinfo --version` 检查 Slurm 可用性。Evidence: `services/slurm_gateway/real_backend.py`, `tests/test_real_slurm_gateway.py`.
- [x] 1.7 实现模板白名单校验和 manifest 字段 schema 校验。Evidence: `services/slurm_gateway/real_backend.py`, `tests/test_real_slurm_gateway.py`.
- [x] 1.8 实现 subprocess 错误处理：超时、非零退出码、输出解析失败。Evidence: `services/slurm_gateway/real_backend.py`, `tests/test_real_slurm_gateway.py`.
- [x] 1.9 更新 `create_gateway()` 工厂方法，`backend == "slurm"` 返回 `RealSlurmGateway`。Evidence: `services/slurm_gateway/gateway.py`, `tests/test_real_slurm_gateway.py`.
- [x] 1.10 编写 RealSlurmGateway 单元测试。Evidence: `tests/test_real_slurm_gateway.py`.

## 2. Job Array 编排与 Resource Profile

- [x] 2.1 实现 `submit_job_array` 方法。Evidence: `services/slurm_gateway/real_backend.py`, `tests/test_job_array.py`, `tests/test_slurm_array_contract.py`.
- [x] 2.2 实现 manifest index 文件生成与 `NHMS_MANIFEST_INDEX` 注入。Evidence: `packages/common/manifest_index.py`, `services/slurm_gateway/real_backend.py`, `infra/sbatch/*.sbatch`, `tests/test_slurm_array_contract.py`.
- [x] 2.3 实现 resource profile 加载与解析。Evidence: `services/slurm_gateway/real_backend.py`, `config/resource_profiles.yaml`, `tests/test_job_array.py`.
- [x] 2.4 实现 sbatch 模板中 resource profile 变量渲染。Evidence: `infra/sbatch/*.sbatch`, `tests/test_job_array.py`, `tests/test_slurm_array_contract.py`.
- [x] 2.5 实现 array job 输入校验：0 task/max_concurrent 拒绝，max_concurrent clamp。Evidence: `services/slurm_gateway/real_backend.py`, `tests/test_job_array.py`.
- [x] 2.6 编写 job array 单元测试。Evidence: `tests/test_job_array.py`, `tests/test_slurm_array_contract.py`.

## 3. Pipeline Job 持久化

- [x] 3.1 实现 pipeline_job ORM 模型。Evidence: `services/orchestrator/persistence.py`, `tests/test_pipeline_persistence.py`.
- [x] 3.2 实现 pipeline_job CRUD 操作。Evidence: `services/orchestrator/persistence.py`, `tests/test_pipeline_persistence.py`.
- [x] 3.3 实现 pipeline_event ORM 模型和追加写入。Evidence: `services/orchestrator/persistence.py`, `tests/test_pipeline_persistence.py`.
- [x] 3.4 实现 Orchestrator 中的状态同步循环。Evidence: `services/orchestrator/chain.py`, `tests/test_orchestration_chain.py`.
- [x] 3.5 编写持久化层单元测试。Evidence: `tests/test_pipeline_persistence.py`.

## 4. 依赖链自动化（Lazy Submit）

- [x] 4.1 实现 Orchestrator 七阶段 lazy submit 编排逻辑。Evidence: `services/orchestrator/chain.py`, `tests/test_orchestration_chain.py`, `tests/test_e2e_m3.py`.
- [x] 4.2 实现 cycle 级编排入口。Evidence: `services/orchestrator/chain.py`, `tests/test_orchestration_chain.py`.
- [x] 4.3 实现编排状态持久化与 crash recovery 基础恢复。Evidence: `services/orchestrator/chain.py`, `services/orchestrator/persistence.py`, `tests/test_orchestration_chain.py`.
- [x] 4.4 实现 stage 失败处理：全部失败阻断，部分失败继续下游。Evidence: `services/orchestrator/chain.py`, `tests/test_orchestration_chain.py`, `tests/test_partial_success.py`.
- [x] 4.5 支持多 cycle 并发编排。Evidence: `services/orchestrator/chain.py`, `tests/test_orchestration_chain.py`.
- [x] 4.6 编写依赖链集成测试。Evidence: `tests/test_orchestration_chain.py`, `tests/test_e2e_m3.py`.

## 5. Partial Success 处理

- [x] 5.1 实现 array job 结果聚合。Evidence: `services/orchestrator/chain.py`, `tests/test_partial_success.py`.
- [x] 5.2 实现 partial 状态转换逻辑。Evidence: `services/orchestrator/chain.py`, `tests/test_partial_success.py`, `tests/test_orchestration_chain.py`.
- [x] 5.3 实现成功流域继续下游并重新生成 manifest index。Evidence: `services/orchestrator/chain.py`, `tests/test_orchestration_chain.py`.
- [x] 5.4 实现 publish 阶段处理：仅发布全流程成功流域，有失败流域时保留 partial 状态。Evidence: `services/orchestrator/chain.py`, `tests/test_e2e_m3.py`.
- [x] 5.5 编写 partial success 单元测试。Evidence: `tests/test_partial_success.py`, `tests/test_orchestration_chain.py`.

## 6. 失败重试机制

- [x] 6.1 实现重试服务层 transient/non-transient 判断。Evidence: `services/orchestrator/retry.py`, `tests/test_retry.py`.
- [x] 6.2 实现自动重试逻辑。Evidence: `services/orchestrator/chain.py`, `services/orchestrator/retry.py`, `tests/test_orchestration_chain.py`, `tests/test_retry.py`.
- [x] 6.3 实现手动重试 handler：`POST /runs/{run_id}/retry`。Evidence: `apps/api/routes/pipeline.py`, `services/orchestrator/retry.py`, `tests/test_retry.py`, `tests/test_retry_cancel_consistency.py`.
- [x] 6.4 实现重试审计记录。Evidence: `services/orchestrator/retry.py`, `tests/test_retry.py`, `tests/test_retry_cancel_consistency.py`.
- [x] 6.5 实现 max retries 耗尽后标记 permanently_failed。Evidence: `services/orchestrator/retry.py`, `tests/test_retry.py`, `tests/test_orchestration_chain.py`.
- [x] 6.6 编写重试机制单元测试。Evidence: `tests/test_retry.py`, `tests/test_retry_cancel_consistency.py`.

## 7. 运维监控 API

- [x] 7.1 实现 `GET /api/v1/pipeline/status`。Evidence: `apps/api/routes/pipeline.py`, `tests/test_monitoring_api.py`.
- [x] 7.2 实现 `GET /api/v1/pipeline/stages`。Evidence: `apps/api/routes/pipeline.py`, `tests/test_monitoring_api.py`, `tests/test_e2e_m3.py`.
- [x] 7.3 实现 `GET /api/v1/jobs`。Evidence: `apps/api/routes/pipeline.py`, `tests/test_monitoring_api.py`, `tests/test_api_contract.py`.
- [x] 7.4 实现 `GET /api/v1/jobs/{job_id}/logs`。Evidence: `apps/api/routes/pipeline.py`, `tests/test_monitoring_api.py`.
- [x] 7.5 实现 `POST /api/v1/runs/{run_id}/retry`。Evidence: `apps/api/routes/pipeline.py`, `tests/test_retry.py`, `tests/test_retry_cancel_consistency.py`.
- [x] 7.6 实现 `POST /api/v1/runs/{run_id}/cancel`。Evidence: `apps/api/routes/pipeline.py`, `tests/test_monitoring_api.py`, `tests/test_retry_cancel_consistency.py`.
- [x] 7.7 实现 `GET /api/v1/metrics/stage-duration` 和 `GET /api/v1/metrics/success-rate`。Evidence: `apps/api/routes/pipeline.py`, `tests/test_monitoring_api.py`.
- [x] 7.8 实现 `GET /api/v1/queue/depth`。Evidence: `apps/api/routes/pipeline.py`, `tests/test_monitoring_api.py`.
- [x] 7.9 实现统一响应包装器。Evidence: `apps/api/routes/pipeline.py`, `apps/api/errors.py`, `tests/test_monitoring_api.py`, `tests/test_api_contract.py`.
- [x] 7.10 注册新 router 到 `apps/api/main.py`。Evidence: `apps/api/main.py`, `tests/test_monitoring_api.py`.
- [x] 7.11 更新 `openapi/nhms.v1.yaml`。Evidence: `openapi/nhms.v1.yaml`, `tests/test_api_contract.py`.
- [x] 7.12 编写监控 API 集成测试。Evidence: `tests/test_monitoring_api.py`.

## 8. 前端产品监控页

- [x] 8.1 创建产品监控页面路由和 RBAC 守卫。Evidence: `apps/frontend/src/pages/MonitoringPage.tsx`, `apps/frontend/src/components/layout/RBACGate.tsx`.
- [x] 8.2 实现 API client 和 TypeScript 类型定义。Evidence: `apps/frontend/src/api/client.ts`, `apps/frontend/src/api/types.ts`, `apps/frontend/src/stores/monitoring.ts`.
- [x] 8.3 实现顶部摘要条和 Slurm 队列深度图。Evidence: `apps/frontend/src/components/monitoring/SummaryBar.tsx`, `apps/frontend/src/components/charts/QueueDonut.tsx`.
- [x] 8.4 实现七阶段流水线卡片和阶段耗时图。Evidence: `apps/frontend/src/components/monitoring/StageList.tsx`, `apps/frontend/src/components/monitoring/StageCard.tsx`, `apps/frontend/src/components/charts/StageDurationBar.tsx`.
- [x] 8.5 实现失败阶段卡片点击展开。Evidence: `apps/frontend/src/components/monitoring/BasinFailures.tsx`, `apps/frontend/src/components/monitoring/StageCard.tsx`.
- [x] 8.6 实现作业列表表格和过滤排序。Evidence: `apps/frontend/src/components/monitoring/JobsTable.tsx`, `apps/frontend/src/components/monitoring/JobFilters.tsx`.
- [x] 8.7 实现日志查看 modal。Evidence: `apps/frontend/src/components/monitoring/LogModal.tsx`, `apps/frontend/src/components/monitoring/JobsTable.tsx`.
- [x] 8.8 实现重试/取消操作按钮。Evidence: `apps/frontend/src/components/monitoring/JobsTable.tsx`, `tests/test_monitoring_api.py`.
- [x] 8.9 实现右侧趋势面板。Evidence: `apps/frontend/src/components/monitoring/TrendPanel.tsx`, `apps/frontend/src/components/charts/TrendLine.tsx`.
- [x] 8.10 实现自动轮询和手动刷新。Evidence: `apps/frontend/src/hooks/usePolling.ts`, `apps/frontend/src/pages/MonitoringPage.tsx`.
- [x] 8.11 实现响应式布局。Evidence: `apps/frontend/src/pages/MonitoringPage.tsx`, `apps/frontend/src/index.css`.

## 9. 端到端验收

- [x] 9.1 编写 E2E 集成测试：mock Slurm 环境下完整链路。Evidence: `tests/test_e2e_m3.py`, `tests/test_orchestration_chain.py`.
- [x] 9.2 编写 partial success E2E 测试。Evidence: `tests/test_e2e_m3.py`, `tests/test_orchestration_chain.py`.
- [x] 9.3 编写 retry E2E 测试。Evidence: `tests/test_orchestration_chain.py`, `tests/test_retry.py`, `tests/test_retry_cancel_consistency.py`.
