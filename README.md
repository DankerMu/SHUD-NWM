# SHUD-NWM · 全国水文模拟系统

> 基于 SHUD 的多流域、多资料源、多模型版本水文模拟、预报、展示与生产运维平台。

SHUD-NWM（National Hydrological Modeling System, NHMS）把气象资料接入、标准化产品生产、SHUD 模型运行、结果解析、水文结果发布、前端展示和生产运维证据链串成一套可重复运行的工程系统。项目当前以多流域业务化调度、display readonly 生产展示和目标环境 live proof 为主线推进。

## 当前状态

- 后端主入口为 `apps/api`，基于 FastAPI 提供模型资产、预报、best-available、状态快照、流水线和 Slurm 网关等 API。
- 前端主入口为 `apps/frontend`，基于 Vite、React、TypeScript、MapLibre、ECharts、Zustand 和 OpenAPI-generated types。
- 业务链路已覆盖 GFS、IFS、ERA5 等资料适配，canonical 气象产品、forcing 生产、SHUD runtime、output parser、hydro display 产品发布和 pipeline 编排。
- M10 production closure、M11 全国总览与流域钻取、M20 production scheduler automation 已形成 deterministic / production-like evidence lane。
- MVP 最短路径聚焦 QHH/有限流域水文气象展示与运维监控：GFS 主源、IFS 并行源、河段流量 `q_down`、forcing 代站变量 `PRCP/TEMP/RH/wind/Rn/Press` 和 pipeline 运维闭环。
- 默认 fast 验证不伪装最终生产就绪；live backend auth、live alert sink、live rollback、accepted live dependency proofs、真实对象存储与目标环境 Slurm/外部资料源证据仍是生产上线前边界。

## 文档状态

- 文档权威状态、历史文档识别和冲突解决顺序见 [`docs/governance/DOC_STATUS.md`](docs/governance/DOC_STATUS.md)。
- 根目录 `IMPLEMENTATION_PLAN.md` 是 historical / superseded 基线；当前实现判断以当前入口、active OpenSpec、runbook、验证矩阵和源码为准。

## 系统能力

### 数据与模型链路

1. **资料源接入**：GFS、IFS、ERA5 已有适配路径；CLDAS 等授权资料源按预留接口接入。
2. **标准化产品**：把 raw met data 转换为统一时空语义下的 canonical product，并保留 source、cycle、valid time、scenario 和 lineage。
3. **Forcing 生产**：面向 SHUD 站点/网格生成 `PRCP`、`TEMP`、`RH`、`wind`、`Rn`、`Press` 等 forcing timeseries。
4. **模型资产管理**：支持 Basins discovery、package publication、registry import、model version lineage、active/inactive 生命周期和只读前端资产页。
5. **运行编排**：`services/orchestrator` 与 `services/slurm_gateway` 负责任务计划、依赖链、Slurm job/array、retry、cancel、partial success 和 evidence。
6. **结果产品**：支持 SHUD output parse、hydro result 入库、display 产品发布、layer metadata 和 API/frontend 消费。
7. **生产证据链**：`services/production_closure` 提供 Slurm、对象存储、气象/QC、staging E2E、全国规模性能、ops/security/readiness 等 opt-in validation lane。

### 前端产品面

当前有效前端在 `apps/frontend`。M26 已收敛为**单图展示**：`/` 是唯一活跃的展示入口，旧的多页 display 路由全部 `replace` 重定向到 `/` 并保留 search + 附加图层/钻取语义参数（见 `apps/frontend/src/App.tsx`）。

活跃路由：

| 路由 | 用途 |
|---|---|
| `/` | 活跃单图展示入口：全国总览、流域/图层/source 控制、地图、运行态势、valid-time timeline，以及流域/河段钻取与河段流量弹窗（原多页能力收敛于此） |
| `/monitoring` | pipeline 运维监控、阶段、作业、队列、日志和趋势（角色门控：operator/model_admin/sys_admin） |
| `/ops` | 只读运维入口，`display_readonly` 下降级展示（角色门控，同上） |
| `/system/model-assets` | 模型资产只读管理页，按角色 gate（model_admin/sys_admin） |

legacy 重定向别名（非活跃独立页，仅作兼容；均 `replace` 重定向到 `/`）：

| 旧路由 | 重定向目标 |
|---|---|
| `/overview`、`/hydro-met`、`/forecast` | `/` |
| `/meteorology` | `/?metStations=1` |
| `/flood-alerts` | `/` |
| `/basins/:basinId` | `/?basinId=…` |
| `/segments/:segmentId` | `/?segmentId=…` |

## 技术栈

| 层 | 主要技术 |
|---|---|
| API | Python 3.11+、FastAPI、Pydantic、SQLAlchemy、Alembic |
| 数据库 | PostgreSQL、TimescaleDB、PostGIS |
| 对象存储 | S3-compatible object store，开发环境使用 MinIO |
| 计算调度 | Slurm、sbatch、sacct、job array |
| 数据处理 | xarray、netCDF4、cfgrib、ecCodes、SciPy、pyproj、pyshp |
| 前端 | Vite、React 18、TypeScript、MapLibre、ECharts、Zustand、Radix UI |
| 测试与质量 | pytest、ruff、Vitest、Playwright、OpenAPI contract checks、OpenSpec |

## 目录结构

```text
SHUD-NWM/
├── apps/
│   ├── api/                         # FastAPI 后端
│   └── frontend/                    # Vite React 前端
├── db/
│   ├── migrations/                  # 数据库迁移
│   └── seeds/                       # 开发/演示数据
├── design/                          # 架构图、数据流图、UI 效果图
├── docs/
│   ├── spec/                        # 总体设计、架构、API、DB、前端、运维等核心规格
│   ├── modules/                     # 16 个模块的设计与开发规格
│   ├── appendices/                  # Schema、OpenAPI 草案、sbatch 模板、验收清单
│   ├── governance/                  # 文档状态、角色边界、遗留路径治理
│   ├── plans/                       # MVP / release plan
│   ├── research/                    # 数据源调研与决策记录
│   ├── report/                      # 汇报材料
│   └── VALIDATION.md                # 验证矩阵
├── infra/
│   ├── docker-compose.dev.yml       # 本地 PostgreSQL/TimescaleDB/PostGIS + MinIO
│   └── sbatch/                      # canonical Slurm 模板
├── openapi/                         # OpenAPI 契约
├── openspec/                        # 按里程碑/issue 管理的变更规格
├── packages/common/                 # 通用状态、配置、迁移等基础能力
├── scripts/                         # 诊断、复现、QHH chain 脚本
├── services/
│   ├── orchestrator/                # pipeline 编排
│   ├── production_closure/          # production-like / opt-in 证据链
│   ├── slurm_gateway/               # Slurm 网关
│   ├── tile_publisher/              # 产品/瓦片发布
│   └── tiles/                       # MVT / layer metadata 相关能力
├── tests/                           # 后端、契约、生产证据与静态测试
└── workers/
    ├── data_adapters/               # GFS / ERA5 / IFS 等资料源适配
    ├── canonical_converter/         # canonical met product
    ├── forcing_producer/            # SHUD forcing
    ├── shud_runtime/                # SHUD runtime adapter
    ├── output_parser/               # SHUD 输出解析
    └── model_registry/              # 模型/Basins 资产注册
```

## 快速开始

### 1. 准备环境

推荐使用仓库托管虚拟环境和 `uv`：

```bash
uv sync --all-extras --dev
```

前端使用 Corepack + pnpm：

```bash
corepack prepare pnpm@10.11.0 --activate
cd apps/frontend
CI=true corepack pnpm install --frozen-lockfile
```

Linux/生产环境迁移时不要复用 macOS `.venv` 或 `node_modules`，应删除后重新安装。

### 2. 配置环境变量

```bash
cp .env.example .env
```

开发默认配置包括：

| 变量 | 默认/用途 |
|---|---|
| `DATABASE_URL` | `postgresql://nhms:nhms_dev@localhost:5432/nhms` |
| `S3_ENDPOINT_URL` | 本地 MinIO API，默认 `http://localhost:9000` |
| `S3_BUCKET_NAME` | 默认 `nhms` |
| `OBJECT_STORE_PREFIX` | 对象存储 URI 前缀，默认 `s3://nhms` |
| `OBJECT_STORE_ROOT` | 本地/生产对象存储根路径 |
| `WORKSPACE_ROOT` | 本地或 HPC 工作目录 |
| `SLURM_GATEWAY_BACKEND` | `mock` 或 `slurm` |
| `SHUD_EXECUTABLE` | SHUD 可执行文件名或路径 |
| `API_PORT` | FastAPI 端口，默认 `8000` |

### 3. 启动本地基础设施与 API

```bash
make dev
```

`make dev` 会启动开发依赖并以 reload 模式运行 FastAPI。默认服务：

| 服务 | 地址 |
|---|---|
| PostgreSQL / TimescaleDB / PostGIS | `localhost:5432` |
| MinIO API | `localhost:9000` |
| MinIO Console | `localhost:9001` |
| FastAPI Swagger UI | `http://localhost:8000/docs` |

常用数据库命令：

```bash
make migrate
make seed-demo
make reset-db
```

也可以手动启动 API：

```bash
uv run python -m uvicorn apps.api.main:app --host 0.0.0.0 --port 8000 --reload
```

### 4. 启动前端

```bash
cd apps/frontend
corepack pnpm dev
```

开发环境下 Vite 会把 `/api` 和 `/health` 代理到 `http://localhost:8000`。生产构建由 FastAPI 服务 `apps/frontend/dist/`。

```bash
cd apps/frontend
corepack pnpm build
```

## 常用验证命令

### 后端 fast checks

```bash
uv run ruff check .
uv run pytest -q
```

### 后端集成测试

真实 PostgreSQL/PostGIS/TimescaleDB 集成测试为 opt-in：

```bash
docker compose -f infra/docker-compose.dev.yml up -d db

NHMS_RUN_INTEGRATION=1 \
NHMS_INTEGRATION_DATABASE_URL=postgresql://nhms:nhms_dev@localhost:5432/nhms \
uv run pytest -q -m integration
```

### 前端测试

```bash
cd apps/frontend
corepack pnpm test
corepack pnpm exec tsc --noEmit
corepack pnpm run check:api-types
corepack pnpm build
corepack pnpm check:bundle
corepack pnpm exec playwright test
corepack pnpm run test:e2e:preview
```

### OpenSpec / 生产证据 lane

```bash
openspec validate m10-production-closure --strict --no-interactive
openspec validate m11-overview-basin-drilldown --strict --no-interactive
openspec validate m20-production-multibasin-continuous-automation --strict --no-interactive
```

M20 scheduler dry-run evidence：

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
```

Production readiness 汇总 lane 示例：

```bash
NHMS_RUN_PRODUCTION_CLOSURE=1 uv run nhms-production validate-readiness \
  --evidence-root artifacts/production-closure \
  --run-id local-scheduler-readiness \
  --scheduler-evidence-root .nhms-workspace/scheduler/evidence \
  --force
```

完整矩阵见 [`docs/VALIDATION.md`](docs/VALIDATION.md)。

## CLI 入口

`pyproject.toml` 暴露了以下常用命令：

| 命令 | 用途 |
|---|---|
| `nhms-gfs` | GFS 资料适配 |
| `nhms-era5` | ERA5 资料适配 |
| `nhms-ifs` | IFS 资料适配 |
| `nhms-canonical` | canonical met product 转换 |
| `nhms-forcing` | SHUD forcing 生产 |
| `nhms-model` | Basins / 模型资产 discovery、publish、registry import |
| `nhms-shud-runtime` | SHUD runtime adapter |
| `nhms-parse` | SHUD output parse |
| `nhms-pipeline` | pipeline / production scheduler 编排 |
| `nhms-production` | production closure / readiness 证据链 |
| `nhms-state` | 状态管理工具 |

## 数据与生产边界

- `data/Basins` 在开发环境通常是指向真实 Basins 数据目录的软链接；生产迁移必须复制实际数据，不应只迁移 symlink。
- production-like lanes 默认使用 deterministic / fake / fixture 证据，不能等价为最终生产就绪声明。
- 真实生产上线前需要在目标环境补齐 live backend identity provider、live alert sink、live rollback、真实 Slurm/SHUD workload、真实对象存储、live GFS/IFS/ERA5 下载稳定性、CLDAS 授权接入、全国规模 PostGIS/MVT 压测等证据。
- Canonical `.pbf` MVT 路径是 live-PostGIS-only；当 live PostGIS MVT 不可用时应显式返回不可用错误，不应伪造成功。

## 文档导航

| 我想了解 | 入口 |
|---|---|
| 系统总体设计 | [`docs/spec/00_overall_design.md`](docs/spec/00_overall_design.md) |
| 架构与端到端流程 | [`docs/spec/01_architecture_and_flow.md`](docs/spec/01_architecture_and_flow.md) |
| 数据产品与时间语义 | [`docs/spec/02_data_product_and_time_semantics.md`](docs/spec/02_data_product_and_time_semantics.md) |
| 数据库设计 | [`docs/spec/03_database_design.md`](docs/spec/03_database_design.md) |
| API 设计 | [`docs/spec/04_api_design.md`](docs/spec/04_api_design.md) |
| Slurm / HPC 设计 | [`docs/spec/05_slurm_hpc_design.md`](docs/spec/05_slurm_hpc_design.md) |
| 前端功能规格 | [`docs/spec/06_frontend_gis_design.md`](docs/spec/06_frontend_gis_design.md) |
| 前端 UI 规范 | [`docs/spec/06B_frontend_ui_design_spec.md`](docs/spec/06B_frontend_ui_design_spec.md) |
| 运维、安全、QC | [`docs/spec/07_devops_ops_security.md`](docs/spec/07_devops_ops_security.md) |
| 路线图与验收 | [`docs/spec/08_roadmap_acceptance.md`](docs/spec/08_roadmap_acceptance.md) |
| 模块索引 | [`docs/modules/00_module_index.md`](docs/modules/00_module_index.md) |
| 验证矩阵 | [`docs/VALIDATION.md`](docs/VALIDATION.md) |
| 当前进度 | [`progress.md`](progress.md) |
| MVP 上线计划 | [`docs/plans/2026-05-25-mvp-launch-plan.md`](docs/plans/2026-05-25-mvp-launch-plan.md) |

## 开发约定

- Python 命令优先使用 `uv run ...`，不要直接依赖系统 Python。
- 前端命令在 `apps/frontend/` 下通过 `corepack pnpm ...` 执行。
- OpenAPI 合同变更后同步更新 `openapi/nhms.v1.yaml` 和前端 generated types。
- 涉及生产状态、模型生命周期、对象存储、Slurm 和外部资料源的变更必须带 deterministic 测试或明确的 opt-in evidence lane。
- 不要把 fast/deterministic evidence 写成最终生产证明；README、文档和 UI 状态都应保留 unavailable、restricted、not_executed、release_blocked 等边界表达。
- 不要恢复已废弃的 legacy 路径；有效代码入口以本 README 的目录结构为准。
