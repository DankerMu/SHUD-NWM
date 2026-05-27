# 项目进度

最后更新：2026-05-27，测试环境。

用途：作为跨 session 继承的项目真实进度索引。本文只保留当前结论、可用能力、证据边界和下一步，不再堆叠历史 review 细节。

## 当前结论

- M21 QHH 水文气象展示 + 运维监控 MVP issue 链路已完成：Epic #202 已关闭，子任务 #203-#214 已全部关闭，最后 PR #226 已合并到 `master`，merge commit 为 `ec5d535db334ddc6dc4f899742c3ff3d667e2df0`。
- MVP 范围已冻结为 QHH/有限流域、GFS 主源、IFS 并行源、河段流量 `q_down`、forcing 代站 `PRCP/TEMP/RH/wind/Rn/Press` 和 pipeline 运维闭环。
- MVP 不承诺水位 `stage`、全国所有流域、CLDAS、ERA5 近实时、真实全国 MVT/PBF 或最终生产就绪。
- 前端 MVP 两个入口已落地：`/hydro-met` 水文气象展示，`/ops` 系统运维。
- 内部 MVP deterministic E2E / browser smoke 已完成；目标环境 live E2E 尚未完成，不能声明 final production readiness。
- 当前正在准备将仓库同步到生产环境复测。迁移目的不是声明上线完成，而是在更接近真实生产的 Linux/Slurm/PostgreSQL/数据目录环境中复跑 E2E，区分哪些问题是本地测试环境导致，哪些是代码、数据资产、接口契约或生产配置本身的问题。
- 2026-05-27 已将本地仓库同步到生产环境 `/home/nwm/NWM`，包括 `data/Basins` 软链指向的真实数据和 `.nhms-runs` 运行缓存；该动作只是 live E2E 复测前置条件，不代表 15 个已记录问题已修复，也不代表 production ready。本地 `.venv` 与 `apps/frontend/node_modules` 已在当前测试机从 macOS 迁移后适配过，但随仓库同步到生产机的依赖目录不能作为生产环境 receipt；生产机仍需按自身 Python/Node 版本和 `/home/nwm/NWM` 路径重建依赖并记录结果。
- 两节点 MVP 边界已冻结为 `compute_control` + `display_readonly`：22 节点负责正式 scheduler、Slurm/Gateway、产物发布和 retry/cancel；27 节点只读消费 DB 与 published artifacts，提供 `/hydro-met`、`/ops`、日志查看、异常展示、诊断信息复制和 22 人工处理建议。MVP 阶段 27 不触发 retry/cancel，不调用 Slurm Gateway，不写 hydro/met/pipeline 终态，不读取 22 私有 workspace。

## MVP 证据状态

统一证据索引：[`docs/runbooks/qhh-mvp-smoke-evidence.md`](docs/runbooks/qhh-mvp-smoke-evidence.md)。

| 证据面 | 当前状态 | 边界 |
| --- | --- | --- |
| QHH GFS backend smoke | 已有 `qhh_gfs_2026052100_smoke` live diagnostic 证据 | 证明诊断脚本链路可跑通，不证明正式 scheduler readiness |
| QHH IFS continuous cycle | 已有 `fcst_ifs_2026052106_basins_qhh_shud` live diagnostic 证据 | 证明记录周期可跑通，不证明未来 IFS 可用性 |
| `/hydro-met` browser smoke | Playwright deterministic mocked API，`1 passed` | 证明 UI/API wiring、station-series、`q_down`、GFS/IFS、IFS 144h 标注，不证明 live backend |
| `/ops` controlled failure/retry smoke | Playwright deterministic mocked API，`11 passed` | 证明旧单体模式下失败行、日志、operator retry 和 retry job/stage 终态；两节点 MVP 中仅作为 regression，不证明 27 可触发 live retry |
| Backend/API contract | `uv run pytest -q tests/test_forecast_api.py tests/test_api_contract.py`，`137 passed, 8 warnings` | 证明 deterministic fixture 合同，不证明目标环境数据 |
| Frontend unit/build | `corepack pnpm test`、`corepack pnpm build`，`536 tests passed` 且 build 通过 | 证明前端健康，不证明 live 部署 |
| OpenAPI/API types | Redocly lint 与 frontend API type drift check 已通过 | 证明契约一致，不证明 endpoint live availability |
| Live target E2E | skipped/blocked | 缺 target DB、对象存储、Slurm、source credentials、共享日志、IdP/operator、target browser、alert、rollback receipts |

## 当前系统能力

### 后端与数据链路

- FastAPI 后端已实现 forecast、models、pipeline、hindcast、flood alerts、best-available、state snapshots、data-source 等路由。
- OpenAPI 契约位于 `openapi/nhms.v1.yaml`，前端类型由该文件生成。
- 数据库 migration 覆盖 core/met/hydro/flood/map/ops schema、索引、pipeline 字段和 best-available lineage。
- GFS、IFS、ERA5 adapter 已实现并有 deterministic 测试覆盖；CLDAS 仍按受限数据源处理。
- Canonical conversion、forcing production、SHUD runtime adapter、output parser、state manager、洪水频率拟合、重现期计算、tile publisher 已实现。
- `met.forcing_station_timeseries` 已由 forcing producer 写入，覆盖 `PRCP/TEMP/RH/wind/Rn/Press`。
- Station-series API、QHH latest-product API、forecast-series `q_down` 查询和 `/hydro-met` deterministic 消费已落地。
- Orchestrator / production scheduler 支持 forecast/analysis/hindcast、GFS/IFS 周期发现、active runnable model 发现、Slurm job array、retry/cancel、partial success、publish stage、pipeline persistence、dry-run evidence 和 readiness ingestion。
- QHH `run_qhh_*` 脚本保留为诊断/复现工具；正式生产调度入口是 `nhms-pipeline plan-production`。

### 前端

- 有效前端为 `apps/frontend`：Vite + React + TypeScript + MapLibre + ECharts + Zustand + OpenAPI-generated types。
- 已实现主要路由：
  - `/hydro-met`：MVP 水文气象页，自动 latest-product bootstrap，站点列表/地图/forcing 曲线，河段列表/地图/`q_down` 曲线，GFS/IFS source 选择和 IFS shorter-horizon 标注。
  - `/ops`：MVP 运维页，source/cycle selector、stage cards、jobs table、log modal、queue/metrics 和 operator RBAC；两节点 `display_readonly` 目标态会把真实 retry/cancel 按钮替换为诊断信息复制和 22 人工处理建议。
  - `/`、`/overview`：全国总览。
  - `/basins/:basinId`：流域钻取。
  - `/segments/:segmentId`：河段预报详情。
  - `/forecast`、`/flood-alerts`、`/monitoring`、`/meteorology`：保留为已有功能和复用基础。
- 前端测试覆盖 unit/component、mock API E2E、preview E2E、visual evidence lane、build、bundle size 和 API type drift。

### Basins 与样例数据

- 开发环境通过 `data/Basins -> /volume/data/nwm/Basins` 软链接接入 Basins 数据；这是开发期依赖，不是可迁移 artifact。
- 当前可发现 13 个 SHUD 模型目录，包括 `qhh`、`heihe`、`kashigeer`、`weiganhe`、`xinanjiang_upstream`、`hetianhe`、`qinyijiang`、`keliya`、`tailanhe` 和 `zhaochen/{WEM,HHY,MC,BST}`。
- QHH 已有真实 GFS/IFS `2026052100` 与 `2026052106` 多周期诊断闭环，仍作为 reproduction evidence 使用。

## 历史里程碑索引

- Epic #120：基础阶段已完成并关闭。
- M9 Basins：Epic #133，子任务 #134-#139 已完成并关闭。
- M10 Production Closure：Epic #146，子任务 #147-#152 已完成并关闭。
- M11 Overview + Basin Drilldown：Epic #159，子任务 #160-#165 已完成并关闭。
- M12 Segment Forecast Detail：`/segments/:segmentId` 本地实现已完成。
- M19 Production Readiness Proof：`nhms-production validate-readiness` 已实现 deterministic evidence / live proof / blocker truth table。
- M20 Production Scheduler Automation：#192-#196 已完成 scheduler dry-run、Slurm evidence、state idempotency、retry/cancel 和 readiness 文档。
- M21 QHH Hydro-met/Ops MVP：#202-#214 已完成并关闭。

## 仍需目标环境补齐的 live proof

这些不是内部 MVP deterministic 完成度缺口，而是正式生产上线前必须在目标环境补齐的 live 证据：

- 生产环境仓库同步 receipt：已完成本地到 `nwm@210.77.77.27:32099:/home/nwm/NWM` 的全量同步；源 commit `42e7018`，远端 commit `42e7018`，未使用 `--delete`，`data/Basins` 以实体目录落盘而非远端软链，`.nhms-runs` 已迁移。远端校验：repo `66G`，`.nhms-runs` `27G`，`data/Basins` `34G`，`data/Basins` top-level entries `10`，`/home` 剩余 `978G`。该迁移只作为 live E2E 复测前置条件，不等同于 bug 修复或 production readiness。
- 生产环境复测归因矩阵：对 `docs/bugs.md` 中 15 个问题逐项标注复测结果，区分 `environment-only`、`production-config`、`data-contract`、`code-contract`、`test-runbook` 和 `frontend-feedback`，避免把环境迁移后的偶然通过误判为根因已修复。
- two-node readonly display boundary receipt：27 必须以 `NHMS_SERVICE_ROLE=display_readonly` 或等价配置启动；`/api/v1/slurm/*` 不可用；`POST /api/v1/runs/{run_id}/retry|cancel` 返回人工处理提示且不调用 Gateway、不写 DB；`/ops` 不显示真实控制按钮，只显示诊断复制和 22 处理建议。
- published artifacts receipt：22 写入的 log/manifest/display product 必须落到 27 可读发布区，DB 中 `log_uri` 指向 published URI；27 不以挂载 22 私有 workspace 作为日志读取前提。
- cross-plane identity receipt：两节点 E2E 必须用同一个 `run_id/source/cycle_time/model_id/basin_id` 串起 22 生产、DB 状态、published logs、latest-product、`/hydro-met` 和 `/ops`，不能只用 historical latest 证明通过。

- target PostgreSQL/PostGIS/TimescaleDB receipt。
- 对象存储和共享 Slurm log root receipt。
- `nhms-pipeline plan-production --plan` scheduler receipt。
- live Slurm `sbatch`/`squeue`/`sacct`/`scancel` receipt。
- live 新周期 GFS/IFS source download receipt。
- live QHH SHUD runtime receipt，并绑定正式 pipeline persistence。
- live `/hydro-met` browser run against target backend。
- live `/ops` 只读展示 run with target identity：展示 22 侧 retry/cancel 前后的状态、日志和诊断信息；27 本身不执行 retry/cancel。
- live alert sink、rollback、nationwide MVT/PBF 和 final production readiness receipts。

## 常用验证命令

后端与 OpenSpec：

```bash
uv run ruff check .
uv run pytest -q
openspec validate m21-qhh-hydro-met-ops-mvp --strict --no-interactive
```

MVP focused checks：

```bash
uv run pytest -q tests/test_forecast_api.py tests/test_api_contract.py
uv run pytest -q tests/test_monitoring_api.py tests/test_retry_cancel_consistency.py
```

前端：

```bash
cd apps/frontend
corepack pnpm test
corepack pnpm exec tsc --noEmit
corepack pnpm run check:api-types
corepack pnpm build
corepack pnpm check:bundle
corepack pnpm test:e2e -- hydro-met.spec.ts --project=chromium --workers=1
corepack pnpm test:e2e -- monitoring.spec.ts --project=chromium --workers=1
```

M20 scheduler fast dry-run / readiness evidence：

```bash
export DATABASE_URL=postgresql://nhms:nhms_dev@localhost:5432/nhms
uv run nhms-pipeline plan-production \
  --dry-run \
  --source gfs \
  --source IFS \
  --lookback-hours 24 \
  --cycle-lag-hours 6 \
  --max-cycles-per-source 1 \
  --workspace-root .nhms-workspace

NHMS_RUN_PRODUCTION_CLOSURE=1 uv run nhms-production validate-readiness \
  --evidence-root artifacts/production-closure \
  --run-id local-m20-scheduler-readiness \
  --scheduler-evidence-root .nhms-workspace/scheduler/evidence \
  --force
```

完整验证说明见 [`docs/VALIDATION.md`](docs/VALIDATION.md)。

## 下一步优先级

1. 保持 MVP evidence matrix 的 claim boundary：新增或刷新证据时必须标注 mode、command、artifact path 和 claim boundary，不能混淆 deterministic、mocked、live。
2. 在生产环境重建依赖并记录 receipt；当前测试机的 `.venv` / `node_modules` 已适配本机，但同步到生产机后只算迁移副产物，不能替代生产机本地重建。
3. 先实施两节点只读展示重构的 Phase 0/PR1-PR4：service role、Slurm router 条件挂载、27 retry/cancel fail-closed、published artifact 日志读取、latest-product 强身份约束。
4. 在目标环境补 live receipts：target DB、对象存储/发布区、Slurm、source credentials、共享日志、IdP/operator、浏览器入口、alert、rollback。
5. 跑一轮 QHH GFS/IFS live MVP smoke：download -> canonical -> forcing -> SHUD -> parse -> publish -> station series -> forecast-series -> latest-product strong identity -> `/hydro-met` -> `/ops` readonly logs/diagnostics，并同步更新 `docs/bugs.md` 的复测归因。
6. 后续生产化再推进 live IdP、live alert sink、live rollback、真实对象存储、真实全国 PostGIS/MVT、CLDAS、ERA5 近实时、全国所有流域和自动化 `operation_request`。

## 注意事项

- 工作区可能存在 `.agents/`、`.codex/`、`data/`、`docs/images/`、`node_modules/`、`dist/`、`__pycache__` 等本地或生成文件；不要误 stage。
- 历史 OpenSpec proposal/tasks 保留当时路径和任务状态用于审计；判断当前完成度以源码、测试、`docs/VALIDATION.md`、M21 evidence matrix 和本文为准。
- 当前测试机的 `.venv` / `node_modules` 不是 macOS 原始依赖，已经过本机 Linux 环境适配；但生产机 Python/Node 版本和仓库绝对路径不同，仍需按 `AGENTS.md` 在 `/home/nwm/NWM` 重新 `uv sync` 和 `corepack pnpm install`，以生产机本地命令结果作为 receipt。
