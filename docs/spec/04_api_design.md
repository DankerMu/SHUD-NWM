# 04. API 设计

版本：v0.1  
日期：2026-04-30

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
GET /api/v1/tiles/hydro/{run_id}/{variable}/{valid_time}/{z}/{x}/{y}.pbf
GET /api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf
GET /api/v1/tiles/met/{product_id}/{variable}/{valid_time}/{z}/{x}/{y}.png
```

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
GET /api/v1/jobs/{slurm_job_id}/logs
POST /api/v1/jobs/{run_id}/retry
POST /api/v1/jobs/{run_id}/cancel
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
      "status": "completed",
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
viewer       查看已发布产品
analyst      查看中间结果和质量标识
developer    查看作业日志、重跑任务
operator     触发重跑、切换 active model
admin        管理资料源、权限、系统配置
```

## 11. API 非功能要求

| 指标 | 要求 |
|---|---|
| 河段点击曲线 P95 | < 2 秒，已发布产品。 |
| 图层时间列表 P95 | < 500 ms。 |
| 最新 run 查询 P95 | < 300 ms。 |
| 瓦片响应 P95 | < 1 秒，缓存命中。 |
| API 版本兼容 | v1 发布后不破坏字段，只新增字段。 |
