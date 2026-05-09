## Why

全国水文模拟系统设计文档已冻结（v0.2-design-freeze），但尚无任何可运行代码。开发人员无法本地启动 API 和数据库，无法验证 migration，前端无法对接 mock 数据。M0 工程初始化是所有后续阶段（M1–M6）的前置条件，必须先交付工程骨架、数据库 migration、OpenAPI 契约、Mock Slurm Gateway、Demo 数据和 CI 基础设施。

## What Changes

- 创建标准化的 monorepo 目录骨架（apps / services / workers / packages / db / infra / tests）
- 编写 10 个有序 SQL migration 文件，覆盖 6 个 schema、21 张核心表、4 组 ENUM
- 生成 OpenAPI v3 契约 `openapi/nhms.v1.yaml`，定义全部核心 schema 和接口
- 实现 Mock Slurm Gateway，支持 submit/cancel/status/logs 四个操作
- 准备 Demo 数据集 seed 脚本（1 流域 + 河段 + 代站 + mock run）
- 落地对象存储 prefix 规范
- 交付 4 个 JSON Schema 文件（run_manifest / run_status / qc_result / pipeline_job）
- 配置 CI 最小检查（markdown lint / openapi validate / schema validate / migration dry-run / unit test）
- 提供 Docker Compose 本地开发环境（PostgreSQL + PostGIS + TimescaleDB）

## Capabilities

### New Capabilities

- `project-scaffold`: monorepo 目录骨架和基础配置（package.json / pyproject.toml / Makefile / Docker Compose）
- `database-migration`: 10 个有序 SQL migration 文件，覆盖 core / met / hydro / flood / map / ops 全部 schema
- `openapi-contract`: OpenAPI v3 完整契约，含 components.schemas / parameters / responses / securitySchemes
- `mock-slurm-gateway`: Mock 模式的 Slurm Gateway 服务，支持本地开发和集成测试
- `demo-seed-data`: Demo 流域数据 seed 脚本，可重复执行
- `object-storage-layout`: 对象存储 prefix 规范和目录校验工具
- `json-schemas`: run_manifest / run_status / qc_result / pipeline_job 四个 JSON Schema
- `ci-pipeline`: CI 最小检查配置（lint / validate / test / migration dry-run）

### Modified Capabilities

（无——本项目尚无现有 spec，全部为新建）

## Impact

- 新增约 15 个顶层目录和 50+ 文件
- 引入 PostgreSQL 15 + PostGIS 3.4 + TimescaleDB 2.x 依赖
- 引入 Node.js（API）或 Python（FastAPI）运行时依赖
- CI 流水线需要 GitHub Actions 或类似平台
- 后续所有模块开发基于此骨架展开
