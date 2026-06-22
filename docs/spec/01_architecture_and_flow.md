# 01. 系统架构与端到端流程

版本：v0.2  
日期：2026-05-06

## 1. 架构分层

全国水文模拟系统分为四个平面。

```text
1. 业务控制平面：资料源、任务状态机、元数据、API、前端发布。
2. HPC 计算平面：资料下载、forcing 生产、SHUD 运行、结果解析、频率计算。
3. 存储平面：PostGIS、TimescaleDB、对象存储、瓦片存储。
4. 前端展示平面：全国地图、时间轴、图层控制、曲线展示。
```

这种分层的核心价值是让”业务状态”和”重计算作业”解耦：控制平面可以轻量、高可用；HPC 平面可以重计算、可重试、可水平扩展。

### 1.1 架构分层与系统架构图映射

上述四平面是**逻辑视角**的划分，侧重于职责解耦与部署策略。系统架构图（`系统架构图.png`）则采用**六层物理视角**，自顶向下为：前端应用层、服务与接口层、数据与产品存储层、调度与计算层、模型与数据资产层、基础设施层，另有运维与安全作为横切面贯穿全栈。

两套体系的对照映射如下：

| 架构图六层（物理视角） | 四平面（逻辑视角） | 说明 |
|---|---|---|
| 前端应用层 | 前端展示平面 | 地图、时间轴、图层控制等用户交互界面 |
| 服务与接口层（API Gateway、地图服务、瓦片服务等） | 业务控制平面 | 资料源管理、任务状态机、元数据与 API 发布 |
| 数据与产品存储层（PostgreSQL/PostGIS、TimescaleDB、对象存储） | 存储平面 | 时序数据、空间数据、原始/派生产品持久化 |
| 调度与计算层（Slurm、Airflow/Prefect、HPC 计算） | HPC 计算平面 | 资料下载、forcing 生产、SHUD 运行、结果解析 |
| 模型与数据资产层（SHUD 模型库、流域/河网/率定版本） | 业务控制平面 + 存储平面 | 模型注册与版本管理属业务控制，模型文件与数据集属存储 |
| 基础设施层（HPC 集群、网络存储等） | HPC 计算平面（底层支撑） | 为调度与计算层提供硬件资源与网络互联 |
| 运维与安全（横切面） | 贯穿全部四平面 | 监控、日志、告警、权限、审计等跨层关注点 |

阅读本文档时，以四平面为主线理解职责边界；参照架构图时，以六层为主线理解部署拓扑与技术选型。

## 2. 核心组件

| 组件 | 责任 |
|---|---|
| Data Source Registry | 登记资料源、权限状态、变量映射、更新规则。 |
| Cycle Discovery Service | 发现 GFS/IFS/ERA5/CLDAS 等资料周期。 |
| Ingestion Worker | 下载、校验、归档原始资料。 |
| Canonical Converter | 转换为统一变量、单位、网格、时间轴。 |
| Forcing Producer | 生成 SHUD 可读气象代站 forcing。 |
| Model Registry | 管理流域、mesh、河网、率定、SHUD 版本。 |
| State Manager | 管理 analysis run 的 `.cfg.ic` 状态快照。 |
| Slurm Gateway | 提交、查询、取消 Slurm 作业。 |
| SHUD Runtime Adapter | 准备 run workspace 并执行 SHUD。 |
| Output Parser | 解析 `.rivqdown`、`.rivystage` 等输出。 |
| Flood Frequency Engine | 计算频率曲线与预报期重现期。 |
| API Service | 对前端和外部系统提供查询接口。 |
| Tile Publisher | 发布河网、水文预报与气象代站矢量瓦片。 |
| Web Frontend | 地图、曲线、scenario、时间轴交互。 |

## 3. Forecast 流程

```mermaid
sequenceDiagram
  participant DS as Data Source
  participant CD as Cycle Discovery
  participant OR as Orchestrator
  participant SG as Slurm Gateway
  participant HPC as HPC Jobs
  participant DB as Metadata/Timeseries DB
  participant OBJ as Object Storage
  participant FE as Frontend

  DS->>CD: 新周期可用
  CD->>DB: 写入 forecast_cycle
  OR->>SG: 提交 download job
  SG->>HPC: sbatch download
  HPC->>OBJ: 保存 raw data
  HPC->>DB: update status raw_complete
  OR->>SG: 提交 canonical convert job
  HPC->>OBJ: 保存 canonical product
  OR->>SG: 提交 forcing array by basin
  HPC->>OBJ: 保存 forcing package
  OR->>SG: 提交 SHUD forecast array
  HPC->>OBJ: 保存 SHUD outputs
  OR->>SG: 提交 parser/frequency array
  HPC->>DB: 写入 river_timeseries / return_period_result
  OR->>SG: 提交 tile publish job
  HPC->>OBJ: 保存 tiles
  FE->>DB: 查询 latest run
  FE->>OBJ: 拉取 tiles
```

## 4. Analysis 流程

```text
1. 发现可用真实场/再分析 forcing。
2. 对每个流域模型生成 forcing package。
3. 选择上一期 StateSnapshot。
4. 运行 SHUD analysis。
5. 解析水文状态和河段结果。
6. 生成新的 StateSnapshot。
7. 标记该状态可被 forecast run 使用。
```

关键要求：如果 ERA5 存在延迟，最近时段可以用 CLDAS、GDAS、GFS analysis 或 best_available 产品补齐，但必须保留来源标识。

## 5. 状态机

### 5.1 forecast_cycle 状态

```text
discovered
  → downloading
  → raw_complete
  → canonical_ready
  → forcing_ready_partial
  → forcing_ready
  → forecast_running
  → parsed_partial
  → complete
  → published
```

失败分支：`failed_download`、`failed_convert`、`failed_forcing`、`failed_run`、`failed_parse`、`failed_publish`。

### 5.2 hydro_run 状态

```text
created → staged → submitted → running → succeeded → parsed → frequency_done → published
```

异常状态：`failed`、`cancelled`、`superseded`。

### 5.3 状态机与监控 UI 阶段映射

前端产品监控页面将 forecast_cycle 状态机映射为七个流水线阶段卡片。映射关系如下：

| 监控 UI 阶段 | forecast_cycle 状态 | hydro_run 状态 | 失败状态 |
|---|---|---|---|
| 资料下载 | discovered, downloading, raw_complete | — | failed_download |
| 标准化转换 | canonical_ready | — | failed_convert |
| Forcing 生产 | forcing_ready_partial, forcing_ready | — | failed_forcing |
| 模型运行 | forecast_running | created, staged, submitted, running, succeeded | failed_run |
| 输出解析 | parsed_partial | parsed | failed_parse |
| 频率计算 | complete | frequency_done | — |
| 产品发布 | published | published | failed_publish |

映射规则：
- 监控 UI 阶段的状态取所有关联流域的最差状态（任一流域失败则该阶段显示"部分失败"）
- 阶段进度以"已完成流域数/总流域数"展示
- 阶段耗时从该阶段首个作业 submitted_at 到最后一个作业 finished_at
- 点击失败状态阶段卡片展开失败流域列表和错误详情

异常状态展示规则：
- failed_* 状态在对应阶段卡片上标红，显示失败流域数
- cancelled 状态灰色显示
- superseded 状态不在监控 UI 展示（已被新 run 替代）

## 6. Manifest 驱动

所有 HPC 作业都通过 manifest 文件驱动，避免作业依赖数据库连接和 Web 服务。

```json
{
  "run_id": "fcst_gfs_2026043000_yangtze_v12",
  "run_type": "forecast",
  "scenario_id": "forecast_gfs_deterministic",
  "model_id": "yangtze_shud_v12",
  "basin_version_id": "yangtze_v2026_01",
  "cycle_time": "2026-04-30T00:00:00Z",
  "start_time": "2026-04-30T00:00:00Z",
  "end_time": "2026-05-07T00:00:00Z",
  "init_state_uri": "s3://nhms/states/yangtze_shud_v12/2026043000.ic",
  "forcing_uri": "s3://nhms/forcing/gfs/2026043000/yangtze_v2026_01/",
  "output_uri": "s3://nhms/runs/fcst_gfs_2026043000_yangtze_v12/output/",
  "threads": 32
}
```

## 7. 文件流

```text
raw/{source}/{cycle_time}/
canonical/{source}/{cycle_time}/{variable}/
forcing/{source}/{cycle_time}/{basin_version_id}/{model_id}/
models/{model_id}/
states/{model_id}/{valid_time}/
runs/{run_id}/input/
runs/{run_id}/output/
runs/{run_id}/logs/
tiles/hydro/{run_id}/
```

## 8. 可靠性原则

1. 每一步必须可重跑，不依赖临时内存状态。
2. 每个输出对象必须带 checksum 或 etag。
3. Slurm 作业成功不等于产品成功，必须经过结果完整性检查。
4. 不覆盖已有产品；新版本通过 version/status 切换为 active。
5. 前端只读取 `published` 状态的产品。
6. 任何派生产品必须保留 source lineage。
