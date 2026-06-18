## ADDED Requirements

### Requirement: Best Available Selection Write
系统 SHALL 在 analysis run 完成后写入 met.best_available_selection，记录每个时间步实际使用的数据源。

#### Scenario: Write ERA5 as selected source
- **WHEN** analysis run 使用 ERA5 forcing 完成，覆盖 valid_time 范围 [T1, T2]
- **THEN** 为该范围内每个时间步 × 每个变量写入 best_available_selection（selected_source='ERA5', source_cycle_time, fallback_order=['ERA5'], quality_flag='best_available_realtime'），UPSERT 语义

#### Scenario: Write degraded source
- **WHEN** analysis run 使用 GFS analysis 补位（ERA5 不可用），覆盖 valid_time 范围 [T1, T2]
- **THEN** 写入 best_available_selection（selected_source='GFS', fallback_order=['ERA5','GFS'], quality_flag='best_available_degraded'）

### Requirement: Best Available Selection Query
系统 SHALL 提供查询 best_available_selection 的接口，支持按时间范围和变量过滤。

#### Scenario: Query best available for time range
- **WHEN** 请求 GET /api/v1/met/best-available?from=2026-04-20&to=2026-04-27&variable=prcp
- **THEN** 返回该时间范围内每个 valid_time 的 selected_source、source_cycle_time、quality_flag

#### Scenario: Query reveals source gap
- **WHEN** 查询时间范围内某些 valid_time 无 best_available_selection 记录
- **THEN** 返回结果中这些时间步缺失，前端可识别数据空白

### Requirement: Best Available Fallback Order
系统 SHALL 按设计文档定义的时间窗口策略确定 fallback_order。

#### Scenario: Recent window fallback order
- **WHEN** valid_time 距当前 0-5 天
- **THEN** fallback_order = ['CLDAS', 'ERA5', 'GFS']（CLDAS 未启用时实际跳过）

#### Scenario: Historical window fallback order
- **WHEN** valid_time 距当前 > 5 天
- **THEN** fallback_order = ['ERA5']（ERA5 为历史数据权威来源）
