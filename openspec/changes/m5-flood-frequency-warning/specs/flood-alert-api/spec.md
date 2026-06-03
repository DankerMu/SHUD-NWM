## ADDED Requirements

### Requirement: 预警概况统计 API

系统 SHALL 提供预警概况统计端点，返回各预警等级的河段数量。

#### Scenario: 正常查询

- **WHEN** 调用 `GET /api/v1/flood-alerts/summary?run_id=fcst_gfs_2026050300_all`
- **THEN** 返回 JSON：
  ```json
  {
    "run_id": "fcst_gfs_2026050300_all",
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

#### Scenario: 指定阈值过滤

- **WHEN** 调用 `GET /api/v1/flood-alerts/summary?run_id=...&threshold=Q10`
- **THEN** alert_counts 仅统计 return_period ≥ 10 的河段（warning 及以上等级）

#### Scenario: run_id 不存在

- **WHEN** 传入不存在的 run_id
- **THEN** 返回 404，error_code = `"RUN_NOT_FOUND"`

#### Scenario: run 未完成频率计算

- **WHEN** 传入 run_id 对应的 hydro_run status 未到达 `"frequency_done"`
- **THEN** 返回 409，error_code = `"FREQUENCY_NOT_COMPUTED"`，message = `"Return period results not yet available for this run"`

#### Scenario: frequency_done 但 0 个河段有可用频率曲线

- **WHEN** 传入 run_id 对应的 hydro_run status = `"frequency_done"` 且所有河段 quality_flag = `"no_usable_frequency_curve"`
- **THEN** 返回 200，alert_counts 全为 0，新增 `unavailable_count` = total_segments
- **AND** 响应包含 `quality_note: "No usable frequency curves available"`

---

### Requirement: 河段排名 API

系统 SHALL 提供按重现期降序排名的河段列表端点。

#### Scenario: 默认 TOP 20

- **WHEN** 调用 `GET /api/v1/flood-alerts/ranking?run_id=...&limit=20`
- **THEN** 返回按 return_period 降序排列的前 20 条河段
- **AND** 每条包含：rank, river_segment_id, basin_version_id, q_value, q_unit, return_period, warning_level, duration

#### Scenario: 按流域过滤

- **WHEN** 调用 `GET /api/v1/flood-alerts/ranking?run_id=...&basin_id=yangtze`
- **THEN** 仅返回该流域下的河段排名

#### Scenario: 分页

- **WHEN** 调用 `GET /api/v1/flood-alerts/ranking?run_id=...&limit=50&offset=50`
- **THEN** 返回第 51-100 名的河段

---

### Requirement: 预警河段筛选 API

系统 SHALL 提供按条件筛选预警河段的端点。

#### Scenario: 按最小重现期筛选

- **WHEN** 调用 `GET /api/v1/flood-alerts/segments?run_id=...&min_return_period=10`
- **THEN** 返回 return_period ≥ 10 的所有河段列表
- **AND** 每条包含：river_segment_id, basin_version_id, q_value, return_period, warning_level, geom_centroid（经纬度）

#### Scenario: 按预警等级筛选

- **WHEN** 调用 `GET /api/v1/flood-alerts/segments?run_id=...&warning_level=high_risk,severe,extreme`
- **THEN** 仅返回 warning_level 在指定列表中的河段

#### Scenario: 按时间步筛选

- **WHEN** 调用 `GET /api/v1/flood-alerts/segments?run_id=...&valid_time=2026-05-04T06:00:00Z`
- **THEN** 返回该时刻的重现期快照（非 max_over_window，而是该时刻的瞬时值）

#### Scenario: 结果为空

- **WHEN** 无河段满足筛选条件
- **THEN** 返回空列表 `{"segments": [], "total": 0}`

---

### Requirement: 单河段预警时间线 API

系统 SHALL 提供单河段的预警时间线端点。

#### Scenario: 正常查询

- **WHEN** 调用 `GET /api/v1/flood-alerts/timeline?run_id=...&segment_id=yangtze_v12_riv_000123`
- **THEN** 返回该河段在 run 覆盖时段内的逐时刻预警信息：
  ```json
  {
    "segment_id": "yangtze_v12_riv_000123",
    "run_id": "...",
    "timeline": [
      {
        "valid_time": "2026-05-04T00:00:00Z",
        "q_value": 1500.0,
        "return_period": 3.2,
        "warning_level": "elevated"
      },
      {
        "valid_time": "2026-05-04T06:00:00Z",
        "q_value": 2800.0,
        "return_period": 18.5,
        "warning_level": "warning"
      }
    ],
    "peak": {
      "valid_time": "2026-05-04T06:00:00Z",
      "q_value": 2800.0,
      "return_period": 18.5,
      "warning_level": "warning"
    },
    "frequency_thresholds": {
      "Q2": 1200, "Q5": 1800, "Q10": 2300,
      "Q20": 2900, "Q50": 3700, "Q100": 4500
    }
  }
  ```

#### Scenario: 河段无频率曲线

- **WHEN** 查询的河段没有频率曲线
- **THEN** timeline 中每条的 return_period = `null`，warning_level = `null`
- **AND** frequency_thresholds = `null`
- **AND** 响应包含 `"quality_note": "No frequency curve available for this segment"`

---

### Requirement: Forecast-series 响应嵌入频率阈值

现有 forecast-series API 响应 SHALL 嵌入频率阈值。

#### Scenario: 频率曲线可用

- **WHEN** 调用 `GET /api/v1/basin-versions/{bv}/river-segments/{seg}/forecast-series`
- **THEN** 响应中包含 `frequency_thresholds` 对象：`{"Q2": 1200, "Q5": 1800, "Q10": 2300, "Q20": 2900, "Q50": 3700, "Q100": 4500}`
- **AND** 前端可用这些阈值在曲线图上绘制水平参考线

#### Scenario: 频率曲线不可用

- **WHEN** 该河段没有频率曲线
- **THEN** `frequency_thresholds` = `null`
