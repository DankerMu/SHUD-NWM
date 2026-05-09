## Context

全国水文模拟系统设计文档已通过 v0.2 设计冻结审核。核心设计包括：
- 6 个数据库 schema（core/met/hydro/flood/map/ops）、21 张核心表、4 组状态 ENUM
- 12 节 API 设计（REST + JSON）、6 角色权限模型
- Slurm + HPC 计算平面与业务控制平面解耦架构
- 前端 8 个页面的功能规格

当前状态：零代码。开发人员无法本地运行任何服务。M0 的职责是交付工程基础设施，使后续 M1（GFS 单流域闭环）可以直接写业务代码。

约束条件：
- 数据库选型已定：PostgreSQL 15 + PostGIS 3.4 + TimescaleDB 2.x
- API 后端语言未锁定，但倾向 Python（FastAPI）或 TypeScript（Express/Fastify）
- 前端框架未锁定，但倾向 Vue 3 或 React
- HPC 环境为 Slurm，M0 阶段用 Mock 替代

## Goals / Non-Goals

**Goals:**
- 开发者 `git clone && make dev` 即可本地启动 API + 数据库
- `make migrate` 创建全部 schema 和表，可重复执行
- `make seed-demo` 插入 demo 数据，前端可查到
- CI 通过 lint / validate / test / migration dry-run
- Mock Slurm Gateway 允许 orchestrator 和前端本地开发
- OpenAPI 契约覆盖全部核心 schema，可生成客户端代码
- JSON Schema 覆盖 manifest / status / qc / job，CI 可校验

**Non-Goals:**
- 不实现任何业务逻辑（adapter、converter、forcing、parser 等属于 M1+）
- 不对接真实 Slurm 或 HPC 环境
- 不搭建生产部署流水线（staging/prod 属于 M3+）
- 不实现前端页面（M1 开始）
- 不做性能优化（M3 全国化阶段考虑）

## Decisions

### D1：后端技术栈选 Python + FastAPI

**选择**：Python 3.11+ / FastAPI / SQLAlchemy + Alembic / asyncpg

**理由**：
- SHUD 生态（rSHUD/AutoSHUD）以 R 为主，但 Web 服务用 Python 生态更成熟
- FastAPI 原生支持 OpenAPI 生成，与设计文档中 openapi/nhms.v1.yaml 对齐
- SQLAlchemy + Alembic 提供成熟的 migration 管理
- asyncpg 提供高性能 PostgreSQL 异步访问
- 科学计算库（numpy/xarray/cfgrib）天然可用，M1 阶段 GRIB2 解析受益

**备选**：TypeScript + Fastify，优势是前后端统一语言，但 GRIB2/NetCDF 处理生态弱

### D2：Migration 使用原始 SQL 而非 ORM 生成

**选择**：`db/migrations/` 下的有序 `.sql` 文件，通过 Alembic 或自定义 runner 执行

**理由**：
- 设计文档已提供完整的 CREATE TABLE SQL，直接复用
- PostGIS geometry / TimescaleDB hypertable / 复合主键等特性用 ORM DDL 表达困难
- 原始 SQL 更透明，DBA 可直接审查

### D3：Monorepo 结构

**选择**：单仓库，按 apps / services / workers / packages / db 分区

**理由**：
- 系统组件间共享 schema 定义、错误码、ID 规范
- 单仓库简化 CI 和版本协调
- M0 阶段组件少，不需要 monorepo 工具链（turborepo/nx），Makefile 够用

### D4：Mock Slurm Gateway 实现为内进程 mock

**选择**：Gateway 服务内置 `backend: mock` 配置，mock 模式下不依赖 Slurm

**理由**：
- 开发者无需安装 Slurm
- Mock 返回确定性结果，便于集成测试
- 与真实 Gateway 共用接口定义，M3 只需切换 backend

### D5：Docker Compose 本地开发

**选择**：`infra/docker-compose.dev.yml` 提供 PostgreSQL + PostGIS + TimescaleDB + MinIO

**理由**：
- 一条命令启动全部依赖
- MinIO 模拟对象存储，prefix 规范可本地验证
- TimescaleDB 官方 Docker 镜像包含 PostGIS

## Risks / Trade-offs

| 风险 | 缓解 |
|---|---|
| Python + FastAPI 后续可能不满足瓦片服务性能要求 | 瓦片服务可独立为 Go/Rust 微服务，M0 不涉及 |
| SQL migration 手写可能遗漏外键或索引 | CI 执行 migration dry-run + `pg_dump --schema-only` 对比 |
| Demo seed 数据与真实数据格式偏差 | Seed 脚本严格使用 ID 命名规范（附录 A），M1 接入真实数据时验证 |
| OpenAPI 手写与 FastAPI 自动生成可能不一致 | M0 先手写 YAML，M1 切换为 FastAPI 自动生成后比对 |
| TimescaleDB hypertable 在空表上行为与有数据时不同 | seed 脚本插入足够时序数据覆盖 hypertable 分区 |
