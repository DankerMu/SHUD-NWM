## 0. 基础设施与配置

- [ ] 0.1 编写 `ops.pipeline_job` 和 `ops.pipeline_event` 数据库 migration SQL，严格对齐上游 schema（`docs/appendices/C_database_schema_draft.md`）：pipeline_job 使用 TEXT 类型主键、含 job_type 字段、status 默认 'pending'；pipeline_event 使用 entity_type/entity_id/event_type/status_from/status_to 字段命名
- [ ] 0.2 在 `met.cycle_status` ENUM 中确认 `forcing_ready_partial` 和 `parsed_partial` 状态值存在（如不存在则新增 migration）
- [ ] 0.3 创建 resource profile YAML 配置（`config/resource_profiles.yaml`），定义 default profile 和 per-model overrides，字段对齐上游 §5：partition、nodes、ntasks、cpus_per_task、memory_gb、walltime、max_concurrent、shud_threads
- [ ] 0.4 创建 `infra/sbatch/` 目录，编写七阶段 sbatch 模板文件（download_source_cycle.sbatch、convert_canonical.sbatch、produce_forcing_array.sbatch、run_shud_forecast_array.sbatch、parse_output_array.sbatch、compute_frequency_array.sbatch、publish_tiles.sbatch），使用 Jinja2 占位符
- [ ] 0.5 扩展 `SlurmGatewaySettings` 配置模型，新增 `slurm_bin_path`、`template_dir`、`allowed_templates`、`sacct_poll_interval_seconds`、`subprocess_timeout_seconds`、`max_retries`、`retry_backoff_seconds` 等字段
- [ ] 0.6 创建 job_type → sbatch 模板映射配置（`config/job_type_templates.yaml`），将上游 §2 的 7 个 job_type 映射到对应模板文件

## 1. Real Slurm Backend

- [ ] 1.1 实现 `RealSlurmGateway` 类（继承 `SlurmGateway` ABC），实现 `submit_job`：接收 (job_type, manifest) → 按 job_type 查找模板 → 渲染模板（manifest + resource profile）→ subprocess 调用 sbatch → 解析 stdout 提取 job_id
- [ ] 1.2 实现 `get_job_status`：调用 `sacct --parsable2 --format=JobID,State,ExitCode,Start,End` → 解析输出 → 映射到 `SlurmJobStatus`
- [ ] 1.3 实现 `cancel_job`：调用 `scancel {job_id}` → 更新状态
- [ ] 1.4 实现 `list_jobs`：调用 sacct with filters（--starttime、--state）→ 分页返回
- [ ] 1.5 实现 `fetch_logs`：从 workspace `{run_id}/logs/` 读取 stdout/stderr 日志文件
- [ ] 1.6 实现 `health`：调用 `sinfo --version` 检查 Slurm 可用性
- [ ] 1.7 实现模板白名单校验（只允许 `template_dir` 下已注册模板）和 manifest 字段 schema 校验（Jinja2 SandboxedEnvironment + subprocess shell=False）
- [ ] 1.8 实现 subprocess 错误处理：超时、非零退出码、输出解析失败的统一异常转换
- [ ] 1.9 更新 `create_gateway()` 工厂方法，`backend == "slurm"` 分支返回 `RealSlurmGateway`
- [ ] 1.10 编写 RealSlurmGateway 单元测试：mock subprocess 验证 sbatch 解析、sacct 解析、scancel 调用、模板白名单拒绝、注入参数拒绝、SandboxedEnvironment 限制

## 2. Job Array 编排与 Resource Profile

- [ ] 2.1 实现 `submit_job_array` 方法：生成 `sbatch --array=0-{N-1}%{max_concurrent}` 命令，提交 array job
- [ ] 2.2 实现 manifest index 文件生成：将流域列表 `[{model_id, basin_version_id, run_id, ...}]` 写入 JSON，array task 通过 `SLURM_ARRAY_TASK_ID` 索引；模板中注入 `NHMS_MANIFEST_INDEX` 环境变量指向索引文件路径
- [ ] 2.3 实现 resource profile 加载与解析：读取 YAML → default + per-model override 合并逻辑（含 ntasks、shud_threads）
- [ ] 2.4 实现 sbatch 模板中 resource profile 变量渲染（`#SBATCH --cpus-per-task={{cpus_per_task}}`、`#SBATCH --ntasks={{ntasks}}`、`export SHUD_THREADS={{shud_threads}}`）
- [ ] 2.5 实现 array job 输入校验：total tasks=0 或 max_concurrent=0 时拒绝提交；max_concurrent > task_count 时 clamp 到 task_count
- [ ] 2.6 编写 job array 单元测试：manifest index 生成、resource profile 合并（含 ntasks/shud_threads）、模板渲染、校验拒绝、max_concurrent clamp

## 3. Pipeline Job 持久化

- [ ] 3.1 实现 pipeline_job ORM 模型（SQLAlchemy），字段严格对齐上游 schema：job_id TEXT PK、run_id、cycle_id、job_type、slurm_job_id、status、stage、submitted_at、started_at、finished_at、exit_code、retry_count、error_code、error_message、log_uri、created_at、updated_at
- [ ] 3.2 实现 pipeline_job CRUD 操作：create、update_status、query_by_cycle、query_by_slurm_job_id、query_by_run_id（双向查询）
- [ ] 3.3 实现 pipeline_event ORM 模型和追加写入：每次状态转换记录 (entity_type, entity_id, event_type, status_from, status_to, message, details)
- [ ] 3.4 实现 Orchestrator 中的状态同步循环：定期调用 sacct → 比对 pipeline_job 当前状态 → 有变化则更新并写 event
- [ ] 3.5 编写持久化层单元测试：CRUD 操作、双向查询、event 追加、状态同步

## 4. 依赖链自动化（Lazy Submit）

- [ ] 4.1 实现 Orchestrator 七阶段 lazy submit 编排逻辑：按 download_source_cycle → convert_canonical → produce_forcing_array → run_shud_forecast_array → parse_output_array → compute_frequency_array → publish_tiles 顺序，每步提交后通过 sacct 轮询等待完成，聚合结果后决定是否提交下一步
- [ ] 4.2 实现 cycle 级编排入口：接受 `(source, cycle_time)` 参数，查询 active models，构建 manifest index，启动编排
- [ ] 4.3 实现编排状态持久化：每步完成后将 stage 和结果写入 pipeline_job 表，支持 crash recovery 从最后完成的 stage 恢复
- [ ] 4.4 实现 stage 失败处理：某阶段全部失败 → 不提交后续阶段 → cycle 进入对应 `failed_*` 状态；部分失败 → 进入 partial 状态 → 用成功流域 manifest 继续下游
- [ ] 4.5 支持多 cycle 并发编排：GFS 00Z 和 06Z 可同时进行，互不阻塞
- [ ] 4.6 编写依赖链集成测试：mock sbatch 验证 7 步 lazy submit 串联、sacct 轮询、失败阻断、partial 继续、crash recovery

## 5. Partial Success 处理

- [ ] 5.1 实现 array job 结果聚合：查询 sacct 获取每个 array task 状态 → 计算 succeeded/failed/cancelled 计数
- [ ] 5.2 实现 partial 状态转换逻辑：全成功 → 正常状态，部分失败 → `_partial` 状态，全失败 → `failed_*` 状态
- [ ] 5.3 实现成功流域继续下游：从上一步成功的 task 列表重新生成 manifest index（re-indexed）→ 下一步 array 仅包含成功流域
- [ ] 5.4 实现 publish 阶段处理：仅发布全流程成功的流域产品；cycle 状态在有失败流域时保持 `parsed_partial`，不进入 `published`
- [ ] 5.5 编写 partial success 单元测试：全成功、部分失败、全失败三种场景的状态转换、manifest 过滤和 re-index

## 6. 失败重试机制

- [ ] 6.1 实现重试服务层：判断 error_code 是否 transient（SLURM_TIMEOUT/NODE_FAILURE/STORAGE_WRITE_FAILED/SBATCH_SUBMISSION_FAILED/SLURM_UNAVAILABLE 可重试；INVALID_MANIFEST/PERMISSION_DENIED/OUTPUT_INCOMPLETE/TEMPLATE_NOT_ALLOWED/MANIFEST_SCHEMA_INVALID/OUT_OF_MEMORY 不可重试；未知 error_code 默认不可重试）
- [ ] 6.2 实现自动重试逻辑：Orchestrator 检测到 task 失败 → 判断 transient → 未超 max_retries → 按 backoff [60, 300, 900]s 延迟重试；仅重试失败 basin（not 整个 array）
- [ ] 6.3 实现手动重试 handler：`POST /runs/{run_id}/retry` → 并发保护（数据库唯一约束或乐观锁，防止重复重试）→ 创建新 pipeline_job（同 run_id、新 slurm_job_id）→ 重新提交 sbatch
- [ ] 6.4 实现重试审计记录：每次重试写入 pipeline_event（event_type='retry'、details 含 trigger=auto/manual、retry_count、previous_error）
- [ ] 6.5 实现 max retries 耗尽后标记 permanently_failed 并触发告警
- [ ] 6.6 编写重试机制单元测试：transient/non-transient 分类、backoff 计算、max retries 耗尽、并发重试保护（409 Conflict）、auto+manual 冲突处理、审计记录

## 7. 运维监控 API

- [ ] 7.1 实现 `GET /api/v1/pipeline/status`：返回指定 (source, cycle_time) 的 cycle 整体状态（current_state、started_at、updated_at）
- [ ] 7.2 实现 `GET /api/v1/pipeline/stages`：查询 pipeline_job 表按 stage 聚合 → 返回 7 阶段 status（使用上游 §11 display status: pending/running/succeeded/partially_failed/failed/skipped）、duration、basin_progress（对象格式 {completed, total, failed}）、per-basin basin_results 数组
- [ ] 7.3 实现 `GET /api/v1/jobs`：分页 + 多条件过滤（source、cycle_time、status、model_id、stage、run_type、scenario），响应含 log_uri 字段
- [ ] 7.4 实现 `GET /api/v1/jobs/{job_id}/logs`：从 log_uri 读取日志内容
- [ ] 7.5 实现 `POST /api/v1/runs/{run_id}/retry`：校验 operator 角色 → 调用重试服务
- [ ] 7.6 实现 `POST /api/v1/runs/{run_id}/cancel`：校验 operator 角色 → 调用 scancel
- [ ] 7.7 实现 `GET /api/v1/metrics/stage-duration` 和 `GET /api/v1/metrics/success-rate`：按天聚合历史数据
- [ ] 7.8 实现 `GET /api/v1/queue/depth`：调用 `squeue` 获取 running/pending/idle 计数
- [ ] 7.9 实现统一响应包装器：所有端点返回 `{request_id, status, data}` 成功格式和 `{request_id, status, error: {code, message, details}}` 错误格式
- [ ] 7.10 注册新 router 到 `apps/api/main.py`，添加 `/pipeline` 和 `/jobs` 路由前缀
- [ ] 7.11 更新 `openapi/nhms.v1.yaml`：新增全部运维监控端点的 schema 定义
- [ ] 7.12 编写监控 API 集成测试：各端点正常响应、权限拒绝（viewer 不能 retry/cancel）、标准响应包装、per-basin breakdown

## 8. 前端产品监控页

- [ ] 8.1 创建产品监控页面路由和 RBAC 守卫（仅 operator/model_admin/sys_admin 可访问）
- [ ] 8.2 实现 API client 和 TypeScript 类型定义：pipeline stages、jobs、metrics 的请求/响应类型
- [ ] 8.3 实现顶部摘要条：当前周期信息、作业计数（成功绿/失败红/运行蓝/等待灰）、Slurm 队列深度 ECharts 环形图
- [ ] 8.4 实现左侧七阶段流水线卡片：纵向排列 + 箭头连接、状态图标（✓/✗/◉/○）、耗时、流域完成率；下方阶段耗时横向柱状图
- [ ] 8.5 实现失败阶段卡片点击展开：显示 per-basin 失败流域列表（model_id、error_code、error_message）
- [ ] 8.6 实现中间作业列表表格：列（run_id、model_id、run_type、scenario[GFS/IFS/best_available]、status[submitted/running/succeeded/failed/cancelled]、slurm_job_id、submitted_at、duration、操作）；支持按 status/run_type/scenario 过滤，按 submitted_at/duration 排序
- [ ] 8.7 实现日志查看 modal：点击"查看日志"按钮 → 调用 `/jobs/{job_id}/logs` → modal 内展示日志文本
- [ ] 8.8 实现重试/取消操作按钮：调用 retry/cancel API → toast 提示结果 → 刷新列表
- [ ] 8.9 实现右侧趋势面板：近 7 天各阶段平均耗时折线图 + 每周期成功率折线图（ECharts）
- [ ] 8.10 实现 10 秒自动轮询（tab 不可见时暂停 via visibilitychange）和手动刷新按钮；轮询失败时显示错误提示
- [ ] 8.11 实现响应式布局：宽屏三栏、中屏两栏（趋势下移）、窄屏单栏堆叠

## 9. 端到端验收

- [ ] 9.1 编写 E2E 集成测试：mock Slurm 环境下，从 Orchestrator 提交 cycle 编排 → 7 阶段 lazy submit → 状态同步 → pipeline stages API → 前端监控页数据消费的完整链路
- [ ] 9.2 编写 partial success E2E 测试：配置部分流域失败 → 验证 partial 状态转换 → 成功流域继续 → 最终 publish 仅含成功流域
- [ ] 9.3 编写 retry E2E 测试：transient 失败 → 自动重试 → 成功；manual retry → 并发保护 → 审计记录
