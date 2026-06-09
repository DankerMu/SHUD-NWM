# NHMS / NWM 项目开发规则

## 双端开发流程

本项目采用 **本地开发 + 远端测试** 模式；远端含两个角色节点（实为三端协作）：

- **本地 (macOS)**: 代码编辑、commit、push、ruff、openspec validate、前端 tsc/pnpm test
  - 仓库路径: `/Users/danker/Desktop/Hydro-SHUD/NWM`
- **node-22 (210.77.77.22:32099, user=frd_muziyao)**: `compute_control` —— 调度/DB/Slurm/Docker/SHUD 完整环境，**后端代码 + 真实 DB pytest 的测试 oracle**
  - 仓库路径: `/scratch/frd_muziyao/NWM`
- **node-27 (210.77.77.27:32099, user=nwm)**: `display_readonly` —— 只读 DB 副本 + published 产物，**display API / 前端生产化 / 只读边界 live 验证 oracle**
  - 仓库路径: `/home/nwm/NWM`
- **同步方式**: GitHub (`DankerMu/SHUD-NWM`) 做中转，三端 push/pull

### 验证 oracle 路由（改了什么 → 在哪验）

| 验证类型 | oracle 节点 |
|---|---|
| 后端单测/集成、真实 DB pytest、Slurm/SHUD 行为 | **node-22** |
| `e2e`/`grib` marker 测试（已从纯 CI 排除，`NHMS_RUN_E2E=1 NHMS_RUN_GRIB=1`，见 `docs/runbooks/ci-test-routing.md`） | **node-22** |
| display_readonly 部署 receipt、只读 DB denied-write、cross-plane identity live、`/hydro-met`+`/ops` 浏览器 e2e | **node-27** |
| ruff、openspec validate、前端 tsc / pnpm test / check:api-types | 本地 |

涉及 display/前端生产化与只读边界的改动，**必须在 node-27 实机产出 live receipt**（见 `docs/runbooks/node-27-bringup-checklist.md` C1–C4），不得用 node-22 或本地 ruff 冒充 PASS。

### 标准开发循环

```
本地改代码 → commit → git push
→ node-22 ssh: cd /scratch/frd_muziyao/NWM && git pull --ff-only → 跑后端验证命令
→ (display/前端) node-27 ssh: cd /home/nwm/NWM && git pull --ff-only → 产 live receipt
→ 失败则本地修复 → 重复
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

### 当前活跃 issue 验证

当前 issue 的验证命令以其 GitHub issue body 的 `Verification:` 字段为准；
完整双端动作流（派发 subagent 修/审 → 远端验证 → 有界评审循环 → merge）见
项目 skill `dual-end-issue-workflow`（`.claude/skills/`，仅本机加载）。

## 文档更新要求

文档权威状态与冲突解决顺序见 [`docs/governance/DOC_STATUS.md`](docs/governance/DOC_STATUS.md)。

开发过程中必须同步维护以下文档：

1. **OpenSpec tasks.md**: 完成一个 task 后立即勾选对应 checkbox
2. **Issue Evidence Floor**: PR 提交前确保所有 evidence 项可满足
3. **AGENTS.md**: 环境变更、新工具引入时更新
4. **本文件 (CLAUDE.md)**: 工作流变更、新里程碑启动、验证命令变化时更新

## PR 规范

- 分支命名: `feat/issue-<N>-<short-desc>`
- PR body 包含: 变更摘要、测试证据、Evidence Floor 覆盖声明
- 合并前必须通过 issue 指定的全部验证命令

### CI 成本纪律（避免重复跑 / 单一终态推送）

`.github/workflows/ci.yml` 触发于 push master + 所有 PR 事件。**2026-06-07 起改为按路径 scope +
draft 快速通道**（见下"CI 范围与门控"），不再每推全跑 7 个 job。提交纪律仍然适用：

- **文档/规格更新必须并入触发合并门 CI 的最后一次 push** —— worklog、`openspec/**`、
  `*.md` 等随活儿一起 commit，或在最后一次代码推送之前推完。
- **不得在等 CI 绿期间再补 docs-only 的尾随 commit**（如"补个 worklog"）——那会重置
  合并门、白跑 CI。
- 一个 PR 的"最后一推"应已是完整终态；该推之后只等 CI 与 merge，不再追加任何 commit。
- 注：openspec/ **不可** gitignore——它是规格源 + `openspec validate` 对象 + 双端同步内容，
  忽略它会破坏 spec-driven 工作流（治错了病）。成本问题用上面的提交纪律解决。

### CI 范围与门控（路径 scope + draft 快速通道）

CI 是**人工合并门**（master 无 branch protection / required checks），不是机器强制；下面是
"哪个改动触发哪个 job"的约定：

- **按路径 scope**：`changes` job（`dorny/paths-filter`）先判改动区，下游 job 仅在相关路径变化时跑。
  纯前端 / 纯 docs PR **不跑** 16min 后端 pytest；纯后端 PR 不跑前端构建。
  - 后端门（`unit-test` 全量 pytest、`real-db-integration` 起 TimescaleDB）：`**/*.py`、`pyproject.toml`、
    `tests/**`、`packages/**`、`apps/api/**`、`services/**`、`workers/**`、`schemas/**`、`db/**`。
  - 前端门（`frontend-build`）：`apps/frontend/**`、`openapi/**`。
  - lint 门：`markdown-lint`←`docs/**`、`openapi-validate`←`openapi/**`、`json-schema-validate`←`schemas/**`。
- **draft = 快速定向通道，ready = 全量合并门**：
  - PR 标为 **draft** 时，后端只跑 `unit-test-fast`（仅本 PR 改动到的 test 文件 + collect-only 冒烟），
    迭代快；真实快速反馈仍以 **node-22 真实 DB** 为准（CI 不是迭代 oracle）。
  - PR 标为 **ready-for-review**（或 push 到 master）时，跑全量 `unit-test` + `real-db-integration` 作为
    合并门。**合并前务必把 PR 转 ready** 以触发全量。
  - **Fail-safe**：忘记标 draft → 默认走全量门，只会多跑、绝不漏测。
- **`concurrency: cancel-in-progress`**：同一 PR 连推多次，自动取消被取代的旧 run。
- **M15 visual evidence 已从自动 CI 移出**：历史 M15 Playwright 视觉证据现在只通过
  `.github/workflows/m15-visual-evidence.yml` 手动触发，运行 `test:e2e:m15-visual`
  / `mocked-regression-chromium`，并校验输入 SHA；它是历史 mocked 视觉证据，不是
  node-27 live display proof。

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
