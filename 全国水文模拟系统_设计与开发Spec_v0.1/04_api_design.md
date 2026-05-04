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
GET /api/v1/river-segments/{segment_id}
GET /api/v1/river-segments/{segment_id}/forecast-series?issue_time=latest&variables=q_down,stage&scenarios=GFS,IFS
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

## 6. 权限策略

```text
viewer       查看已发布产品
analyst      查看中间结果和质量标识
developer    查看作业日志、重跑任务
operator     触发重跑、切换 active model
admin        管理资料源、权限、系统配置
```

## 7. API 非功能要求

| 指标 | 要求 |
|---|---|
| 河段点击曲线 P95 | < 2 秒，已发布产品。 |
| 图层时间列表 P95 | < 500 ms。 |
| 最新 run 查询 P95 | < 300 ms。 |
| 瓦片响应 P95 | < 1 秒，缓存命中。 |
| API 版本兼容 | v1 发布后不破坏字段，只新增字段。 |
