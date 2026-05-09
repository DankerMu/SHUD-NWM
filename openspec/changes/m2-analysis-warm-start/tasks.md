## 0. M2 基础设施与依赖

- [ ] 0.1 安装 Python 依赖：cdsapi（ERA5 CDS API 客户端）
- [ ] 0.2 更新 Docker Compose：添加 ERA5 adapter worker 服务定义
- [ ] 0.3 创建 M2 配置段：config/dev.yaml 中添加 era5_adapter（cds_timeout_seconds、max_retries、area）、analysis_pipeline（enabled、dry_run）、state_manager（soft_stale_threshold_days=7、hard_stale_threshold_days=30）的配置
- [ ] 0.4 环境变量定义：CDS_API_KEY、CDS_API_URL、ERA5_AREA（默认 55,70,15,140）、STATE_SOFT_STALE_THRESHOLD_DAYS（默认 7）、STATE_HARD_STALE_THRESHOLD_DAYS（默认 30）、ERA5_CDS_TIMEOUT_SECONDS（默认 7200）、ERA5_MAX_RETRIES（默认 3）
- [ ] 0.5 实现 mock ERA5 数据生成器：生成符合 GRIB 格式的测试数据（8 变量 × 24 时次/天），用于无 CDS API 环境测试

## 1. ERA5 Data Acquisition

- [ ] 1.1 实现 met.data_source 初始化：首次运行时 upsert ERA5 记录（source_id='ERA5', source_name='ERA5 Reanalysis', source_type='reanalysis', status='enabled', native_format='GRIB', adapter_name='era5'）
- [ ] 1.2 实现 ERA5 adapter cycle discovery：通过 CDS API 检查指定日期范围的数据可用性，为每个可用日期创建 met.forecast_cycle（source_id='ERA5', cycle_time=日期00Z, status='discovered'）；延迟 < 5 天的日期标记 not_yet_available
- [ ] 1.3 实现 ERA5 GRIB download：通过 CDS API 按天 + 区域裁剪请求 8 个变量（2m_temperature, 2m_dewpoint_temperature, 10m_u_component_of_wind, 10m_v_component_of_wind, surface_pressure, total_precipitation, surface_net_solar_radiation, surface_net_thermal_radiation），下载到 raw/ERA5/{date}/，更新 status: discovered→downloading→raw_complete
- [ ] 1.4 实现 CDS API 请求拆分与重试：按天拆分请求，异步提交，超时（ERA5_CDS_TIMEOUT_SECONDS）重试（ERA5_MAX_RETRIES），超限后 status='failed_download'
- [ ] 1.5 实现下载校验：文件大小 + checksum 完整性检查
- [ ] 1.6 实现幂等逻辑：已存在且 checksum 匹配时返回 already_done
- [ ] 1.7 实现 CLI：nhms-era5 download --date --area（供 sbatch 调用）
- [ ] 1.8 单元测试：cycle discovery mock 测试、CDS API 请求构建测试、超时重试测试、checksum 校验测试

## 2. ERA5 Canonical Conversion

- [ ] 2.1 实现 ERA5 变量标准化映射：2m_temperature→air_temperature_2m, 2m_dewpoint_temperature(计算用), 10m_u_component_of_wind→wind_u_10m, 10m_v_component_of_wind→wind_v_10m, surface_pressure→pressure_surface, total_precipitation→prcp_rate_or_amount, ssr+str→net_radiation
- [ ] 2.2 实现温度转换：K → ℃
- [ ] 2.3 实现露点→RH 计算：Magnus 公式 e_d/e_s，clamp(0,1)
- [ ] 2.4 实现降水转换：ERA5 adapter 标记 accumulation_type，转换器按 metadata 差分得 mm/step，转 mm/day
- [ ] 2.5 实现辐射转换：ssr + str 累计 J/m² 按时段差分后除秒数转 W/m²，Rn = ssr_W + str_W，lineage_json.radiation_method='direct_net'
- [ ] 2.6 实现 wind magnitude 合成：wind_speed = sqrt(u² + v²)
- [ ] 2.7 实现 NetCDF4 输出到 canonical/ERA5/{date}/{variable}/
- [ ] 2.8 实现 DB 持久化：写入 met.canonical_met_product（source_id='ERA5'），更新 forecast_cycle.status='canonical_ready'
- [ ] 2.9 复用 nhms-canonical CLI：扩展 convert 子命令支持 --source-id ERA5
- [ ] 2.10 单元测试：露点→RH 边界测试（Td=T 时 RH=1、Td≪T 时 RH→0）、辐射 J/m²→W/m² 计算测试、降水差分负值处理测试、零降水和极小值测试

## 3. Analysis Forcing Production

- [ ] 3.1 复用 M1 forcing producer 核心逻辑（IDW 插值、.tsd.forc 输出、CSV 调试输出），修改点：source_id 从 'GFS' 改为 'ERA5'，canonical 变量映射使用 ERA5 标准变量名（含 net_radiation 替代 shortwave_down）
- [ ] 3.2 实现 ERA5 forcing_version 记录：写入 met.forcing_version（source_id='ERA5', model_id, cycle_time, start_time, end_time, station_count, forcing_package_uri, checksum, lineage_json），关联 forcing_version_component 血缘
- [ ] 3.3 实现 ERA5 latency fallback：当目标日期 ERA5 canonical 不可用时，查找同时段 GFS 已有 canonical 产品（forecast_cycle.status='canonical_ready' 且 source_id='GFS'），选择 lead_time 最小的 GFS forecast hours 作为准分析场，生成 forcing 时 source_id='GFS'、lineage_json 中 fallback_reason='era5_latency'
- [ ] 3.4 单元测试：ERA5 forcing 产出验证（变量数、站点数、时间步数）、fallback 逻辑测试（ERA5 不可用时正确选择 GFS）

## 4. State Manager

- [ ] 4.1 实现 StateSnapshot 存储：analysis run 成功后提取 `.cfg.ic`，上传到 states/{model_id}/{valid_time}/state.cfg.ic，写入 hydro.state_snapshot（state_id='state_{model_id}_{valid_time}', model_id, run_id, valid_time=end_time, state_uri, checksum=SHA256, usable_flag=false）；同 (model_id, valid_time) 不同 checksum 时将旧记录标记 superseded
- [ ] 4.2 实现 StateSnapshot QC：检查 `.cfg.ic` 文件存在、大小 > 0、checksum 匹配，通过后设 usable_flag=true 并写入 ops.qc_result；失败时 usable_flag 保持 false 并写入 ops.qc_result（含 error_code）
- [ ] 4.3 实现最近可用状态查询：SELECT ... WHERE model_id=$1 AND usable_flag=true AND valid_time<=$2 ORDER BY valid_time DESC LIMIT 1
- [ ] 4.4 实现状态过旧检测：valid_time 距 forecast cycle_time > soft 阈值（STATE_SOFT_STALE_THRESHOLD_DAYS，默认 7 天）→ run_manifest 标记 init_state_quality='degraded_stale_init_state'；> hard 阈值（STATE_HARD_STALE_THRESHOLD_DAYS，默认 30 天）→ 拒绝使用该 state，fallback cold-start，run_manifest 标记 init_state_quality='cold_start_stale_state'
- [ ] 4.5 实现幂等：同 (model_id, valid_time) 已存在且 checksum 匹配时 already_done
- [ ] 4.6 实现 REST API：GET /api/v1/state-snapshots?model_id=&usable=true（分页排序）和 GET /api/v1/state-snapshots/{state_id}
- [ ] 4.7 单元测试：存储测试、state_id 格式验证（state_ 前缀）、QC 通过→usable=true 测试、QC 失败→usable=false + qc_result 测试、最近可用查询测试（多 state 确定性选择）、soft/hard 阈值边界测试、superseded 冲突测试

## 5. Analysis Run Pipeline

- [ ] 5.1 实现 analysis hydro_run 创建：run_type='analysis', scenario_id='analysis_true_field', model_id, forcing_version_id, status='created'；init_state_id 填最近可用 state 或 NULL（cold-start）；重复防护（同 model + 重叠 date_range 已有 active run 时拒绝）
- [ ] 5.2 实现 analysis workspace 准备：复用 M1 SHUD runtime adapter 的 workspace 逻辑（从 model_package_uri 拉取模型包、从 forcing_package_uri 拉取 forcing），修改点：.cfg.para 中时间窗口对应 analysis 日期范围、INIT_MODE 依据 init_state_id 设为 3 或 1
- [ ] 5.3 实现 analysis SHUD 执行：通过 Slurm 提交 run_shud_analysis 作业，status: created→staged→submitted→running→succeeded/failed；Slurm TIMEOUT 视为 failed（error_code='SLURM_TIMEOUT'）
- [ ] 5.4 实现 analysis output parsing：复用 M1 output parser（.rivqdown 解析、m³/d→m³/s 转换、river_timeseries 入库），修改点：lead_time_hours=NULL（非预报），scenario_id 通过 hydro_run JOIN 关联；解析成功后 hydro_run.status 从 succeeded → parsed
- [ ] 5.5 实现 analysis → state snapshot 链：run parsed 后触发 state snapshot 存储（4.1）+ QC（4.2），确保 usable_flag=true 后 forecast 可用
- [ ] 5.6 实现 analysis pipeline lazy submission 编排器：复用 M1 orchestrator lazy submission 逻辑（前一 stage 成功后才提交下一 stage，stage 失败时中止后续），修改点：6 个 stage（ERA5 download → canonical → forcing → analysis → parse → state save+QC）；每个 stage 写入 ops.pipeline_job
- [ ] 5.7 实现 pipeline_event 日志：状态流转写入 ops.pipeline_event（entity_type='analysis_pipeline', entity_id, event_type, status_from, status_to）
- [ ] 5.8 实现 analysis pipeline trigger CLI：nhms-pipeline trigger-analysis --model-id --date-range，含重复防护（同 model + 重叠 date_range 拒绝）
- [ ] 5.9 实现 sbatch 模板：download_era5.sbatch（调用 nhms-era5）、convert_canonical_era5.sbatch（调用 nhms-canonical --source-id ERA5）、produce_forcing_analysis.sbatch（调用 nhms-forcing --source-id ERA5）、run_shud_analysis.sbatch（调用 nhms-shud-runtime --run-type analysis）、parse_analysis_output.sbatch（调用 nhms-parse）、save_state_snapshot.sbatch（调用 nhms-state save + QC）
- [ ] 5.10 实现 Mock Gateway 集成：通过 slurm_gateway.backend=mock 提交 analysis pipeline，mock 延迟后自动 succeeded
- [ ] 5.11 单元测试：analysis run 创建测试、init_state 选择测试、重复防护测试、lazy submission 测试、stage 失败中止测试、mock 全流程端到端测试

## 6. Forecast Warm-start

- [ ] 6.1 修改 forecast run 创建逻辑：查询最近可用 StateSnapshot（调用 4.3），应用 freshness 检测（4.4），填入 hydro_run.init_state_id
- [ ] 6.2 修改 workspace 准备：init_state_id 不为空时从 initial_state.ic_file_uri 下载 `.cfg.ic` 到 workspace，.cfg.para 设 INIT_MODE=3；为空时 INIT_MODE=1
- [ ] 6.3 实现 init state 完整性验证：启动前检查 `.cfg.ic` checksum，失败时标记 usable_flag=false（error_code='INIT_STATE_CORRUPTED'）并查找下一可用 state
- [ ] 6.4 修改 run_manifest JSON：使用嵌套结构 initial_state: { state_id, ic_file_uri, quality } 和 runtime: { init_mode }，遵循 Appendix B manifest schema
- [ ] 6.5 实现 init_state_quality 标记：state 过旧（>soft threshold）时 initial_state.quality='degraded_stale_init_state'；超 hard threshold 时 fallback cold-start 且 initial_state.quality='cold_start_stale_state'；无 state 时 initial_state.quality='cold_start_no_state'
- [ ] 6.6 单元测试：warm-start 选择测试、cold-start fallback 测试、soft/hard stale 阈值测试、init state 校验失败重选测试、manifest 嵌套结构验证

## 7. Best Available Selection

- [ ] 7.1 实现 best_available_selection 写入：analysis run 完成后遍历 forcing 覆盖的 valid_time × variable，UPSERT 写入 met.best_available_selection（selected_source, source_cycle_time, fallback_order, quality_flag）；ERA5 来源时 quality_flag='best_available_realtime'、fallback_order=['ERA5']；GFS 补位时 quality_flag='best_available_degraded'、fallback_order=['ERA5','GFS']
- [ ] 7.2 实现 fallback_order 按时间窗口确定：0-5 天 ['CLDAS','ERA5','GFS']（CLDAS 跳过，记录实际 enabled 的源），> 5 天 ['ERA5']
- [ ] 7.3 实现 UPSERT 覆盖优先级：后续 ERA5 数据到达后覆盖之前的 GFS fallback 记录（ERA5 优先级 > GFS）
- [ ] 7.4 实现 REST API：GET /api/v1/met/best-available?from=&to=&variable=（返回每个 valid_time 的来源信息）
- [ ] 7.5 单元测试：UPSERT 幂等测试、ERA5 覆盖 GFS 测试、fallback_order 逻辑测试、API 查询测试

## 8. Frontend Curve Splicing

- [ ] 8.1 修改 forecast-series API：新增 include_analysis=true 参数，查询时 JOIN hydro_run 按 scenario_id 分段，返回 analysis_true_field + forecast 两段 series；无参数时保持 M1 行为（仅 forecast）
- [ ] 8.2 实现 analysis 时间范围计算：issue_time - 7d 到 issue_time（开区间，不含 issue_time 本身），数据不足 7 天时返回实际可用天数
- [ ] 8.3 实现 analysis-only 响应：forecast 未完成但 analysis 可用时，仅返回 analysis_true_field segment
- [ ] 8.4 实现前端双段曲线渲染：analysis 蓝色实线 + forecast 橙色实线
- [ ] 8.5 实现 issue_time 分界线：竖向虚线 + "起报时间" 标注 + hover tooltip
- [ ] 8.6 实现资料来源标注：从 API 响应的 source 字段动态获取来源名称（如 ERA5 或 GFS fallback），不硬编码
- [ ] 8.7 实现向后兼容：不传 include_analysis 时保持 M1 行为（仅 forecast）
- [ ] 8.8 实现 analysis 缺失优雅降级：无 analysis 数据时正常渲染 forecast 曲线
- [ ] 8.9 单元测试：API include_analysis 参数测试、analysis-only 响应测试、boundary dedup 测试、时间范围计算测试、前端渲染 snapshot 测试

## 9. 端到端验收测试

- [ ] 9.1 实现 E2E 测试脚本：使用 0.5 的 mock ERA5 数据 + mock shud_omp，触发一个完整 analysis pipeline
- [ ] 9.2 验证 analysis 数据链路：ERA5 raw → canonical → forcing → SHUD analysis → river_timeseries（通过 hydro_run JOIN 可查 scenario='analysis_true_field'） → state_snapshot（usable_flag=true）
- [ ] 9.3 验证 warm-start forecast：使用 analysis 产生的 state_snapshot 触发 forecast run，确认 runtime.init_mode=3，manifest 中 initial_state.ic_file_uri 正确
- [ ] 9.4 验证曲线拼接：API forecast-series?include_analysis=true 返回两段 series（analysis + forecast），boundary 无重复
- [ ] 9.5 验证 best_available_selection：analysis run 后表中有正确的 selected_source='ERA5'、fallback_order=['ERA5']
- [ ] 9.6 验证前端渲染：点击河段后 ECharts 显示双段拼接曲线 + 分界线 + 动态来源标注
- [ ] 9.7 验证 degraded 标记：无 state 时 forecast manifest 中 initial_state.quality='cold_start_no_state'；state 过旧时 initial_state.quality='degraded_stale_init_state'；超 hard 阈值时 initial_state.quality='cold_start_stale_state'
