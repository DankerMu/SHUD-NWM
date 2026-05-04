# 02. 原始数据发现与下载模块：开发 Spec

版本：v0.1  
日期：2026-04-30

## 1. 开发目标

交付可测试、可部署、可观测的 **原始数据发现与下载模块**，满足总体设计中关于数据血缘、版本管理、Slurm/HPC 解耦和前端发布的要求。

## 2. 功能需求

### 2.1 必须实现

- 根据 manifest 下载文件。
- 支持断点续传、重试和并发下载。
- 校验文件数量、大小、checksum、必要变量。
- 写入 raw_uri、manifest_uri 和下载日志。

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
上游：Data Source Adapter、Cycle Discovery。
必要上下文：environment, operator, request_id, trace_id
配置：config/{env}.yaml + secrets manager
```

## 4. 输出

```text
下游：Canonical Converter、对象存储、元数据库。
状态：created/running/succeeded/failed/published
日志：结构化 JSON lines
元数据：写入相关数据库表
大文件：写入对象存储或 HPC workspace
```

## 5. 数据库/存储影响

- `met.forecast_cycle`
- `met.raw_asset`
- `ops.slurm_job`
- `ops.pipeline_event`

实现要求：写数据库必须在事务中完成；大文件写入成功后再更新对象 URI；时序数据写入必须支持 upsert 或先删后写，但禁止产生重复主键；对象存储写入必须记录 checksum/etag。

## 6. 接口

- `Slurm job download_source_cycle`
- `内部 CLI nhms-ingest download --source --cycle`
- `状态回写 ops.job_status`

## 7. 配置项

```yaml
raw_data_ingestion:
  enabled: true
  dry_run: false
  max_retries: 3
  retry_backoff_seconds: [60, 300, 900]
  log_level: INFO
  workspace_root: /work/nhms
  object_store_prefix: s3://nhms
```

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

- [ ] 指定 GFS cycle 能下载完整未来 7 天所需文件。
- [ ] 下载失败可按文件粒度重试。
- [ ] 对象存储路径符合 raw/{source}/{cycle_time}/ 规范。
- [ ] 下载完成后 forecast_cycle 状态进入 raw_complete。

## 12. Definition of Done

- 代码合并到主分支。
- 单元测试和集成测试通过。
- 文档和配置示例更新。
- 可在 staging 环境完成一次成功运行。
- 指标、日志、错误码可在运维界面或日志系统中查询。
