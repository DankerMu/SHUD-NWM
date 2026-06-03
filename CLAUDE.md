# NHMS / NWM 项目开发规则

## 双端开发流程

本项目采用 **本地开发 + 远端测试** 模式：

- **本地 (macOS)**: 代码编辑、commit、push
- **远端 (210.77.77.22:32099, user=frd_muziyao)**: 测试运行、数据库、Slurm、Docker 完整环境
- **仓库路径**: 远端 `/scratch/frd_muziyao/NWM`，本地 `/Users/danker/Desktop/Hydro-SHUD/NWM`
- **同步方式**: GitHub (`DankerMu/SHUD-NWM`) 做中转，双端 push/pull

### 标准开发循环

```
本地改代码 → commit → git push
→ 远端 ssh: cd /scratch/frd_muziyao/NWM && git pull
→ 远端跑验证命令（见下方 issue 验证节）
→ 失败则本地修复 → 重复
```

### 远端 SSH 连接

```bash
ssh -p 32099 frd_muziyao@210.77.77.22
```

### 环境隔离原则

- **不同步** `.venv/`、`node_modules/`、`.nhms-*`、`pgdata/`、`minio-data/`、`infra/env/compute.env`
- 两端系统不同（macOS vs Ubuntu），运行环境各自独立初始化
- `.env.example` 和 `infra/env/*.example` 是模板，可以同步；实际 `.env` 和 `compute.env` 不同步

## Issue 驱动开发

每个 issue 的验证标准写在 `openspec/changes/<milestone>/tasks.md` 的 "Evidence Floor" 中。

### 当前活跃里程碑

- **M23**: `openspec/changes/m23-qhh-22-production-automation/`
  - 设计文档: `design.md`、`proposal.md`
  - 规格文档: `specs/<feature>/spec.md`
  - 任务清单: `tasks.md`（含 Evidence Floor 和验证命令）

### Issue 验证模板

每个 issue 的验证命令在其 GitHub issue body 的 `Verification:` 字段中定义。通用格式：

```bash
# 1. 单元/集成测试
uv run pytest -q tests/<相关测试文件>.py

# 2. 代码风格
uv run ruff check .

# 3. OpenSpec 规格验证
openspec validate <change-name> --strict --no-interactive
```

### 当前 Issue #256 验证命令

```bash
uv run pytest -q tests/test_forcing_producer.py tests/test_orchestration_chain.py tests/test_production_scheduler.py tests/test_ifs_forecast_integration.py tests/test_production_met_validation.py
uv run ruff check .
openspec validate m23-qhh-22-production-automation --strict --no-interactive
```

## 文档更新要求

开发过程中必须同步维护以下文档：

1. **OpenSpec tasks.md**: 完成一个 task 后立即勾选对应 checkbox
2. **Issue Evidence Floor**: PR 提交前确保所有 evidence 项可满足
3. **AGENTS.md**: 环境变更、新工具引入时更新
4. **本文件 (CLAUDE.md)**: 工作流变更、新里程碑启动、验证命令变化时更新

## PR 规范

- 分支命名: `feat/issue-<N>-<short-desc>`
- PR body 包含: 变更摘要、测试证据、Evidence Floor 覆盖声明
- 合并前必须通过 issue 指定的全部验证命令

## 技术栈速查

| 组件 | 技术 | 备注 |
|------|------|------|
| 后端 | Python, FastAPI | `uv run` 执行 |
| 前端 | pnpm, TypeScript | `apps/frontend/` |
| 数据库 | PostgreSQL + TimescaleDB + PostGIS | 远端有实例 |
| 对象存储 | MinIO (dev) / S3 | 远端有实例 |
| HPC 调度 | Slurm | 仅远端可用 |
| 水文模型 | SHUD | 仅远端可用 |
| 规格管理 | OpenSpec | `openspec validate` |
| 代码检查 | ruff | `uv run ruff check .` |

## 服务器拓扑

| 节点 | 地址 | 角色 |
|------|------|------|
| Node-22 | 210.77.77.22:32099 | 计算控制：自动化调度、Slurm、SHUD 执行 |
| Node-27 | 210.77.77.27:32099 | 只读展示：DB 只读副本 + 发布产物访问 |
| 本地 Mac | localhost | 开发编辑 |

## 隧穿配置

| 本地端口 | 远端目标 | 用途 |
|----------|----------|------|
| 18080 | 23.95.164.218:8080 | API 代理 (sub2api) |
| 8080 | 210.77.77.27:8080 | 服务访问 |
