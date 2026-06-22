# 14. 瓦片发布模块：模块设计

版本：v0.2  
日期：2026-05-06

## 1. 模块目标

把全国河网、水文结果、重现期和气象代站发布为可高性能浏览的矢量瓦片或地图数据。

## 2. 上下游关系

| 方向 | 内容 |
|---|---|
| 上游 | PostGIS、TimescaleDB、return_period_result、气象代站清单。 |
| 下游 | 前端地图。 |
| 主要数据表/存储 | `map.tile_layer`, `map.tile_cache`, `hydro.river_timeseries`, `flood.return_period_result` |

## 3. 职责边界

- 发布流域边界和河网矢量瓦片。
- 按 run_id/valid_time 生成水文属性瓦片。
- 本版本洪水重现期地图数据通过 GeoJSON 接口发布；MVT/PBF 需要 PostGIS tile clipping
  和编码能力，作为后续生产级优化。
- 发布气象代站点位 MVT。
- 维护 tile layer index。
- 缓存和失效管理。

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

- `GET /api/v1/tiles/...`
- `GET /api/v1/tiles/flood-return-period?run_id=&duration=&valid_time=&bbox=&return_period=`
  返回 GeoJSON `FeatureCollection`，属性包含 `segment_id`、`value`、`unit`、
  `quality_flag`、`return_period`、`warning_level`。
- `CLI nhms-pipeline publish-tiles --cycle-id <cycle_id>`
- `map.tile_layer` / `map.tile_cache`

### 6.1 Issue #122 release behavior

Forecast M3 发布阶段使用 `nhms-pipeline publish-tiles --cycle-id <cycle_id>`。本版本不生成完整全国
MVT/PBF 金字塔；最小发布产物是洪水重现期 GeoJSON delivery metadata：

- 从 `hydro.hydro_run` + `flood.return_period_result` 中发现指定 cycle 的 `frequency_done` 或
  `published` forecast run。
- 以确定性 `layer_id=flood_return_period_<run_id>` upsert `map.tile_layer`，`tile_format=geojson`，
  `tile_uri_template=/api/v1/tiles/flood-return-period?run_id=<run_id>&duration={duration}&valid_time={valid_time}`。
- 重复执行同一 cycle 必须返回相同 logical layer，不产生重复 `map.tile_layer` 或冲突 cache row。
- 成功后 run 可标记为 `published`；M3 cycle 仍保持既有最终语义：全量成功为 `complete`，上游部分流域成功为
  `parsed_partial`。
- 找不到产品、缺少 `DATABASE_URL` 且对象存储中也没有
  `tiles/hydro/<cycle_id>/flood-return-period/metadata.json` 时，CLI 返回非 0 JSON：
  `status=failed_publish`、稳定 `error_code`/`error_message`。Slurm 模板不吞掉该失败，编排器映射为
  `failed_publish`。

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

- 全国河网不通过全量 GeoJSON 加载。
- 洪水重现期 GeoJSON 是短期发布格式，必须在文档和 OpenAPI 中标注全国尺度性能限制；
  全国尺度高频浏览应升级为按 z/x/y 裁剪、简化和缓存的 MVT/PBF。
- 新 run published 后图层索引可发现。
- 瓦片/地图属性包含 segment_id、value、unit、quality_flag、return_period、warning_level。
- 时间切换不触发全量数据下载。

## 10. 与其它模块的契约

- 输入对象必须处于 ready/published 状态，除非执行的是恢复或调试任务。
- 输出对象必须在 QC 通过后才能进入 ready/published 状态。
- 删除或替换产物必须通过新版本或 superseded 状态表达。
