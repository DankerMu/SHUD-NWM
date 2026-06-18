## 0. M1 基础设施与依赖

- [ ] 0.1 安装 Python 依赖：cfgrib, eccodes, xarray, netCDF4, scipy, httpx, click
- [ ] 0.2 安装前端依赖：maplibre-gl, echarts, vue3（或 React）相关包
- [ ] 0.3 更新 Docker Compose：添加 M1 所需的 worker 服务定义（adapter, converter, forcing-producer, runtime, parser）
- [ ] 0.4 创建 M1 配置文件：config/dev.yaml 中添加 GFS adapter、canonical、forcing、runtime、parser 的配置段
- [ ] 0.5 实现 mock GFS 数据生成器：生成符合 GRIB2 格式的测试数据（7 变量 × 57 时次），用于无网络环境测试
- [ ] 0.6 环境变量定义：GFS_NOMADS_BASE_URL、WORKSPACE_ROOT、OBJECT_STORE_PREFIX 等

## 1. GFS Data Acquisition

- [ ] 1.1 实现 DataSourceAdapter 基类接口（discover_cycles, build_manifest, download_plan, verify_manifest）
- [ ] 1.2 实现 met.data_source 初始化：首次运行时 upsert GFS 记录（source_id, source_name, source_type, status='enabled', native_format='GRIB2', adapter_name='gfs'）
- [ ] 1.3 实现 GFS adapter cycle discovery：扫描 NOMADS 指定日期 00/06/12/18 UTC 四个周期，写入 met.forecast_cycle（cycle_id, source_id, cycle_time, status='discovered'）
- [ ] 1.4 实现 GFS manifest 生成：构建周期内 forecast hours [0,3,6,...,168]（M1 默认 7 天）的文件列表（7 变量），含 URL、变量映射、时间范围，存储到 manifest_uri
- [ ] 1.5 实现 GRIB2 raw download：按 manifest 下载到 raw/{source_id}/{cycle_time}/，更新 forecast_cycle.status 为 'downloading' → 'raw_complete'
- [ ] 1.6 实现下载校验：文件大小 + checksum 完整性检查，失败时更新 forecast_cycle.status='failed_download'，记录 error_code/error_message
- [ ] 1.7 实现幂等逻辑：已存在且 checksum 匹配时返回 already_done，不重复下载
- [ ] 1.8 实现 latency-aware 轮询：按 latency_rule 轮询直到所有 forecast hours 文件可用，支持 retry_count
- [ ] 1.9 实现 CLI 入口：nhms-gfs download --source-id --cycle-time（供 sbatch 模板调用）
- [ ] 1.10 单元测试：cycle discovery mock 测试、manifest 构建测试、checksum 校验测试、轮询超时测试

## 2. Canonical Conversion

- [ ] 2.1 实现变量标准化映射：GFS 原生名（tmp2m, apcp, rh2m, u10m, v10m, pressfc, dswrf）→ 7 个标准变量名（prcp_rate_or_amount, air_temperature_2m, relative_humidity_2m, wind_u_10m, wind_v_10m, pressure_surface, shortwave_down）
- [ ] 2.2 实现单位转换：K→℃、累计降水→时段量（相邻步差分）、百分数→0-1 小数
- [ ] 2.3 实现时间轴生成：从 cycle_time 计算每个 forecast hour 的 valid_time 和 lead_time_hours
- [ ] 2.4 实现 lineage_json 构建：记录 raw GRIB2 文件 URI → canonical 产品的转换链
- [ ] 2.5 实现 NetCDF4 输出：按变量分文件写入 canonical/{source_id}/{cycle_time}/{variable}/，含 CF 属性
- [ ] 2.6 实现 DB 持久化：写入 met.canonical_met_product（canonical_product_id, source_id, source_version, cycle_time, valid_time, lead_time_hours, variable, unit, grid_id, grid_definition_uri, native_time_resolution, native_spatial_resolution, object_uri, checksum, quality_flag='ok', lineage_json）
- [ ] 2.7 更新 forecast_cycle.status：全部变量转换完成后更新为 'canonical_ready'
- [ ] 2.8 实现幂等逻辑：canonical_product_id 已存在且 checksum 匹配时跳过
- [ ] 2.9 实现 CLI 入口：nhms-canonical convert --source-id --cycle-time（供 sbatch 模板调用）
- [ ] 2.10 单元测试：变量映射测试、单位转换边界测试（零降水、负温度）、时间轴计算测试、lineage 必需键检查

## 3. Forcing Production

- [ ] 3.1 实现气象代站加载：从 met.met_station 按 basin_version_id 读取代站定义（station_id, geom Point, elevation_m, station_role）
- [ ] 3.2 实现 IDW 插值权重计算：格点→代站反距离权重，权重归一化（sum=1），结果存入 met.interp_weight（source_id, grid_id, model_id, station_id, variable, grid_cell_id, weight, method='idw'）
- [ ] 3.3 实现权重复用逻辑：同一 (source_id, grid_id, model_id) 组合的权重已存在时直接加载
- [ ] 3.4 实现 6 个 forcing 变量生成：从 canonical NetCDF4 提取格点场，乘以权重矩阵，得到代站值（PRCP, TEMP, RH, wind, Rn, Press）
- [ ] 3.5 实现 wind magnitude 合成：wind_speed = sqrt(wind_u_10m² + wind_v_10m²)
- [ ] 3.6 实现 .tsd.forc 文件输出：SHUD 可读格式（header + timestep × station 矩阵）
- [ ] 3.7 实现 CSV 调试输出：同数据 CSV 格式，便于人工校验
- [ ] 3.8 实现 forcing_version 记录：写入 met.forcing_version（forcing_version_id, model_id, source_id, cycle_time, start_time, end_time, station_count, forcing_package_uri, checksum, lineage_json）
- [ ] 3.9 实现 forcing_version_component 血缘：每个 component 写入（forcing_version_id, canonical_product_id, variable, valid_time_start, valid_time_end, role='forcing_input'）
- [ ] 3.10 实现 forcing_station_timeseries 写入：长表格式，每站每变量每时间步一行（forcing_version_id, basin_version_id, station_id, valid_time, source_id, variable, value, unit, quality_flag）
- [ ] 3.11 更新 forecast_cycle.status：forcing 完成后更新为 'forcing_ready'
- [ ] 3.12 实现幂等逻辑：forcing_version 已存在且 checksum 有效时跳过；checksum 为空或失败时重新生成并替换 component/timeseries（无重复主键）
- [ ] 3.13 实现 CLI：nhms-forcing produce --source-id --cycle-time --model-id
- [ ] 3.14 单元测试：IDW 权重归一化测试、sqrt wind 计算测试、.tsd.forc 格式校验测试、长表写入验证、幂等重跑验证

## 4. Model Registration

（可与 1-3 并行开发，但 4.8 demo 注册必须在 3.1/5.1 之前完成）

- [ ] 4.1 实现 basin + basin_version 注册 API：POST /api/v1/basins, POST /api/v1/basins/{basin_id}/versions（含 geom MultiPolygon 4490, active_flag）
- [ ] 4.2 实现 river_network_version 注册：关联 basin_version_id，含 segment_count, source_uri；批量导入 river_segment（segment_order, downstream_segment_id, length_m, geom LineString 4490, properties_json）
- [ ] 4.3 实现 mesh_version 注册：记录 mesh 版本标识和对象存储 URI
- [ ] 4.4 实现 model_instance 注册：POST /api/v1/models（model_id, basin_version_id, river_network_version_id, mesh_version_id, calibration_version_id, shud_code_version, model_package_uri, resource_profile）
- [ ] 4.5 实现模型包校验 CLI：nhms-model validate-package（检查 mesh/para/calib 文件存在性和一致性）
- [ ] 4.6 实现 model activation：PUT /api/v1/models/{model_id}/active（设置 active_flag=true/false）
- [ ] 4.7 实现 river_segment_crosswalk 维护：跨版本河段映射
- [ ] 4.8 实现查询接口：GET /api/v1/models?basin_version_id=&active=true，支持分页
- [ ] 4.9 注册 demo 全套资产：basin + basin_version + river_network_version（含河段几何） + mesh_version + model_instance + met_station（代站坐标）
- [ ] 4.10 单元测试：注册验证、重复注册拒绝、active_flag 切换、model_package_uri 校验

## 5. SHUD Runtime Adapter

- [ ] 5.1 实现 workspace 准备：从 model_package_uri 拉取模型包（mesh, para, calib），从 forcing_package_uri 拉取 forcing 文件
- [ ] 5.2 实现 .cfg.para 生成：根据 run_manifest（嵌套结构：model.model_id, forcing.forcing_uri 等）设置时间窗口（start_time, end_time）、输出配置（output_interval）、cold-start 标记（M1: init_state_id=NULL）
- [ ] 5.3 实现 hydro_run 记录创建：orchestrator 在 workspace 准备前创建 hydro_run（run_type='forecast', scenario_id, model_id, basin_version_id, forcing_version_id, source_id, cycle_time, status='created'），准备完成后更新为 'staged'
- [ ] 5.4 实现 shud_omp 执行：subprocess.run() 调用，捕获 stdout/stderr，检查 exit code；执行期间 status='running'
- [ ] 5.5 实现输出完整性校验：.rivqdown 文件存在、行数 = expected_timesteps、列数 = segment_count + 1
- [ ] 5.6 实现结果上传：output → output_uri, logs → log_uri，更新 hydro_run（output_uri, log_uri, status='succeeded'）；失败时 status='failed' + error_code/error_message
- [ ] 5.7 实现 CLI：nhms-shud-runtime execute --manifest
- [ ] 5.8 实现 sbatch 模板：run_shud_forecast.sbatch
- [ ] 5.9 实现 mock shud_omp 脚本：生成固定格式 .rivqdown 输出，用于无 SHUD 环境的测试
- [ ] 5.10 单元测试：workspace 文件校验、.cfg.para 生成测试、输出完整性检查测试、run_status 状态机流转测试（created→staged→running→succeeded/failed）

## 6. Output Parsing

- [ ] 6.1 实现 .rivqdown 文件解析器：读取 CSV/DAT 格式，提取时间列 + 各河段流量列；SHUD 时间戳转 UTC valid_time（使用 hydro_run.start_time 参考）
- [ ] 6.2 实现单位转换：m³/d ÷ 86400 → m³/s
- [ ] 6.3 实现列数-河段一致性校验：列数 MUST 等于 river_network_version 的 segment_count
- [ ] 6.4 实现 TimescaleDB 入库：按完整主键 (run_id, river_network_version_id, river_segment_id, variable, valid_time) 写入 hydro.river_timeseries，每行包含：run_id, basin_version_id, river_network_version_id, river_segment_id, valid_time, lead_time_hours, variable='q_down', value, unit='m3/s', quality_flag；upsert 语义
- [ ] 6.5 实现 lead_time_hours 计算：valid_time - hydro_run.cycle_time（小时数）
- [ ] 6.6 实现 QC 检查：流量非负、不超过合理上限（可配置阈值），写入 ops.qc_result
- [ ] 6.7 实现幂等重解析：ON CONFLICT DO UPDATE，不产生重复主键
- [ ] 6.8 解析完成后更新 hydro_run.status 为 'parsed'
- [ ] 6.9 实现 CLI：nhms-parse shud-output --run-id
- [ ] 6.10 单元测试：格式解析测试、单位转换精度测试、列数不匹配错误测试、QC 边界测试、variable='q_down' 验证、lead_time_hours 计算验证

## 7. Slurm Job Chain

- [ ] 7.1 定义 5 个 sbatch 模板文件：download_gfs.sbatch（调用 nhms-gfs）, convert_canonical.sbatch（调用 nhms-canonical）, produce_forcing.sbatch（调用 nhms-forcing）, run_shud_forecast.sbatch（调用 nhms-shud-runtime）, parse_output.sbatch（调用 nhms-parse）
- [ ] 7.2 实现 lazy submission 编排器：每个 stage 成功后才提交下一个 stage（非预提交 afterok），stage 失败时中止后续提交
- [ ] 7.3 实现 pipeline_job 记录：每个 stage 提交后写入 ops.pipeline_job（job_type, slurm_job_id, submitted_at, started_at, finished_at）
- [ ] 7.4 实现 pipeline_event 日志：状态流转写入 ops.pipeline_event（entity_type, entity_id, event_type, status_from, status_to, created_at）
- [ ] 7.5 实现端到端 cycle trigger：接收 (source_id, cycle_time, model_id) 参数，创建 hydro_run，触发完整 5 阶段链；含重复 trigger 防护（同 cycle 已有 active pipeline 时拒绝）
- [ ] 7.6 实现 met.cycle_status 同步：各阶段完成后更新 forecast_cycle.status（discovered→downloading→raw_complete→canonical_ready→forcing_ready→forecast_running→complete）注意：met.cycle_status ENUM 中无 'parsed' 值，hydro_run.status='parsed' 后 cycle 应直接标记为 'complete'
- [ ] 7.7 实现 Mock Gateway 集成：通过 slurm_gateway.backend=mock 提交，mock 延迟后自动 succeeded
- [ ] 7.8 实现 stage 状态查询：按 cycle_time 查询各 stage 当前状态
- [ ] 7.9 单元测试：lazy submission 测试、stage 失败中止测试、mock 全流程端到端测试、重复 trigger 拒绝测试

## 8. Forecast API

- [ ] 8.1 实现 GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series：返回 {segment_id, issue_time, unit, series: [{scenario_id, segment_role, points: [[timestamp, value],...]}]}，支持 issue_time=latest、variables=q_down、scenarios=GFS 过滤
- [ ] 8.2 实现 GET /api/v1/runs/{run_id}：返回单个 run 详情（所有 hydro_run 字段）
- [ ] 8.3 实现 GET /api/v1/runs：列表查询，支持 basin_id, source, cycle_time, status 过滤 + offset/limit 分页
- [ ] 8.4 实现 GET /api/v1/data-sources 和 GET /api/v1/data-sources/{source_id}/cycles?from=&to=&status=
- [ ] 8.5 实现 GET /api/v1/met/stations?basin_version_id=&model_id=（传 model_id 时返回有 interp_weight 的代站集合）
- [ ] 8.6 实现统一错误响应：{request_id, status: "error", error: {code, message, details}}，错误码含 RUN_NOT_FOUND, SEGMENT_NOT_FOUND, RUN_NOT_PUBLISHED, SOURCE_NOT_FOUND, MISSING_REQUIRED_FILTER
- [ ] 8.7 实现响应分页：offset/limit 参数，返回 total_count + items
- [ ] 8.8 集成测试：forecast-series 返回 [timestamp, value] 元组格式、variable='q_down' 查询正确、错误码正确、空 series 返回 200 空数组

## 9. Forecast Frontend

- [ ] 9.1 实现 MapLibre 地图初始化：底图加载（天地图或 OSM）、中国范围初始视口
- [ ] 9.2 实现河网图层：GeoJSON LineString 加载 demo 流域河段，feature properties 含 river_segment_id, river_network_version_id, basin_version_id
- [ ] 9.3 实现河段 hover 交互：高亮线段 + tooltip 显示 segment_id 和名称
- [ ] 9.4 实现河段 click 交互：点击后从 feature properties 提取 basin_version_id + segment_id，打开侧面板
- [ ] 9.5 实现预报曲线图：ECharts 折线图，x=valid_time（7天），y=flow（m³/s），标题含河段名+起报时间
- [ ] 9.6 实现 chart-API 集成：点击河段后 fetch GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series，解析 [timestamp, value] 元组渲染曲线
- [ ] 9.7 实现 loading/error/empty 状态：请求中显示骨架屏，错误显示提示，空 series 显示「暂无预报数据」
- [ ] 9.8 实现基础响应式布局：左侧地图 + 右侧面板，支持移动端收起
- [ ] 9.9 集成测试：点击河段 → API 调用 → 曲线渲染完整流程

## 10. 端到端验收测试

- [ ] 10.1 实现 E2E 测试脚本：使用 mock GFS 数据 + mock shud_omp，触发一个完整 cycle（source_id='GFS', cycle_time=指定时间）
- [ ] 10.2 验证数据链路完整性：raw → canonical → forcing → runs → river_timeseries 各表均有对应记录
- [ ] 10.3 验证 API 响应：GET forecast-series 返回正确曲线数据（variable='q_down', unit='m3/s', points 为 [timestamp, value] 元组）
- [ ] 10.4 验证前端渲染：点击河段后 ECharts 曲线正确显示 7 天预报
- [ ] 10.5 验证血缘追溯：从 river_timeseries → hydro_run → forcing_version → forcing_version_component → canonical_met_product → forecast_cycle → data_source 完整链路可查
