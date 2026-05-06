# 09. SHUD Runtime Adapter 模块：模块设计

版本：v0.2  
日期：2026-05-06

## 1. 模块目标

在 HPC workspace 中准备 SHUD 输入文件、执行 SHUD/shud_omp、收集输出和日志。

## 2. 上下游关系

| 方向 | 内容 |
|---|---|
| 上游 | run_manifest、模型包、forcing package、state snapshot。 |
| 下游 | SHUD 原始输出、Output Parser。 |
| 主要数据表/存储 | `hydro.hydro_run`, `ops.quality_check` |

## 3. 职责边界

- 拉取模型包、forcing、`.cfg.ic`。
- 生成或修改 `.cfg.para`。
- 执行 shud/shud_omp。
- 检查输出文件完整性。
- 上传 output/logs 到对象存储。

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

- `CLI nhms-shud-runtime execute --manifest`
- `Slurm batch template run_shud_forecast.sbatch`

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

- 执行命令、参数、线程数写入日志。
- run_workspace 可复现。
- SHUD 非零退出时保留 stdout/stderr。
- 输出完整性检查通过才标记 succeeded。

## 10. 与其它模块的契约

- 输入对象必须处于 ready/published 状态，除非执行的是恢复或调试任务。
- 输出对象必须在 QC 通过后才能进入 ready/published 状态。
- 删除或替换产物必须通过新版本或 superseded 状态表达。
