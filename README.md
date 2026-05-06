# 全国水文模拟系统

> 多流域、多资料源、多模型版本的业务化水文模拟与预报平台

---

## 目录结构

```text
SHUD-NWM/
├── README.md                      ← 本文件，项目入口
│
├── docs/                          ← 全部文档
│   ├── spec/                      ← 核心设计规格（开发主入口）
│   │   ├── README.md              ← Spec 导航与阅读顺序
│   │   ├── 00_overall_design.md   ← 总体设计
│   │   ├── 01_architecture_and_flow.md
│   │   ├── 02_data_product_and_time_semantics.md
│   │   ├── 03_database_design.md
│   │   ├── 04_api_design.md
│   │   ├── 05_slurm_hpc_design.md
│   │   ├── 06_frontend_gis_design.md      ← 前端功能规格（859行）
│   │   ├── 06B_frontend_ui_design_spec.md ← 前端 UI 设计规范（676行）
│   │   ├── 07_devops_ops_security.md
│   │   ├── 08_roadmap_acceptance.md
│   │   └── 09_sources.md
│   │
│   ├── modules/                   ← 16 个模块的设计与开发规格
│   │   ├── 00_module_index.md     ← 模块索引
│   │   ├── 01 ~ 16 _design.md    ← 模块架构设计
│   │   └── 01 ~ 16 _spec.md      ← 模块开发规格
│   │
│   ├── appendices/                ← 附录（命名规范、Schema、模板、清单）
│   ├── report/                    ← 汇报材料（面向甲方）
│   │   └── 建设汇报稿.md
│   └── research/                  ← 调研与决策跟踪
│       ├── 气象数据梳理与决策跟踪.md
│       └── 数据下载账号与稳定性策略.md
│
└── design/                        ← 全部设计图
    ├── architecture/              ← 架构与数据设计图
    │   ├── 系统架构图.png
    │   ├── 业务运转数据流转图.png
    │   └── 数据关系图.png
    └── ui/                        ← 前端效果图
        └── 前端效果图1.png ~ 前端效果图8.png
```

## 快速导航

| 我想…… | 看这里 |
|---|---|
| 快速了解系统全貌 | [`docs/report/建设汇报稿.md`](docs/report/建设汇报稿.md) |
| 开始开发，了解架构 | [`docs/spec/README.md`](docs/spec/README.md) → 阅读顺序 |
| 查看某个模块的开发规格 | [`docs/modules/00_module_index.md`](docs/modules/00_module_index.md) |
| 查看数据库表定义 | [`docs/spec/03_database_design.md`](docs/spec/03_database_design.md) |
| 查看 API 接口 | [`docs/spec/04_api_design.md`](docs/spec/04_api_design.md) |
| 查看前端页面规格 | [`docs/spec/06_frontend_gis_design.md`](docs/spec/06_frontend_gis_design.md) |
| 查看前端 UI 样式/组件/图表 | [`docs/spec/06B_frontend_ui_design_spec.md`](docs/spec/06B_frontend_ui_design_spec.md) |
| 查看气象数据源详情 | [`docs/research/气象数据梳理与决策跟踪.md`](docs/research/气象数据梳理与决策跟踪.md) |
| 查看开发路线图 | [`docs/spec/08_roadmap_acceptance.md`](docs/spec/08_roadmap_acceptance.md) |
| 查看实施计划与阅读清单 | [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) |
| 查看设计图 | [`design/`](design/) |

## 设计图与文档对照

| 设计图 | 对应规格文档 |
|---|---|
| [`architecture/系统架构图.png`](design/architecture/系统架构图.png) | `docs/spec/01` §1.1 六层→四平面映射 |
| [`architecture/业务运转数据流转图.png`](design/architecture/业务运转数据流转图.png) | `docs/spec/01` §3 Forecast 流程 |
| [`architecture/数据关系图.png`](design/architecture/数据关系图.png) | `docs/spec/03` 数据库设计 |
| [`ui/前端效果图1.png`](design/ui/前端效果图1.png) | `docs/spec/06` §2 全国总览 |
| [`ui/前端效果图2.png`](design/ui/前端效果图2.png) | `docs/spec/06` §7.2 流域详情 |
| [`ui/前端效果图3.png`](design/ui/前端效果图3.png) | `docs/spec/06` §7.6 预报曲线详情 |
| [`ui/前端效果图4.png`](design/ui/前端效果图4.png) | `docs/spec/06` §13 洪水预警 |
| [`ui/前端效果图5.png`](design/ui/前端效果图5.png) | `docs/spec/06` §8B 气象空间展示 |
| [`ui/前端效果图6.png`](design/ui/前端效果图6.png) | `docs/spec/06` §8 气象代站查询 |
| [`ui/前端效果图7.png`](design/ui/前端效果图7.png) | `docs/spec/06` §14 资产管理 |
| [`ui/前端效果图8.png`](design/ui/前端效果图8.png) | `docs/spec/06` §15 产品监控 |
