# analysis-forecast-curve-splicing Specification

## Purpose
TBD - created by archiving change m2-analysis-warm-start. Update Purpose after archive.
## Requirements
### Requirement: API Spliced Curve Response
系统 SHALL 在 forecast-series 接口中返回 analysis + forecast 两段数据，支持前端拼接展示。

#### Scenario: Full spliced response
- **WHEN** 请求 GET /api/v1/basin-versions/{bv}/river-segments/{seg}/forecast-series?issue_time=latest&include_analysis=true
- **THEN** 返回 series 数组包含两个条目：scenario_id='analysis_true_field'（过去 7 天）和 scenario_id='forecast_gfs_deterministic'（未来 7 天），每条含 points: [[timestamp, value], ...]。scenario_id 通过 JOIN hydro_run 获取，不直接存储在 river_timeseries 中

#### Scenario: Analysis only response
- **WHEN** forecast run 尚未完成但 analysis 数据可用，请求 include_analysis=true
- **THEN** 返回仅含 scenario_id='analysis_true_field' 的 series

#### Scenario: Forecast only response (backward compatible)
- **WHEN** 请求不含 include_analysis 参数（默认行为，兼容 M1）
- **THEN** 返回仅含 forecast scenario 的 series（与 M1 行为一致）

#### Scenario: No data available
- **WHEN** 请求 include_analysis=true 但 analysis 和 forecast 均无数据
- **THEN** 返回 HTTP 200，series 为空数组

### Requirement: Analysis Time Range Calculation
系统 SHALL 自动计算 analysis 段的时间范围。

#### Scenario: Standard 7-day analysis window
- **WHEN** issue_time = 2026-04-30T00:00:00Z
- **THEN** analysis 查询 river_timeseries JOIN hydro_run（hr.scenario_id='analysis_true_field'），valid_time BETWEEN '2026-04-23T00:00:00Z' AND '2026-04-30T00:00:00Z'（含边界）

#### Scenario: Analysis data shorter than 7 days
- **WHEN** analysis 数据不足 7 天（如刚开始运行 analysis run）
- **THEN** 返回实际可用天数的数据，不补零

#### Scenario: Boundary point deduplication
- **WHEN** analysis 段和 forecast 段在 issue_time 处有重叠数据点
- **THEN** analysis 段不包含 issue_time 时刻（开区间），forecast 段包含 issue_time 时刻（闭区间），避免重复

### Requirement: Frontend Curve Splicing Display
前端 SHALL 在预报曲线图中展示 analysis + forecast 拼接曲线。

#### Scenario: Dual-segment curve rendering
- **WHEN** API 返回 analysis + forecast 两段数据
- **THEN** 前端用不同颜色渲染两段（如 analysis 蓝色实线、forecast 橙色实线），在 issue_time 处画竖向分界线

#### Scenario: Source annotation
- **WHEN** 曲线渲染完成
- **THEN** 图表标注资料来源（从 API 响应中的 source 字段获取，如 ERA5 或 GFS fallback）和起报时间，不硬编码来源名称

#### Scenario: Analysis segment missing
- **WHEN** API 仅返回 forecast segment（无 analysis 数据）
- **THEN** 前端正常渲染 forecast 曲线，不显示 analysis 段和分界线

### Requirement: Issue Time Display
前端 SHALL 明确显示预报起报时间（issue_time）和分界线含义。

#### Scenario: Issue time indicator
- **WHEN** 拼接曲线展示
- **THEN** 分界线处标注"起报时间"文字和具体时间值，鼠标 hover 显示 tooltip 说明"左侧为真实场 analysis，右侧为预报"

