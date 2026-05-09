## 1. 项目骨架与基础配置

- [ ] 1.1 创建 monorepo 目录结构（apps/api, apps/web, services/orchestrator, services/slurm-gateway, services/tile-publisher, workers/data_adapters, workers/canonical_converter, workers/forcing-producer, workers/shud-runtime, workers/output-parser, workers/flood-frequency, packages/common, schemas/, db/migrations, db/seeds, openapi/, infra/, tests/），每个目录至少包含一个占位文件（.gitkeep 或 __init__.py）
- [ ] 1.2 创建 pyproject.toml（python >= 3.11, fastapi, uvicorn, sqlalchemy, asyncpg, psycopg2-binary, alembic, pydantic；dev: pytest, pytest-asyncio, ruff, httpx），配置 ruff 规则（E/F/I, line-length=120）
- [ ] 1.3 创建 Makefile（dev, migrate, reset-db, seed-demo, test, lint 六个 target），每个 target 失败时返回非零退出码
- [ ] 1.4 创建 .env.example，包含以下变量并附注释：DATABASE_URL, S3_ENDPOINT_URL, S3_BUCKET_NAME, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, SLURM_GATEWAY_BACKEND=mock, API_PORT=8000
- [ ] 1.5 创建 .gitignore，包含 .env 条目（确保 .env 不被 git 跟踪）
- [ ] 1.6 创建 infra/docker-compose.dev.yml（PostgreSQL 15 + PostGIS 3.4 + TimescaleDB 2.x 使用 timescale/timescaledb-ha 镜像 + MinIO，含数据卷和健康检查；MinIO 启动时自动创建 nhms bucket）
- [ ] 1.7 创建 apps/api 入口：FastAPI minimal app（main.py），包含 GET /health 端点，配置 uvicorn reload 模式启动（端口读取 API_PORT 环境变量，默认 8000）
- [ ] 1.8 验证 `make dev`：docker compose up 启动全部依赖服务，FastAPI 在 reload 模式运行，http://localhost:8000/docs 在 30 秒内可访问，stdout 打印所有运行中服务的 URL

## 2. 数据库 Migration

- [ ] 2.1 编写 000001_extensions.sql（CREATE EXTENSION IF NOT EXISTS postgis, timescaledb, pgcrypto），可重复执行不报错
- [ ] 2.2 编写 000002_schemas.sql（CREATE SCHEMA IF NOT EXISTS core, met, hydro, flood, map, ops）
- [ ] 2.3 编写 000003_enums.sql（hydro.run_type [analysis, forecast, hindcast], hydro.run_status [created, staged, submitted, running, succeeded, parsed, frequency_done, published, failed, cancelled, superseded], met.source_status [enabled, restricted, planned, mock, deprecated], met.cycle_status [discovered, downloading, raw_complete, canonical_ready, forcing_ready_partial, forcing_ready, forecast_running, parsed_partial, complete, published, failed_download, failed_convert, failed_forcing, failed_run, failed_parse, failed_publish]），使用 DO $$ 块实现幂等
- [ ] 2.4 编写 000004_core.sql（core.basin, core.basin_version, core.river_network_version, core.river_segment, core.model_instance 五表，含 geometry(MultiPolygon/LineString, 4490)、GiST 索引、复合主键和外键）
- [ ] 2.5 编写 000005_met.sql（met.data_source, met.forecast_cycle, met.canonical_met_product, met.met_station, met.interp_weight, met.forcing_version, met.forcing_version_component, met.forcing_station_timeseries, met.best_available_selection 九表，含 hypertable 转换和 ENUM 引用）
- [ ] 2.6 编写 000006_hydro.sql（hydro.hydro_run, hydro.state_snapshot, hydro.river_timeseries 三表，含 hypertable、复合外键和 ENUM 引用）
- [ ] 2.7 编写 000007_flood.sql（flood.flood_frequency_curve, flood.return_period_result 两表，含 hypertable 和唯一约束）
- [ ] 2.8 编写 000008_map.sql（map.tile_layer, map.tile_cache 两表，含复合主键和 BYTEA 列）
- [ ] 2.9 编写 000009_ops.sql（ops.pipeline_job, ops.pipeline_event, ops.qc_result, ops.audit_log 四表，含索引）
- [ ] 2.10 编写 000010_indexes.sql（补充业务查询索引：canonical_met_source_cycle_idx, met_station_basin_idx, river_ts_segment_time_idx 等，全部使用 IF NOT EXISTS）
- [ ] 2.11 创建 migration tracking 表（public.schema_migrations），记录已执行的 migration 文件名和执行时间
- [ ] 2.12 实现 migration runner（Alembic 或自定义），支持 `make migrate`：仅执行未 apply 的 migration，跳过已 apply 的，stdout 报告 apply 数量
- [ ] 2.13 实现 rollback 支持：每个 migration 文件包含 rollback section（或对应的 down 文件），按反向依赖顺序 DROP 对象
- [ ] 2.14 实现 `make reset-db`（DROP 全部 schema + 重新执行 migrate + seed）
- [ ] 2.15 验证 migration 幂等性：连续执行两次 `make migrate` 不报错，第二次报告 0 个新 migration
- [ ] 2.16 验证表总数：全部 migration 执行后，`SELECT count(*) FROM pg_tables WHERE schemaname IN ('core','met','hydro','flood','map','ops')` = 25（core 5 + met 9 + hydro 3 + flood 2 + map 2 + ops 4）
- [ ] 2.17 验证 hypertable 创建：`SELECT hypertable_name FROM timescaledb_information.hypertables` 包含 forcing_station_timeseries, best_available_selection, river_timeseries, return_period_result
- [ ] 2.18 验证 ENUM 值完整性：`SELECT enumlabel FROM pg_enum JOIN pg_type ON pg_enum.enumtypid = pg_type.oid WHERE typname = 'run_status'` 返回全部 11 个值

## 3. OpenAPI 契约

- [ ] 3.1 创建 openapi/nhms.v1.yaml 基础框架（openapi: "3.0.3", info.title, info.version: "1.0.0", servers: [{url: /api/v1}], tags 定义）
- [ ] 3.2 定义 components.securitySchemes：BearerAuth（type: http, scheme: bearer, bearerFormat: JWT），设为全局默认 security，注释说明 auth 实现推迟到 M1+
- [ ] 3.3 定义标准响应信封 schema：SuccessEnvelope（request_id, status: "ok", data）和 ErrorResponse（request_id, status: "error", error: {code, message, details}），所有 200/201 响应使用 SuccessEnvelope，所有 4xx/5xx 响应引用 ErrorResponse
- [ ] 3.4 定义 components.schemas -- 核心实体：Basin, BasinVersion（含 GeoJSON MultiPolygon geom）, RiverSegment（含 GeoJSON LineString geom）, ModelInstance
- [ ] 3.5 定义 components.schemas -- 气象实体：MetStation（含 GeoJSON Point geom）, ForcingVersion, DataSource, ForecastCycle
- [ ] 3.6 定义 components.schemas -- 水文实体：HydroRun（run_type enum: [analysis, forecast, hindcast], status enum: [created, staged, submitted, running, succeeded, parsed, frequency_done, published, failed, cancelled, superseded]）, RiverSeriesResponse（含 SeriesSegment 子 schema 和 frequency_thresholds）
- [ ] 3.7 定义 components.schemas -- 预警与运维：FloodAlertSummary（含 alert_counts 对象）, PipelineStage（status enum: [pending, running, succeeded, partially_failed, failed, skipped]）, PipelineJob, QcResult
- [ ] 3.8 定义 paths -- 流域与版本（section 3）：GET /api/v1/basins, GET /api/v1/basins/{basin_id}/versions
- [ ] 3.9 定义 paths -- 数据源与周期（section 3）：GET /api/v1/data-sources, GET /api/v1/data-sources/{source_id}/cycles（query: from, to, status）
- [ ] 3.10 定义 paths -- 模型与资产（section 6）：GET /api/v1/models（query: basin_version_id, active）, GET /api/v1/models/{model_id}, GET /api/v1/models/{model_id}/flood-frequency-curves, GET /api/v1/basin-versions/{basin_version_id}/river-network-versions, PUT /api/v1/models/{model_id}/active
- [ ] 3.11 定义 paths -- 河段与预报序列（sections 3-4）：GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}, GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series（query: issue_time, variables, scenarios），响应引用 RiverSeriesResponse
- [ ] 3.12 定义 paths -- 气象站（section 3）：GET /api/v1/met/stations（query: basin_version_id, model_id）, GET /api/v1/met/stations/{station_id}/series（query: forcing_version_id, variables）
- [ ] 3.13 定义 paths -- Run 管理（section 3）：GET /api/v1/runs（query: basin_id, source, cycle_time, status）, GET /api/v1/runs/{run_id}
- [ ] 3.14 定义 paths -- 瓦片（section 5）：GET /api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf, GET /api/v1/tiles/hydro/{run_id}/{variable}/{valid_time}/{z}/{x}/{y}.pbf, GET /api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf, GET /api/v1/tiles/met/{product_id}/{variable}/{valid_time}/{z}/{x}/{y}.png；z/x/y 为 integer；.pbf 响应 content-type 为 application/x-protobuf，.png 为 image/png
- [ ] 3.15 定义 paths -- 流水线与监控（section 7）：GET /api/v1/pipeline/status（query: source, cycle_time）, GET /api/v1/pipeline/stages（query: source, cycle_time）, GET /api/v1/jobs（query: source, cycle_time, status, model_id, limit, offset）, GET /api/v1/jobs/{job_id}/logs, POST /api/v1/runs/{run_id}/retry, POST /api/v1/runs/{run_id}/cancel, GET /api/v1/queue/depth
- [ ] 3.16 定义 paths -- 洪水预警（section 8）：GET /api/v1/flood-alerts/summary（query: run_id, threshold）, GET /api/v1/flood-alerts/ranking（query: run_id, limit）, GET /api/v1/flood-alerts/segments（query: run_id, min_return_period, valid_time）, GET /api/v1/flood-alerts/timeline（query: run_id, segment_id）
- [ ] 3.17 定义 paths -- 血缘追踪（section 9）：GET /api/v1/lineage/river-point（query: run_id, segment_id, valid_time, variable）, GET /api/v1/lineage/forcing-point（query: forcing_version_id, station_id, valid_time, variable）, GET /api/v1/lineage/product/{product_id}
- [ ] 3.18 定义 paths -- 图层与时间导航：GET /api/v1/layers, GET /api/v1/layers/{layer_id}/valid-times
- [ ] 3.19 添加安全/角色文档任务：在 YAML 中注释 6 角色权限模型（admin, operator, analyst, viewer, system, public），标注各端点所需最低角色
- [ ] 3.20 添加非功能性需求文档：在 info.description 或 x-extensions 中记录分页约定（limit/offset）、速率限制占位、ISO 8601 UTC 时间格式要求
- [ ] 3.21 使用 openapi-spec-validator 或 @redocly/cli 验证 YAML 合法性，零错误通过

## 4. Mock Slurm Gateway

- [ ] 4.1 创建 services/slurm-gateway/ 模块骨架（__init__.py, gateway.py, mock_backend.py, models.py, config.py）
- [ ] 4.2 定义 Gateway 抽象接口：submit_job, cancel_job, get_job_status, list_jobs, fetch_logs 五个方法
- [ ] 4.3 定义 HTTP API 路由：POST /jobs（submit_job）, DELETE /jobs/{job_id}（cancel_job）, GET /jobs/{job_id}（get_job_status）, GET /jobs（list_jobs, 支持 limit/offset）, GET /jobs/{job_id}/logs（fetch_logs）
- [ ] 4.4 添加 GET /health 端点：返回 {backend: "mock", version: "0.1.0", status: "ok"}
- [ ] 4.5 实现 backend 配置切换：default=mock；当 SLURM_GATEWAY_BACKEND=slurm 时 raise NotImplementedError("Real Slurm backend will be available in M3")
- [ ] 4.6 实现 mock submit_job：验证 manifest 必填字段（run_id, model_id 等），缺失返回 422 INVALID_MANIFEST 含缺失字段列表；重复 run_id（非终态）返回 409 DUPLICATE_RUN；成功返回 201 + mock job_id（mock_1001 起顺序递增）
- [ ] 4.7 实现 mock cancel_job：submitted/running 立即标记 cancelled 返回 200；已终态返回 409 JOB_ALREADY_TERMINAL；不存在返回 404 JOB_NOT_FOUND
- [ ] 4.8 实现 mock get_job_status：返回 job_id, run_id, status, submitted_at, started_at, finished_at, updated_at；不存在返回 404
- [ ] 4.9 实现 mock 状态自动转换：submitted -> running -> succeeded，延迟可配（delay_to_running_seconds=2, delay_to_succeeded_seconds=5），零延迟时立即到 succeeded
- [ ] 4.10 实现 mock fetch_logs：succeeded 返回含 job_id/run_id/提交/启动/完成的完整日志（complete=true）；running 返回部分日志（complete=false）；failed 返回含错误码的日志；不存在返回 404
- [ ] 4.11 实现确定性失败模拟：failure_rate（0.0-1.0 浮点数，使用 seed-based random 确保可复现）+ force_fail_run_ids 列表；失败时 error_code=SIMULATED_FAILURE/FORCED_FAILURE
- [ ] 4.12 实现 POST /internal/mock/reset：清空内存 job 注册表，job ID 计数器重置为 mock_1001，仅 backend=mock 时可用
- [ ] 4.13 编写单元测试 -- 状态转换：正常路径 submitted->running->succeeded，cancel 路径，failed 路径
- [ ] 4.14 编写单元测试 -- 幂等与错误处理：重复 run_id 拒绝，终态 cancel 拒绝，不存在 job 404，manifest 校验 422
- [ ] 4.15 编写单元测试 -- 并发安全与确定性：并发提交产生不同 job_id，seed-based failure_rate 可复现，reset 后 ID 重置

## 5. JSON Schema

- [ ] 5.1 编写 schemas/run_manifest.schema.json：顶层 required [schema_version, run_id, run_type, scenario_id, start_time, end_time]；嵌套 required model 对象（required: model_id, model_package_uri）；嵌套 required forcing 对象（required: forcing_uri）；嵌套 required outputs 对象（required: output_uri）；run_type enum [analysis, forecast, hindcast]；时间字段 format: date-time
- [ ] 5.2 编写 schemas/run_status.schema.json：required [run_id, status, timestamp]；status enum 对齐 hydro.run_status 全部 11 个值；可选 error_code, error_message, exit_code, progress_pct, current_step, log_uri
- [ ] 5.3 编写 schemas/qc_result.schema.json：required [qc_checkpoint, target_type, target_id, passed, severity, checks_json]；severity enum [info, warning, error]；checks_json 含 checks 数组（每项 required name+passed）
- [ ] 5.4 编写 schemas/pipeline_job.schema.json：required [job_id, job_type, status]；status enum [pending, submitted, running, succeeded, failed, cancelled]；可选 run_id, cycle_id, slurm_job_id, stage, submitted_at, started_at, finished_at, exit_code, retry_count, error_code, error_message, log_uri
- [ ] 5.5 添加条件校验任务：run_manifest 中 run_type=forecast 时 cycle_time 和 source_id 为 required（使用 if/then 或文档约定）
- [ ] 5.6 为每个 schema 编写 schemas/examples/ 下的示例 JSON 文件（run_manifest.example.json, run_status.example.json, qc_result.example.json, pipeline_job.example.json），使用长江 demo 数据
- [ ] 5.7 验证全部 schema 自身合法（JSON Schema draft-07+），使用 check-jsonschema 或 ajv-cli
- [ ] 5.8 验证全部 example 文件通过对应 schema 校验，明确指定验证工具（check-jsonschema --check-metaschema 或 ajv validate）

## 6. 对象存储布局

- [ ] 6.1 验证 infra/docker-compose.dev.yml 中 MinIO 配置完整：端口 9000(API)/9001(console)，启动时自动创建 nhms bucket，持久化卷，健康检查 30 秒内通过
- [ ] 6.2 实现 packages/common 中的 prefix 校验工具（validate_object_path 函数），覆盖 10 种 prefix 模式：raw/, canonical/, forcing/, models/, states/, runs/{id}/input/, runs/{id}/output/, runs/{id}/logs/, tiles/met/, tiles/hydro/
- [ ] 6.3 编写 prefix 校验单元测试（合法路径 + 非法路径用例，含嵌套深度不足的 forcing 路径）
- [ ] 6.4 添加 S3 SDK smoke test：使用 boto3 连接 MinIO，验证 put_object/get_object/list_objects 三个操作正常工作
- [ ] 6.5 seed 脚本中在各 prefix 下创建占位对象（README.md 或 .keep），确保所有数据库 *_uri 字段引用的 S3 对象实际存在
- [ ] 6.6 验证 MinIO 健康检查：docker compose up 后，MinIO 在 30 秒内响应健康检查

## 7. Demo 数据集

- [ ] 7.1 创建 db/seeds/seed_demo.sql 或 seed_demo.py，使用 INSERT ... ON CONFLICT DO NOTHING 实现幂等
- [ ] 7.2 插入 core 数据：1 basin(yangtze) + 1 basin_version(yangtze_v2026_01, 含 MultiPolygon SRID 4490, active_flag=true) + 1 river_network_version(yangtze_rivnet_v01)
- [ ] 7.3 插入 core 数据：10-50 river_segments（yangtze_rivnet_v01_riv_NNNN, 含 LineString SRID 4490, downstream_segment_id 形成连通网络, length_m > 0）+ 1 model_instance(yangtze_shud_v12, model_package_uri 指向 s3://nhms/models/)
- [ ] 7.4 插入 met 数据：1 data_source(GFS, status=mock) + 1 forecast_cycle(gfs_2026050100, status=complete) + 3-5 met_stations（含 Point SRID 4490, station_role=forcing_proxy）
- [ ] 7.5 插入 met 数据：interp_weight + 1 forcing_version(forc_gfs_2026050100_yangtze_shud_v12) + forcing_station_timeseries（7 天 x 6 变量 x 站点数, hourly）
- [ ] 7.6 插入 hydro 数据：1 hydro_run(fcst_gfs_2026050100_yangtze_shud_v12, status=published) + 7 天 river_timeseries（含全部河段, 变量包括 q_down 和 y_stage, hourly）
- [ ] 7.7 插入 flood 数据：flood_frequency_curve（含 q2/q5/q10/q20/q50/q100）+ return_period_result（至少 5 个河段, 含 warning_level）
- [ ] 7.8 插入 map 数据：map.tile_layer seed（至少一条 river-network 图层记录）
- [ ] 7.9 插入 ops 数据：ops.pipeline_job seed（至少一条 demo job 记录）+ ops.qc_result seed（至少一条 demo QC 记录）
- [ ] 7.10 验证所有 ID 符合附录 A 命名规范（basin_id, basin_version_id, river_segment_id, run_id, forcing_version_id 格式）
- [ ] 7.11 验证 FK 完整性：`SELECT count(*) FROM hydro.river_timeseries rt LEFT JOIN core.river_segment rs ON rt.river_segment_id = rs.river_segment_id AND rt.river_network_version_id = rs.river_network_version_id WHERE rs.river_segment_id IS NULL` = 0（无 FK 孤儿）
- [ ] 7.12 验证 URI prefix：所有 *_uri 字段以 s3://nhms/ 开头，路径结构符合对象存储 prefix 规范
- [ ] 7.13 验证 seed 幂等性：连续执行两次 `make seed-demo` 不产生重复数据
- [ ] 7.14 验证 `make reset-db && make seed-demo` 全链路通过

## 8. CI 流水线

- [ ] 8.1 创建 .github/workflows/ci.yml，触发条件：push to main + pull_request
- [ ] 8.2 创建 .markdownlint.yaml 配置文件（配置 line-length 对表格和代码块的豁免等规则）
- [ ] 8.3 配置 markdown-lint job（使用 markdownlint-cli2，检查 docs/**/*.md，timeout-minutes: 5）
- [ ] 8.4 配置 openapi-validate job（校验 openapi/nhms.v1.yaml，timeout-minutes: 5）
- [ ] 8.5 配置 json-schema-validate job（使用 check-jsonschema 或 ajv-cli，校验 schemas/*.schema.json 元验证 + examples 对应验证，timeout-minutes: 5）
- [ ] 8.6 配置 sql-migration-dry-run job（GitHub Actions service container: timescale/timescaledb-ha 镜像 pinned tag，执行 make migrate，timeout-minutes: 15）
- [ ] 8.7 在 sql-migration-dry-run job 中添加 migration 幂等性检查：执行 make migrate 两次，第二次不报错
- [ ] 8.8 在 sql-migration-dry-run job 中添加 seed-demo dry-run：make migrate 后执行 make seed-demo
- [ ] 8.9 配置 unit-test job（pytest + coverage 报告，timeout-minutes: 15）
- [ ] 8.10 Pin 所有 tool/action 版本：actions/checkout@v4, actions/setup-python@v5 + python-version, actions/setup-node 版本号，Docker 镜像 tag 不使用 latest
- [ ] 8.11 每个 job 设置 timeout-minutes：lint/validate 类 ≤ 10 分钟，migration/test 类 ≤ 30 分钟
- [ ] 8.12 验证全部 job 可并行运行且独立失败（markdown-lint, openapi-validate, json-schema-validate 无依赖关系）
- [ ] 8.13 记录 branch protection 配置清单：文档说明需在 GitHub 仓库设置中启用 "Require status checks to pass before merging"，列出全部 required checks 名称
