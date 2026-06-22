# 全国水文模拟系统：总体设计与模块开发 Spec

版本：v0.2  
更新日期：2026-05-06  
文档格式：Markdown  
目标系统名称：**全国水文模拟系统**

本文档包用于研发立项、架构评审、模块拆分、任务排期和后续开发接口定义。所有文件均为 `.md` 格式。

---

## 已确认的产品约束

1. 数据源权限可分阶段解决；系统设计中同时预留 GFS、IFS、ERA5、CLDAS 等适配器。
2. 前端时间轴按图层或数据源的原生时间分辨率展示，不强制统一插值到固定小时步长。
3. 模型运行调度采用 Slurm + HPC；Web 服务只做编排、登记、查询、发布，不直接运行 SHUD。
4. “径流频率”按洪水频率 / 重现期产品实现，不用简单百分位替代。
5. GFS、IFS 等预报源分 scenario 存储、展示和比较。
6. 对外系统名称统一为”全国水文模拟系统”。

---

## 项目文档全景

本项目文档分布在两个层级：**项目根目录**（综述与汇报类）和 **Spec 目录**（设计与开发规格）。

### 项目根目录

```text
SHUD-NWM/
├── design/                                ← 全部设计图
│   ├── architecture/                      ← 架构图 + 数据流转图 + 数据关系图
│   │   ├── 系统架构图.png
│   │   ├── 业务运转数据流转图.png
│   │   └── 数据关系图.png
│   └── ui/                                ← 前端效果图 1-8
│       └── 前端效果图1.png ~ 前端效果图8.png
├── docs/
│   ├── report/
│   │   └── 建设汇报稿.md                  ← 面向甲方的系统建设汇报文档
│   ├── research/
│   │   └── 气象数据梳理与决策跟踪.md        ← 气象数据源全面梳理与接入方案
│   ├── spec/                              ← 本目录，设计与开发规格文档包
│   ├── modules/                           ← 模块拆解文档
│   └── appendices/                        ← 附录文档
```

### Spec 目录

```text
docs/spec/
├── README.md                              ← 本文件，入口与导航
│
│  ── 核心设计文档 ──
├── 00_overall_design.md                   ← 总体设计：目标、范围、核心对象、架构决策、Hindcast 设计
├── 01_architecture_and_flow.md            ← 系统架构：四平面/六层映射、流程、状态机、监控 UI 映射
├── 02_data_product_and_time_semantics.md  ← 数据产品：时间语义、Scenario、best_available 规则
├── 03_database_design.md                  ← 数据库：21 张核心表 + 4 组 ENUM + 职责分工说明
├── 04_api_design.md                       ← API：GIS 查询 + 模型资产 + 运维监控 + 预警聚合 + 血缘 + 阶段状态（12 节）
├── 05_slurm_hpc_design.md                 ← HPC 调度：Slurm 作业体系、依赖链、失败处理
├── 06_frontend_gis_design.md              ← 前端功能规格：全部 8 个页面的布局/交互/字段/API（859 行）
├── 06B_frontend_ui_design_spec.md         ← 前端 UI 规范：设计 Token/组件/图表配置/动效/状态/响应式（676 行）
├── 07_devops_ops_security.md              ← 运维安全：环境、日志、监控、QC 流水线集成、权限
├── 08_roadmap_acceptance.md               ← 路线图：6 阶段里程碑与验收标准
├── 09_sources.md                          ← 外部依据与参考链接
│
│  ── 模块拆解 ──（位于 docs/modules/）
│   ├── 00_module_index.md                 ← 模块索引
│   ├── 01_data_source_adapter_design.md   ← 数据源适配器设计
│   ├── 01_data_source_adapter_spec.md     ← 数据源适配器开发规格
│   ├── 02_raw_data_ingestion_design.md
│   ├── 02_raw_data_ingestion_spec.md
│   ├── 03_canonical_met_product_design.md
│   ├── 03_canonical_met_product_spec.md
│   ├── 04_forcing_production_design.md
│   ├── 04_forcing_production_spec.md
│   ├── 05_model_registry_versioning_design.md
│   ├── 05_model_registry_versioning_spec.md
│   ├── 06_analysis_state_pipeline_design.md
│   ├── 06_analysis_state_pipeline_spec.md
│   ├── 07_forecast_pipeline_design.md
│   ├── 07_forecast_pipeline_spec.md
│   ├── 08_slurm_gateway_design.md
│   ├── 08_slurm_gateway_spec.md
│   ├── 09_shud_runtime_adapter_design.md
│   ├── 09_shud_runtime_adapter_spec.md
│   ├── 10_output_parser_ingest_design.md
│   ├── 10_output_parser_ingest_spec.md
│   ├── 11_flood_frequency_return_period_design.md
│   ├── 11_flood_frequency_return_period_spec.md
│   ├── 12_database_storage_design.md
│   ├── 12_database_storage_spec.md
│   ├── 13_api_backend_design.md
│   ├── 13_api_backend_spec.md
│   ├── 14_tile_publication_service_design.md
│   ├── 14_tile_publication_service_spec.md
│   ├── 15_frontend_application_design.md
│   ├── 15_frontend_application_spec.md
│   ├── 16_monitoring_qc_ops_design.md
│   └── 16_monitoring_qc_ops_spec.md
│
│  ── 附录 ──（位于 docs/appendices/）
    ├── A_id_and_versioning_convention.md   ← ID 与版本命名规范
    ├── B_run_manifest_schema.md            ← 运行 Manifest JSON Schema
    ├── C_database_schema_draft.md          ← 数据库 Schema 草案
    ├── D_sbatch_templates.md               ← Slurm sbatch 模板
    ├── E_api_openapi_draft.md              ← API OpenAPI 草案
    └── F_acceptance_checklist.md           ← 验收检查清单
```

---

## 推荐阅读顺序

### 快速了解（30 分钟）

1. **本文件** `README.md` — 了解文档结构和阅读路径
2. **`../report/建设汇报稿.md`** — 系统全貌概览（含全部设计图），适合首次接触本项目

### 架构与业务理解（2-3 小时）

3. **`00_overall_design.md`** — 系统边界、建设范围、核心业务对象、关键架构决策（Analysis/Forecast 分离、Scenario 管理、版本绑定）、Hindcast 设计
4. **`01_architecture_and_flow.md`** — 四平面逻辑架构与六层物理架构的映射关系、端到端 Forecast/Analysis 流程、状态机定义、状态机到监控 UI 的映射
5. **`02_data_product_and_time_semantics.md`** — 时间语义统一规则、资料源配置模板、Scenario 定义、best_available 产品规则、前端时间列表逻辑
6. **`../research/气象数据梳理与决策跟踪.md`** — 9 类气象数据源详细卡片、SHUD forcing 需求、接入优先级与开发顺序

### 工程设计（按需阅读）

7. **`03_database_design.md`** — 6 个 Schema、21 张核心表定义（含 met_station/interp_weight/forcing_station_timeseries/forcing_version_component/return_period_result）、4 组状态 ENUM、查询模式、版本切换规则、与附录 C 的职责分工
8. **`04_api_design.md`** — 12 节 API 定义（GIS 查询 + 瓦片 + 模型资产 + 运维监控 + 预警聚合 + 数据血缘 + 权限 + 阶段展示状态 + 性能要求）
9. **`05_slurm_hpc_design.md`** — 8 类 Slurm 作业、Job Array 策略、依赖链编排、失败处理、幂等性、安全
10. **`06_frontend_gis_design.md`** — 全部 8 个页面的功能规格（搭配 `../../design/ui/前端效果图*.png` 对照阅读）
11. **`06B_frontend_ui_design_spec.md`** — 设计 Token（色彩/字体/间距）、11 个组件样式规范、8 套 ECharts 图表配置、图标/动效/状态设计/响应式
12. **`07_devops_ops_security.md`** — 环境部署、日志/监控/告警、三级 QC 体系（含 QC 流水线集成规范：触发时机、阻断规则、结果存储、告警复核）、权限角色

### 路线图与验收

13. **`08_roadmap_acceptance.md`** — 6 阶段开发路线图与每阶段验收标准

### 模块开发

14. **`../modules/00_module_index.md`** — 16 个模块的索引与依赖关系
15. **`../modules/*_design.md`** — 各模块架构设计
16. **`../modules/*_spec.md`** — 各模块开发规格（接口、输入输出、异常处理）

### 参考与附录

17. **`09_sources.md`** — SHUD 文档、NOAA GFS、ECMWF、ERA5 等外部依据
18. **`../appendices/*`** — ID 命名规范、Manifest Schema、数据库 Schema 草案、sbatch 模板、OpenAPI 草案、验收清单

---

## 设计图索引

| 设计图 | 文件 | 对应文档 | 说明 |
|---|---|---|---|
| 系统架构图 | `../../design/architecture/系统架构图.png` | `01` 六层架构 | 六层物理视角的全栈技术架构 |
| 业务运转数据流转图 | `../../design/architecture/业务运转数据流转图.png` | `01` 端到端流程 | 六大环节数据流+控制流+状态回传 |
| 数据关系图 | `../../design/architecture/数据关系图.png` | `03` 数据库设计 | 五大实体域的逻辑关联 |
| 前端效果图 1 | `../../design/ui/前端效果图1.png` | `06` §2 全国总览 | 默认首页、河网、流域弹窗 |
| 前端效果图 2 | `../../design/ui/前端效果图2.png` | `06` §7.2 流域详情 | 河段交互、详情面板 |
| 前端效果图 3 | `../../design/ui/前端效果图3.png` | `06` §7.6 预报曲线 | 全屏三栏、多源对比、频率阈值 |
| 前端效果图 4 | `../../design/ui/前端效果图4.png` | `06` §13 洪水预警 | 预警总览、TOP 排名 |
| 前端效果图 5 | `../../design/ui/前端效果图5.png` | `06` §8B 气象代站空间 | 代站点位、forcing 曲线 |
| 前端效果图 6 | `../../design/ui/前端效果图6.png` | `06` §8 气象代站 | 站点列表、forcing 时序 |
| 前端效果图 7 | `../../design/ui/前端效果图7.png` | `06` §14 资产管理 | 模型版本、关系图 |
| 前端效果图 8 | `../../design/ui/前端效果图8.png` | `06` §15 产品监控 | 流水线状态、趋势图 |
