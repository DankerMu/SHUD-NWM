# 两节点只读展示型 MVP 重构方案

最后更新：2026-05-27  
适用范围：QHH/有限流域水文气象展示 + 运维监控 MVP  
执行目标：22 节点独立生产，27 节点只读消费和异常提示  
建议执行方式：Codex 按阶段拆 PR 实施

## 1. 背景和结论

生产环境迁移后，系统已经从单机 production-like 验证进入两节点部署：

- **22 节点**：计算控制面，负责资料下载、调度、Slurm、SHUD 运行、解析入库和产物发布。
- **27 节点**：展示服务面，负责 FastAPI 查询、前端页面、运行状态展示、日志查看和异常提示。

当前最稳妥的 MVP 生产模式不是“27 前端直接重启 22 任务”，而是：

```text
22 按计划独立运行
  ↓ 写入
PostgreSQL + published artifacts
  ↑ 读取
27 展示状态、曲线、日志和异常
  ↓ 人工通知
运维人员登录 22 排查、修复或重跑
```

本方案将系统重构为 **Compute-produced / Display-consumed** 模式：

- 22 是生产者。
- 27 是消费者。
- DB 是结构化状态账本。
- Published artifacts 是日志、manifest 和文件产物发布区。
- 异常处理走人工运维，不在 MVP 阶段引入自动 `operation_request`。

## 2. 设计原则

### 2.1 必须坚持

1. **27 不直接控制 Slurm**：不安装、不调用、不依赖 Slurm CLI。
2. **27 不运行正式 scheduler**：`nhms-pipeline plan-production` 只在 22 执行。
3. **27 不读取 22 私有工作目录**：只读取 DB 和发布区产物。
4. **27 不用 mock gateway 冒充生产动作**：display 模式下 Slurm 相关 mutation 必须 fail closed。
5. **27 不写 hydro/met/pipeline 终态**：终态来自 22、worker、Slurm/Gateway receipt 和 pipeline persistence。
6. **异常闭环先人工处理**：`/ops` 展示失败、复制诊断信息和处理建议，由运维去 22 执行恢复。
7. **E2E 以同一个 run identity 验证**：`run_id/source/cycle_time/model_id/basin_id` 必须贯穿 22 和 27。

### 2.2 明确不做

MVP 阶段不做：

- 27 前端一键真实 retry/cancel。
- 27 到 22 的 RPC/HTTP 控制通道。
- `operation_request` 自动执行队列。
- 27 直接调用 `/api/v1/slurm/*`。
- 27 读取 22 的 `.nhms-runs/`、`WORKSPACE_ROOT` 或私有 `/scratch`。
- 27 使用写权限修改 `hydro_run`、`pipeline_job`、`forecast_cycle` 等业务终态。

上述能力可作为后续自动化运维阶段再设计。

## 3. 目标架构

```text
┌────────────────────────────────────────────┐
│ 22 节点：Compute Control Plane              │
│                                            │
│ - GFS/IFS cycle discovery/download          │
│ - raw mirror / canonical / forcing          │
│ - nhms-pipeline plan-production             │
│ - Slurm Gateway / sbatch / sacct            │
│ - SHUD runtime / output parser              │
│ - station-series write                      │
│ - q_down write                              │
│ - pipeline/job/stage persistence            │
│ - logs/manifests/display products publish   │
└──────────────────────┬─────────────────────┘
                       │ write
                       ▼
┌────────────────────────────────────────────┐
│ 共享状态与发布层                             │
│                                            │
│ PostgreSQL                                 │
│ - core / met / hydro / flood / map / ops   │
│                                            │
│ Published artifacts                        │
│ - published://logs/...                     │
│ - published://manifests/...                │
│ - published://display-products/...         │
│ - s3://... or shared publish root          │
└──────────────────────▲─────────────────────┘
                       │ read
                       ▼
┌────────────────────────────────────────────┐
│ 27 节点：Display Service Plane              │
│                                            │
│ - FastAPI display/read APIs                 │
│ - /hydro-met                               │
│ - /ops                                     │
│ - latest-product read                       │
│ - station-series read                       │
│ - forecast-series read                      │
│ - pipeline/jobs/logs read                   │
│ - error display / diagnostic copy           │
│ - manual ops guidance                       │
└────────────────────────────────────────────┘
```

## 4. 目标职责表

| 能力 | 22 节点 | 计算节点 | 27 节点 | 说明 |
| --- | --- | --- | --- | --- |
| GFS/IFS 发现下载 | 执行 | 可执行 task | 不执行 | 27 只展示下载状态 |
| `plan-production` | 执行 | 不执行 | 不执行 | 正式调度入口只在 22 |
| Slurm submit/cancel | 执行 | 接收任务 | 不执行 | 27 不需要 Slurm CLI |
| Basins/model assets | 读写/管理 | 读 | 通常不读 | 计算侧 source of truth |
| workspace | 读写 | 读写 | 不读 | 中间态，不是展示契约 |
| PostgreSQL | 读写 | 读写或间接写 | 只读 | 27 只消费状态 |
| published artifacts | 写 | 写 | 读 | 日志、manifest、display product |
| `/hydro-met` | 不提供 | 不提供 | 提供 | 展示入口 |
| `/ops` | 可提供内部诊断 | 不提供 | 提供只读监控 | 不执行真实控制动作 |
| retry/cancel | 人工或 22 控制面执行 | 执行 task | 只显示处理建议 | MVP 不做前端触发 |
| 异常通知 | 写状态/日志 | 写错误 | 展示异常 | 运维人工处理 |

## 5. 当前实现中的冲突点

### 5.1 FastAPI 无条件挂载 Slurm router

当前 `apps/api/main.py` 包含 `slurm_router`，并在应用启动时无条件 `include_router`。这导致 27 节点也可能暴露 `/api/v1/slurm/*` 控制接口。

风险：

- 27 节点未安装 Slurm CLI 时路由不可用。
- 27 若使用默认 mock backend，可能返回“看似成功”的假提交。
- 安全边界不清晰，展示服务面变成控制面。

### 5.2 retry/cancel 在 27 上可能直接执行

当前 pipeline route 中 `POST /api/v1/runs/{run_id}/retry` 和 `POST /api/v1/runs/{run_id}/cancel` 会尝试调用本地 Slurm Gateway。两节点模式下这不符合“27 只读展示”的目标。

风险：

- 27 没有 Slurm 权限，真实 retry/cancel 失败。
- 27 使用 mock gateway，前端误以为已重启。
- 27 需要写 DB 终态，和只读账号冲突。

### 5.3 日志读取偏本地文件路径

当前 `/jobs/{job_id}/logs` 倾向根据 `log_uri` 解析本地路径并读取文件。两节点下，22 写出的日志路径在 27 上通常不可见。

风险：

- 22 上日志存在，27 显示 `JOB_LOG_NOT_FOUND`。
- 为了快速修复而挂载 22 workspace，破坏部署边界。
- 私有路径、token、临时目录可能泄露到前端。

### 5.4 latest-product 对 E2E 不够严格

业务页面可以使用 latest-product，但跨面 E2E 必须验证“22 本轮生产出的 run 被 27 消费”，不能让 27 读取历史旧周期误判通过。

风险：

- 22 本轮失败，但 27 读到历史 latest 成功数据。
- E2E 报告显示展示成功，实际跨面联调未通过。

### 5.5 DB 权限未分层

展示服务面理想状态应使用只读 DB 账号。当前部分 API 路径仍可能执行 mutation，若 27 使用只读账号会失败；若给 27 写权限又扩大风险面。

风险：

- 生产配置在“能跑”和“安全边界”之间摇摆。
- 27 写入终态造成状态来源不可信。

## 6. 重构目标状态

### 6.1 服务角色

新增统一服务角色配置：

```text
NHMS_SERVICE_ROLE=dev_monolith       # 本地开发兼容模式
NHMS_SERVICE_ROLE=compute_control    # 22 计算控制面
NHMS_SERVICE_ROLE=display_readonly   # 27 展示服务面
NHMS_SERVICE_ROLE=slurm_gateway      # 独立 Slurm Gateway，可选
```

推荐行为：

| 角色 | include slurm_router | retry/cancel | DB 权限 | 用途 |
| --- | --- | --- | --- | --- |
| `dev_monolith` | 是 | 可执行 mock/测试 | 开发库读写 | 本地开发和现有测试兼容 |
| `compute_control` | 是或内部可用 | 可执行真实控制 | 控制面读写 | 22 |
| `display_readonly` | 否 | 不执行，只返回人工处理建议 | 只读 | 27 |
| `slurm_gateway` | 是 | 仅 Gateway API | 不直接业务写 | 独立 Gateway |

### 6.2 27 API 行为

27 上保留：

```text
GET /api/v1/mvp/qhh/latest-product
GET /api/v1/met/stations
GET /api/v1/met/stations/{station_id}/series
GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series
GET /api/v1/pipeline/status
GET /api/v1/pipeline/stages
GET /api/v1/jobs
GET /api/v1/jobs/{job_id}/logs
GET /api/v1/metrics/stage-duration
GET /api/v1/metrics/success-rate
GET /api/v1/queue/depth
```

27 上禁用或隐藏：

```text
POST /api/v1/runs/{run_id}/retry
POST /api/v1/runs/{run_id}/cancel
POST /api/v1/slurm/jobs
POST /api/v1/slurm/job-arrays
DELETE /api/v1/slurm/jobs/{job_id}
```

如果外部仍调用 retry/cancel，display 模式返回：

```json
{
  "status": "error",
  "error": {
    "code": "CONTROL_PLANE_MANUAL_ACTION_REQUIRED",
    "message": "Retry/cancel must be executed on the compute control plane for this deployment.",
    "details": {
      "run_id": "...",
      "suggested_action": "Log into node 22 and follow the QHH recovery runbook.",
      "display_mode": "display_readonly"
    }
  }
}
```

## 7. 详细实施阶段

## Phase 0：冻结部署决策和不变量

### 目标

在代码修改前冻结两节点只读展示型 MVP 的边界，避免 Codex 后续把自动控制链路重新加回来。

### 任务

1. 在 `docs/runbooks/two-node-production-e2e-plan.md` 中补一句：MVP 阶段 27 不触发 retry/cancel，只提供诊断信息和人工处理建议。
2. 在 `progress.md` 中新增“two-node readonly display boundary”。
3. 在本方案中标记 `operation_request` 为未来扩展，不进入本轮 MVP。

### 验收

- [ ] 文档明确 27 不控制 Slurm。
- [ ] 文档明确 retry/cancel 由 22 人工或控制面执行。
- [ ] 文档明确 27 只读 DB 和 published artifacts。

## Phase 1：新增服务角色配置

### 目标

让同一套代码在 22、27、本地开发中有不同启动行为。

### 涉及文件

建议新增：

```text
apps/api/runtime_mode.py
```

建议修改：

```text
apps/api/main.py
services/slurm_gateway/config.py
```

### 实现建议

新增枚举：

```python
class ServiceRole(str, Enum):
    DEV_MONOLITH = "dev_monolith"
    COMPUTE_CONTROL = "compute_control"
    DISPLAY_READONLY = "display_readonly"
    SLURM_GATEWAY = "slurm_gateway"
```

新增配置：

```text
NHMS_SERVICE_ROLE
NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS=true
NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS=false
```

`apps/api/main.py` 中改为：

```python
if runtime_mode.include_slurm_router:
    app.include_router(slurm_router)
```

推荐默认值：

- 本地测试默认 `dev_monolith`，避免一次性打爆旧测试。
- 生产部署必须显式设置 `display_readonly` 或 `compute_control`。
- 若 `ENV=production` 但未设置 `NHMS_SERVICE_ROLE`，启动时 fail fast。

### 测试

新增测试：

```text
tests/test_runtime_mode.py
```

覆盖：

- `display_readonly` 不挂载 `/api/v1/slurm/health`。
- `compute_control` 可挂载 Slurm router。
- production 未设置 service role 时失败或返回明确配置错误。
- `display_readonly` 下 Slurm backend 即使为 mock 也不能被 API 控制路径调用。

### 验收命令

```bash
uv run pytest -q tests/test_runtime_mode.py tests/test_api_contract.py
uv run ruff check apps/api services/slurm_gateway tests/test_runtime_mode.py
```

## Phase 2：禁用 27 控制面 mutation

### 目标

`display_readonly` 模式下，27 不能直接 retry/cancel，也不能修改 pipeline/hydro/met 终态。

### 涉及文件

```text
apps/api/routes/pipeline.py
apps/api/errors.py 或新增 apps/api/runtime_errors.py
apps/frontend/src/pages/OpsPage* 或 monitoring/ops 相关组件
openapi/nhms.v1.yaml
```

### 后端实现建议

在 `retry_run` 和 `cancel_run` 开头加入运行模式判断：

```python
if runtime_mode.is_display_readonly:
    raise ApiError(
        status_code=409,
        code="CONTROL_PLANE_MANUAL_ACTION_REQUIRED",
        message="Retry/cancel must be executed on the compute control plane.",
        details={...},
    )
```

注意：

- display 模式下不得写 `pipeline_job`。
- display 模式下不得写 `pipeline_event`。
- display 模式下不得调用 `get_slurm_gateway()`。
- display 模式下不得因 gateway 缺失返回 500。

### 前端实现建议

`/ops` 在 display 模式下：

- 不显示“重启”“取消”执行按钮。
- 显示“复制诊断信息”。
- 显示“查看 22 节点处理步骤”。
- 显示“已通知运维”本地 UI 状态，MVP 不写 DB。

诊断信息至少包含：

```text
source_id
cycle_time
run_id
model_id
stage
job_id
slurm_job_id
status
error_code
error_message
log_uri
suggested_22_commands
```

### 测试

后端：

- `display_readonly` 调 retry 返回 `CONTROL_PLANE_MANUAL_ACTION_REQUIRED`。
- `display_readonly` 调 cancel 返回 `CONTROL_PLANE_MANUAL_ACTION_REQUIRED`。
- 返回前不创建新 job，不更新 run，不调用 gateway。
- `compute_control` 模式保留原 retry/cancel 测试。

前端：

- display 模式下失败 job 显示诊断按钮，不显示重启按钮。
- 复制诊断信息包含 run/cycle/job/log/error。
- 如果用户通过旧按钮或直接 API 触发，UI 显示人工处理提示。

### 验收命令

```bash
uv run pytest -q tests/test_monitoring_api.py tests/test_retry_cancel_consistency.py tests/test_runtime_mode.py
cd apps/frontend && corepack pnpm test -- ops monitoring
cd apps/frontend && corepack pnpm build
```

## Phase 3：发布产物和日志读取重构

### 目标

27 通过 published artifacts 读取日志和 manifest，不读 22 私有工作目录。

### 新增模块

```text
services/artifacts/__init__.py
services/artifacts/config.py
services/artifacts/reader.py
services/artifacts/uri.py
```

### 支持 URI

MVP 阶段建议支持：

```text
published://logs/<run_id>/<job_id>.out
published://manifests/<run_id>/summary.json
s3://<bucket>/<prefix>/logs/<run_id>/<job_id>.out
file://<allowed-publish-root>/logs/<run_id>/<job_id>.out
```

明确禁止：

```text
/workspace/...
.nhms-runs/...
22 私有 /scratch/...
compute node local /tmp/...
带 userinfo/query/fragment 的 URL
包含 ..、反斜杠、编码分隔符的路径
```

### 配置

```text
NHMS_ARTIFACT_BACKEND=local|s3
NHMS_PUBLISHED_ARTIFACT_ROOT=/mnt/nhms-published
NHMS_PUBLISHED_ARTIFACT_URI_PREFIX=published://
S3_ENDPOINT_URL=...
S3_BUCKET_NAME=...
S3_PREFIX=...
NHMS_LOG_TAIL_MAX_BYTES=1048576
```

### 修改点

`apps/api/routes/pipeline.py` 的 `/jobs/{job_id}/logs`：

- 不再直接依赖 `_local_log_path(job.log_uri)`。
- 改为 `ArtifactReader.read_text_tail(job.log_uri)`。
- 返回 redacted/safe `log_uri`。
- 读取失败时返回稳定错误：
  - `JOB_LOG_NOT_PUBLISHED`
  - `JOB_LOG_URI_UNSUPPORTED`
  - `JOB_LOG_ACCESS_DENIED`
  - `JOB_LOG_NOT_FOUND`

### 22 发布要求

22 生产链路必须把日志发布成 27 可读 URI，并写入 `ops.pipeline_job.log_uri`：

```text
published://logs/<source>/<cycle>/<run_id>/<job_id>.out
published://logs/<source>/<cycle>/<run_id>/<job_id>.err
```

如果当前只能写本地 Slurm stdout/stderr，先增加同步步骤：

```text
slurm local log
  -> controlled publish root
  -> DB log_uri = published://...
```

### 测试

新增：

```text
tests/test_artifact_reader.py
tests/test_pipeline_logs_artifacts.py
```

覆盖：

- published URI 正常读取。
- s3 URI mock 读取。
- 超大日志 tail 限制。
- 禁止路径穿越。
- 禁止 22 workspace 私有路径。
- unsupported URI 返回稳定错误。
- details 不泄露本地绝对路径或密钥。

### 验收命令

```bash
uv run pytest -q tests/test_artifact_reader.py tests/test_pipeline_logs_artifacts.py tests/test_monitoring_api.py
uv run ruff check services/artifacts apps/api/routes/pipeline.py tests/test_artifact_reader.py
```

## Phase 4：latest-product 增加严格身份约束

### 目标

业务页面仍可按 `source=GFS` 获取 latest；E2E 可指定本轮 22 产出的 `run_id/cycle_time/model_id`，避免历史旧数据冒充通过。

### 涉及文件

```text
apps/api/routes/forecast.py
packages/common/forecast_store.py
openapi/nhms.v1.yaml
apps/frontend/src/pages/hydroMet/bootstrap.ts
apps/frontend/e2e/hydro-met.spec.ts
```

### API 建议

扩展现有接口：

```http
GET /api/v1/mvp/qhh/latest-product
  ?source=GFS
  &cycle_time=2026-05-27T00:00:00Z
  &run_id=fcst_gfs_2026052700_basins_qhh_shud
  &model_id=basins_qhh_shud
```

语义：

- 无约束参数：返回业务 latest。
- 有约束参数：只允许返回匹配候选。
- 若不匹配：返回 `QHH_LATEST_PRODUCT_UNAVAILABLE`，details 中给出候选和不满足原因。

### 验收规则

- E2E 必须使用 22 本轮 `run_id/cycle_time` 查询。
- 浏览器 E2E 不允许只靠 `source=GFS` 判断跨面成功。
- latest-product response 必须包含：
  - `run_id`
  - `source_id`
  - `cycle_time`
  - `model_id`
  - `basin_version_id`
  - `river_network_version_id`
  - `forcing_version_id`
  - `station_count`
  - `segment_count`
  - `display_start_time`
  - `display_end_time`
  - unavailable reasons if any

### 测试

新增/更新：

```text
tests/test_forecast_api.py
tests/test_api_contract.py
apps/frontend/src/pages/hydroMet/__tests__/bootstrap.test.ts
```

覆盖：

- 无约束 latest 正常。
- 指定 run_id 正常。
- 指定 cycle_time 正常。
- run_id/source 不匹配返回 unavailable。
- 历史旧 run 不满足 E2E expected identity。
- IFS 144h horizon 仍保留。

## Phase 5：27 `/ops` 改为只读监控和人工运维提示

### 目标

`/ops` 不再表达“我可以直接控制任务”，而是表达“我可以发现问题并把诊断信息交给运维”。

### UI 调整

保留：

- source/cycle selector
- stage cards
- stage progress
- jobs table
- log modal
- queue depth
- success/duration metrics
- error_code/error_message
- product readiness
- latest-product readiness

替换：

| 原功能 | 新功能 |
| --- | --- |
| 重启按钮 | 复制诊断信息 |
| 取消按钮 | 查看 22 节点处理建议 |
| retry 成功 toast | 已复制/请通知运维 |
| retry job 状态追踪 | 等待 22 处理后自动刷新 DB 状态 |

### 诊断信息模板

```text
【QHH MVP 运维诊断】
source: <source_id>
cycle_time: <cycle_time>
run_id: <run_id>
model_id: <model_id>
stage: <stage>
job_id: <job_id>
slurm_job_id: <slurm_job_id>
status: <status>
error_code: <error_code>
error_message: <error_message>
log_uri: <log_uri>

建议在 22 节点检查：
1. 查看 scheduler evidence：<path or query>
2. 查看 Slurm：squeue/sacct -j <slurm_job_id>
3. 查看发布日志：<published log uri>
4. 判断是否需要手工重跑 plan-production 或单独恢复该周期。
```

### 测试

前端覆盖：

- display mode 下失败任务无 retry/cancel 按钮。
- 诊断信息包含关键字段。
- 日志可打开或显示 published log missing 原因。
- viewer/operator/sys_admin 的差异仅影响可见信息，不触发真实控制动作。
- `/ops` 不依赖 `/api/v1/slurm/*`。

## Phase 6：22 手工运维 runbook

### 目标

27 只提示，运维需要有明确的 22 处理手册。

### 新增文档

建议新增：

```text
docs/runbooks/qhh-22-manual-recovery.md
```

内容包括：

1. 如何按 run_id/source/cycle 查 scheduler evidence。
2. 如何查 Slurm job：`squeue`、`sacct`、stdout/stderr。
3. 如何查 DB 状态：`forecast_cycle`、`hydro_run`、`pipeline_job`。
4. 如何判断下载失败、forcing 失败、SHUD 失败、parse 失败、publish 失败。
5. 如何安全重跑：
   - 重跑整个 cycle。
   - 只补 publish/log sync。
   - 跳过已成功阶段。
6. 如何记录人工处理结果。
7. 哪些操作禁止做：
   - 删除成功 run。
   - 覆盖已发布产物。
   - 在 27 上执行 Slurm。

### 与 27 集成

`/ops` 的“查看处理建议”链接到该 runbook，或者展示 runbook 摘要。

## Phase 7：DB 权限分层

### 目标

生产部署中 27 用只读账号启动，避免展示面误写业务终态。

### 推荐账号

```text
nhms_control_rw     # 22 scheduler/control plane
nhms_worker_rw      # 计算节点 worker，或合并到 control_rw
nhms_display_ro     # 27 API/frontend display
nhms_admin          # migration/admin only
```

### 27 只读要求

27 需要 SELECT 权限：

```text
core.*
met.*
hydro.*
flood.*
map.*
ops.pipeline_job
ops.pipeline_event
ops.audit_log 只读，如需要
```

27 不需要写：

```text
met.forecast_cycle
met.forcing_version
met.forcing_station_timeseries
hydro.hydro_run
hydro.river_timeseries
ops.pipeline_job
```

### 测试

新增 opt-in integration：

```text
tests/test_display_readonly_db_role.py
```

覆盖：

- display API 用 readonly DB 可以完成 `/hydro-met` 所需查询。
- display API 用 readonly DB 可以完成 `/ops` 所需查询。
- retry/cancel 在 display mode 下不会尝试写 DB。
- 任何隐藏 mutation 被调用时返回 display-readonly error，不触发 DB write。

## Phase 8：两节点 E2E 更新

### 目标

E2E 结果能准确判断：22 生产通过、27 展示通过、跨面联调通过。

### 修改文档

更新：

```text
docs/runbooks/two-node-production-e2e-plan.md
docs/runbooks/qhh-mvp-production-like-e2e-checklist.md
docs/runbooks/qhh-mvp-smoke-evidence.md
progress.md
```

### E2E 必须记录

```text
compute_control_plane: PASS | PARTIAL | FAIL | BLOCKED
display_service_plane: PASS | PARTIAL | FAIL | BLOCKED
cross_plane_e2e: PASS | PARTIAL | FAIL | BLOCKED
manual_ops_boundary: PASS | FAIL
final_production_readiness_claimed: false
```

### E2E 强制身份

```text
source: GFS 或 IFS
cycle_time: <22 本轮生产 cycle>
model_id: basins_qhh_shud
basin_id: basins_qhh
run_id: <22 pipeline run_id>
forcing_version_id: <22 forcing_version_id>
```

27 浏览器 E2E 必须使用这些 ID 验证，不得只用 historical latest。

## 8. Codex 执行拆分建议

建议不要一次性大改，按下面 PR 顺序执行。

### PR 1：Service role 和 Slurm router 条件挂载

目标：让 27 display mode 不暴露 `/api/v1/slurm/*`。

文件：

```text
apps/api/runtime_mode.py
apps/api/main.py
tests/test_runtime_mode.py
docs/runbooks/two-node-production-e2e-plan.md
```

验收：

```bash
NHMS_SERVICE_ROLE=display_readonly uv run pytest -q tests/test_runtime_mode.py
uv run pytest -q tests/test_api_contract.py tests/test_openapi_drift.py
```

### PR 2：display mode 禁用 retry/cancel 控制动作

目标：27 不直接执行 retry/cancel。

文件：

```text
apps/api/routes/pipeline.py
tests/test_monitoring_api.py
tests/test_retry_cancel_consistency.py
openapi/nhms.v1.yaml
```

验收：

```bash
uv run pytest -q tests/test_monitoring_api.py tests/test_retry_cancel_consistency.py tests/test_runtime_mode.py
```

### PR 3：ArtifactReader 和 published log 读取

目标：27 从发布区读取日志，不读 22 私有路径。

文件：

```text
services/artifacts/*
apps/api/routes/pipeline.py
tests/test_artifact_reader.py
tests/test_pipeline_logs_artifacts.py
```

验收：

```bash
uv run pytest -q tests/test_artifact_reader.py tests/test_pipeline_logs_artifacts.py tests/test_monitoring_api.py
```

### PR 4：latest-product E2E identity filters

目标：跨面 E2E 锁定 22 本轮 run。

文件：

```text
apps/api/routes/forecast.py
packages/common/forecast_store.py
openapi/nhms.v1.yaml
apps/frontend/src/pages/hydroMet/bootstrap.ts
tests/test_forecast_api.py
apps/frontend/src/pages/hydroMet/__tests__/bootstrap.test.ts
```

验收：

```bash
uv run pytest -q tests/test_forecast_api.py tests/test_api_contract.py
cd apps/frontend && corepack pnpm run check:api-types
cd apps/frontend && corepack pnpm test -- hydroMet
```

### PR 5：`/ops` 只读监控 UI

目标：替换 retry/cancel 按钮为诊断信息和人工处理建议。

文件：

```text
apps/frontend/src/pages/OpsPage*
apps/frontend/src/pages/MonitoringPage*
apps/frontend/e2e/monitoring.spec.ts
apps/frontend/src/**/__tests__/*ops*
```

验收：

```bash
cd apps/frontend
corepack pnpm test
corepack pnpm build
corepack pnpm test:e2e -- monitoring.spec.ts --project=mocked-regression-chromium --workers=1
```

注意：如果该 E2E 仍使用 mock API，证据必须标注 mocked regression，不得当成 live。

### PR 6：22 人工恢复 runbook 和两节点 E2E 更新

目标：让运维能按照 27 诊断信息在 22 处理问题。

文件：

```text
docs/runbooks/qhh-22-manual-recovery.md
docs/runbooks/two-node-production-e2e-plan.md
docs/runbooks/qhh-mvp-production-like-e2e-checklist.md
progress.md
```

验收：

```bash
npx --yes markdownlint-cli2 docs/runbooks/qhh-22-manual-recovery.md docs/runbooks/two-node-production-e2e-plan.md progress.md
```

### PR 7：Display readonly DB role 验证

目标：证明 27 用只读 DB 账号也能展示 `/hydro-met` 和 `/ops`。

文件：

```text
db/docs or docs/runbooks/display-readonly-db-role.md
tests/test_display_readonly_db_role.py
```

验收：

```bash
NHMS_RUN_INTEGRATION=1 NHMS_DISPLAY_READONLY_DATABASE_URL=... uv run pytest -q tests/test_display_readonly_db_role.py
```

## 9. 发布和回滚策略

### 发布步骤

1. 先在本地 `dev_monolith` 保持原测试通过。
2. 在 27 测试 `display_readonly`，确认 `/api/v1/slurm/*` 不可见。
3. 在 27 使用只读 DB 跑 `/hydro-met` 和 `/ops` 查询。
4. 在 22 跑 `compute_control`，确认 scheduler/Slurm 继续可用。
5. 跑 two-node E2E，锁定同一个 `run_id/source/cycle/model_id`。
6. 更新 evidence summary，明确 `final_production_readiness_claimed=false`。

### 回滚策略

每个 PR 都应可单独回滚：

- PR 1 回滚：恢复单体 API 路由。
- PR 2 回滚：恢复 retry/cancel 行为。
- PR 3 回滚：恢复本地日志读取。
- PR 4 回滚：恢复 latest-only 查询。
- PR 5 回滚：恢复原 `/ops` UI。

但生产环境推荐不要回滚到“27 可直接控制 Slurm”的状态。若必须临时恢复，应只在受控内网和明确 service role 下启用。

## 10. 验收总清单

### 10.1 27 display_readonly 通过条件

- [ ] 27 启动时 `NHMS_SERVICE_ROLE=display_readonly`。
- [ ] `/api/v1/slurm/*` 不可用或未注册。
- [ ] `/api/v1/runs/{run_id}/retry` 返回人工处理提示，不调用 gateway。
- [ ] `/api/v1/runs/{run_id}/cancel` 返回人工处理提示，不调用 gateway。
- [ ] `/hydro-met` 可显示 station series 和 q_down。
- [ ] `/ops` 可显示 stages、jobs、logs 和 error。
- [ ] `/ops` 不显示真实重启/取消按钮。
- [ ] 日志来自 published artifacts。
- [ ] 27 可以使用只读 DB 账号。

### 10.2 22 compute_control 通过条件

- [ ] 22 启动时 `NHMS_SERVICE_ROLE=compute_control` 或等价 CLI 环境。
- [ ] `nhms-pipeline plan-production --plan` 可执行。
- [ ] Slurm/Gateway 可提交真实任务。
- [ ] 计算节点能读 Basins/workspace/object-store。
- [ ] 结果写入 DB。
- [ ] 日志和 manifest 发布到 27 可读位置。
- [ ] 失败任务由 22 人工或控制面恢复。

### 10.3 跨面 E2E 通过条件

- [ ] 22 和 27 证据使用同一个 `run_id/source/cycle_time/model_id/basin_id`。
- [ ] 27 没有使用历史旧 cycle 冒充本轮产物。
- [ ] 27 没有通过 Slurm/mock gateway 执行控制动作。
- [ ] 27 能读本轮 published logs。
- [ ] 失败项能归因到计算控制面、产物发布面、DB/API 合同面或前端展示面。

## 11. 未来扩展：自动 operation_request

本轮不实现。但如果后续需要前端一键重启，可以引入：

```text
ops.operation_request
```

设计方向：

```text
27 写 operation_request
22 control worker 读取 pending request
22 执行 retry/cancel
22 写 operation_result 和 pipeline event
27 展示结果
```

该扩展必须满足：

- 27 仍不直接调用 Slurm。
- 22 是唯一执行者。
- request/result 有 audit。
- 请求可取消、可超时、可幂等。
- 所有执行结果绑定 Slurm/Gateway receipt。

这属于后续自动化运维阶段，不进入当前两节点只读展示型 MVP 重构。

## 12. 给 Codex 的执行提示

每个 PR 执行前，先回答三个问题：

1. 这个改动是否会让 27 直接控制 22 或 Slurm？如果会，停止。
2. 这个改动是否让 27 依赖 22 私有路径？如果会，停止。
3. 这个改动是否可能把 mock/deterministic 证据当成 live？如果会，停止。

每个 PR 合并前，必须在 PR 描述中填写：

```text
Service role affected:
  - dev_monolith:
  - compute_control:
  - display_readonly:

DB write surface:
  - added:
  - removed:
  - unchanged:

Artifact/log surface:
  - URI types:
  - redaction:
  - max bytes:

E2E claim boundary:
  - deterministic:
  - mocked:
  - production-like:
  - live:
```

最终目标不是“让 27 也能控制任务”，而是让 27 在生产环境中稳定、可信、只读地展示 22 已生产的结果，并把异常清楚地交给运维人员处理。
