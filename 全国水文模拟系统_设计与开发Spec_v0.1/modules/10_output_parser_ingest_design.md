# 10. SHUD 输出解析与入库模块：模块设计

版本：v0.1  
日期：2026-04-30

## 1. 模块目标

解析 SHUD 原始输出，重点入库河段 `.rivqdown` 和 `.rivystage`，并保留单位转换和质量标识。

## 2. 上下游关系

| 方向 | 内容 |
|---|---|
| 上游 | SHUD Runtime 输出。 |
| 下游 | TimescaleDB、Flood Frequency Engine、API、瓦片服务。 |
| 主要数据表/存储 | `hydro.river_timeseries`, `hydro.hydro_run`, `ops.quality_check` |

## 3. 职责边界

- 解析 CSV 或 DAT 输出。
- 把 `.rivqdown` 从 m3/d 转为 m3/s。
- 检查列数与河段数一致。
- 按 run_id/segment_id/valid_time 入库。
- 支持解析作业单独重跑。

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

- `CLI nhms-parse shud-output --run-id`
- `Slurm job parse_output_array`
- `GET /api/v1/runs/{run_id}/parse-status`

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

- 解析 `.rivqdown` 和 `.rivystage`。
- 同一 run 重跑解析不产生重复行。
- 单位转换准确并保留原始单位信息。
- 异常 NaN/Inf 有 QC 标识。

## 10. 与其它模块的契约

- 输入对象必须处于 ready/published 状态，除非执行的是恢复或调试任务。
- 输出对象必须在 QC 通过后才能进入 ready/published 状态。
- 删除或替换产物必须通过新版本或 superseded 状态表达。
