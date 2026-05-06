# 01. 数据源适配器模块：模块设计

版本：v0.2  
日期：2026-05-06

## 1. 模块目标

把 GFS、IFS、ERA5、CLDAS 等异构资料源抽象成统一接口，屏蔽权限、文件格式、发布时间、变量命名和下载方式差异。

## 2. 上下游关系

| 方向 | 内容 |
|---|---|
| 上游 | 外部资料源、资料源配置、权限配置。 |
| 下游 | Cycle Discovery、Raw Data Ingestion、Canonical Converter。 |
| 主要数据表/存储 | `met.data_source`, `met.forecast_cycle`, `ops.adapter_event_log` |

## 3. 职责边界

- 维护资料源状态 enabled/restricted/planned/deprecated/mock。
- 实现 discover_cycles、build_manifest、download_plan、verify_manifest 等统一接口。
- 维护变量映射、单位映射、周期规则和 latency rule。
- 把权限未解决的数据源以 restricted adapter 方式预留。

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

- `GET /api/v1/data-sources`
- `GET /api/v1/data-sources/{source_id}/cycles`
- `内部接口 DataSourceAdapter.discover_cycles()`
- `内部接口 DataSourceAdapter.build_manifest(cycle_time)`

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

- GFS adapter 可发现指定日期 00/06/12/18 周期。
- IFS adapter 能表达 00/12 与 06/18 时效差异。
- CLDAS adapter 在无权限时返回 restricted，不阻断系统启动。
- 每个 manifest 包含文件列表、变量、时间范围、checksum 或待校验标识。

## 10. 与其它模块的契约

- 输入对象必须处于 ready/published 状态，除非执行的是恢复或调试任务。
- 输出对象必须在 QC 通过后才能进入 ready/published 状态。
- 删除或替换产物必须通过新版本或 superseded 状态表达。
