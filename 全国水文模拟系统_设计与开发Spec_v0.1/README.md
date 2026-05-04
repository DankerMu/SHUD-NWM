# 全国水文模拟系统：总体设计与模块开发 Spec

版本：v0.1  
生成日期：2026-04-30  
文档格式：Markdown  
目标系统名称：**全国水文模拟系统**

本包用于研发立项、架构评审、模块拆分、任务排期和后续招标/外包接口定义。所有文件均为 `.md` 格式。

## 已确认的产品约束

1. 数据源权限可分阶段解决；系统设计中同时预留 GFS、IFS、ERA5、CLDAS 等适配器。
2. 前端时间轴按图层或数据源的原生时间分辨率展示，不强制统一插值到固定小时步长。
3. 模型运行调度采用 Slurm + HPC；Web 服务只做编排、登记、查询、发布，不直接运行 SHUD。
4. “径流频率”按洪水频率 / 重现期产品实现，不用简单百分位替代。
5. GFS、IFS 等预报源分 scenario 存储、展示和比较。
6. 对外系统名称统一为“全国水文模拟系统”。

## 文件目录

```text
.
├── README.md
├── 00_overall_design.md
├── 01_architecture_and_flow.md
├── 02_data_product_and_time_semantics.md
├── 03_database_design.md
├── 04_api_design.md
├── 05_slurm_hpc_design.md
├── 06_frontend_gis_design.md
├── 07_devops_ops_security.md
├── 08_roadmap_acceptance.md
├── 09_sources.md
├── modules/
│   ├── 00_module_index.md
│   ├── 01_data_source_adapter_design.md
│   ├── 01_data_source_adapter_spec.md
│   ├── ...
│   └── 16_monitoring_qc_ops_spec.md
└── appendices/
    ├── A_id_and_versioning_convention.md
    ├── B_run_manifest_schema.md
    ├── C_database_schema_draft.md
    ├── D_sbatch_templates.md
    ├── E_api_openapi_draft.md
    └── F_acceptance_checklist.md
```

## 推荐阅读顺序

1. `00_overall_design.md`：系统边界、总体目标和关键架构决策。
2. `01_architecture_and_flow.md`：control plane、HPC compute plane 与端到端流水线。
3. `02_data_product_and_time_semantics.md`：时间语义、资料源、scenario、analysis/forecast 拼接规则。
4. `03_database_design.md`、`04_api_design.md`、`05_slurm_hpc_design.md`、`06_frontend_gis_design.md`：工程架构。
5. `modules/*_design.md` 与 `modules/*_spec.md`：模块拆解开发。
6. `appendices/*`：开发模板、命名规范、验收清单。
