# QHH MVP 模拟生产环境 E2E 测试清单

最后更新：2026-05-27
适用范围：M21 QHH 水文气象展示 + 运维监控 MVP  
推荐证据目录：`artifacts/mvp-e2e/<run_id>/`

## 本次执行记录：2026-05-27 production-like E2E

run_id: `qhh-mvp-e2e-20260527T004907Z`
证据目录：`artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/`
汇总：`artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/summary.md`
问题清单：`docs/bugs.md`
执行结论：`BLOCKED`，不声明 production ready。

说明：本轮按清单在本地直接拉起真实 API + 静态前端，连接共享 PostgreSQL 和 Slurm live diagnostic；浏览器检查使用 `agent-browser`；未使用 API mock。现有 Playwright E2E specs 仍包含 `page.route('**/api/v1/**')`，仅作为 mocked regression，不计入 live E2E 通过证据。

| 清单章节 | 本次状态 | 证据 | 复测/阻塞项 |
| --- | --- | --- | --- |
| 4.1 run id / 证据目录 | PASS | `environment.md`, `command_index.md`, `summary.md` | 无 |
| 4.2 目标环境变量 | PARTIAL | `environment.md`, `storage_roots.log` | DB URL 已脱敏；`psql` CLI 缺失见 `BUG-20260527-001` |
| 5.1 后端基础检查 | PASS | `backend/ruff.log`, `backend/backend_hydro_met_api.log`, `backend/backend_ops_api.log` | 137 + 98 tests passed |
| 5.2 OpenSpec / OpenAPI / 前端类型 | PASS | `backend/openspec_m21.log`, `backend/openapi_lint.log`, `frontend/frontend_api_types.log` | 无 |
| 5.3 前端单测与构建 | PASS | `frontend/frontend_unit.log`, `frontend/frontend_build.log`, `frontend/frontend_bundle.log` | 26 files / 536 tests passed；bundle check passed |
| 6.1 数据库检查 | PARTIAL | `db/db_python_preflight.log` | DB/PostGIS/schema 通过；TimescaleDB 未启用；`alembic_version` 缺失见 `BUG-20260527-002` |
| 6.2 对象存储和工作目录 | PASS | `storage_roots.log`, `scheduler_plan.log` | 登录节点可写；scheduler preflight 显示 compute-node-visible |
| 6.3 Slurm 检查 | PASS | `slurm/slurm_preflight.log`, `slurm/slurm_smoke_sacct.log` | CLI smoke job `5858` completed |
| 6.4 最小 Slurm smoke | PASS | `slurm/slurm_smoke_stdout.log`, `slurm/slurm_smoke_sacct.log` | `cn12`, exit `0:0` |
| 计算节点 DB 访问 | PASS | `slurm/slurm_db_probe_stdout.log`, `slurm/slurm_db_probe_sacct.log` | job `5859` completed，client IP `10.0.2.112` |
| 7.1-7.3 QHH 模型资产和基线数据 | PARTIAL | `db/qhh_baseline.log`, `db/qhh_schema_aware_counts.log`, `db/coverage_probe.log` | active model 和 386 stations 存在；清单 SQL basin id 假阴性见 `BUG-20260527-003`；segment universe 不一致见 `BUG-20260527-008` |
| 8 GFS/IFS 气象源 E2E | PARTIAL | `db/coverage_probe.log`, `scheduler_dry_run.log` | 历史 GFS/IFS canonical/forcing/station 数据存在；本轮 scheduler 下载阶段未闭环，见 `BUG-20260527-006` |
| 9 canonical / forcing / station series | PARTIAL | `db/coverage_probe.log`, `api/api_station_series.json` | GFS/IFS 六变量 station-series 可查；本轮新 cycle 未生成 forcing |
| 10.1 production `plan-production` | FAIL | `scheduler_plan.log`, `scheduler_plan_compact_summary.json` | QHH-only GFS/IFS download stage 均 `SLURM_GATEWAY_ERROR` HTTP 404，见 `BUG-20260527-006` |
| 10.1 dry-run | PARTIAL | `scheduler_dry_run.log`, `scheduler_dry_run_compact_summary.json` | dry-run 无 filters 选中 `yangtze`，且出现下载 side effect，见 `BUG-20260527-004`/`005` |
| 10.2 pipeline job 持久化 | PARTIAL | `db/final_pipeline_jobs_snapshot.log`, `api/api_jobs_all.json` | scheduler failed jobs 已落库；历史 cycle 查询不一致见 `BUG-20260527-009` |
| 10.3 hydro_run / river_timeseries | PARTIAL | `db/coverage_probe.log`, `api/api_forecast_series_qdown_gfs_issue.json` | 指定河段 q_down API 可查；全量 q_down 覆盖不足见 `BUG-20260527-007`/`008` |
| 11.1 latest-product API | FAIL | `api/api_latest_product_gfs.json`, `api/api_latest_product_ifs.json` | GFS/IFS 均 `QHH_LATEST_PRODUCT_UNAVAILABLE`，见 `BUG-20260527-007` |
| 11.2 station inventory API | PASS | `api/api_met_stations.json` | 返回 QHH stations |
| 11.3 station-series API | PASS | `api/api_station_series.json` | 返回 6 个变量序列 |
| 11.4 forecast-series q_down API | PASS | `api/api_forecast_series_qdown_gfs_issue.json`, `api/api_forecast_series_qdown_latest.json` | 指定样本可用；不代表全 segment 覆盖 |
| 11.5 pipeline/status/stages/jobs/logs API | PARTIAL | `api/api_pipeline_status_gfs_00.json`, `api/api_pipeline_stages_gfs_00.json`, `api/api_jobs_gfs_00.json`, `api/api_job_logs_known_frequency.json` | jobs/stages 历史查询不一致、logs 404，见 `BUG-20260527-009`/`010` |
| 12 `/hydro-met` 浏览器 E2E | FAIL | `browser/hydro-met.delayed.snapshot.txt`, `screenshots/hydro-met-delayed.png` | latest-product bootstrap 被阻塞，页面显示不可用，见 `BUG-20260527-007` |
| 13 `/ops` 浏览器 E2E | PARTIAL | `browser/ops.snapshot.txt`, `browser/ops-sysadmin.snapshot.txt`, `browser/ops-scheduler-failed.snapshot.txt`, `screenshots/ops-scheduler-failed.png` | viewer 权限拒绝可见；sys_admin failed cycle 可见；日志/重试浏览器动作不完整见 `BUG-20260527-014` |
| 14 retry / cancel E2E | PARTIAL | `api/api_retry_cycle_gfs_2026052618_operator.json`, `api/api_cancel_cycle_gfs_2026052618_operator.json`, `db/final_pipeline_jobs_snapshot.log` | RBAC 和 API 状态流可测；产生 `mock_1001/mock_1002`，不是 live Slurm receipt，见 `BUG-20260527-013` |
| 15 负向和边界测试 | PARTIAL | `api/neg_*.json`, `api/api_summary.json` | 大部分验证稳定；operator cancel missing run 返回 200，见 `BUG-20260527-011` |
| 16 性能和资源测试 | PARTIAL | `api/perf_api_20x_summary.json`, `slurm/slurm_smoke_sacct.log` | 可用 API 20 次循环通过；未能跑完整生产 pipeline 资源曲线 |
| 17 权限和审计测试 | PARTIAL | `api/api_retry_cycle_gfs_2026052618_viewer.json`, `api/api_cancel_cycle_gfs_2026052618_viewer.json`, `evidence_secret_scan.log` | viewer forbidden 通过；test-mode sys_admin 不是 live IdP；证据扫描未发现 DB 明文密码，bundle 关键词为 false positive |
| 18 证据打包 | PASS | `summary.md`, `command_index.md`, `current_evidence_listing.log` | 已生成本轮 summary |
| 19 验收闸门 | BLOCKED | `summary.md`, `docs/bugs.md` | 未满足完整 GFS cycle、latest-product、`/hydro-met`、logs、live retry receipt |
| 20 最终交付物 | PARTIAL | `summary.md`, `scheduler_plan_compact_summary.json`, `docs/bugs.md` | 有 blocker 列表；未形成通过结论 |

补测优先级：

1. 修复 Slurm Gateway HTTP 404，使 `plan-production --model-id basins_qhh_shud --basin-id basins_qhh` 能提交真实 download job。
2. 对齐 QHH segment universe 和 q_down valid-time metadata，使 latest-product 对 GFS/IFS 至少一个源返回 200。
3. 统一 cycle/run identity，使 `/ops` 能查到历史和新 run 的 jobs/stages/logs。
4. 为正式 failed job 写入可读 `log_uri`，再用浏览器完成 log/retry/cancel live receipt。
5. 新增无 `page.route` 的 live frontend E2E specs，保留现有 mocked specs 作为回归测试。

## 1. 文档目的

本文用于指导一次接近生产环境的端到端测试。测试目标不是证明最终生产就绪，而是证明当前 MVP 在模拟生产条件下能够从气象资料、模型运行、结果入库、API 查询、前端展示到运维重启形成完整闭环。

本清单面向以下对象：

- 项目负责人：确认 MVP 是否可以进入试运行。
- 后端/数据工程师：确认数据下载、转换、forcing、模型运行和入库链路。
- 前端工程师：确认 `/hydro-met` 和 `/ops` 两个 MVP 页面能连真实后端运行。
- 运维人员：确认调度、Slurm、日志、失败重启和证据留存。
- 测试人员：按清单逐项记录通过、失败、跳过和阻塞原因。

## 2. MVP 测试边界

### 2.1 本次必须覆盖

- QHH/有限流域，不覆盖全国所有流域。
- GFS 主源和 IFS 并行源。
- 水文变量：河段流量 `q_down`。
- 气象代站变量：`PRCP`、`TEMP`、`RH`、`wind`、`Rn`、`Press`。
- MVP 页面：`/hydro-met` 水文气象展示页，`/ops` 运维监控页。
- 正式调度入口：`uv run nhms-pipeline plan-production`。
- 运维接口：pipeline stage、jobs、logs、retry、cancel、queue 和 metrics。

### 2.2 本次不承诺

- 不承诺水位 `stage`。
- 不承诺全国所有流域和所有河段完整上线。
- 不承诺 CLDAS 正式接入。
- 不承诺 ERA5 近实时。
- 不承诺真实全国 MVT/PBF。
- 不承诺最终生产 readiness。
- 不把 deterministic/mock/browser fixture 证据升级为 live 生产证据。

### 2.3 证据模式定义

| 模式 | 含义 | 是否可作为最终生产就绪证据 |
| --- | --- | --- |
| deterministic | 本地固定夹具或测试数据库 | 否 |
| mocked | 前端或 API mock | 否 |
| production-like | 使用目标部署形态，但可能仍使用受控数据/受控账号 | 否，除非配套 live receipt |
| live diagnostic | 使用真实数据或真实 Slurm 的诊断链路 | 否，除非绑定正式 scheduler/pipeline |
| live receipt | 目标环境、真实依赖、真实执行、可追溯证据 | 可以作为对应 surface 的生产证明 |

## 3. 测试总流程

```text
环境预检
  ↓
数据库 / 对象存储 / Slurm / 日志根检查
  ↓
Basins/QHH 模型资产检查
  ↓
GFS/IFS 周期发现和下载
  ↓
raw mirror 校验
  ↓
canonical 产品生成
  ↓
forcing 生成和代站时序落库
  ↓
正式 scheduler plan-production
  ↓
Slurm SHUD 运行
  ↓
输出解析和 display product 发布
  ↓
station-series API 验证
  ↓
forecast-series q_down API 验证
  ↓
/hydro-met 真实后端浏览器验证
  ↓
/ops 真实后端运维验证
  ↓
受控失败和 retry/cancel 验证
  ↓
证据打包和 readiness/blocker 汇总
```

## 4. 测试前统一配置

### 4.1 run id

```bash
export MVP_E2E_RUN_ID="qhh-mvp-e2e-$(date -u +%Y%m%dT%H%M%SZ)"
export MVP_E2E_EVIDENCE_ROOT="artifacts/mvp-e2e/${MVP_E2E_RUN_ID}"
mkdir -p "$MVP_E2E_EVIDENCE_ROOT"
```

验收要求：

- [ ] `MVP_E2E_RUN_ID` 全局唯一。
- [ ] 所有日志、命令输出和 JSON 证据都写入同一证据目录。
- [ ] 证据目录不得包含明文密码、token、签名 URL 或个人敏感信息。

### 4.2 目标环境变量

```bash
export DATABASE_URL="postgresql://<user>:<password>@<db-host>:5432/nhms"
export NHMS_PRODUCTION_SLURM_ENABLED=1
export WORKSPACE_ROOT="/scratch/<user>/nhms-production"
export OBJECT_STORE_ROOT="/scratch/<user>/nhms-production/object-store"
export SLURM_SHARED_LOG_ROOT="/scratch/<user>/nhms-production/slurm-logs"
export NHMS_RUNTIME_ROOT="/scratch/<user>/nhms-production/runtime"
export NHMS_API_BASE_URL="http://<api-host>:8000/api/v1"
export NHMS_FRONTEND_BASE_URL="http://<frontend-host>:4173"
```

验收要求：

- [ ] `DATABASE_URL` 指向目标数据库，不是本机临时数据库。
- [ ] compute node 可以访问 `DATABASE_URL`。
- [ ] `WORKSPACE_ROOT`、`OBJECT_STORE_ROOT`、`SLURM_SHARED_LOG_ROOT`、`NHMS_RUNTIME_ROOT` 都在共享或目标可访问路径。
- [ ] `SLURM_SHARED_LOG_ROOT` 不使用 compute-node-local 的 `/tmp`。
- [ ] 前端访问的 API base 与测试后端一致。
- [ ] 所有路径都不存在 `..`、反斜杠、软链逃逸或越界写入。

## 5. 静态质量和契约预检

### 5.1 后端基础检查

```bash
uv run ruff check . 2>&1 | tee "$MVP_E2E_EVIDENCE_ROOT/ruff.log"
uv run pytest -q tests/test_forecast_api.py tests/test_api_contract.py 2>&1 \
  | tee "$MVP_E2E_EVIDENCE_ROOT/backend_hydro_met_api.log"
uv run pytest -q tests/test_monitoring_api.py tests/test_retry_cancel_consistency.py 2>&1 \
  | tee "$MVP_E2E_EVIDENCE_ROOT/backend_ops_api.log"
```

检查项：

- [ ] 代码 lint 通过。
- [ ] latest-product、station-series、forecast-series 合同测试通过。
- [ ] pipeline/jobs/logs/retry/cancel 相关测试通过。
- [ ] 测试失败时记录失败文件、失败用例和失败原因。

### 5.2 OpenSpec / OpenAPI / 前端类型

```bash
openspec validate m21-qhh-hydro-met-ops-mvp --strict --no-interactive 2>&1 \
  | tee "$MVP_E2E_EVIDENCE_ROOT/openspec_m21.log"

npx --yes @redocly/cli@1.25.13 lint openapi/nhms.v1.yaml --skip-rule no-unused-components 2>&1 \
  | tee "$MVP_E2E_EVIDENCE_ROOT/openapi_lint.log"

cd apps/frontend
corepack pnpm run check:api-types 2>&1 \
  | tee "../../$MVP_E2E_EVIDENCE_ROOT/frontend_api_types.log"
```

检查项：

- [ ] M21 OpenSpec 严格校验通过。
- [ ] OpenAPI lint 通过。
- [ ] 前端生成类型与 OpenAPI 无漂移。
- [ ] 若 OpenAPI 失败，不继续执行浏览器 E2E。

### 5.3 前端单测与构建

```bash
cd apps/frontend
corepack pnpm test 2>&1 | tee "../../$MVP_E2E_EVIDENCE_ROOT/frontend_unit.log"
corepack pnpm build 2>&1 | tee "../../$MVP_E2E_EVIDENCE_ROOT/frontend_build.log"
corepack pnpm check:bundle 2>&1 | tee "../../$MVP_E2E_EVIDENCE_ROOT/frontend_bundle.log"
```

检查项：

- [ ] 前端单测通过。
- [ ] 前端生产构建通过。
- [ ] bundle 大小未明显超预算。
- [ ] 构建产物不包含本地绝对路径、token 或调试私有信息。

## 6. 目标基础设施预检

### 6.1 数据库检查

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "select current_database(), current_user, now();" \
  | tee "$MVP_E2E_EVIDENCE_ROOT/db_connectivity.log"

psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "select postgis_full_version();" \
  | tee "$MVP_E2E_EVIDENCE_ROOT/db_postgis.log"

psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "select extname from pg_extension where extname in ('postgis','timescaledb');" \
  | tee "$MVP_E2E_EVIDENCE_ROOT/db_extensions.log"
```

检查项：

- [ ] 数据库可连接。
- [ ] PostGIS 可用。
- [ ] TimescaleDB 可用，或明确记录当前测试环境未启用的影响。
- [ ] migration 已执行到当前仓库要求版本。
- [ ] `core`、`met`、`hydro`、`flood`、`map`、`ops` schema 存在。
- [ ] 目标库不是误用开发临时库。

### 6.2 对象存储和工作目录检查

```bash
mkdir -p "$WORKSPACE_ROOT" "$OBJECT_STORE_ROOT" "$SLURM_SHARED_LOG_ROOT" "$NHMS_RUNTIME_ROOT"
touch "$WORKSPACE_ROOT/.write-test" "$OBJECT_STORE_ROOT/.write-test" \
  "$SLURM_SHARED_LOG_ROOT/.write-test" "$NHMS_RUNTIME_ROOT/.write-test"
ls -la "$WORKSPACE_ROOT" "$OBJECT_STORE_ROOT" "$SLURM_SHARED_LOG_ROOT" "$NHMS_RUNTIME_ROOT" \
  | tee "$MVP_E2E_EVIDENCE_ROOT/storage_roots.log"
```

检查项：

- [ ] 所有根目录存在。
- [ ] 当前用户有读写权限。
- [ ] Slurm compute node 也能读取运行 manifest 和写日志。
- [ ] 写入测试完成后清理 `.write-test`。
- [ ] 对象根路径不会与历史生产对象混写。

### 6.3 Slurm 检查

```bash
sinfo -o '%P|%a|%l|%D|%t|%N' | tee "$MVP_E2E_EVIDENCE_ROOT/slurm_sinfo.log"
squeue -u "$USER" -o '%i|%P|%j|%u|%T|%M|%D|%R' | tee "$MVP_E2E_EVIDENCE_ROOT/slurm_squeue_before.log"
sacctmgr show user "$USER" format=User,DefaultAccount,Admin,Cluster%20 -P \
  | tee "$MVP_E2E_EVIDENCE_ROOT/slurm_account.log"
```

检查项：

- [ ] `sinfo` 可用。
- [ ] `squeue` 可用。
- [ ] `sbatch` 可用。
- [ ] `sacct` 可用。
- [ ] `scancel` 可用。
- [ ] 默认 account/partition 与配置一致。
- [ ] 当前队列没有大量历史残留测试任务。

### 6.4 最小 Slurm smoke

```bash
mkdir -p "$SLURM_SHARED_LOG_ROOT/smoke"
cat >"$SLURM_SHARED_LOG_ROOT/smoke/mvp-smoke.sbatch" <<'EOF'
#!/usr/bin/env bash
#SBATCH --job-name=nhms-mvp-smoke
#SBATCH --partition=CPU
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --time=00:02:00
#SBATCH --output=SLURM_LOG_ROOT/smoke/slurm-%j.out
#SBATCH --error=SLURM_LOG_ROOT/smoke/slurm-%j.err
set -euo pipefail
echo "MVP_SLURM_SMOKE_START $(date -Iseconds) host=$(hostname) job=${SLURM_JOB_ID:-none}"
python3 - <<'PY'
import os, sys
print("PYTHON_OK", sys.version.split()[0], os.environ.get("SLURM_JOB_ID"))
PY
echo "MVP_SLURM_SMOKE_DONE $(date -Iseconds)"
EOF
sed -i "s|SLURM_LOG_ROOT|$SLURM_SHARED_LOG_ROOT|g" "$SLURM_SHARED_LOG_ROOT/smoke/mvp-smoke.sbatch"
jobid=$(sbatch --parsable "$SLURM_SHARED_LOG_ROOT/smoke/mvp-smoke.sbatch")
echo "$jobid" | tee "$MVP_E2E_EVIDENCE_ROOT/slurm_smoke_jobid.txt"
sacct -j "$jobid" --format=JobIDRaw,JobName,Partition,State,ExitCode,Elapsed,NodeList -P \
  | tee "$MVP_E2E_EVIDENCE_ROOT/slurm_smoke_sacct.log"
```

检查项：

- [ ] Slurm smoke job 能提交。
- [ ] 终态为 `COMPLETED`。
- [ ] 退出码为 `0:0`。
- [ ] stdout 包含 `MVP_SLURM_SMOKE_START`、`PYTHON_OK`、`MVP_SLURM_SMOKE_DONE`。
- [ ] stderr 为空或仅包含可解释的环境警告。

## 7. QHH 模型资产和基线数据检查

### 7.1 Basins/QHH 资产检查

```bash
uv run nhms-model discover-basins --basins-root data/Basins \
  --output "$MVP_E2E_EVIDENCE_ROOT/basins_inventory.json"
```

检查项：

- [ ] `data/Basins` 可访问。
- [ ] QHH 模型目录存在。
- [ ] QHH forcing、GIS、runtime 输入目录完整。
- [ ] 不依赖不可迁移的本地软链作为生产证据。
- [ ] 资产清单包含文件数量、大小、checksum 或可追溯来源。

### 7.2 模型注册和 active model 检查

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "
select model_id, basin_version_id, river_network_version_id, active_flag, model_package_uri
from core.model_instance
where model_id like '%qhh%' or basin_version_id like '%qhh%'
order by created_at desc;
" | tee "$MVP_E2E_EVIDENCE_ROOT/qhh_model_registry.log"
```

检查项：

- [ ] 至少一个 QHH model active。
- [ ] model_id 与 scheduler 配置一致。
- [ ] basin_version_id 可关联到 QHH basin。
- [ ] river_network_version_id 可关联到 QHH river segments。
- [ ] model_package_uri 指向可访问对象或共享路径。
- [ ] 不使用过期、deprecated 或 superseded 模型作为 MVP 默认模型。

### 7.3 河段和代站基础数据检查

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "
select count(*) as station_count
from met.met_station ms
join core.basin_version bv on bv.basin_version_id = ms.basin_version_id
where bv.basin_id = 'qhh' and ms.active_flag = true;
" | tee "$MVP_E2E_EVIDENCE_ROOT/qhh_station_count.log"

psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "
select count(*) as segment_count
from core.river_segment rs
join core.river_network_version rnv on rnv.river_network_version_id = rs.river_network_version_id
join core.basin_version bv on bv.basin_version_id = rnv.basin_version_id
where bv.basin_id = 'qhh';
" | tee "$MVP_E2E_EVIDENCE_ROOT/qhh_segment_count.log"
```

检查项：

- [ ] QHH active station 数量与 MVP 预期接近。
- [ ] QHH river segment 数量非零。
- [ ] station 与模型使用关系可通过 model_id 查询。
- [ ] river segment 拥有前端展示所需 geometry 或可降级展示。

## 8. GFS/IFS 气象源 E2E 检查

### 8.1 周期发现

使用正式 scheduler 的 dry-run 模式先发现候选周期，不产生下载、Slurm 或结果写入：

`--plan` 只保留给 dry-run/no-mutation smoke 或业务验证证据；真实生产提交必须在
10.1 使用 `--submit`。

```bash
uv run nhms-pipeline plan-production \
  --dry-run \
  --source gfs \
  --source IFS \
  --lookback-hours 24 \
  --cycle-lag-hours 6 \
  --max-cycles-per-source 1 \
  --workspace-root "$WORKSPACE_ROOT/.nhms-workspace" \
  2>&1 | tee "$MVP_E2E_EVIDENCE_ROOT/scheduler_dry_run.log"
```

检查项：

- [ ] dry-run 输出 `pass_id`。
- [ ] dry-run 输出 `artifact_path`。
- [ ] 输出包含 GFS 或明确的 GFS 阻塞原因。
- [ ] 输出包含 IFS 或明确的 IFS 阻塞原因。
- [ ] `adapter_download_called=false`。
- [ ] `slurm_submit_called=false`。
- [ ] `slurm_status_sync_called=false`。
- [ ] `slurm_cancellation_called=false`。
- [ ] `shud_runtime_called=false`。
- [ ] `hydro_result_table_writes=false`。
- [ ] `met_result_table_writes=false`。
- [ ] `pipeline_status_writes=false`。
- [ ] `pipeline_event_writes=false`。

### 8.2 GFS 下载和完整性

检查项：

- [ ] GFS cycle_time 识别正确，时区为 UTC。
- [ ] GFS cycle 状态从 discovered 进入 downloading。
- [ ] 必需 forecast hour 覆盖 MVP 窗口。
- [ ] 必需变量可生成 `PRCP/TEMP/RH/wind/Rn/Press`。
- [ ] raw 文件进入对象存储或 raw mirror。
- [ ] raw manifest 记录来源、文件数、大小、checksum 或 etag。
- [ ] 文件大小、时间戳、forecast hour 和变量级校验通过。
- [ ] 失败下载有重试记录和稳定 error_code。
- [ ] 下载失败不会生成假 canonical 或假 forcing。

### 8.3 IFS 下载和短时效处理

检查项：

- [ ] IFS cycle_time 识别正确，时区为 UTC。
- [ ] 00/12 UTC 周期按可用时效作为完整 7 天候选。
- [ ] 06/18 UTC 周期按实际可用时效处理。
- [ ] 若实际可用时效为 144h，API 和 UI 必须展示 shorter-horizon 标注。
- [ ] IFS Open Data 不足或过期时，系统显示 unavailable/blocked，而不是生成补齐数据。
- [ ] IFS raw mirror 必须先落对象存储，再进入 canonical/forcing。
- [ ] IFS 失败不影响已成功的 GFS 产品展示。

### 8.4 met.forecast_cycle 状态检查

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "
select source_id, cycle_time, status, retry_count, error_code, error_message
from met.forecast_cycle
where source_id in ('GFS','gfs','IFS','ifs')
order by cycle_time desc
limit 20;
" | tee "$MVP_E2E_EVIDENCE_ROOT/met_cycle_status.log"
```

检查项：

- [ ] 周期状态符合业务阶段。
- [ ] 状态不混用 `success`、`done`、`complete` 等非约定值。
- [ ] 失败周期有 error_code 和 error_message。
- [ ] partial/unavailable 场景不会被标为 ready。

## 9. canonical、forcing 和 station series 检查

### 9.1 canonical 产品检查

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "
select source_id, cycle_time, variable, count(*) as product_count,
       min(valid_time) as min_valid_time, max(valid_time) as max_valid_time
from met.canonical_met_product
where source_id in ('GFS','gfs','IFS','ifs')
group by source_id, cycle_time, variable
order by cycle_time desc, source_id, variable;
" | tee "$MVP_E2E_EVIDENCE_ROOT/canonical_products.log"
```

检查项：

- [ ] canonical 产品按 source/cycle/variable 入库。
- [ ] valid_time 轴连续或缺口有质量标记。
- [ ] 每个产品有 object_uri、checksum、unit 和 lineage。
- [ ] malformed raw 数据会被 QC 阻断。

### 9.2 forcing version 检查

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "
select forcing_version_id, model_id, source_id, cycle_time, start_time, end_time,
       station_count, forcing_package_uri, checksum
from met.forcing_version
where model_id like '%qhh%' or forcing_version_id like '%qhh%'
order by created_at desc
limit 20;
" | tee "$MVP_E2E_EVIDENCE_ROOT/forcing_versions.log"
```

检查项：

- [ ] GFS 和 IFS 各自生成 forcing_version 或明确记录未生成原因。
- [ ] forcing_version 绑定 model_id、source_id、cycle_time。
- [ ] station_count 与 QHH 预期一致或差异有原因。
- [ ] forcing_package_uri 可访问。
- [ ] checksum 存在。
- [ ] start_time/end_time 覆盖模型运行窗口。

### 9.3 station-series 数据入库检查

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "
select source_id, variable, count(*) as row_count,
       count(distinct station_id) as station_count,
       min(valid_time) as min_valid_time,
       max(valid_time) as max_valid_time
from met.forcing_station_timeseries
where forcing_version_id in (
  select forcing_version_id
  from met.forcing_version
  where model_id like '%qhh%'
  order by created_at desc
  limit 4
)
group by source_id, variable
order by source_id, variable;
" | tee "$MVP_E2E_EVIDENCE_ROOT/station_series_db_coverage.log"
```

检查项：

- [ ] 六个 MVP 变量均有记录：`PRCP`、`TEMP`、`RH`、`wind`、`Rn`、`Press`。
- [ ] 每个变量覆盖 station_count 合理。
- [ ] 每个变量 valid_time 范围与 forcing_version 一致。
- [ ] value 不包含非有限值，或非有限值被 QC 阻断。
- [ ] unit 非空。
- [ ] quality_flag 非空。
- [ ] 不同 source/cycle 的样本不会混在同一 forcing_version 中。

## 10. 正式 scheduler 和模型运行 E2E

### 10.1 生产模式 plan-production

仅在前序检查通过后执行真实提交；该路径必须使用 `--submit`：

```bash
uv run nhms-pipeline plan-production \
  --submit \
  --source gfs \
  --source IFS \
  --lookback-hours 24 \
  --cycle-lag-hours 6 \
  --max-cycles-per-source 1 \
  --workspace-root "$WORKSPACE_ROOT" \
  2>&1 | tee "$MVP_E2E_EVIDENCE_ROOT/scheduler_plan.log"
```

检查项：

- [ ] 生产模式输出 scheduler pass evidence。
- [ ] 至少一个 GFS 或 IFS candidate 被提交，或明确记录 blocked reason。
- [ ] `submitted_count` 与预期一致。
- [ ] 每个 candidate 有 source_id、cycle_time、model_id、scenario_id。
- [ ] 每个 submitted candidate 有 run_id 和 forcing_version_id。
- [ ] active duplicate run 不被重复提交。
- [ ] 已终态成功 run 不被重复提交，除非显式 retry/rerun。
- [ ] Slurm preflight 通过。
- [ ] 若 preflight 阻塞，不能写入假 run 结果。

### 10.2 pipeline job 持久化

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "
select job_id, run_id, cycle_id, model_id, stage, status, slurm_job_id,
       submitted_at, started_at, finished_at, retry_count, log_uri, error_code
from ops.pipeline_job
where model_id like '%qhh%' or run_id like '%qhh%'
order by created_at desc
limit 100;
" | tee "$MVP_E2E_EVIDENCE_ROOT/pipeline_jobs.log"
```

检查项：

- [ ] 存在 `download` stage。
- [ ] 存在 `convert` stage。
- [ ] 存在 `forcing` stage。
- [ ] 存在 `forecast` 或 `shud_forecast` stage。
- [ ] 存在 `parse` stage。
- [ ] 存在 `frequency` stage，若 QHH 无频率曲线则质量状态明确为 `no_frequency_curve`。
- [ ] 存在 `publish` stage。
- [ ] 每个 stage 有状态、时间戳和可追溯 run_id。
- [ ] Slurm stage 有 slurm_job_id。
- [ ] 失败 stage 有 error_code。
- [ ] log_uri 不为空且路径有界。

### 10.3 hydro_run 和 river_timeseries 检查

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "
select run_id, run_type, scenario_id, model_id, basin_version_id, forcing_version_id,
       source_id, cycle_time, start_time, end_time, status, slurm_job_id,
       output_uri, log_uri, error_code
from hydro.hydro_run
where model_id like '%qhh%' or run_id like '%qhh%'
order by created_at desc
limit 20;
" | tee "$MVP_E2E_EVIDENCE_ROOT/hydro_runs.log"

psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "
select rt.run_id, h.source_id, h.cycle_time, rt.variable,
       count(*) as row_count,
       count(distinct rt.river_segment_id) as segment_count,
       min(rt.valid_time) as min_valid_time,
       max(rt.valid_time) as max_valid_time
from hydro.river_timeseries rt
join hydro.hydro_run h on h.run_id = rt.run_id
where h.model_id like '%qhh%' and rt.variable = 'q_down'
group by rt.run_id, h.source_id, h.cycle_time, rt.variable
order by h.cycle_time desc
limit 20;
" | tee "$MVP_E2E_EVIDENCE_ROOT/river_timeseries_coverage.log"
```

检查项：

- [ ] hydro_run 状态进入 `parsed`、`frequency_done` 或 `published`。
- [ ] `q_down` 入库非空。
- [ ] segment_count 接近模型输出河段数。
- [ ] valid_time 范围覆盖预报窗口。
- [ ] unit 为 `m3/s` 或等价流量单位。
- [ ] 失败运行不会发布 display product。

## 11. 后端 API E2E 检查

### 11.1 latest-product API

```bash
curl -sS "$NHMS_API_BASE_URL/mvp/qhh/latest-product?source=GFS" \
  | tee "$MVP_E2E_EVIDENCE_ROOT/api_latest_product_gfs.json"

curl -sS "$NHMS_API_BASE_URL/mvp/qhh/latest-product?source=IFS" \
  | tee "$MVP_E2E_EVIDENCE_ROOT/api_latest_product_ifs.json"
```

检查项：

- [ ] 返回 `status=ok`。
- [ ] 返回 basin_id 或 basin_version_id。
- [ ] 返回 model_id。
- [ ] 返回 river_network_version_id。
- [ ] 返回 run_id。
- [ ] 返回 forcing_version_id。
- [ ] 返回 source_id 和 cycle_time。
- [ ] 返回 station_count 和 segment_count。
- [ ] 不选择 failed/cancelled/incomplete 产品。
- [ ] IFS 短时效返回 actual horizon 或 unavailable reason。

### 11.2 station inventory API

```bash
curl -sS "$NHMS_API_BASE_URL/met/stations?model_id=<model_id>&limit=20" \
  | tee "$MVP_E2E_EVIDENCE_ROOT/api_met_stations.json"
```

检查项：

- [ ] 返回 station list。
- [ ] 每个 station 有 station_id。
- [ ] 每个 station 有经纬度或明确 geometry unavailable。
- [ ] station_count 与 latest-product 一致或差异有解释。
- [ ] 不返回非该模型使用的 station。

### 11.3 station-series API

```bash
curl -sS "$NHMS_API_BASE_URL/met/stations/<station_id>/series?forcing_version_id=<forcing_version_id>&variables=PRCP,TEMP,RH,wind,Rn,Press&limit=2000" \
  | tee "$MVP_E2E_EVIDENCE_ROOT/api_station_series.json"
```

检查项：

- [ ] 返回 station_id。
- [ ] 返回 forcing_version_id。
- [ ] 返回 source_id。
- [ ] 返回 cycle_time。
- [ ] 六个变量均有 series 或明确 unavailable reason。
- [ ] 每个变量有 unit。
- [ ] 每个点有 valid_time、value、quality_flag。
- [ ] limit 生效。
- [ ] truncated 字段正确。
- [ ] 请求不存在 station 时返回稳定 404 或 unavailable。
- [ ] 请求非法变量时返回稳定 validation error。
- [ ] 不返回合成点或前端 fixture 点。

### 11.4 forecast-series q_down API

```bash
curl -sS "$NHMS_API_BASE_URL/basin-versions/<basin_version_id>/river-segments/<segment_id>/forecast-series?river_network_version_id=<river_network_version_id>&issue_time=latest&variables=q_down&scenarios=GFS,IFS" \
  | tee "$MVP_E2E_EVIDENCE_ROOT/api_forecast_series_qdown.json"
```

检查项：

- [ ] 返回 selected segment_id。
- [ ] 返回 `q_down` 点列。
- [ ] GFS scenario 可展示。
- [ ] IFS scenario 可展示或明确 unavailable。
- [ ] unit 是流量单位。
- [ ] valid_time 单调递增。
- [ ] 不出现 stage/water level 文案。
- [ ] 无数据时返回明确 empty/unavailable，不生成假曲线。

### 11.5 pipeline/status/stages/jobs/logs API

```bash
curl -sS "$NHMS_API_BASE_URL/pipeline/status?source=GFS&cycle_time=<cycle_time>" \
  | tee "$MVP_E2E_EVIDENCE_ROOT/api_pipeline_status.json"

curl -sS "$NHMS_API_BASE_URL/pipeline/stages?source=GFS&cycle_time=<cycle_time>" \
  | tee "$MVP_E2E_EVIDENCE_ROOT/api_pipeline_stages.json"

curl -sS "$NHMS_API_BASE_URL/jobs?source=GFS&cycle_time=<cycle_time>&limit=100" \
  | tee "$MVP_E2E_EVIDENCE_ROOT/api_jobs.json"

curl -sS "$NHMS_API_BASE_URL/jobs/<job_id>/logs" \
  | tee "$MVP_E2E_EVIDENCE_ROOT/api_job_logs.json"
```

检查项：

- [ ] pipeline status 返回 current_state。
- [ ] stages 覆盖 download/convert/forcing/forecast/parse/frequency/publish。
- [ ] jobs 表包含 run_id、stage、status、slurm_job_id、timestamps、retry_count。
- [ ] logs 返回 bounded tail。
- [ ] 不存在 job 时返回稳定 404。
- [ ] log_uri 不可读时返回稳定错误。

## 12. `/hydro-met` 浏览器 E2E

### 12.1 启动服务

```bash
# 后端服务需连接目标 DATABASE_URL，并能访问对象根和日志根。
uv run uvicorn apps.api.main:app --host 0.0.0.0 --port 8000 \
  2>&1 | tee "$MVP_E2E_EVIDENCE_ROOT/api_server.log"

cd apps/frontend
corepack pnpm build
corepack pnpm preview --host 0.0.0.0 --port 4173 \
  2>&1 | tee "../../$MVP_E2E_EVIDENCE_ROOT/frontend_preview.log"
```

检查项：

- [ ] `/health` 或等价健康检查可访问。
- [ ] `/api/v1/...` 从前端同源或配置 API base 可访问。
- [ ] 前端 preview 使用生产构建产物。
- [ ] 页面没有依赖本地 mock 服务。

### 12.2 手工浏览器检查

访问：

```text
<frontend-base-url>/hydro-met?source=GFS
```

检查项：

- [ ] 页面自动加载 latest-product。
- [ ] 显示 QHH/有限流域身份。
- [ ] 显示 source、cycle_time、run_id、forcing_version_id。
- [ ] 站点列表加载。
- [ ] 站点地图点加载。
- [ ] 点击站点后显示六变量 forcing 曲线。
- [ ] 曲线显示 unit 和 quality_flag。
- [ ] 河段列表加载。
- [ ] 河段地图线加载。
- [ ] 点击河段后显示 `q_down` 流量曲线。
- [ ] GFS/IFS 切换生效。
- [ ] IFS 144h 场景展示短时效标注。
- [ ] 页面文案使用“流量/流量曲线/河段流量”，不使用“水位”。
- [ ] 无数据时展示 unavailable/empty，而不是假曲线。
- [ ] 控制台无未处理异常。

### 12.3 自动浏览器检查

```bash
cd apps/frontend
NHMS_E2E_LIVE_API_BASE="$NHMS_API_BASE_URL" \
NHMS_E2E_FRONTEND_BASE="$NHMS_FRONTEND_BASE_URL" \
corepack pnpm test:e2e -- hydro-met.spec.ts --project=mocked-regression-chromium --workers=1 \
  2>&1 | tee "../../$MVP_E2E_EVIDENCE_ROOT/e2e_hydro_met_live.log"
```

检查项：

- [ ] 若当前 Playwright spec 仍是 mocked API，证据必须标注 mocked，不可算 live。
- [ ] 若使用 live API，记录 API base、frontend base 和 target run_id。
- [ ] 截图或 trace 存档。
- [ ] 失败时保留 Playwright report。

## 13. `/ops` 浏览器 E2E

### 13.1 手工浏览器检查

访问：

```text
<frontend-base-url>/ops?source=GFS&cycle=<cycle_time>
```

检查项：

- [ ] 非 operator 角色不能执行 retry/cancel。
- [ ] operator/model_admin/sys_admin 可以看到运维控件。
- [ ] source/cycle selector 正确。
- [ ] stage cards 显示当前周期各阶段。
- [ ] jobs table 显示 stage、status、slurm_job_id、duration、retry_count。
- [ ] 点击日志按钮能打开 log modal。
- [ ] queue depth 显示。
- [ ] success-rate 和 stage-duration 显示或明确 unavailable。
- [ ] failed/submission_failed/permanently_failed job 显示 retry 入口。
- [ ] succeeded/running job 不显示误导性 retry。
- [ ] 页面不会混入其他 cycle 的 jobs。

### 13.2 自动浏览器检查

```bash
cd apps/frontend
NHMS_E2E_LIVE_API_BASE="$NHMS_API_BASE_URL" \
NHMS_E2E_FRONTEND_BASE="$NHMS_FRONTEND_BASE_URL" \
corepack pnpm test:e2e -- monitoring.spec.ts --project=mocked-regression-chromium --workers=1 \
  2>&1 | tee "../../$MVP_E2E_EVIDENCE_ROOT/e2e_ops_live.log"
```

检查项：

- [ ] 若使用 mock，证据标注 mocked。
- [ ] 若使用 live API，记录 operator identity、source、cycle_time、run_id。
- [ ] failed row、log modal、retry request、retry job 终态都有证据。
- [ ] 截图或 trace 存档。

## 14. 受控失败、retry 和 cancel E2E

### 14.1 受控失败准备

必须使用安全、可回滚、不会污染成功产品的失败方式。允许选项：

- 使用测试 run_id 和测试 model/cycle。
- 使用专门的 controlled failure flag。
- 使用 malformed output fixture 触发 parser/QC failure。
- 使用 dry-run/fake worker 模式验证 API 和 UI，不声明 live retry。

禁止选项：

- 不得破坏生产 Basins 资产。
- 不得删除真实对象存储产品。
- 不得修改 active model 状态。
- 不得污染已发布成功 run。

### 14.2 失败可见性检查

检查项：

- [ ] 失败 run 写入 hydro_run 或 pipeline_job。
- [ ] 失败 stage 在 `/api/v1/pipeline/stages` 可见。
- [ ] 失败 job 在 `/api/v1/jobs` 可见。
- [ ] 失败 job 有 error_code。
- [ ] 失败 job 有 log_uri。
- [ ] `/ops` 页面显示失败行。
- [ ] log modal 能看到失败原因。

### 14.3 retry API 检查

```bash
curl -sS -X POST "$NHMS_API_BASE_URL/runs/<failed_run_id>/retry" \
  -H "X-User-Role: operator" \
  | tee "$MVP_E2E_EVIDENCE_ROOT/api_retry_response.json"
```

检查项：

- [ ] operator 可发起 retry。
- [ ] viewer/未授权用户不能发起 retry。
- [ ] retry 返回 pipeline_job_id 或 job_id。
- [ ] retry_count 增加。
- [ ] 新 job 状态进入 submitted/running/succeeded 或明确 submission_failed。
- [ ] 若 Slurm submission_failed，返回稳定 error_code。
- [ ] 成功 retry 不覆盖原失败证据。
- [ ] 成功 retry 不删除 sibling success output。

### 14.4 cancel API 检查

```bash
curl -sS -X POST "$NHMS_API_BASE_URL/runs/<running_run_id>/cancel" \
  -H "X-User-Role: operator" \
  | tee "$MVP_E2E_EVIDENCE_ROOT/api_cancel_response.json"
```

检查项：

- [ ] operator 可取消 running/submitted run。
- [ ] viewer/未授权用户不能取消。
- [ ] Slurm active job 被 scancel 或记录幂等已终态。
- [ ] pipeline_job 状态更新为 cancelled 或稳定失败。
- [ ] hydro_run 状态更新为 cancelled，若适用。
- [ ] cancel 不影响其他已成功 run。

## 15. 负向和边界测试

### 15.1 数据不可用

- [ ] 无 latest-product 时，`/hydro-met` 显示暂无可用产品。
- [ ] station list 为空时，页面显示暂无代站。
- [ ] station-series 为空时，曲线区域显示暂无样本。
- [ ] forecast-series 为空时，河段曲线显示暂无流量曲线。
- [ ] GFS 失败时，IFS 成功产品仍可展示。
- [ ] IFS 失败时，GFS 成功产品仍可展示。
- [ ] 所有失败都不生成假数据。

### 15.2 参数校验

- [ ] 非法 source 返回 validation error。
- [ ] 非法 cycle_time 返回 validation error。
- [ ] 非法 station_id 返回 not found。
- [ ] 非法 segment_id 返回 not found。
- [ ] 非法 variable 返回 validation error。
- [ ] limit 超限返回 validation error 或自动 capped，并返回 capped metadata。
- [ ] from > to 返回 validation error。

### 15.3 IFS 短时效

- [ ] IFS 06/18 UTC 若只有 144h，不补齐到 168h。
- [ ] API 返回 actual end time。
- [ ] UI 明确提示可用时效不足 7 天。
- [ ] GFS/IFS 对比图不会误把两条曲线画成同一长度。

### 15.4 no_frequency_curve 边界

- [ ] QHH 无频率曲线时，产品质量为 `no_frequency_curve`。
- [ ] 不展示真实重现期或预警等级。
- [ ] `/hydro-met` 和 `/ops` 不把 `no_frequency_curve` 解释为失败。
- [ ] 文案说明这是 MVP 接受的质量状态，不是洪水预警能力证明。

## 16. 性能和资源测试

### 16.1 API 响应时间

建议阈值：

| API | 建议 P95 阈值 | 说明 |
| --- | ---: | --- |
| latest-product | 1s | 启动页关键路径 |
| station inventory | 2s | 带分页/limit |
| station-series | 2s | 单站六变量，有 limit |
| forecast-series | 2s | 单河段 GFS/IFS 曲线 |
| pipeline status/stages | 1s | 运维刷新关键路径 |
| jobs list | 2s | 带分页/筛选 |
| job logs | 2s | bounded tail |

检查项：

- [ ] 记录每个 API 至少 20 次请求耗时。
- [ ] 计算 p50、p95、max。
- [ ] 超阈值 API 记录 query plan 或后端日志。
- [ ] station-series 不做无界全表扫描。
- [ ] jobs/logs 不返回超大 payload。

### 16.2 前端加载性能

- [ ] `/hydro-met` 首屏可在可接受时间内显示 skeleton 或主要布局。
- [ ] `/hydro-met` 数据加载失败时不白屏。
- [ ] `/ops` stage 和 jobs 刷新不造成页面卡顿。
- [ ] ECharts 曲线点数过多时启用 limit/truncation。
- [ ] MapLibre 图层缺数据时有降级状态。

### 16.3 Slurm 资源

- [ ] 记录每个 SHUD job 的 elapsed。
- [ ] 记录 MaxRSS 或可用内存指标。
- [ ] 记录 CPU/线程配置。
- [ ] 记录 workspace 和 object store 占用。
- [ ] 超过资源阈值时标记 blocker 或 risk。

## 17. 安全、权限和审计测试

### 17.1 角色权限

| 操作 | viewer | analyst | operator | model_admin | sys_admin |
| --- | --- | --- | --- | --- | --- |
| 查看 `/hydro-met` | 允许 | 允许 | 允许 | 允许 | 允许 |
| 查看 `/ops` | 拒绝或只读 | 拒绝或只读 | 允许 | 允许 | 允许 |
| 查看日志 | 拒绝或受限 | 可选 | 允许 | 允许 | 允许 |
| retry run | 拒绝 | 拒绝 | 允许 | 允许 | 允许 |
| cancel run | 拒绝 | 拒绝 | 允许 | 允许 | 允许 |

检查项：

- [ ] 未登录请求 mutation 返回 `AUTH_REQUIRED` 或等价错误。
- [ ] 角色不足请求 mutation 返回 `RBAC_FORBIDDEN` 或等价错误。
- [ ] live IdP 未证明时，生产 readiness 不被声明为通过。
- [ ] mutation 有 audit record。
- [ ] audit 不记录明文密码或 token。

### 17.2 日志和敏感信息

- [ ] API 响应不泄露数据库 URL 明文密码。
- [ ] scheduler evidence 不泄露 secret-shaped 值。
- [ ] Slurm 脚本环境变量经过 allowlist 或 redaction。
- [ ] log tail 不返回超过限制的内容。
- [ ] 前端错误提示不暴露内部绝对路径。

## 18. 证据打包

### 18.1 必需证据文件

本次 E2E 完成后，证据目录至少包含：

```text
artifacts/mvp-e2e/<run_id>/
  environment.md
  command_index.md
  db_connectivity.log
  db_extensions.log
  storage_roots.log
  slurm_sinfo.log
  slurm_smoke_sacct.log
  basins_inventory.json
  qhh_model_registry.log
  qhh_station_count.log
  qhh_segment_count.log
  scheduler_dry_run.log
  scheduler_plan.log
  met_cycle_status.log
  canonical_products.log
  forcing_versions.log
  station_series_db_coverage.log
  hydro_runs.log
  river_timeseries_coverage.log
  api_latest_product_gfs.json
  api_latest_product_ifs.json
  api_met_stations.json
  api_station_series.json
  api_forecast_series_qdown.json
  api_pipeline_status.json
  api_pipeline_stages.json
  api_jobs.json
  api_job_logs.json
  api_retry_response.json
  api_cancel_response.json
  e2e_hydro_met_live.log
  e2e_ops_live.log
  summary.md
```

### 18.2 summary.md 模板

```markdown
# QHH MVP production-like E2E summary

run_id: <run_id>
target_environment: <env name>
started_at: <utc>
finished_at: <utc>
operator: <redacted id>

## Result

- Overall: passed / blocked / failed / partially_passed
- Final production readiness claimed: false

## Product identity

- basin: qhh
- source/cycle: GFS <cycle>, IFS <cycle>
- model_id:
- run_id:
- forcing_version_id:
- station_count:
- segment_count:

## Passed surfaces

- DB:
- object store:
- Slurm:
- source download:
- forcing station series:
- SHUD runtime:
- station-series API:
- forecast-series API:
- /hydro-met browser:
- /ops browser:
- retry/cancel:

## Blockers

| blocker_id | surface | reason | owner | removal criteria |
| --- | --- | --- | --- | --- |

## Evidence index

| file | purpose | status |
| --- | --- | --- |
```

## 19. 验收闸门

### 19.1 内部 MVP 试运行通过条件

以下条件全部满足时，可以判定“内部 MVP 试运行通过”：

- [ ] QHH active model、station、river segment 可查询。
- [ ] 至少一个 GFS cycle 完成 download -> canonical -> forcing -> SHUD -> parse -> publish。
- [ ] station-series API 返回六变量真实样本或明确可解释的变量缺失原因。
- [ ] forecast-series API 返回 QHH 河段 `q_down` 曲线。
- [ ] `/hydro-met` 连目标后端展示 station 曲线和 river flow 曲线。
- [ ] `/ops` 连目标后端展示 stages、jobs 和 logs。
- [ ] 至少一次受控失败在 `/ops` 可见。
- [ ] retry API 可创建 retry job，且终态有证据。
- [ ] 所有证据标注 deterministic、mocked、production-like 或 live。
- [ ] summary 明确 `final_production_readiness_claimed=false`。

### 19.2 任何一项出现即阻塞

- [ ] station-series 或 forecast-series 使用前端假数据。
- [ ] SHUD 任务直接从外部网站下载气象资料，而不是使用 raw mirror/canonical/forcing 链路。
- [ ] IFS 144h 数据被静默补齐成 168h。
- [ ] q_down 被前端写成水位。
- [ ] retry 覆盖或删除了原失败证据。
- [ ] Slurm 日志在 compute node local 路径，登录节点不可读。
- [ ] API 或证据泄露明文密钥。
- [ ] deterministic/mock 结果被写成 live 生产就绪证明。
- [ ] 数据库、对象存储或 Slurm 指向错误环境。

## 20. 最终交付物

完成本清单后，应提交或归档：

- [ ] E2E 证据目录压缩包或对象存储 URI。
- [ ] `summary.md`。
- [ ] scheduler evidence artifact。
- [ ] readiness validation summary，若执行。
- [ ] 失败项/blocker 列表。
- [ ] 是否允许进入试运行的明确结论。

## 21. 推荐执行顺序

1. 执行静态质量和契约预检。
2. 执行数据库、对象存储、Slurm 预检。
3. 检查 QHH 模型、站点和河段基础数据。
4. 执行 scheduler dry-run。
5. 执行 scheduler production-like plan。
6. 等待 Slurm 和 pipeline 终态。
7. 检查 canonical、forcing、station-series 和 q_down 入库。
8. 执行 backend API curl 检查。
9. 执行 `/hydro-met` 浏览器检查。
10. 执行 `/ops` 浏览器检查。
11. 构造受控失败并执行 retry/cancel。
12. 收集证据、填写 summary、列出 blockers。

## 22. 维护规则

- 新增 MVP 功能时，必须先补本清单对应测试项。
- 任何测试项从 mocked 升级为 live，必须记录新的证据文件和执行命令。
- 任何生产依赖未执行，必须写 skipped/blocked reason。
- 不删除历史 evidence；新证据用新的 `run_id`。
- qhh 诊断脚本可作为复现工具，但正式 E2E 以 `nhms-pipeline plan-production` 为准。
- 本清单通过后只能说明内部 MVP 试运行通过，不能自动声明最终生产上线。
