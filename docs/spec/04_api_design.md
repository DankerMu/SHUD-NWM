# 04. API 设计

版本：v0.2  
日期：2026-05-06

## 1. API 风格

建议后端业务 API 使用 REST + JSON。高频瓦片走专门 tile endpoint，曲线和元数据走业务 API。接口统一版本前缀：

```text
/api/v1/...
```

所有时间字段使用 ISO 8601 UTC。

## 2. 通用响应结构

```json
{
  "request_id": "req_20260430_abcdef",
  "status": "ok",
  "data": {}
}
```

错误结构：

```json
{
  "request_id": "req_20260430_abcdef",
  "status": "error",
  "error": {
    "code": "RUN_NOT_PUBLISHED",
    "message": "Requested run is not published",
    "details": {}
  }
}
```

## 3. 核心接口

```http
GET /api/v1/basins
GET /api/v1/basins/{basin_id}/versions
GET /api/v1/models?basin_version_id=&active=true
GET /api/v1/data-sources
GET /api/v1/data-sources/{source_id}/cycles?from=&to=&status=
GET /api/v1/layers
GET /api/v1/layers/{layer_id}/valid-times
GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}
GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series?issue_time=latest&variables=q_down,stage&scenarios=GFS,IFS
GET /api/v1/met/stations?basin_version_id=&model_id=
# 当传入 model_id 时，返回该 model 通过 interp_weight 实际使用的代站集合；
# met_station 表本身不含 model_id 字段，关联通过 interp_weight 表实现。
GET /api/v1/met/stations/{station_id}/series?forcing_version_id=&variables=PRCP,TEMP,RH,wind,Rn,Press
GET /api/v1/runs/{run_id}
GET /api/v1/runs?basin_id=&source=&cycle_time=&status=
GET /api/v1/pipeline/status?source=&cycle_time=
```

## 4. 河段预报曲线响应

```json
{
  "segment_id": "yangtze_v12_riv_000123",
  "issue_time": "2026-04-30T00:00:00Z",
  "unit": "m3/s",
  "series": [
    {
      "scenario_id": "analysis_true_field",
      "segment_role": "past_7_days",
      "points": [["2026-04-29T00:00:00Z", 920.1]]
    },
    {
      "scenario_id": "forecast_gfs_deterministic",
      "segment_role": "future_7_days",
      "points": [["2026-05-01T00:00:00Z", 1100.2]]
    }
  ],
  "frequency_thresholds": {
    "Q2": 1200,
    "Q5": 1800,
    "Q10": 2300,
    "Q20": 2900,
    "Q50": 3700,
    "Q100": 4500
  }
}
```

## 5. 瓦片接口

```http
GET /api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf
GET /api/v1/tiles/hydro-national/{variable}/{valid_time}/{z}/{x}/{y}.pbf  # canonical discharge layer URL (per PR #602)
GET /api/v1/tiles/hydro/{run_id}/{variable}/{valid_time}/{z}/{x}/{y}.pbf  # direct-deeplink only; not surfaced via /api/v1/layers discharge entry — see openspec/specs/mvt-tile-contract/spec.md
GET /api/v1/tiles/flood-return-period?run_id=&duration=1h&valid_time=&bbox=&return_period=
GET /api/v1/tiles/met/{product_id}/{variable}/{valid_time}/{z}/{x}/{y}.png
```

洪水重现期地图数据本版本发布为 GeoJSON，而不是 MVT/PBF。原因是 `.pbf`
瓦片需要 PostGIS tile clipping 和 MVT 编码能力，本阶段不把该实现作为发布范围。
`/api/v1/tiles/flood-return-period` 返回 `application/json` 的 GeoJSON
`FeatureCollection`，前端以 MapLibre `geojson` source 加载。旧的
`/api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf`
路径仅作为兼容入口重定向到 GeoJSON 查询接口，不表达真实 z/x/y 瓦片语义。

洪水重现期 GeoJSON feature properties：

| 字段 | 类型 | 说明 |
|---|---|---|
| `segment_id` | string | 河段标识。 |
| `value` | number | 用于展示的流量值。 |
| `unit` | string | 流量单位，例如 `m³/s` 或 `m3/s`。 |
| `quality_flag` | string | 数据质量标识。 |
| `return_period` | number | 重现期年数；无可用曲线时为 `0`。 |
| `warning_level` | string | 告警等级，例如 `normal`、`warning`、`danger`；无可用曲线时为 `unavailable`。 |

GeoJSON 在全国尺度会产生较大的单次响应，适合本阶段的功能验证和受限范围浏览。
生产级全国无级缩放仍应优先使用 MVT/PBF，并按 z/x/y 进行裁剪、简化和缓存。
瓦片发布缓存表以迁移文件为准，命名为 `map.tile_cache`。

## 6. 模型资产管理接口

```http
GET /api/v1/models/{model_id}
GET /api/v1/models/{model_id}/versions
GET /api/v1/models/{model_id}/states?limit=&offset=
GET /api/v1/models/{model_id}/flood-frequency-curves
GET /api/v1/basin-versions/{basin_version_id}/river-network-versions
PUT /api/v1/models/{model_id}/active
```

model 详情响应示例：

```json
{
  "model_id": "yangtze_shud_v12",
  "basin_version_id": "yangtze_v2026_01",
  "river_network_version_id": "yangtze_rivnet_v2026_01",
  "mesh_version_id": "yangtze_mesh_v2026_01",
  "calibration_version_id": "yangtze_calib_v5",
  "shud_code_version": "2.0.1",
  "active_flag": true,
  "river_segment_count": 1248,
  "node_count": 2865,
  "basin_area_km2": 186500,
  "created_at": "2026-03-15T10:00:00Z"
}
```

## 7. 运维监控接口

```http
GET /api/v1/pipeline/status?source=&cycle_time=
GET /api/v1/pipeline/stages?source=&cycle_time=
GET /api/v1/jobs?source=&cycle_time=&status=&model_id=&limit=&offset=
GET /api/v1/jobs/{job_id}/logs
POST /api/v1/runs/{run_id}/retry
POST /api/v1/runs/{run_id}/cancel
GET /api/v1/metrics/stage-duration?source=&days=7
GET /api/v1/metrics/success-rate?source=&days=7
GET /api/v1/queue/depth
```

pipeline stages 响应示例：

```json
{
  "source": "GFS",
  "cycle_time": "2026-05-03T00:00:00Z",
  "stages": [
    {
      "stage": "download",
      "status": "succeeded",
      "started_at": "2026-05-03T04:20:00Z",
      "finished_at": "2026-05-03T04:35:00Z",
      "duration_seconds": 900,
      "basin_progress": "30/30"
    },
    {
      "stage": "shud_forecast",
      "status": "running",
      "started_at": "2026-05-03T05:10:00Z",
      "finished_at": null,
      "duration_seconds": null,
      "basin_progress": "18/30"
    }
  ]
}
```

## 8. 洪水预警聚合接口

```http
GET /api/v1/flood-alerts/summary?run_id=&threshold=Q5
GET /api/v1/flood-alerts/ranking?run_id=&limit=20
GET /api/v1/flood-alerts/segments?run_id=&min_return_period=5&valid_time=
GET /api/v1/flood-alerts/timeline?run_id=&segment_id=
```

alerts summary 响应示例：

```json
{
  "run_id": "fcst_gfs_2026050300_all",
  "threshold": "Q5",
  "total_segments": 12500,
  "alert_counts": {
    "normal": 12200,
    "elevated": 180,
    "watch": 72,
    "warning": 31,
    "high_risk": 12,
    "severe": 4,
    "extreme": 1
  },
  "updated_at": "2026-05-03T08:00:00Z"
}
```

## 9. 数据血缘接口

系统核心卖点之一是可追溯。以下接口支持从任意前端曲线点反查完整数据链路，满足路线图"任意曲线点可追溯到 run_id、forcing_version、source cycle"的验收要求。

```http
GET /api/v1/lineage/river-point?run_id=&segment_id=&valid_time=&variable=
GET /api/v1/lineage/forcing-point?forcing_version_id=&station_id=&valid_time=&variable=
GET /api/v1/lineage/product/{product_id}
```

river-point 响应示例：

```json
{
  "segment_id": "yangtze_v12_riv_000123",
  "valid_time": "2026-05-01T06:00:00Z",
  "variable": "q_down",
  "lineage": {
    "run_id": "fcst_gfs_2026043000_yangtze_v12",
    "model_id": "yangtze_shud_v12",
    "init_state_id": "state_yangtze_v12_2026042918",
    "forcing_version_id": "forc_gfs_2026043000_yangtze_v12",
    "source_id": "GFS",
    "cycle_time": "2026-04-30T00:00:00Z",
    "canonical_product_ids": ["can_gfs_2026043000_prcp_030", "can_gfs_2026043000_temp_030"],
    "parser_job_id": "parse_fcst_gfs_2026043000_yangtze_v12",
    "qc_result_ids": ["qc_001", "qc_002"],
    "published_layer_id": "hydro_q_fcst_gfs_2026043000"
  }
}
```

## 10. 权限策略

```text
viewer       查看已发布地图和曲线
analyst      查看 QC 标识、历史版本、下载结果
operator     触发重跑、取消作业、重新发布产品
model_admin  注册模型版本、切换 active model
sys_admin    管理数据源、用户、系统配置
developer    开发/调试环境角色，可看日志、Mock、debug 接口（生产默认不可分配）
```

## 11. Pipeline 阶段展示状态

API 返回的 pipeline stages `status` 字段使用**前端展示状态**，与数据库 ENUM 状态（`hydro.run_status`、`met.cycle_status`）区分：

```text
pending             尚未开始
running             执行中
succeeded           全部成功
partially_failed    部分流域失败
failed              全部失败
skipped             跳过（如 IFS 未接入时）
```

> 当前 `04_api_design.md` 是设计性接口清单，不作为代码生成源。OpenAPI 完整契约（含 `components.schemas`、`parameters`、`responses`、`securitySchemes`）属于 M0 工程初始化交付物，将在 `openapi/nhms.v1.yaml` 中实现。

## 12. API 非功能要求

| 指标 | 要求 |
|---|---|
| 河段点击曲线 P95 | < 2 秒，已发布产品。 |
| 图层时间列表 P95 | < 500 ms。 |
| 最新 run 查询 P95 | < 300 ms。 |
| 瓦片响应 P95 | < 1 秒，缓存命中。 |
| API 版本兼容 | v1 发布后不破坏字段，只新增字段。 |
