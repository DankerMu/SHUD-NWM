# 02. 数据产品与时间语义

版本：v0.1  
日期：2026-04-30

## 1. 目标

全国水文模拟系统同时处理再分析资料、近实时实况资料、数值天气预报、SHUD 模型输出和前端可视化产品。不同资料源的发布时间、有效时间、预报时效、可用延迟、原生时间分辨率不同，因此必须统一时间语义。

## 2. 标准时间字段

| 字段 | 定义 | 示例 |
|---|---|---|
| `cycle_time` | 资料起报或发布周期时间。 | `2026-04-30T00:00:00Z` |
| `issue_time` | 系统认为该资料可用于业务的发布时间。 | `2026-04-30T04:20:00Z` |
| `valid_time` | 数据代表的真实有效时间。 | `2026-05-01T06:00:00Z` |
| `lead_time_hours` | `valid_time - cycle_time`。再分析可为 0 或 null。 | `30` |
| `ingest_time` | 系统下载或登记时间。 | `2026-04-30T04:31:12Z` |
| `publish_time` | 产品对前端可见时间。 | `2026-04-30T06:10:00Z` |
| `native_time_resolution` | 数据源原生时间分辨率。 | `PT3H`、`PT1H` |
| `model_output_interval` | SHUD 输出间隔。 | `PT1H`、`PT3H` |

## 3. Data source 状态

```text
enabled      已可生产
restricted   代码支持但权限未开通，例如 CLDAS 初期
planned      计划接入
mock         测试源
deprecated   停用但历史可查
```

## 4. 资料源配置模板

```yaml
source: GFS
status: enabled
provider: NOAA/NCEP
native_format: GRIB2
cycle_hours_utc: [0, 6, 12, 18]
variables:
  temperature_2m: tmp2m
  precipitation: apcp
  relative_humidity: rh2m
  wind_u_10m: u10m
  wind_v_10m: v10m
  pressure_surface: pressfc
  shortwave_down: dswrf
latency_rule: "poll until all required forecast hours exist"
license_note: "public/open data; verify operational terms before production"
```

## 5. Canonical Meteorological Product

```text
canonical_product_id
source
source_version
cycle_time
valid_time
lead_time_hours
variable
unit
grid_id
grid_definition_uri
native_time_resolution
native_spatial_resolution
object_uri
checksum
quality_flag
lineage_json
```

## 6. 标准变量

| 标准变量 | SHUD forcing 对应 | 推荐单位 | 备注 |
|---|---|---|---|
| `prcp_rate_or_amount` | `PRCP` | `mm/day` 或按 SHUD 配置换算 | 累计降水必须转时段量。 |
| `air_temperature_2m` | `TEMP` | `degC` | K 转 ℃。 |
| `relative_humidity_2m` | `RH` | `0-1` | 百分数转 0–1。 |
| `wind_speed_10m` | `wind` | `m/s` | 可由 U/V 分量合成。 |
| `net_radiation` | `Rn` | `W/m2` | 可由短波/长波及地表参数估算。 |
| `surface_pressure` | `Press` | `Pa` | 可缺省，但生产建议尽量提供。 |

## 7. Scenario 语义

| Scenario | 含义 |
|---|---|
| `analysis_true_field` | 真实场/再分析驱动的 analysis 结果。 |
| `forecast_gfs_deterministic` | GFS 确定性预报。 |
| `forecast_ifs_deterministic` | IFS 确定性预报。 |
| `forecast_best_available` | 按优先级拼接或融合后的业务产品。 |
| `forecast_gfs_ifs_compare` | 前端对比展示，不一定需要实体派生表。 |
| `hindcast_replay` | 历史回放或复盘。 |

## 8. Best available 产品规则

`best_available` 不应覆盖 GFS/IFS 原始 scenario，而是一个派生层。每个时间点记录来源：

```json
{
  "valid_time": "2026-05-01T00:00:00Z",
  "variable": "prcp",
  "selected_source": "CLDAS",
  "fallback_order": ["CLDAS", "ERA5", "GFS", "IFS"],
  "source_cycle_time": "2026-04-30T00:00:00Z",
  "quality_flag": "best_available_realtime"
}
```

## 9. 前端时间列表

后端每个图层返回：

```json
{
  "layer_id": "met_prcp_gfs_2026043000",
  "native_time_resolution": "PT3H",
  "valid_times": [
    "2026-04-30T00:00:00Z",
    "2026-04-30T03:00:00Z",
    "2026-04-30T06:00:00Z"
  ]
}
```

前端时间滑块只在 `valid_times[]` 上移动，不自行推断缺失时刻。

## 10. Analysis + Forecast 曲线拼接

```text
past_segment:
  scenario = analysis_true_field
  time_range = [issue_time - 7d, issue_time]

future_segment_gfs:
  scenario = forecast_gfs_deterministic
  time_range = [issue_time, issue_time + 7d]

future_segment_ifs:
  scenario = forecast_ifs_deterministic
  time_range = [issue_time, min(issue_time + 7d, ifs_available_end)]
```

前端必须显示资料来源和起报时间。
