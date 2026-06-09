# Bugs

## 2026-05-27 QHH MVP production-like E2E

测试模式：本地直接拉起真实 API + 静态前端，无 API mock；数据库为共享 PostgreSQL；Slurm 做 live diagnostic；浏览器检查使用 `agent-browser`。本轮不声明 production ready。

证据目录：

- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/`
- 汇总：`artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/summary.md`
- 环境：`http://127.0.0.1:8001`，DB `postgresql://nhms:****@10.0.2.100:55432/nhms`

### BUG-20260527-000: 本地仓库未同步导致 production-like E2E checklist 不可见

状态：resolved

用户指定清单：

```text
docs/runbooks/qhh-mvp-production-like-e2e-checklist.md
```

排查结果：远端 `origin/master` 已在 commit `42e70188df881f971bfb78ece65dea9484c1ec01` 新增该文件，本地此前未同步所以找不到。

处理结果：已同步到 `master` / `origin/master` 的 `42e70188df881f971bfb78ece65dea9484c1ec01`，清单文件已存在。

根因分析：

- 已确认根因：本地 `master` 落后于 `origin/master`，导致新增 runbook 没有出现在工作区。
- 定位入口：Git 历史中的 `42e70188df881f971bfb78ece65dea9484c1ec01` 是清单新增提交；同步后 `docs/runbooks/qhh-mvp-production-like-e2e-checklist.md` 可见。
- 修复方向：执行生产级 E2E 前先固定并记录仓库 commit，必要时先 `git fetch`/`git pull --ff-only`，避免用旧工作区跑新清单。

### BUG-20260527-001: `psql` CLI 缺失，清单 DB 命令无法原样执行

状态：open

现象：环境中 `psql` 不在 `PATH`，清单第 6.1 节中的 `psql "$DATABASE_URL" ...` 不能直接运行。

影响：DB 预检不能按清单命令逐条留证。已用 `uv run` + SQLAlchemy 替代验证 DB 可连接、PostGIS 可用、目标 schema 存在。

根因分析：

- 已确认根因：E2E 运行机缺少 PostgreSQL client 工具，清单依赖系统级 `psql`，但仓库本身只保证 Python 虚拟环境和 SQLAlchemy/驱动可用。
- 定位入口：`environment_cli_check.log` 显示 `psql` 不在 `PATH`；`db_python_preflight.log` 证明同一 DB URL 可通过仓库受管 Python 链路连接。
- 修复方向：在 E2E 环境初始化中安装 PostgreSQL client，或把清单 DB probe 改为仓库内 `uv run` Python 脚本，统一生成可审计日志。

证据：

- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/environment_cli_check.log`
- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/db/db_python_preflight.log`

复测条件：安装 `psql` 或将清单 DB 检查改成仓库受管 Python/SQLAlchemy probe。

### BUG-20260527-002: 目标库缺少 `alembic_version`，migration level 无法确认

状态：open

现象：DB 预检查询 `alembic_version` 报错：

```text
relation "alembic_version" does not exist
```

影响：虽然 `core/met/hydro/flood/map/ops` schema 存在，但无法确认目标库是否已迁移到当前仓库要求版本。

根因分析：

- 已确认根因：当前仓库的 SQL migration 账本不是 Alembic，而是 `public.schema_migrations`；清单/预检查询了错误的 migration marker。
- 定位入口：`packages/common/migrate.py` 定义 `SCHEMA_MIGRATIONS_TABLE = "public.schema_migrations"`；`scripts/apply_smoke_migrations.py`、`tests/integration_helpers.py` 和真实集成测试均围绕 `schema_migrations` 校验版本。
- 修复方向：把清单第 6.1 的 migration 检查改成查询 `public.schema_migrations`，或提供兼容 probe 同时识别 Alembic 与仓库 SQL migration receipt。

证据：

- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/db/db_python_preflight.log`

复测条件：目标库具备 migration 版本表，或提供等价 schema/version receipt。

### BUG-20260527-003: 清单 SQL 使用 `bv.basin_id='qhh'`，实际 basin id 为 `basins_qhh`

状态：open

现象：按清单第 7.3 节的 `bv.basin_id='qhh'` 查询会得到 0 站点/河段；schema-aware 查询显示实际 `basin_id` 是 `basins_qhh`。

影响：清单原 SQL 会产生假阴性，误判 QHH 资产不存在。

根因分析：

- 已确认根因：runbook 手写 SQL 使用了旧/简写 basin id `qhh`，而系统真实 basin identity 是 `basins_qhh`。
- 定位入口：`packages/common/forecast_store.py` 中 QHH 常量为 `QHH_BASIN_ID = "basins_qhh"`；清单第 7.3 节仍按 `bv.basin_id='qhh'` 过滤。
- 修复方向：修正清单 SQL 和诊断脚本中的 basin id；后续最好复用代码常量或集中配置，避免文档与实现再次漂移。

证据：

- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/db/qhh_baseline.log`
- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/db/qhh_schema_aware_counts.log`

复测条件：修正清单 SQL 或统一 basin id。

### BUG-20260527-004: `plan-production --dry-run` 未显式过滤时选中了非 QHH 模型

状态：open

现象：dry-run 未传 `--model-id basins_qhh_shud --basin-id basins_qhh` 时，候选同时包含 `basins_qhh_shud` 和 `yangtze_shud_v12`。

影响：QHH MVP E2E 边界被扩大，可能污染证据和运行范围。

根因分析：

- 已确认根因：`plan-production` 是“发现全部 active model”的生产调度入口；CLI 未传 `--model-id/--basin-id` 时，scheduler 的 filter tuple 为空，不会自动限定 QHH。
- 定位入口：`services/orchestrator/cli.py` 把未传参数保留为空 tuple；`services/orchestrator/scheduler.py` 只在 filters 非空时排除模型。
- 修复方向：QHH E2E 清单必须强制所有 scheduler 命令带 `--model-id basins_qhh_shud --basin-id basins_qhh`；如需 QHH 专用命令，应在 CLI/profile 层显式封装默认 filters。

证据：

- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/scheduler_dry_run_compact_summary.json`

复测条件：QHH 生产级 E2E 默认限定 QHH，或清单强制要求所有 scheduler 命令带 QHH filters。

### BUG-20260527-005: `plan-production --dry-run` 存在下载 side effect

状态：open

现象：dry-run summary 的 `no_mutation_proof.adapter_download_called=false`，但 dry-run 输出中出现 ECMWF 0h GRIB 下载进度。

影响：dry-run 不是严格无副作用；证据中的 no-mutation 结论与实际输出矛盾。

根因分析：

- 已确认根因：scheduler 的 dry-run 跳过执行候选 `_execute_candidates`，但仍会在候选发现阶段调用 source adapter 的 cycle discovery；IFS discovery 会下载/缓存 0h GRIB 判断数据可用性。
- 定位入口：`services/orchestrator/scheduler.py` 的 `run_once()` 在 dry-run 前仍执行 `_discover_cycles()`；`_discover_source_window()` 调用 adapter `discover_cycles()`。summary 中的 `adapter_download_called=false` 只覆盖执行阶段，不覆盖 discovery 阶段。
- 修复方向：定义 dry-run 的副作用边界；若要求严格无副作用，给 adapter discovery 增加 no-download/list-only 模式，并把 evidence 的 no-mutation proof 覆盖 discovery。

证据：

- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/scheduler_dry_run.log`
- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/scheduler_dry_run_compact_summary.json`

复测条件：dry-run 不做网络下载/落盘，或 evidence 明确声明允许的 discovery side effect。

### BUG-20260527-006: QHH-only `plan-production` 在 Slurm Gateway 提交阶段 HTTP 404

状态：open

现象：带 QHH filters 执行 production-like plan 后，GFS/IFS 候选均在 download stage 失败：

```text
SLURM_GATEWAY_ERROR: Slurm Gateway returned HTTP 404.
```

同时 Slurm CLI smoke 与 compute-node DB probe 均成功，说明基础 Slurm 和计算节点到 DB 链路可用。

影响：正式 scheduler -> Slurm Gateway -> download -> forcing -> SHUD -> parse -> publish 无法闭环，不能形成 live scheduler receipt。

根因分析：

- 已确认根因：调度器提交 Slurm 的 HTTP endpoint 与本地/部署中实际可用的 Slurm Gateway endpoint 不一致；这不是 Slurm CLI 或计算节点到 PostgreSQL 的连通性问题。
- 定位入口：`services/orchestrator/chain.py` 的 `HttpSlurmGatewayClient` 固定调用 `POST /api/v1/slurm/jobs` 和 `POST /api/v1/slurm/job-arrays`；`services/slurm_gateway/routes.py` 提供的路由也在该 namespace 下。返回 HTTP 404 说明 `SLURM_GATEWAY_URL` 指向的服务未挂载这些路径，或 base URL/path 配错。
- 修复方向：统一 `SLURM_GATEWAY_URL`、API router mount 和 Slurm Gateway 启动方式；复测前先用同一个 URL `GET /api/v1/slurm/health`、`POST /api/v1/slurm/jobs` 做最小提交探针。

证据：

- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/scheduler_plan.log`
- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/scheduler_plan_compact_summary.json`
- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/slurm/slurm_smoke_sacct.log`
- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/slurm/slurm_db_probe_sacct.log`

复测条件：GFS/IFS QHH production plan 能提交真实 Slurm job，并写入 `slurm_job_id`、日志 URI 和最终 accounting。

### BUG-20260527-007: `/api/v1/mvp/qhh/latest-product` 不可用

状态：open

复现：

```bash
curl 'http://127.0.0.1:8001/api/v1/mvp/qhh/latest-product?source=GFS'
curl 'http://127.0.0.1:8001/api/v1/mvp/qhh/latest-product?source=IFS'
```

实际结果：GFS 和 IFS 均返回 `QHH_LATEST_PRODUCT_UNAVAILABLE`。主要原因：

- `SEGMENT_COUNT_MISMATCH`: expected `3738`, actual `1633`
- `Q_DOWN_VALID_TIME_MISSING`

影响：`/hydro-met` 无法通过 latest-product bootstrap 展示完整水文气象产品。

根因分析：

- 已确认根因：latest-product readiness 要求 `q_down` 在展示窗口内覆盖 `core.river_network_version.segment_count` 的完整河段数；当前结果只有 `1633` 个 segment，而模型/river network 期望是 `3738`，因此 common valid-time window 不成立。
- 定位入口：`packages/common/forecast_store.py` 的 latest-product 查询从 `core.river_network_version.segment_count` 取 expected count，并在 q_down readiness 中拒绝 `SEGMENT_COUNT_MISMATCH` 和 `Q_DOWN_VALID_TIME_MISSING`；`apps/api/routes/forecast.py` 只是透传该 store 结果。
- 修复方向：先修复 QHH segment universe 与 SHUD 输出入库覆盖；若 MVP 允许子集展示，需要修改 readiness 合同并让 API/前端明确暴露 subset coverage，而不是冒充全量产品。

证据：

- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/api/api_latest_product_gfs.json`
- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/api/api_latest_product_ifs.json`
- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/browser/hydro-met.delayed.snapshot.txt`
- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/screenshots/hydro-met-delayed.png`

复测条件：latest-product 对 GFS/IFS 至少一个源返回 200，且包含站点、河段、run/version/cycle readiness 元数据。

### BUG-20260527-008: QHH 河段总数在资产、调度和结果之间不一致

状态：open

现象：

- `map.river_segment` 对 `basins_qhh_rivnet_vbasins` 有 `5371` 条。
- scheduler/model metadata 期望 `3738` 个 segment。
- `hydro.river_timeseries` 的 `q_down` 结果只覆盖 `1633` 个 segment。

影响：latest-product readiness 被阻塞，也无法证明全 QHH 河段 `q_down` 覆盖。

根因分析：

- 已确认根因：QHH 的地图资产、模型注册/调度 metadata、SHUD 输出解析/入库结果没有使用同一套 segment universe。
- 定位入口：`packages/common/model_registry.py` 要求注册 payload 的 `segment_count` 等于提交的 river segments；scheduler 和 latest-product 均使用模型/river network metadata 作为期望；真实 `hydro.river_timeseries` 只落了 `1633` 个 q_down segment。
- 修复方向：确定 QHH MVP 的唯一 river network source of truth，重建或迁移 `map.river_segment`、`core.river_network_version`、模型 manifest、SHUD parser 映射和历史结果；如选择子集，应把子集作为显式 network/version 注册。

证据：

- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/db/qhh_schema_aware_counts.log`
- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/db/coverage_probe.log`
- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/api/api_latest_product_gfs.json`

复测条件：资产、scheduler expectation、SHUD output、入库结果使用同一 segment universe，或 readiness 规则明确可接受子集并对前端呈现负责。

### BUG-20260527-009: `/ops` 历史周期 jobs/stages 与已持久化 job 不一致

状态：open

复现：

```bash
curl 'http://127.0.0.1:8001/api/v1/jobs?source=GFS&cycle_time=2026-05-21T00:00:00Z&limit=20'
curl 'http://127.0.0.1:8001/api/v1/pipeline/stages?source=GFS&cycle_time=2026-05-21T00:00:00Z'
```

实际结果：jobs 返回 0，stage 全部 pending；但 DB 中有同业务周期附近的 `frequency` succeeded jobs。

影响：`/ops` 不能作为真实历史 pipeline 执行状态证据。

根因分析：

- 已确认根因：`met.forecast_cycle`、`ops.pipeline_job` 与 API 查询使用的 cycle identity 不一致。API 把 `source=GFS&cycle_time=2026-05-21T00:00:00Z` 解析为 canonical `gfs_2026052100`，但历史 `ops.pipeline_job.cycle_id` 写成了 `gfs_2026-05-21 08:00:00+08:00` 这类非 canonical 字符串。
- 定位入口：`apps/api/routes/pipeline.py` 中 `/jobs` 会先通过 `_fetch_forecast_cycle_or_404()` 找到 `met.forecast_cycle.cycle_id`，再按 exact `PipelineJob.cycle_id == cycle_id` 过滤；`/pipeline/stages` 也用 resolved cycle id 汇总 stage。
- 修复方向：统一所有 producer 写入的 `cycle_id` 格式，给历史数据做一次迁移或兼容视图；API 侧可短期兼容旧格式，但长期应以 `met.forecast_cycle.cycle_id` 为唯一主键。

证据：

- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/api/api_jobs_gfs_00.json`
- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/api/api_pipeline_stages_gfs_00.json`
- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/browser/ops-sysadmin.snapshot.txt`
- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/db/coverage_probe.log`

复测条件：`met.forecast_cycle`、`ops.pipeline_job`、API 和 `/ops` 使用一致的 cycle/run identity。

### BUG-20260527-010: 已有 jobs 缺少可读日志，logs API 返回 404

状态：open

复现：

```bash
curl 'http://127.0.0.1:8001/api/v1/jobs/qhh_gfs_2026052100_smoke_frequency/logs'
```

实际结果：返回 `JOB_LOG_NOT_FOUND`。DB 中历史 `ops.pipeline_job.log_uri` 为 `NULL`；本轮 scheduler-failed jobs 也没有可读 log URI。

影响：`/ops` 日志弹窗、失败排障和生产 evidence 不满足清单要求。

根因分析：

- 已确认根因：job 级日志没有以 logs API 可读的形式持久化。历史 `ops.pipeline_job.log_uri` 为 `NULL`，而 `hydro.model_run.log_uri` 中的 `s3://.../logs/` 不是当前 job logs endpoint 的读取来源。
- 定位入口：`apps/api/routes/pipeline.py` 的 logs endpoint 在 `job.log_uri` 为空时直接返回 `JOB_LOG_NOT_FOUND`；即使有 URI，`_local_log_path()` 也只支持 `file://`、绝对/相对本地路径，不支持 `s3://` object-store URI。
- 修复方向：正式 pipeline job 写入 bounded stdout/stderr 或结构化日志到 `ops.pipeline_job.log_uri`，并决定 logs API 是否支持 object store；不支持时应在生产链路落本地可读路径或代理下载。

证据：

- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/api/api_job_logs_known_frequency.json`
- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/browser/ops-scheduler-failed.after-log.snapshot.txt`

复测条件：所有正式 pipeline job 至少提供 bounded stdout/stderr 或结构化日志 URI，logs API 可读。

### BUG-20260527-011: operator cancel 缺失 run 返回 200 空结果

状态：open

现象：viewer cancel 缺失 run 正确返回 403；operator cancel 不存在 run 返回 200，响应是空取消结果。

影响：负向语义不稳定，操作员可能误判不存在 run 已被取消。

根因分析：

- 已确认根因：`cancel_run` 把“没有 active job”设计成幂等成功，但没有先区分 run 不存在、run 已终态和 run 存在但无活跃 job。
- 定位入口：`apps/api/routes/pipeline.py` 的 cancel endpoint 只筛选 `store.query_jobs_by_run(run_id)` 中的 active jobs；结果为空时继续返回 ok 空取消结果，没有 not-found guard。viewer 的 403 来自 RBAC 前置校验，不代表缺失 run 语义正确。
- 修复方向：固定 cancel contract：不存在 run 返回 `404`，已终态 run 返回明确幂等成功或 `409`；同步 OpenAPI、后端测试和前端提示。

证据：

- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/api/neg_cancel_missing_operator.json`
- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/api/neg_cancel_missing_viewer.json`

复测条件：不存在 run 的 cancel 返回稳定 `404` 或明确的 idempotent contract，并在 OpenAPI/测试中固定。

### BUG-20260527-012: 现有 Playwright E2E specs 仍是 mocked regression，不是 live E2E

状态：partially_fixed

现象：前端 `hydro-met.spec.ts`、`monitoring.spec.ts` 等默认 Playwright specs 使用 `page.route('**/api/v1/**')` mock API。

影响：这些测试不能作为本轮生产级 E2E 证据，只能作为 mocked regression 前端回归证据。

根因分析：

- 已确认根因：现有 Playwright specs 是 deterministic frontend regression 设计，通过 `page.route('**/api/v1/**')` 截获 API 响应；它们不连接本地真实 API、共享 PostgreSQL 或 Slurm。
- 定位入口：默认 `corepack pnpm test:e2e` / `corepack pnpm exec playwright test --list` 现在只列出 `mocked-regression-chromium`；不再提供 generic `chromium` alias，明确不是 live receipt。
- 已落地修复：保留 mocked specs 作为前端回归测试，新增 `test:e2e:live-display` profile/spec，要求显式、无 userinfo 凭据的 `PLAYWRIGHT_LIVE_BASE_URL` 和 `PLAYWRIGHT_LIVE_API_BASE_URL`，并通过静态 guard 禁止 live-display specs 注册 `page.route('**/api/v1/**')`。
- live PASS 标准：浏览器页面本身必须从配置的 API binding 读取 `/api/v1/runtime/config`，在有界 runtime config 响应体内收到严格等于 `display_readonly` 的 `service_role`，同时从同一 binding 读取监控只读 API；监控只读 API 证据只记录 URL/status，不解析响应体。RBAC `权限不足`、runtime config 不可用、任何 `/api/v1/slurm/*` 浏览器请求、retry/cancel mutation 都不能算 PASS。
- 剩余状态：当前本地没有 live display_readonly runtime；`corepack pnpm run test:e2e:live-display -- --list` 在缺少 required env vars 时按预期输出 `Live display Playwright profile BLOCKED` 并非零退出。这是 `BLOCKED`，不能记为 `PASS`。

证据：

- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/frontend/e2e_specs_mocked_detection.log`
- `apps/frontend/playwright.config.ts`
- `apps/frontend/playwright.live-display.config.ts`
- `apps/frontend/e2e/live-display.spec.ts`
- `apps/frontend/src/__tests__/playwrightConfig.test.ts`
- `docs/VALIDATION.md`

复测条件：提供真实 display_readonly frontend/API runtime，设置不含 username/password userinfo 的 `PLAYWRIGHT_LIVE_BASE_URL` 和 `PLAYWRIGHT_LIVE_API_BASE_URL`，运行 `corepack pnpm run test:e2e:live-display`；目标页面必须可进入 `/monitoring` 而非 RBAC deny，live-display spec 不得使用 broad `page.route('**/api/v1/**')` mock。

### BUG-20260527-013: retry/cancel API 在本环境创建 mock Slurm job id，不能算 live Slurm retry receipt

状态：open

现象：对 `cycle_gfs_2026052618` 直接调用 retry/cancel，operator/sys_admin 通过 RBAC，但创建的 `slurm_job_id` 为 `mock_1001`、`mock_1002`。

影响：只能证明 API 权限和状态流局部可走，不能证明生产级 retry 已提交到 Slurm。

根因分析：

- 已确认根因：API retry/cancel 路径使用的 in-process Slurm Gateway 默认 backend 是 `mock`，除非显式配置 `SLURM_GATEWAY_BACKEND=slurm` 并接入真实 Slurm 设置。
- 定位入口：`services/slurm_gateway/config.py` 默认 `backend: str = "mock"`；`services/slurm_gateway/gateway.py` 在 `backend == "mock"` 时创建 `MockSlurmGateway`；mock backend 生成 `mock_1001` 这类 job id，并被 retry 逻辑持久化到 `slurm_job_id`。
- 修复方向：生产级 retry/cancel E2E 必须启动 real Slurm Gateway 或配置 API route dependency 使用真实 HTTP gateway；复测 receipt 需要 `sacct` 可查并回写 job/status/log/accounting。

证据：

- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/api/api_retry_cycle_gfs_2026052618_operator.json`
- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/api/api_cancel_cycle_gfs_2026052618_operator.json`
- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/db/final_pipeline_jobs_snapshot.log`

复测条件：retry 产生真实 Slurm job id，`sacct` 可查，并最终回写 job/status/log/accounting。

### BUG-20260527-014: `/ops` 浏览器操作反馈不完整

状态：open

现象：

- scheduler failed cycle 页面能显示 `failed_download` 和 `submission_failed` job。
- 点击“查看日志”后没有清晰的日志不可用反馈或弹窗内容。
- 点击“重试”后，agent-browser 运行中未观察到可见状态变化或 jobs API 变化；直接 API retry 另行验证可创建 mock retry job。

影响：运维用户无法从浏览器完成可验证的日志查看和 retry 操作闭环。

根因分析：

- 已确认根因：前端把 `log_uri=null` 的 job 标成 `unavailable`，但仍展示可点击的“查看日志”按钮，点击后只能进入 logs API 的 404 路径；日志不可用没有在按钮层禁用或给出稳定的就地反馈。
- 已确认根因：retry 按钮只显示瞬时 toast 并刷新当前 source/cycle 的 jobs/stages；本环境后端 retry 产生的是 mock Slurm job，且 `/ops` 仍受 cycle identity/filter 问题影响，页面没有持久化显示“已提交 retry job id / 最新请求结果”。
- 定位入口：`apps/frontend/src/components/monitoring/JobsTable.tsx` 计算了 `logAvailable = Boolean(job.log_uri)`，但 `查看日志` 按钮不受该值控制；`runAction()` 成功后只 toast 并调用 `refreshSelectedContext()`。`apps/frontend/src/stores/monitoring.ts` 刷新仍按当前 source/cycle 查询 `/api/v1/jobs`。
- 修复方向：日志按钮按 `log_uri` 禁用并展示明确原因；retry 成功后在 UI 中持久展示返回的 retry job/slurm id，并确保刷新查询能看到新 job，或在不可见时提示被当前过滤条件隐藏。

证据：

- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/browser/ops-scheduler-failed.snapshot.txt`
- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/browser/ops-scheduler-failed.after-log.snapshot.txt`
- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/browser/ops-scheduler-failed.after-retry.snapshot.txt`
- `artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/screenshots/ops-scheduler-failed-after-retry.png`

复测条件：日志不可用时有明确 UI feedback；retry 点击触发请求、可见状态变化，并能在 jobs 列表看到 retry job。
