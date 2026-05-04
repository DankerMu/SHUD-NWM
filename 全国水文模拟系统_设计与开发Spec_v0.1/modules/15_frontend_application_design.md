# 15. 前端 Web 应用模块：模块设计

版本：v0.1  
日期：2026-04-30

## 1. 模块目标

提供全国地图、图层控制、scenario 对比、时间轴、气象代站曲线和河段预报曲线。

## 2. 上下游关系

| 方向 | 内容 |
|---|---|
| 上游 | API Service、Tile Service。 |
| 下游 | 最终业务用户。 |
| 主要数据表/存储 | `不直接访问数据库` |

## 3. 职责边界

- 地图初始化和底图切换。
- 图层树和时间轴。
- 河段 hover/click。
- 站点 hover/click。
- 曲线展示和阈值线。
- scenario 对比。

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

- `MapLibre sources/layers`
- `API client`
- `Chart components`

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

- 支持地形/影像/矢量底图切换。
- 按图层 valid_times[] 驱动时间轴。
- 点击河段展示 analysis + GFS + IFS 曲线。
- 点击站点展示 forcing 六要素曲线。

## 10. 与其它模块的契约

- 输入对象必须处于 ready/published 状态，除非执行的是恢复或调试任务。
- 输出对象必须在 QC 通过后才能进入 ready/published 状态。
- 删除或替换产物必须通过新版本或 superseded 状态表达。
