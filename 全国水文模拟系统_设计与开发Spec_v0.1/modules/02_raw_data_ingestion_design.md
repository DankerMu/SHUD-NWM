# 02. 原始数据发现与下载模块：模块设计

版本：v0.1  
日期：2026-04-30

## 1. 模块目标

按资料源周期下载原始 GRIB/NetCDF/其他格式资料，完成完整性校验并归档到对象存储。

## 2. 上下游关系

| 方向 | 内容 |
|---|---|
| 上游 | Data Source Adapter、Cycle Discovery。 |
| 下游 | Canonical Converter、对象存储、元数据库。 |
| 主要数据表/存储 | `met.forecast_cycle`, `met.raw_asset`, `ops.slurm_job`, `ops.pipeline_event` |

## 3. 职责边界

- 根据 manifest 下载文件。
- 支持断点续传、重试和并发下载。
- 校验文件数量、大小、checksum、必要变量。
- 写入 raw_uri、manifest_uri 和下载日志。

## 4. 不负责事项

- 不承担其它模块的数据修复职责。
- 不绕过统一状态机直接发布产品。
- 不覆盖历史版本；任何变更通过新版本或状态切换表达。
- 不在日志中写入密钥、账号、token 等敏感信息。

## 5. 关键设计

### 5.1 Manifest / metadata first

模块输入输出必须先有元数据登记，再有实际文件或时序数据写入。这样可支持失败恢复、重跑和结果追溯。

### 5.2 幂等运行

同一任务重复执行时，如果目标产物已经存在并通过 checksum/QC，模块应返回 already_done，而不是重复写入或覆盖。

### 5.3 质量标识

模块输出必须包含 `quality_flag` 或同等字段。严重质量问题阻断发布；可接受问题保留标记供前端和分析人员识别。

## 6. 主要接口

- `Slurm job download_source_cycle`
- `内部 CLI nhms-ingest download --source --cycle`
- `状态回写 ops.job_status`

## 7. 状态与错误

建议状态：

```text
created
running
succeeded
failed
skipped
superseded
```

建议错误码：

```text
INPUT_NOT_FOUND
INVALID_MANIFEST
PERMISSION_DENIED
OUTPUT_INCOMPLETE
QC_FAILED
STORAGE_WRITE_FAILED
UNKNOWN_ERROR
```

## 8. 观测性

每次执行至少记录：module_name、run_id/cycle_id/forcing_version_id/model_id、input_uri、output_uri、start_time、end_time、duration_seconds、status、error_code、log_uri。

## 9. 验收标准

- 指定 GFS cycle 能下载完整未来 7 天所需文件。
- 下载失败可按文件粒度重试。
- 对象存储路径符合 raw/{source}/{cycle_time}/ 规范。
- 下载完成后 forecast_cycle 状态进入 raw_complete。

## 10. 与其它模块的契约

- 输入对象必须处于 ready/published 状态，除非执行的是恢复或调试任务。
- 输出对象必须在 QC 通过后才能进入 ready/published 状态。
- 删除或替换产物必须通过新版本或 superseded 状态表达。
