## Context

M1/M2 阶段使用 MockSlurmGateway（`services/slurm_gateway/mock_backend.py`）模拟作业提交和状态转换，验证了单流域 forecast/analysis 闭环。现在需要切换到真实 Slurm 后端，支持全国 ≥10 个流域并行调度，并提供完整的运维监控能力。

当前 gateway 架构：`SlurmGateway(ABC)` → `MockSlurmGateway`，通过 `create_gateway()` 工厂方法按 `backend` 配置选择后端。`gateway.py:69` 已预留 `backend == "slurm"` 分支但未实现。

关键约束：
- HPC 环境通过 SSH 或专用提交节点访问 Slurm，Web 服务不直接 SSH 到 HPC
- 所有作业必须 manifest 驱动，不依赖数据库连接（设计文档 §6）
- 单流域失败不能阻断其它流域（路线图验收标准）
- sbatch 模板白名单，禁止任意 shell 注入（安全设计 §11）

## Goals / Non-Goals

**Goals:**
- 实现 RealSlurmGateway，通过 subprocess 调用 sbatch/sacct/scancel
- 支持 job array（`--array=0-N%M`）多流域并行提交
- 实现七阶段依赖链的自动编排（`--dependency=afterok`）
- 部分流域失败时 cycle 可进入 partial 状态，成功流域继续下游
- pipeline_job 表持久化作业生命周期
- 运维监控 API 和前端监控页面
- 手动+自动失败重试

**Non-Goals:**
- 不做多集群调度（当前仅支持单个 Slurm 集群）
- 不做实时 WebSocket 推送（MVP 用轮询）
- 不实现 SHUD 运行时本身的变更（复用 M1/M2 的 shud_runtime_adapter）
- 不引入 Airflow/Prefect 等外部编排器（Orchestrator 自行管理依赖链）

## Decisions

### D1: Real Slurm Backend 实现方式

**选择**: subprocess 调用 sbatch/sacct/scancel CLI

**备选方案**:
- PySlurm（Python binding）: 需要编译安装、与 Slurm 版本强耦合
- Slurm REST API（slurmrestd）: 不是所有 HPC 集群都部署了 slurmrestd

**理由**: CLI 是最通用、最可靠的方式，所有 Slurm 集群都支持。通过模板白名单和参数校验确保安全。

### D2: Orchestrator 依赖链编排

**选择**: Lazy submit — Orchestrator 在控制平面逐步提交，每步提交后通过 sacct 轮询等待完成，聚合结果后决定是否提交下一步

**备选方案**:
- 一次性 upfront 提交全部 7 步并用 `--dependency=afterok` 串联: `afterok` 在任一 array task 失败时不会触发后续，无法实现 partial success
- 使用外部编排器（Airflow）: 引入额外运维复杂度

**理由**: Lazy submit 让 Orchestrator 在每步完成后聚合 array task 结果，对成功流域生成新 manifest index 继续下游（partial success），对失败流域触发重试或标记失败。这是实现"单流域失败不阻断其它流域"验收标准的唯一方式。

### D3: Partial Success 状态转换

**选择**: 在现有 forecast_cycle 状态机中增加 `_partial` 后缀状态

**理由**: `forcing_ready_partial` 和 `parsed_partial` 已在设计文档 §5.1 定义，直接复用。Orchestrator 检查 array job 结果：全部成功→正常状态，部分失败→partial 状态，全部失败→failed 状态。

### D4: 状态同步方式

**选择**: MVP 使用轮询（Orchestrator 定期调用 sacct 查询状态）

**备选方案**:
- Callback API（作业结束后调用内部 API）: 需要 HPC 到控制平面的网络通路
- 写 status.json 到对象存储后轮询: 设计文档 §7 推荐方式

**理由**: sacct 轮询最简单，无需额外基础设施。轮询间隔配置化（默认 30s），终态后停止轮询。后续可升级为 callback 方式。

### D5: Resource Profile 配置

**选择**: YAML 配置文件，per-model 覆盖默认值

```yaml
resource_profiles:
  default:
    partition: compute
    nodes: 1
    ntasks: 1
    cpus_per_task: 32
    memory_gb: 128
    walltime: "06:00:00"
    max_concurrent: 4
    shud_threads: 32
  overrides:
    yangtze_shud_v12:
      cpus_per_task: 64
      memory_gb: 256
      walltime: "12:00:00"
      shud_threads: 64
```

**理由**: 不同流域模型规模差异大（河段数从几十到上千），需要灵活配置。YAML 比数据库字段更易维护和版本控制。

### D6: sbatch 模板管理

**选择**: 模板文件放 `infra/sbatch/`，使用 Jinja2 渲染 manifest 变量

**安全措施**: 
- 模板文件路径白名单（只允许 `infra/sbatch/*.sbatch`）
- manifest 字段通过 schema 校验
- 渲染后的命令不包含用户输入的任意 shell

### D7: 前端监控页面技术选择

**选择**: 复用现有前端框架 + ECharts

**理由**: 趋势图和环形图用 ECharts 已有基础（M1 预报曲线），流水线卡片用标准 UI 组件。轮询间隔 10s 刷新。

## Risks / Trade-offs

| 风险 | 缓解 |
|------|------|
| Slurm CLI 输出格式可能因版本差异而不同 | sacct 使用 `--parsable2 --format=` 固定输出格式，单元测试覆盖解析逻辑 |
| 轮询 sacct 对 Slurm 控制节点有压力 | 配置轮询间隔，批量查询（一次 sacct 查多个 job_id），终态后停止 |
| 网络问题导致 sbatch 提交失败 | 提交失败自动重试（最多 3 次），超过后标记 pipeline_job 为 failed |
| array job 部分 task 的状态查询复杂 | sacct 支持 `--array` 参数查询 array task 状态，按 task_id 逐个解析 |
| resource profile 配置错误导致作业被 Slurm 拒绝 | dry-run 模式预先验证；sbatch 失败后记录 Slurm 错误信息 |
| 前端轮询量大时 API 压力 | 后端缓存 pipeline stages 结果（TTL 5s），避免每次请求都查数据库 |

## Open Questions

- Q1: HPC 提交节点是否与 Web 服务在同一网络？如果隔离，需要通过 SSH 隧道或部署 Gateway agent 到提交节点。当前设计假设同网络。
- Q2: sacct 查询频率限制？需要与 HPC 管理员确认 Slurm accounting 数据库的查询压力上限。
