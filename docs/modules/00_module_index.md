# 模块索引

版本：v0.2  
日期：2026-05-06

> 模块文档为开发拆解说明；如与 `docs/spec/03_database_design.md`、`04_api_design.md` 不一致，以 Spec 主文档为准。

## 当前代码路径

M6 hardening 后，活跃代码路径以 importable package 和实际构建目录为准：

- 前端：`apps/frontend`
- Slurm 模板：`infra/sbatch`
- Worker packages：`workers/data_adapters`、`workers/canonical_converter`、`workers/forcing_producer`、`workers/shud_runtime`、`workers/output_parser`、`workers/flood_frequency`
- Storage roots：`WORKSPACE_ROOT` 仅用于本地/HPC 临时执行 workspace；`OBJECT_STORE_ROOT` + `OBJECT_STORE_PREFIX` 用于 durable raw/canonical/forcing/runs/states/tiles/log artifacts。

已退休目录 `apps/web`、`services/tile-publisher`、`workers/sbatch_templates` 和
hyphenated worker placeholders 已从 active tree 移除，不是当前开发入口。legacy
Slurm 模板名和迁移说明归档在 `docs/archived/legacy-slurm-templates.md`；当前路径分类以
`docs/governance/LEGACY_DEAD_CODE_INVENTORY.md` 和
`docs/governance/ROLE_BOUNDARY.md` 为准。

| 编号 | 模块 | 设计文档 | 开发 Spec |
|---|---|---|---|
| 01 | 数据源适配器模块 | `01_data_source_adapter_design.md` | `01_data_source_adapter_spec.md` |
| 02 | 原始数据发现与下载模块 | `02_raw_data_ingestion_design.md` | `02_raw_data_ingestion_spec.md` |
| 03 | 统一气象中间产品模块 | `03_canonical_met_product_design.md` | `03_canonical_met_product_spec.md` |
| 04 | SHUD forcing 生产模块 | `04_forcing_production_design.md` | `04_forcing_production_spec.md` |
| 05 | 模型资产与版本管理模块 | `05_model_registry_versioning_design.md` | `05_model_registry_versioning_spec.md` |
| 06 | Analysis 真实场状态运行模块 | `06_analysis_state_pipeline_design.md` | `06_analysis_state_pipeline_spec.md` |
| 07 | Forecast 预报运行模块 | `07_forecast_pipeline_design.md` | `07_forecast_pipeline_spec.md` |
| 08 | Slurm Gateway 模块 | `08_slurm_gateway_design.md` | `08_slurm_gateway_spec.md` |
| 09 | SHUD Runtime Adapter 模块 | `09_shud_runtime_adapter_design.md` | `09_shud_runtime_adapter_spec.md` |
| 10 | SHUD 输出解析与入库模块 | `10_output_parser_ingest_design.md` | `10_output_parser_ingest_spec.md` |
| 11 | 洪水频率与重现期模块 | `11_flood_frequency_return_period_design.md` | `11_flood_frequency_return_period_spec.md` |
| 12 | 数据库与对象存储模块 | `12_database_storage_design.md` | `12_database_storage_spec.md` |
| 13 | 后端 API 服务模块 | `13_api_backend_design.md` | `13_api_backend_spec.md` |
| 14 | 瓦片发布模块 | `14_tile_publication_service_design.md` | `14_tile_publication_service_spec.md` |
| 15 | 前端 Web 应用模块 | `15_frontend_application_design.md` | `15_frontend_application_spec.md` |
| 16 | 监控、质量控制与运维模块 | `16_monitoring_qc_ops_design.md` | `16_monitoring_qc_ops_spec.md` |
