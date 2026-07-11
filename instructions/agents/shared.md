# NHMS / NWM — Agent 指南

## 项目速览

- 技术栈：Python/FastAPI · pnpm/TypeScript · PostgreSQL+TimescaleDB+PostGIS · MinIO · Slurm · SHUD · OpenSpec
- 关键命令：test `uv run pytest -q` · lint `uv run ruff check .` · build `cd apps/frontend && pnpm build` · spec `openspec validate <change> --strict --no-interactive`
- 目录约定：`apps/api/`(FastAPI) · `apps/frontend/`(pnpm/TS) · `packages/`(共享库) · `services/`/`workers/`(后端服务) · `tests/`(pytest) · `schemas/`(JSON Schema) · `db/`(迁移) · `openspec/`(规格) · `docs/`(文档)

## 双端开发流程

本项目采用 **本地开发 + 远端测试** 模式；三端协作：

- **本地 (macOS)**: 代码编辑、commit、push、ruff、openspec validate、前端 tsc/pnpm test
  - 仓库路径: `/Users/danker/Desktop/Hydro-SHUD/NWM`
- **node-22 (210.77.77.22:32099, user=frd_muziyao)**:
  **纯计算节点**（Slurm/SHUD/forcing wrapper），**不连任何活 DB**；产物写 NFS
  `/ghdc/data/nwm/`（与 27 `/home/ghdc/nwm/` 同一份 NFS，零延迟无 rsync）；
  承担**调度 oracle**（Slurm/SHUD runtime 行为），**数据 + display oracle 在 27**。
  注：22 host 本地 historical PG :55433 已 archived/stopped，仅作显式 rollback archive，**不要连**
  - 仓库路径: `/scratch/frd_muziyao/NWM`（唯一工作树，必须保留 ff-only 同步）
- **node-27 (210.77.77.27:32099, user=nwm)**: 当前 active **primary PostgreSQL（本机 :55432）+ ingest 进程 + display API（:8080）+ 前端**都在本机；27 自己读 NFS 上的 22 产物、自己写入自己的 PG；basin 源数据在 `/home/ghdc/nwm/Basins` —— **所有真实数据 oracle、后端真实 DB pytest oracle、display/前端 live 验证 oracle**
  - 仓库路径: `/home/nwm/NWM`
  - readonly 是 role-level（`nhms_display_ro` 无 INSERT/UPDATE/DELETE），不是 standby 副本
- **同步方式**: GitHub (`DankerMu/SHUD-NWM`) 做中转，三端 push/pull

### 验证 oracle 路由（改了什么 -> 在哪验）

| 验证类型 | oracle 节点 |
|---|---|
| 后端单测/集成、真实 DB pytest、`e2e`/`grib` marker、SHUD 产物校验、display 部署 receipt、display 边界 deny-write、cross-plane identity live、`/`(单图展示，旧 `/hydro-met` 为 redirect alias)+`/ops` 浏览器 e2e | **node-27** |
| Slurm 调度行为本身的验证（罕见；改 sbatch / 计算资源时） | **node-22** |
| ruff、openspec validate、前端 tsc / pnpm test / check:api-types | 本地 |

涉及 display/前端生产化与只读边界的改动，**必须在 node-27 实机产出 live receipt**（见 `docs/runbooks/node-27-bringup-checklist.md` C1-C4），不得用本地 ruff 冒充 PASS。

### 标准开发循环

```
本地改代码 -> commit -> git push
-> node-27 ssh: cd /home/nwm/NWM && git pull --ff-only -> 跑后端验证 + 真实 DB pytest + display live receipt
-> (仅当改了 Slurm/SHUD 调度) node-22 ssh: cd /scratch/frd_muziyao/NWM && git pull --ff-only -> 触发计算
-> 失败则本地修复 -> 重复
```

### 远端 SSH 连接

```bash
ssh -p 32099 frd_muziyao@210.77.77.22   # node-22 compute_control
ssh -p 32099 nwm@210.77.77.27           # node-27 display_readonly
```

### 远端同步纪律（ff-only，绝不吞 stash）

- 两端工作树共享、可能有未提交内容；pull 前先 `git status --porcelain` 把关，用 `git pull --ff-only`，**绝不自动 `git stash pop`**（吞掉冲突会静默丢工作）。
- node-27 历史落后较多时，ff 合并可能因 **untracked 同名文件**中止（master 新跟踪的文件本地有同名 untracked）。处置：先确认内容与 master 一致（`diff` 为 0 / 备份到 `~/NWM-presync-backup-<date>/`），再清理冲突 untracked 后 ff；**绝不动** gitignored 数据/证据目录（`artifacts/`、`.nhms-*`、`data/Basins/` 等），有价值的本地证据先 `git stash push -- <file>` 保全。

### 环境隔离原则

- **不同步** `.venv/`、`node_modules/`、`.nhms-*`、`pgdata/`、`minio-data/`、`infra/env/compute.env`
- 两端系统不同（macOS vs Ubuntu），运行环境各自独立初始化
- `.env.example` 和 `infra/env/*.example` 是模板，可以同步；实际 `.env` 和 `compute.env` 不同步

## 开发环境约定

### Python

- **一律用 `uv`**（`uv run`、`uv pip` 等），禁止裸 `python` / `python3` / `pip`。
- 安装/刷新依赖：`uv sync --all-extras --dev`
- 运行命令示例：

```bash
uv run pytest -q
uv run ruff check .
uv run python -m uvicorn apps.api.main:app --host 0.0.0.0 --port 8000 --reload
```

### 前端

```bash
cd apps/frontend
pnpm install
pnpm test
pnpm build
```

### Linux / 生产环境迁移

macOS 的 `.venv` 和 `node_modules` 不可复用到 Linux，必须删除重建。初始化顺序：

1. `uv sync --all-extras --dev`
2. `corepack prepare pnpm@10.11.0 --activate`
3. `CI=true corepack pnpm install --frozen-lockfile`

迁移后验证：

- `uv run ruff check .` 必须通过
- `uv run pytest -q tests/test_api.py tests/test_gateway.py` 必须通过
- `cd apps/frontend && corepack pnpm test` 必须通过
- `cd apps/frontend && corepack pnpm build` 必须通过

## Issue 驱动开发

每个 issue 的验证标准写在 `openspec/changes/<milestone>/tasks.md` 的 "Evidence Floor" 中。

### 当前优先事项

- **Governance-3B #368**：对齐高影响 stale 文档事实与 display readonly live MVT 配置。
- **Governance-3C #369**：后续整理 `docs/bugs.md` ledger。
- **Governance-3D #370**：对齐 `.agents` / `.codex` / artifact ownership；已跟踪 project assets 走 PR review，新生成证据默认 local/generated。
- **node-27 / station-MVT #342**：station-MVT 点图层端点仍是独立 open backend 工作，不并入 #368。

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

当前 issue 的验证命令以其 GitHub issue body 的 `Verification:` 字段为准。

## 文档更新要求

文档权威状态与冲突解决顺序见 [`docs/governance/DOC_STATUS.md`](docs/governance/DOC_STATUS.md)。

开发过程中必须同步维护以下文档：

1. **OpenSpec tasks.md**: 完成一个 task 后立即勾选对应 checkbox
2. **Issue Evidence Floor**: PR 提交前确保所有 evidence 项可满足
3. **AGENTS.md / CLAUDE.md**: 环境变更、新工具引入时更新源（`instructions/agents/`）并重新生成

## PR 规范

- 分支命名: `feat/issue-<N>-<short-desc>`
- PR body 包含: 变更摘要、测试证据、Evidence Floor 覆盖声明
- 合并前必须通过 issue 指定的全部验证命令

### CI 成本纪律（避免重复跑 / 单一终态推送）

`.github/workflows/ci.yml` 触发于 push master + 所有 PR 事件。**2026-06-07 起改为按路径 scope + draft 快速通道**（见下"CI 范围与门控"），不再每推全跑 7 个 job。提交纪律仍然适用：

- **文档/规格更新必须并入触发合并门 CI 的最后一次 push** —— worklog、`openspec/**`、`*.md` 等随活儿一起 commit，或在最后一次代码推送之前推完。
- **不得在等 CI 绿期间再补 docs-only 的尾随 commit**（如"补个 worklog"）——那会重置合并门、白跑 CI。
- 一个 PR 的"最后一推"应已是完整终态；该推之后只等 CI 与 merge，不再追加任何 commit。
- 注：openspec/ **不可** gitignore——它是规格源 + `openspec validate` 对象 + 双端同步内容，忽略它会破坏 spec-driven 工作流（治错了病）。成本问题用上面的提交纪律解决。

### CI 范围与门控（路径 scope + draft 快速通道）

CI 是**人工合并门**（master 无 branch protection / required checks），不是机器强制；下面是"哪个改动触发哪个 job"的约定：

- **按路径 scope**：`changes` job（`dorny/paths-filter`）先判改动区，下游 job 仅在相关路径变化时跑。纯前端 / 纯 docs PR **不跑** 16min 后端 pytest；纯后端 PR 不跑前端构建。
  - 后端门（`unit-test` 全量 pytest、`real-db-integration` 起 TimescaleDB）：`**/*.py`、`pyproject.toml`、`tests/**`、`packages/**`、`apps/api/**`、`services/**`、`workers/**`、`schemas/**`、`db/**`。
  - 前端门（`frontend-build`）：`apps/frontend/**`、`openapi/**`。
  - lint 门：`markdown-lint`<-`docs/**`、`openapi-validate`<-`openapi/**`、`json-schema-validate`<-`schemas/**`。
- **draft = 快速定向通道，ready = 全量合并门**：
  - PR 标为 **draft** 时，后端只跑 `unit-test-fast`（仅本 PR 改动到的 test 文件 + collect-only 冒烟），迭代快；真实快速反馈仍以 **node-27 真实 DB** 为准（CI 不是迭代 oracle）。
  - PR 标为 **ready-for-review**（或 push 到 master）时，跑全量 `unit-test` + `real-db-integration` 作为合并门。**合并前务必把 PR 转 ready** 以触发全量。
  - **Fail-safe**：忘记标 draft -> 默认走全量门，只会多跑、绝不漏测。
- **`concurrency: cancel-in-progress`**：同一 PR 连推多次，自动取消被取代的旧 run。
- **M15 visual evidence 已从自动 CI 移出**：历史 M15 Playwright 视觉证据现在只通过 `.github/workflows/m15-visual-evidence.yml` 手动触发，运行 `test:e2e:m15-visual` / `mocked-regression-chromium`，并校验输入 SHA；它是历史 mocked 视觉证据，不是 node-27 live display proof。

## 技术栈速查

| 组件 | 技术 | 备注 |
|------|------|------|
| 后端 | Python, FastAPI | `uv run` 执行 |
| 前端 | pnpm, TypeScript | `apps/frontend/` |
| 数据库 | PostgreSQL + TimescaleDB + PostGIS | 远端有实例 |
| 对象存储 | MinIO (dev) / S3 | 远端有实例 |
| 气象代站时间序列 | 直读 object-store | `/home/ghdc/nwm/object-store/forcing/.../shud/X<lon>Y<lat>.csv` |
| HPC 调度 | Slurm | 仅远端可用 |
| 水文模型 | SHUD | 仅远端可用 |
| 规格管理 | OpenSpec | `openspec validate` |
| 代码检查 | ruff | `uv run ruff check .` |

## 服务器拓扑

| 节点 | 地址 | 角色 | DB |
|------|------|------|----|
| Node-22 | 210.77.77.22:32099 | 纯计算（Slurm/SHUD/forcing） | **不连任何活 DB**（本机 :55433 PG 已 archived/stopped，仅作显式 rollback archive） |
| Node-27 | 210.77.77.27:32099 | active primary PG + ingest + display API + 前端 | **本机 PG :55432**（自写自读） |
| 本地 Mac | localhost | 开发编辑 | 不连远端 DB |

生产 display API + 前端公网入口：`https://test.nwm.ac.cn`（27 反代对外，无需 SSH 隧穿）。

**非平凡改动前必读：**

- `docs/runbooks/two-node-deployment-overview.md` — 部署拓扑（注：M22 设计意图文档；当前与设计的差异见 banner）
- `docs/governance/ROLE_BOUNDARY.md` — 服务角色边界（注："node-22 owns database mutation" 是设计意图措辞；当前物理部署不同——见 banner）

## 已装能力

**Packs**：`agentic-issue-delivery`、`codebase-stewardship`

**Skills**（投影在 `.claude/skills/`（Claude）或 `.agents/skills/`（Codex））：

- 核心工作流：`subagent-workflow`（issue 实现全流程）· `stage-change-pipeline`（设计到 issue 全流水线）· `risk-adaptive-cross-review`（审核语义源）
- 执行编排：由 native 子代理（`implementer`/`reviewer`/`verifier`）执行，编排见 `subagent-workflow`
- 设计与澄清：`clarify` · `grill-me` · `grill-with-docs` · `brainstorming` · `future-aware-architecture` · `implementation-planning` · `blind-spot-pass`
- 代码质量：`review` · `entropy-review` · `repo-entropy-audit` · `improve-codebase-architecture` · `control-plane-auditor`
- 工具：`gh-create-issue` · `git-worktree-workflows` · `project-documentation` · `deep-research` · `codeagent`

**Agents**（投影在 `.claude/agents/`（Claude）或 `.codex/agents/`（Codex））：`implementer` · `reviewer` · `verifier` · `explorer` · `monitor` · `issue-scribe`

## 项目本地适配（living 文件，按需创建）

- `openspec/project-profile.md` — workflow 适配（入口/契约/风险轴）；`subagent-workflow` 首次运行可自动 bootstrap。
- `openspec/glossary.md` — 领域 ubiquitous language 单一来源；由 `grill-with-docs` / `improve-codebase-architecture` 维护。
- `docs/adr/NNNN-slug.md` — 长期架构决策账本（三门槛：难回退 + 无背景会困惑 + 真实权衡）。

## 反熵约定

根指令保持精简。包/能力的操作细节下沉到各自 `SKILL.md` / pack `README.md` / `CHANGELOG.md`，不在本文件展开；子树需细化时就近新增 scoped 指令文件。

## Observable Completion

完工附一行 `Execution Summary: agents=...; skills=...; tools=...; verification=...; limits=...`；保持事实、不展开隐藏推理。
