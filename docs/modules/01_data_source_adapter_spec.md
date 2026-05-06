# 01. 数据源适配器模块：开发 Spec

版本：v0.2  
日期：2026-05-06

## 1. 开发目标

交付可测试、可部署、可观测的 **数据源适配器模块**，满足总体设计中关于数据血缘、版本管理、Slurm/HPC 解耦和前端发布的要求。

## 2. 功能需求

### 2.1 必须实现

- 维护资料源状态 enabled/restricted/planned/deprecated/mock。
- 实现 discover_cycles、build_manifest、download_plan、verify_manifest 等统一接口。
- 维护变量映射、单位映射、周期规则和 latency rule。
- 把权限未解决的数据源以 restricted adapter 方式预留。

### 2.2 应实现

- 支持 dry-run 模式，只生成计划和 manifest，不写正式产物。
- 支持 force-rerun，但必须写审计日志。
- 支持按 run_id/cycle_id/model_id 精确重跑。
- 支持结构化日志和 request_id/job_id 关联。

### 2.3 暂不实现

- 人工编辑生产数据。
- 跳过 QC 直接发布。
- 未经版本管理覆盖历史结果。

## 3. 输入

```text
上游：外部资料源、资料源配置、权限配置。
必要上下文：environment, operator, request_id, trace_id
配置：config/{env}.yaml + secrets manager
```

## 4. 输出

```text
下游：Cycle Discovery、Raw Data Ingestion、Canonical Converter。
状态：created/running/succeeded/failed/published
日志：结构化 JSON lines
元数据：写入相关数据库表
大文件：写入对象存储或 HPC workspace
```

## 5. 数据库/存储影响

- `met.data_source`
- `met.forecast_cycle`
- `ops.adapter_event_log`

实现要求：写数据库必须在事务中完成；大文件写入成功后再更新对象 URI；时序数据写入必须支持 upsert 或先删后写，但禁止产生重复主键；对象存储写入必须记录 checksum/etag。

## 6. 接口

- `GET /api/v1/data-sources`
- `GET /api/v1/data-sources/{source_id}/cycles`
- `内部接口 DataSourceAdapter.discover_cycles()`
- `内部接口 DataSourceAdapter.build_manifest(cycle_time)`

## 7. 配置项

```yaml
data_source_adapter:
  enabled: true
  dry_run: false
  max_retries: 3
  retry_backoff_seconds: [60, 300, 900]
  log_level: INFO
  workspace_root: /work/nhms
  object_store_prefix: s3://nhms
```

### 7b. 每源 Adapter 配置模板

#### GFS

```yaml
GFS:
  account_required: false
  status: enabled
  cycle_hours_utc: [0, 6, 12, 18]
  forecast_hours:
    start: 0
    end: 168
  preferred_channel: aws_open_data
  fallback_channels:
    - nomads
    - ncep_product_server
    - ncei_archive
  poll_interval_minutes: 10
  max_wait_minutes: 180
  mirror_required: true
```

#### IFS Open Data

```yaml
IFS_OPEN_DATA:
  account_required: false
  status: enabled
  license_required: true
  license_note: "ECMWF Open Data ToU / CC-BY-4.0"
  cycle_hours_utc: [0, 6, 12, 18]
  lead_time_policy:
    "00": "0-168h"
    "12": "0-168h"
    "06": "0-144h"
    "18": "0-144h"
  preferred_client: ecmwf-opendata
  preferred_source: ecmwf
  fallback_sources:
    - aws
    - azure
    - google
  mirror_required: true
  max_wait_minutes: 240
```

#### ERA5

```yaml
ERA5:
  account_required: true
  credential_type: cds_api_token
  status: enabled
  latency_days: 5
  product:
    dataset: reanalysis-era5-single-levels
    format: grib
  request_split:
    by: [year, month, variable_group, area]
  retry_policy:
    max_retries: 5
    backoff_minutes: [10, 30, 60, 180, 360]
  mirror_required: true
  era5t_replacement_policy: true
```

#### CLDAS

```yaml
CLDAS:
  account_required: true
  status: restricted
  resolution: "0.0625°"
  temporal: "1h"
  preferred_channel: cma_data_platform
  mirror_required: true
```

### 7c. 下载轮询与完整性检查

周期发现不依赖固定可用时间假设。Adapter 通过轮询确认文件完整性：

```text
1. discover_cycle → 检测是否存在新周期
2. poll file availability → 逐文件检查 f000/f003/.../f168
3. check required variables → 确认 PRCP/TEMP/RH/wind/Rn/Press 存在
4. check file integrity → 文件大小 > 最小阈值、GRIB message count 合理
5. build manifest → 文件列表、变量、时间范围、checksum
6. download → 写入 raw object store
7. verify → checksum/etag 校验
8. mark raw_complete
```

多通道 fallback 策略：主通道失败时自动切换备用通道，按 `fallback_channels` 顺序尝试。同一周期所有文件应尽量从同一通道下载以保证一致性；仅在主通道超时或不可用时切换。

## 8. 测试要求

### 8.1 单元测试

- manifest schema 校验。
- 参数校验。
- 错误码映射。
- 幂等逻辑。

### 8.2 集成测试

- 使用 mock 数据源或小流域样例完成一次端到端调用。
- 验证数据库状态转移。
- 验证对象存储路径和 checksum。
- 验证失败重试和失败终态。

### 8.3 回归测试

- 固定一个历史周期和测试流域，比较输出行数、时间轴、关键统计值。
- 新版本不得破坏已发布 API 字段。

## 9. 性能要求

- 支持按流域/周期并发运行。
- 不在内存中一次性加载全国全部河段时序。
- 大文件以流式处理或分块处理为主。
- 指标和日志写入不应成为主流程瓶颈。

## 10. 安全要求

- 禁止把 token、密码、下载凭证写入日志。
- 所有外部输入必须校验。
- 文件路径必须限制在配置的 workspace/object prefix 内。
- 对外 API 必须执行鉴权和授权。

## 11. 验收清单

- [ ] GFS adapter 可发现指定日期 00/06/12/18 周期。
- [ ] IFS adapter 能表达 00/12 与 06/18 时效差异。
- [ ] CLDAS adapter 在无权限时返回 restricted，不阻断系统启动。
- [ ] 每个 manifest 包含文件列表、变量、时间范围、checksum 或待校验标识。

## 12. Definition of Done

- 代码合并到主分支。
- 单元测试和集成测试通过。
- 文档和配置示例更新。
- 可在 staging 环境完成一次成功运行。
- 指标、日志、错误码可在运维界面或日志系统中查询。
