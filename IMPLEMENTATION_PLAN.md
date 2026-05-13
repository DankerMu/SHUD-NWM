# 全国水文模拟系统：实施计划

版本：v0.2-design-freeze  
日期：2026-05-06

本文档基于 GPT 审核反馈和 v0.2 设计冻结，将路线图（`docs/spec/08_roadmap_acceptance.md`）展开为可执行的任务包和阅读清单。每个阶段开工前，开发人员按"必读"清单完成设计理解，再按任务包逐项交付。

---

## 阶段 0：工程初始化（M0）

**目标**：开发者可本地启动 API + 数据库，CI 通过 lint/test，migration 可重复执行，Mock 数据跑通前端首页。

### 必读文档

| 优先级 | 文档 | 重点关注 |
|---|---|---|
| ★★★ | `docs/spec/00_overall_design.md` | §1-5 系统边界、核心对象、架构决策 |
| ★★★ | `docs/spec/01_architecture_and_flow.md` | §1-2 四平面分层、核心组件清单 |
| ★★★ | `docs/spec/03_database_design.md` | 全文：6 schema、21 张核心表、4 组 ENUM |
| ★★★ | `docs/spec/04_api_design.md` | §1-3 API 风格、响应结构、核心接口清单 |
| ★★ | `docs/appendices/A_id_and_versioning_convention.md` | ID 命名规范——决定 seed 数据格式 |
| ★★ | `docs/appendices/C_database_schema_draft.md` | 接近 migration 的 SQL 草案 |
| ★★ | `docs/spec/07_devops_ops_security.md` | §1-2 环境划分、服务部署 |
| ★ | `docs/appendices/F_acceptance_checklist.md` | M0 验收对照 |

### 任务包

#### M0-1：工程目录骨架

```text
SHUD-NWM/
├── apps/
│   ├── api/                    ← FastAPI 后端
│   └── frontend/               ← Vite / React / TypeScript 前端
├── services/
│   ├── orchestrator/           ← 流水线编排器
│   └── slurm_gateway/          ← Slurm 提交代理
├── workers/
│   ├── data_adapters/          ← GFS/IFS/ERA5/CLDAS 适配器
│   ├── canonical_converter/    ← 标准化转换
│   ├── forcing_producer/       ← forcing 生产
│   ├── shud_runtime/           ← SHUD 运行适配
│   ├── output_parser/          ← 输出解析入库
│   └── flood_frequency/        ← 洪水频率计算
├── packages/
│   ├── common/                 ← 共享工具、类型、错误码
│   └── schemas/                ← JSON Schema（manifest、QC、job）
├── db/
│   ├── migrations/             ← 有序 SQL migration
│   └── seeds/                  ← Demo 数据
├── openapi/                    ← nhms.v1.yaml
├── infra/
│   └── sbatch/                 ← canonical real Slurm templates
├── tests/                      ← 集成测试
└── docs/                       ← 现有文档（不动）
```

Legacy placeholder paths such as `apps/web`, `workers/forcing-producer`, `workers/shud-runtime`, `workers/output-parser`, and `workers/flood-frequency` are non-canonical. Active code, package entry points, and tests use the underscore Python package paths above.

#### M0-2：数据库 Migration

按外键依赖顺序编排：

| 文件 | 内容 |
|---|---|
| `000001_extensions.sql` | PostGIS、TimescaleDB 扩展 |
| `000002_schemas.sql` | core / met / hydro / flood / map / ops |
| `000003_enums.sql` | run_type / run_status / source_status / cycle_status |
| `000004_core.sql` | basin → basin_version → river_network_version → river_segment → model_instance |
| `000005_met.sql` | data_source → forecast_cycle → canonical_met_product → met_station → interp_weight → forcing_version → forcing_version_component → forcing_station_timeseries → best_available_selection |
| `000006_hydro.sql` | hydro_run → state_snapshot → river_timeseries |
| `000007_flood.sql` | flood_frequency_curve → return_period_result |
| `000008_map.sql` | tile_layer → tile_cache |
| `000009_ops.sql` | pipeline_job → pipeline_event → qc_result → audit_log |
| `000010_indexes.sql` | 补充索引、hypertable 创建 |

验收：`make migrate && make reset-db && make seed-demo` 可重复执行。

#### M0-3：OpenAPI 契约

文件：`openapi/nhms.v1.yaml`。至少包含以下 schema：

```text
Basin, BasinVersion, ModelInstance, MetStation, RiverSegment,
HydroRun, ForcingVersion, RiverSeriesResponse,
FloodAlertSummary, PipelineStage, PipelineJob, QcResult, ErrorResponse
```

参考：`docs/spec/04_api_design.md` 全部 12 节 + `docs/appendices/E_api_openapi_draft.md`。

#### M0-4：Mock Slurm Gateway

提供 `slurm_gateway.backend = mock` 模式，支持：

- `submit_job` → 返回 mock job_id，延迟后自动转 succeeded
- `cancel_job` → 立即标记 cancelled
- `get_job_status` → 返回当前状态
- `fetch_logs` → 返回 mock 日志文本

目的：让 orchestrator 和前端"产品监控"页面可本地开发。

#### M0-5：Demo 数据集

| 数据 | 数量 |
|---|---|
| basin | 1 个 demo 流域 |
| river_segment | 10–50 条 |
| met_station | 3–5 个 |
| mock GFS cycle | 1 个 |
| mock forcing_version | 1 个 |
| mock hydro_run | 1 个 |
| river_timeseries | 1 组（7 天 × 河段数） |
| return_period_result | 1 组 |

#### M0-6：对象存储 Prefix 落地

按 `docs/spec/01_architecture_and_flow.md` §7 文件流定义。M6 后根目录语义固定为：

- `WORKSPACE_ROOT`：本地/HPC 临时执行 workspace，用于作业运行目录、临时 manifest、临时输出和 Slurm 本地文件。
- `OBJECT_STORE_ROOT`：持久化对象存储根目录，用于 raw、canonical、forcing、runs、states、tiles、持久化日志等可复用产物。
- `OBJECT_STORE_PREFIX`：对象 URI 前缀（例如 `s3://nhms`），用于把 `OBJECT_STORE_ROOT` 下的相对 key 呈现为稳定 URI；为空时保留本地相对 key。

`WORKSPACE_ROOT` 可以和 `OBJECT_STORE_ROOT` 不同；持久化产物必须通过 `OBJECT_STORE_ROOT` + `OBJECT_STORE_PREFIX` 解析。

```text
raw/{source}/{cycle_time}/
canonical/{source}/{cycle_time}/{variable}/
forcing/{source}/{cycle_time}/{basin_version_id}/{model_id}/
models/{model_id}/
states/{model_id}/{valid_time}/
runs/{run_id}/input/
runs/{run_id}/output/
runs/{run_id}/logs/
tiles/met/{product_id}/
tiles/hydro/{run_id}/
```

#### M0-7：CI 最小检查

- Markdown lint
- OpenAPI validate
- JSON Schema validate（manifest、QC result、pipeline job）
- SQL migration dry-run
- 基础单元测试

#### M0-8：JSON Schema 交付

```text
schemas/run_manifest.schema.json      ← 参考 docs/appendices/B_run_manifest_schema.md
schemas/run_status.schema.json
schemas/qc_result.schema.json
schemas/pipeline_job.schema.json
```

CI 校验所有示例 manifest。

---

## 阶段 1：GFS + 单流域 Forecast 闭环

**目标**：至少 1 个流域、1 个 GFS 周期完成未来 7 天预报，`.rivqdown` 转 m³/s 入库，前端点击河段展示曲线。

### 必读文档

| 优先级 | 文档 | 重点关注 |
|---|---|---|
| ★★★ | `docs/spec/02_data_product_and_time_semantics.md` | §4-7 资料源配置、canonical 字段、标准变量、forcing 变量转换规则 |
| ★★★ | `docs/spec/01_architecture_and_flow.md` | §3 Forecast 流程时序图、§5 状态机、§6 Manifest 驱动 |
| ★★★ | `docs/spec/05_slurm_hpc_design.md` | 全文：8 类作业、依赖链、workspace、失败处理 |
| ★★★ | `docs/research/气象数据梳理与决策跟踪.md` | GFS 相关章节：变量、分辨率、周期、下载策略；§1b 账号与下载通道策略表 |
| ★★ | `docs/research/数据下载账号与稳定性策略.md` | 各源下载通道 fallback、轮询策略、文件校验机制（参考文档） |
| ★★ | `docs/modules/01_data_source_adapter_*.md` | GFS adapter 设计与开发规格；§7b 每源配置模板、§7c 下载轮询策略 |
| ★★ | `docs/modules/02_raw_data_ingestion_*.md` | 下载、校验、归档 |
| ★★ | `docs/modules/03_canonical_met_product_*.md` | canonical 转换 |
| ★★ | `docs/modules/04_forcing_production_*.md` | forcing 生产（注意 met_station 表名） |
| ★★ | `docs/modules/09_shud_runtime_adapter_*.md` | SHUD 调用、workspace |
| ★★ | `docs/modules/10_output_parser_ingest_*.md` | 输出解析（注意 river_network_version_id） |
| ★★ | `docs/spec/04_api_design.md` | §3-4 核心接口、河段预报曲线响应 |
| ★★ | `docs/appendices/B_run_manifest_schema.md` | manifest 完整字段 |
| ★★ | `docs/appendices/D_sbatch_templates.md` | sbatch 模板 |
| ★ | `docs/spec/06_frontend_gis_design.md` | §2 全国总览、§7 流域详情页 |

### 任务包

| 编号 | 任务 | 涉及模块 |
|---|---|---|
| M1-1 | GFS adapter：cycle discovery + raw download + manifest | 01, 02 |
| M1-2 | Canonical converter：GRIB2 → 标准变量/单位/时间轴 | 03 |
| M1-3 | Forcing producer：met_station + interp_weight + .tsd.forc + CSV | 04 |
| M1-4 | Model registry：注册 demo model_instance | 05 |
| M1-5 | SHUD runtime adapter：workspace 准备 + shud_omp 调用 | 09 |
| M1-6 | Output parser：.rivqdown → m³/s → river_timeseries 入库 | 10 |
| M1-7 | Slurm 作业链：download → canonical → forcing → forecast → parse | 08 |
| M1-8 | API 实现：河段预报曲线查询 + 基础 run 查询 | 13 |
| M1-9 | 前端：河网底图 + 河段点击弹窗 + 预报曲线图 | 15 |

### 闭环验证

```text
GFS 周期发现 → 下载 → canonical → forcing → SHUD forecast
→ 输出解析 → river_timeseries 入库 → 前端点击河段看曲线
```

---

## 阶段 2：Analysis Run 与 Warm-start

**目标**：Analysis run 生成 StateSnapshot，Forecast 使用 init_state_id warm-start，前端拼接过去 7 天 + 未来 7 天。

### 必读文档

| 优先级 | 文档 | 重点关注 |
|---|---|---|
| ★★★ | `docs/spec/01_architecture_and_flow.md` | §4 Analysis 流程 |
| ★★★ | `docs/spec/02_data_product_and_time_semantics.md` | §8-9 Scenario 语义、best_available 规则、§11 曲线拼接 |
| ★★★ | `docs/spec/00_overall_design.md` | §5.1 Analysis/Forecast 分离、§6.1 Near-real-time analysis |
| ★★ | `docs/modules/06_analysis_state_pipeline_*.md` | analysis 流水线设计 |
| ★★ | `docs/modules/07_forecast_pipeline_*.md` | forecast warm-start 逻辑 |
| ★★ | `docs/research/气象数据梳理与决策跟踪.md` | ERA5 章节：延迟、变量、分辨率 |
| ★ | `docs/spec/06_frontend_gis_design.md` | §7.6 预报曲线详情页（analysis/forecast 分界线） |

### 任务包

| 编号 | 任务 |
|---|---|
| M2-1 | ERA5 adapter：cycle discovery + download + canonical convert |
| M2-2 | Analysis run pipeline：ERA5 forcing → SHUD analysis → StateSnapshot |
| M2-3 | State manager：StateSnapshot 存储、查询最近可用、usable_flag 管理 |
| M2-4 | Forecast warm-start：init_state_id → init_state_uri → SHUD 启动 |
| M2-5 | 前端曲线拼接：analysis_true_field（过去 7 天）+ forecast（未来 7 天） |
| M2-6 | best_available_selection 表写入与查询 |

---

## 阶段 3：Slurm 全国化

**目标**：≥10 个流域并行提交，单流域失败不阻断其它流域。

### 必读文档

| 优先级 | 文档 | 重点关注 |
|---|---|---|
| ★★★ | `docs/spec/05_slurm_hpc_design.md` | §3 Job array 策略、§4 依赖链、§5 resource profile、§9 失败处理 |
| ★★★ | `docs/spec/01_architecture_and_flow.md` | §5.3 状态机与监控 UI 阶段映射 |
| ★★ | `docs/modules/08_slurm_gateway_*.md` | gateway 设计 |
| ★★ | `docs/spec/04_api_design.md` | §7 运维监控接口 |
| ★★ | `docs/spec/07_devops_ops_security.md` | §4-5 监控指标、告警 |
| ★ | `docs/spec/06_frontend_gis_design.md` | §15 产品监控页 |

### 任务包

| 编号 | 任务 |
|---|---|
| M3-1 | Real Slurm Gateway：替换 mock，sbatch + sacct + scancel |
| M3-2 | Job array 编排：`--array=0-N%M` + per-model resource profile |
| M3-3 | 依赖链自动化：download → canonical → forcing → forecast → parse → frequency → publish |
| M3-4 | Partial success：部分流域失败时 cycle 进入 parsed_partial / forcing_ready_partial |
| M3-5 | pipeline_job 表写入：Slurm job_id、stage、status、log_uri、retry_count |
| M3-6 | 运维监控 API：/pipeline/stages、/jobs、/metrics |
| M3-7 | 前端产品监控页：七阶段卡片、进度条、失败流域展开 |
| M3-8 | 失败重试：POST /runs/{run_id}/retry + 自动 retry（max_retries 配置） |

---

## 阶段 4：IFS 与多 Scenario

**目标**：同一河段同一起报时刻展示 GFS + IFS 两条曲线。

### 必读文档

| 优先级 | 文档 | 重点关注 |
|---|---|---|
| ★★★ | `docs/spec/02_data_product_and_time_semantics.md` | §8 Scenario 语义、§9 best_available |
| ★★★ | `docs/spec/00_overall_design.md` | §5.2 分 scenario 保存 GFS/IFS |
| ★★ | `docs/research/气象数据梳理与决策跟踪.md` | IFS 章节：Open Data API、周期、变量差异 |
| ★★ | `docs/spec/04_api_design.md` | §3 forecast-series 接口 scenarios 参数、§4 河段曲线响应 |
| ★ | `docs/spec/06_frontend_gis_design.md` | §7.6 多源对比曲线 |

### 任务包

| 编号 | 任务 |
|---|---|
| M4-1 | IFS adapter：ECMWF Open Data API → download → canonical |
| M4-2 | IFS forcing + forecast run：复用 M1 forcing/forecast 流程 |
| M4-3 | Scenario 管理：GFS/IFS 独立 scenario_id 写入 hydro_run |
| M4-4 | API 多 scenario 查询：`?scenarios=GFS,IFS` 返回多条曲线 |
| M4-5 | 前端 GFS/IFS 对比曲线：颜色区分、图例、可用时效标注 |
| M4-6 | IFS 06/18 周期不足 7 天时前端 available lead range 标注 |

---

## 阶段 5：洪水频率 / 重现期产品

**目标**：每个已启用河段有频率曲线，预报 run 完成后自动计算重现期，前端展示预警配色。

### 必读文档

| 优先级 | 文档 | 重点关注 |
|---|---|---|
| ★★★ | `docs/spec/03_database_design.md` | §5.17 flood_frequency_curve、§5.18 return_period_result |
| ★★★ | `docs/modules/11_flood_frequency_return_period_*.md` | 频率计算设计与开发规格 |
| ★★ | `docs/spec/00_overall_design.md` | §6.3 Hindcast/Replay 设计 |
| ★★ | `docs/spec/04_api_design.md` | §8 洪水预警聚合接口 |
| ★★ | `docs/spec/07_devops_ops_security.md` | §6.3 洪水频率 QC |
| ★ | `docs/spec/06_frontend_gis_design.md` | §13 洪水预警页 |
| ★ | `docs/spec/09_sources.md` | USGS Bulletin 17C / PeakFQ 参考 |

### 任务包

| 编号 | 任务 |
|---|---|
| M5-1 | Hindcast 能力：ERA5 历史回放 → river_timeseries 样本 |
| M5-2 | 洪水频率引擎：P-III / GEV 拟合 → flood_frequency_curve 入库 |
| M5-3 | 重现期计算：每次 forecast run → return_period_result |
| M5-4 | 频率 QC：Q2<Q5<...<Q100 单调性、样本年限检查 |
| M5-5 | 预警聚合 API：/flood-alerts/summary、/ranking、/segments、/timeline |
| M5-6 | 前端预警页：河段配色、TOP 排名、预警时间线 |
| M5-7 | 瓦片发布：flood-return-period vector tiles |

---

## 阶段 6：系统硬化与交付对齐（M6，已完成）

**目标**：消除 M3-M5 交付中的路径、状态、对象存储、API/OpenAPI、前端类型和验证证据漂移，形成可发布基线。

### 任务包

| 编号 | 任务 |
|---|---|
| M6-1 | Slurm array contract 对齐：`infra/sbatch` 模板、worker CLI、manifest index、array task id |
| M6-2 | Source identity canonicalization：统一 GFS/ERA5/IFS source_id 归一化 |
| M6-3 | Object store split-root：`WORKSPACE_ROOT` 与 `OBJECT_STORE_ROOT`/`OBJECT_STORE_PREFIX` 语义分离 |
| M6-4 | Retry/cancel 状态一致性：pipeline_job、hydro_run、forecast_cycle、事件与 API 响应同步 |
| M6-5 | API contract alignment：OpenAPI、后端响应、frontend generated types 同步 |
| M6-6 | Delivery traceability：文档、schema、OpenSpec task state 和验证证据补齐 |

### 验收证据

M6 hardening is tracked in `openspec/changes/m6-system-hardening-alignment/` with regression coverage in:

- `tests/test_slurm_array_contract.py`
- `tests/test_source_identity.py`
- `tests/test_object_store_roots.py`
- `tests/test_retry_cancel_consistency.py`
- `tests/test_api_contract.py`

---

## 阶段 7：CLDAS 接入（后续）

**目标**：CLDAS 资料参与 analysis run，best_available 产品显示实际来源。

### 必读文档

| 优先级 | 文档 | 重点关注 |
|---|---|---|
| ★★★ | `docs/research/气象数据梳理与决策跟踪.md` | CLDAS 章节：权限、分辨率、变量 |
| ★★★ | `docs/spec/02_data_product_and_time_semantics.md` | §7.1 CLDAS 降水转换、§9 best_available 空间规则 |
| ★★ | `docs/modules/01_data_source_adapter_*.md` | adapter 接口契约 |
| ★ | `docs/spec/04_api_design.md` | §9 数据血缘接口 |

### 任务包

| 编号 | 任务 |
|---|---|
| M7-1 | CLDAS adapter：权限配置 + 下载策略 |
| M7-2 | CLDAS canonical convert：瞬时降水率 → 时段量 |
| M7-3 | CLDAS QC：空间覆盖范围检查 |
| M7-4 | best_available 规则更新：CLDAS 优先级接入 |
| M7-5 | data_source 状态切换：restricted → enabled |

---

## 横切任务（贯穿全阶段）

这些任务不属于某个单独阶段，但在多个阶段中持续演进。

### 前端 UI 体系

| 阶段 | 必读文档 |
|---|---|
| M0 起 | `docs/spec/06B_frontend_ui_design_spec.md`：设计 Token、组件规范、图表配置 |
| M1 起 | `docs/spec/06_frontend_gis_design.md`：8 个页面的功能规格 |
| 全程 | `design/ui/前端效果图1-8.png`：对照阅读 |

### 数据血缘

| 阶段 | 必读文档 |
|---|---|
| M1 起 | `docs/spec/04_api_design.md` §9：血缘接口 |
| M1 起 | `docs/spec/03_database_design.md` §5.11b：forcing_version_component |
| 全程 | `docs/appendices/F_acceptance_checklist.md` §2：血缘验收 |

### QC 与运维

| 阶段 | 必读文档 |
|---|---|
| M0 起 | `docs/spec/07_devops_ops_security.md`：QC 流水线集成规范 |
| M3 起 | `docs/modules/16_monitoring_qc_ops_*.md`：监控模块 |
| 全程 | `docs/appendices/F_acceptance_checklist.md` §3-4：运行与前端验收 |

### 瓦片服务

| 阶段 | 必读文档 |
|---|---|
| M1 起 | `docs/spec/04_api_design.md` §5：瓦片接口 |
| M1 起 | `docs/modules/14_tile_publication_service_*.md`：瓦片发布模块 |
| M3 起 | `docs/spec/06_frontend_gis_design.md` §8B：气象空间展示 |

### 模型资产管理

| 阶段 | 必读文档 |
|---|---|
| M1 起 | `docs/modules/05_model_registry_versioning_*.md`：模型注册 |
| M3 起 | `docs/spec/04_api_design.md` §6：模型资产管理接口 |
| M3 起 | `docs/spec/06_frontend_gis_design.md` §14：资产管理页 |

---

## 阶段依赖关系

```text
M0 ─→ M1 ─→ M2 ─→ M3 ─→ M4
                        ↘
                    M5（需要 M2 的 hindcast 能力）
                        ↘
                    M6 hardening（M3-M5 后交付对齐）
                        ↘
                    M7 CLDAS（需要 best_available 基础）
```

- M0 是所有后续阶段的前置。
- M1 和 M2 严格串行：forecast 闭环必须先于 analysis warm-start。
- M3 在 M2 之后：全国化需要先验证单流域闭环。
- M4 与 M5 可部分并行：IFS 接入和洪水频率计算相互独立。
- M6 是 M3-M5 合并后的系统硬化与交付对齐阶段，已完成。
- M7 依赖 best_available 框架（M2）和 adapter 模式（M1），可在 M4/M5/M6 之后启动。

---

## 关键里程碑验收

| 里程碑 | 验收标志 |
|---|---|
| M0 完成 | `make migrate && make seed-demo && make dev` 全通；CI 绿色 |
| M1 完成 | 1 个流域 GFS forecast 入库，前端点击河段有曲线 |
| M2 完成 | Analysis state 可用，forecast warm-start 成功，曲线拼接正确 |
| M3 完成 | ≥10 流域并行 forecast，单流域失败不阻塞，监控页可用 |
| M4 完成 | GFS/IFS 双曲线对比展示 |
| M5 完成 | 频率曲线入库，重现期产品自动计算，预警地图配色 |
| M6 完成 | 系统硬化完成：Slurm array、source identity、object-store split-root、retry/cancel、API/OpenAPI/frontend types、交付证据对齐 |
| M7 完成 | CLDAS enabled，best_available 可追溯来源 |
| 最终验收 | `docs/appendices/F_acceptance_checklist.md` 全部 ✓ |
