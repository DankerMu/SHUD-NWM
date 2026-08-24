<!--
Generated from instructions/agents/shared.md and instructions/agents/codex.md
by the project-instruction-bootstrap skill. Edit those sources, then re-run the skill.
Do not hand-edit this file.
-->

# NHMS / NWM — Agent 指南

## 项目速览

- 技术栈：Python/FastAPI · pnpm/TypeScript · PostgreSQL+TimescaleDB+PostGIS · MinIO · Slurm · SHUD · OpenSpec
- 关键命令：test `uv run pytest -q` · lint `uv run ruff check .` · build `cd apps/frontend && pnpm build` · spec `openspec validate <change> --strict --no-interactive`
- 目录约定：`apps/api/`(FastAPI) · `apps/frontend/`(pnpm/TS) · `packages/`(共享库) · `services/`/`workers/`(后端服务) · `tests/`(pytest) · `schemas/`(JSON Schema) · `db/`(迁移) · `openspec/`(规格) · `docs/`(文档)
- 气象代站时间序列直读 object-store，路径口径见 `docs/runbooks/object-store-forcing-series-read.md`

## 三端协作

**本地开发 + 远端测试**：代码只在本地编辑与 commit，经 GitHub (`DankerMu/SHUD-NWM`) 中转到两个远端验证。

| 端 | 地址 / 仓库路径 | 角色 | DB |
|---|---|---|---|
| 本地 Mac | `/Users/danker/Desktop/Hydro-SHUD/NWM` | 编辑、commit、push、ruff、openspec、前端 tsc/test | 不连远端 DB |
| node-22 | `ssh -p 32099 frd_muziyao@210.77.77.22`，`/scratch/frd_muziyao/NWM` | 纯计算（Slurm/SHUD/forcing wrapper），产物写 NFS `/ghdc/data/nwm/` | **不连任何活 DB**；本机 :55433 已 archived/stopped，仅作 rollback archive，**不要连** |
| node-27 | `ssh -p 32099 nwm@210.77.77.27`，`/home/nwm/NWM` | active primary PG + ingest + display API(:8080) + 前端；自己读 NFS 上 22 的产物 | **本机 PG :55432**（自写自读） |

- 22 的 `/ghdc/data/nwm/` 与 27 的 `/home/ghdc/nwm/` 是同一份 NFS，零延迟无需 rsync；basin 源数据在 `/home/ghdc/nwm/Basins`。
- node-27 的 readonly 是 **role-level**（`nhms_display_ro` 无 INSERT/UPDATE/DELETE），不是 standby 副本。
- 生产 display API + 前端公网入口：`https://test.nwm.ac.cn`（27 反代对外，无需 SSH 隧穿）。
- node-27 DB 数据文件全部在 `pg_default`（`/home/nwm/nhms-pgdata`，与 object store 共用 1.7 TB 卷）；容量核查一律 `df -h /home` + `psql` 实测，不引用文档里的历史数字。容器 `nhms-db` 由裸 `docker run` 创建（无 compose/systemd unit），重建流程与存储分层历史见 `docs/runbooks/tier-node27-timeseries-storage.md` 与 ADR 0002。

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

### 远端同步纪律（ff-only，绝不吞 stash）

- 两端工作树共享、可能有未提交内容；pull 前先 `git status --porcelain` 把关，用 `git pull --ff-only`，**绝不自动 `git stash pop`**（吞掉冲突会静默丢工作）。
- ff 合并可能因 **untracked 同名文件**中止。处置：先确认内容与 master 一致（`diff` 为 0 / 备份到 `~/NWM-presync-backup-<date>/`），再清理冲突 untracked 后 ff；**绝不动** gitignored 数据/证据目录（`artifacts/`、`.nhms-*`、`data/Basins/` 等），有价值的本地证据先 `git stash push -- <file>` 保全。
- 远端投送脚本/SQL 用 `ssh 'cat > file' < local`，不要靠嵌套引号传 SQL（单引号会被吃、`$` 会被远端展开）；长作业 `{ setsid nohup ... & }` 分离并把输出写文件。

### 环境隔离原则

**不同步** `.venv/`、`node_modules/`、`.nhms-*`、`pgdata/`、`minio-data/`、`infra/env/compute.env`；两端系统不同（macOS vs Ubuntu），运行环境各自初始化。`.env.example` 和 `infra/env/*.example` 是模板可同步，实际 `.env` / `compute.env` 不同步。Linux 端首次初始化与迁移后验证清单见 `docs/runbooks/two-node-deployment-overview.md`。

## 开发环境约定

- **Python 一律用 `uv`**（`uv run`、`uv pip`），禁止裸 `python` / `python3` / `pip`；装依赖 `uv sync --all-extras --dev`。node-27 上需 `export PATH=$HOME/.local/bin:$PATH`。
- 仓库默认解释器是 Python 3.11，由根 `.python-version` 钉住（与 CI 的 `python-version: "3.11"` 同源）；`pyproject.toml` 的支持范围仍是 `requires-python >=3.11`。日常安装/运行照旧 `uv sync --all-extras --dev` / `uv run`。显式跨版本行为核查用 `uv run --python <version> ...`（例：`uv run --python 3.14 python -V`）。node-22 将在下次受控同步时有意收敛到 3.11（与 CI/node-27 对齐）；该 pin 不含任何 Slurm 运行时验证。
- 前端：`cd apps/frontend && pnpm install && pnpm test && pnpm build`；Linux 端用 `corepack pnpm`（版本以 `package.json` 的 `packageManager` 为准）。

## Issue 驱动开发

- 每个 issue 的验证标准写在 `openspec/changes/<change>/tasks.md` 的 **Evidence Floor**；具体验证命令以该 issue body 的 `Verification:` 字段为准。
- 跨窗口优先级**以 GitHub open issues 为准**，本文件不再维护优先项清单（历来是 stale 高发区）。

## 文档更新要求

文档权威状态与冲突解决顺序见 [`docs/governance/DOC_STATUS.md`](docs/governance/DOC_STATUS.md)。开发过程中必须同步维护：

1. **OpenSpec tasks.md**：完成一个 task 后立即勾选
2. **Issue Evidence Floor**：PR 提交前确保所有 evidence 项可满足
3. **AGENTS.md / CLAUDE.md**：环境变更、新工具引入时改源（`instructions/agents/`）并重新生成，勿手改生成文件

## PR 规范

- 分支命名 `feat/issue-<N>-<short-desc>`；PR body 含变更摘要、测试证据、Evidence Floor 覆盖声明、偏离记录；合并前通过 issue 指定的全部验证命令。

### CI 成本纪律（避免重复跑 / 单一终态推送）

- **文档/规格更新必须并入触发合并门 CI 的最后一次 push**（worklog、`openspec/**`、`*.md` 随活儿一起 commit）。
- **不得在等 CI 绿期间再补 docs-only 的尾随 commit**——那会重置合并门、白跑 CI。一个 PR 的"最后一推"应已是完整终态。
- `openspec/` **不可** gitignore——它是规格源 + `openspec validate` 对象 + 双端同步内容；成本问题用上面的提交纪律解决。

### CI 门控要点

规则以 `.github/workflows/ci.yml`（+ `governance.yml` 的 report-only 审计）为准；下面只是读结果时必须知道的四条：

- **按路径 scope**：`changes` job 先判改动区，纯前端/纯 docs PR 不跑后端 pytest；`real-db-integration`（显示名 "SQL Migration Dry Run"）有独立且窄得多的 `database` filter，且 PR 上还要求非 draft。
- **PR 上后端只跑定向测试**：`unit-test-targeted`（显示名 "Unit Tests"）按本 PR diff 由 `scripts/select_ci_tests.py` 选文件；选不出时降级为 `--collect-only` 冒烟（**零断言执行**，step summary 会标注）。全量 `unit-test`（"Unit Tests (full)"）只在 push master 或手动 `workflow_dispatch` 跑。
- **PR 绿 ≠ 全量 pytest 通过**：全量回归是 merge 后 master run 才跑的**事后**发现；迭代 oracle 仍是 node-27 真实 DB，不是 CI。
- **draft -> ready 不触发新 run**（`on.pull_request` 未声明 `types:`）；要让 draft 期间跳过的 job 真跑起来，必须再推一个 commit 或 close/reopen。

## 已装能力

**Packs**：`agentic-issue-delivery`、`codebase-stewardship`

**Skills**：

- 核心工作流：`subagent-workflow`（issue 实现全流程）· `stage-change-pipeline`（设计到 issue 全流水线）· `risk-adaptive-cross-review`（审核语义源）
- 设计与澄清：`clarify` · `grill-me` · `grill-with-docs` · `future-aware-architecture` · `implementation-planning` · `blind-spot-pass`
- 代码质量与诊断：`entropy-review` · `repo-entropy-audit` · `improve-codebase-architecture` · `control-plane-auditor` · `diagnosing-bugs`
- 工具：`gh-create-issue` · `git-worktree-workflows` · `project-documentation` · `deep-research` · `codeagent` · `handoff` · `ask-danker` · `project-instruction-bootstrap`（本文件生成器）

**Agents**：`implementer` · `reviewer` · `verifier` · `explorer` · `monitor` · `issue-scribe`——实现/修复、交叉审查、发现裁决、只读勘察、长作业看守、立单，由 `subagent-workflow` 编排。

## 项目本地适配（living 文件，均已存在）

- `openspec/project-profile.md` — workflow 适配（入口/契约/风险轴）；`subagent-workflow` Phase 0.5 维护。
- `openspec/glossary.md` — 领域 ubiquitous language 单一来源；由 `grill-with-docs` / `improve-codebase-architecture` 维护。
- `docs/adr/NNNN-slug.md` — 长期架构决策账本（三门槛：难回退 + 无背景会困惑 + 真实权衡）。

## 反熵约定

根指令保持精简：只放**跨子树的纪律与路由**。操作细则下沉到 runbook / `SKILL.md` / pack `README.md`，历史考古下沉到 ADR；绝对数字（容量、行数、耗时）一律以实测为准，不写进本文件。子树需细化时就近新增 scoped 指令文件。

## Observable Completion

完工附一行 `Execution Summary: agents=...; skills=...; tools=...; verification=...; limits=...`；保持事实、不展开隐藏推理。

## Codex Notes

- 仓库级指令集中在根 `AGENTS.md`；子树需细化时新增 scoped `AGENTS.md`，勿膨胀根文件。
- Codex runtime 安装：skills -> `.agents/skills/`，agents -> `.codex/agents/`；改 canonical 后重装，勿编辑投影副本。

<对话风格>
自然段落写作，克制标题、列表与加粗。禁止在结尾进行"如果你.../需要我.../可以的话..."式追问。
</对话风格>
