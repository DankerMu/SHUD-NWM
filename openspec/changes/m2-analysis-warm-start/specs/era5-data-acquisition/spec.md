## ADDED Requirements

### Requirement: ERA5 Data Source Registration
系统 SHALL 在 `met.data_source` 中注册 ERA5 数据源，source_id='ERA5', source_type='reanalysis', status='enabled', native_format='GRIB', adapter_name='era5'。

#### Scenario: First run auto-registration
- **WHEN** ERA5 adapter 首次运行且 met.data_source 中无 ERA5 记录
- **THEN** 系统 upsert ERA5 记录，status='enabled'，adapter_name='era5'

### Requirement: ERA5 Cycle Discovery
系统 SHALL 通过 CDS API 发现可用的 ERA5 数据切片。ERA5 为再分析资料，cycle_time 按天组织（非 forecast cycle）。

#### Scenario: Discover available ERA5 data for date range
- **WHEN** 运行 ERA5 cycle discovery 指定 date_range=[2026-04-20, 2026-04-25]
- **THEN** 系统检查 CDS API 上各日期数据可用性，为每个可用日期创建 met.forecast_cycle 记录（source_id='ERA5', cycle_time=该日00Z, status='discovered'）

#### Scenario: ERA5 latency detection
- **WHEN** 请求的日期距今 < 5 天且 ERA5 数据尚未可用
- **THEN** 系统标记该日期为 not_yet_available，不创建 forecast_cycle 记录

### Requirement: ERA5 GRIB Download
系统 SHALL 通过 CDS API 下载 ERA5 Single Levels 数据，按天 + 中国区域裁剪。

#### Scenario: Successful ERA5 download
- **WHEN** 提交 ERA5 download 任务（date=2026-04-20, area=[55,70,15,140], variables=[2m_temperature, 2m_dewpoint_temperature, 10m_u_component_of_wind, 10m_v_component_of_wind, surface_pressure, total_precipitation, surface_net_solar_radiation, surface_net_thermal_radiation]）
- **THEN** 系统通过 CDS API 请求数据，下载 GRIB 文件到 raw/ERA5/{date}/，更新 forecast_cycle.status 为 downloading → raw_complete，记录 checksum

#### Scenario: CDS API request timeout
- **WHEN** CDS API 请求排队超过配置的超时阈值（如 2 小时）
- **THEN** 系统取消请求并重试（最多 max_retries 次），记录 retry_count，超限后 status='failed_download'

#### Scenario: Idempotent download
- **WHEN** 同一日期的 ERA5 数据已下载且 checksum 匹配
- **THEN** 系统返回 already_done，不重复下载

### Requirement: ERA5 Canonical Conversion
系统 SHALL 将 ERA5 GRIB 数据转换为 canonical 标准格式，含 ERA5 特有的变量映射和单位转换。

#### Scenario: Temperature conversion
- **WHEN** 处理 ERA5 2m_temperature
- **THEN** 系统将 K 转为 ℃，输出 canonical 变量 air_temperature_2m，unit='degC'

#### Scenario: Dewpoint to RH conversion
- **WHEN** 处理 ERA5 2m_temperature + 2m_dewpoint_temperature
- **THEN** 系统通过 Magnus 公式计算 RH = clamp(e_d/e_s, 0, 1)，输出 canonical 变量 relative_humidity_2m，unit='0-1'

#### Scenario: Precipitation accumulation differencing
- **WHEN** 处理 ERA5 total_precipitation
- **THEN** ERA5 adapter 标记 accumulation_type，转换器按 metadata 执行差分得到时段降水量 mm/step，再转 mm/day

#### Scenario: Radiation J/m² to W/m²
- **WHEN** 处理 ERA5 surface_net_solar_radiation (ssr) + surface_net_thermal_radiation (str)
- **THEN** 系统按时段差分累计 J/m²，除以时段秒数转 W/m²，Rn = ssr_W + str_W，lineage_json.radiation_method='direct_net'

#### Scenario: Wind speed synthesis
- **WHEN** 处理 ERA5 10m_u/v_component_of_wind
- **THEN** 系统合成 wind_speed = sqrt(u² + v²)，unit='m/s'

#### Scenario: Canonical DB persistence
- **WHEN** 所有 ERA5 变量转换完成
- **THEN** 系统写入 met.canonical_met_product（source_id='ERA5', 各标准字段），更新 forecast_cycle.status='canonical_ready'

### Requirement: ERA5 CLI Interface
系统 SHALL 提供 CLI 入口供 sbatch 模板调用。

#### Scenario: ERA5 download CLI
- **WHEN** 执行 `nhms-era5 download --date 2026-04-20 --area 55,70,15,140`
- **THEN** 系统执行 ERA5 download + 校验，返回 0 成功 / 非 0 失败

#### Scenario: ERA5 canonical CLI
- **WHEN** 执行 `nhms-canonical convert --source-id ERA5 --cycle-time 2026-04-20T00:00:00Z`
- **THEN** 系统执行 ERA5 canonical 转换，与 GFS canonical 复用同一 CLI 入口
