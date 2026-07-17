---
name: project-instruction-bootstrap
description: >
  为目标项目安装 pack 之后，扫描项目并补全/对齐它自己的 CLAUDE.md 与 AGENTS.md。
  推荐用 shared 源生成模式：项目维护 instructions/agents/{shared,claude,codex}.md，
  本 skill 充当生成器拼接出两文件（shared 段单一事实源、零漂移），不给项目装脚本或 hook。
  内容含项目自身约定、已装 pack/skill/agent 用法、可移植编排骨架。增量、绝不覆盖、写前出 diff。
  触发词："补全项目指令"、"装完 pack 初始化 CLAUDE.md/AGENTS.md"、"bootstrap project instructions"。
  不用于 my-agents 仓库自身的根指令（那是 instructions/root 生成的）。
disable-model-invocation: true
invocation_posture: manual-first
version: 0.2.1
---

# Project Instruction Bootstrap

安装 pack 只是把能力投影进目标项目的 `.claude/` 或 `.agents/`、`.codex/`。但**目标项目自己的根指令**——`CLAUDE.md`（Claude Code 读）和 `AGENTS.md`（Codex 读）——还没人写。本 skill 在装完 pack 后补这一课。

`CLAUDE.md` 与 `AGENTS.md` 之间存在**大量逐字一致的共享内容**（项目速览、已装能力、本地适配、Observable Completion 等），只有末尾平台差异段不同。共享内容若分别手写进两个文件，必然漂移——这正是 my-agents 用 `instructions/root/shared.md` 消除的问题。所以本 skill 的首选做法是**给目标项目也建立 shared 源，并由 skill 自己充当生成器**。

## 两种模式

### 生成模式（推荐，默认用于新项目）

- 源放在 `instructions/agents/`：`shared.md`（两平台共享主体）、`claude.md`（Claude 增量）、`codex.md`（Codex 增量）。
- 本 skill 拼接生成：`CLAUDE.md = shared.md + claude.md`，`AGENTS.md = shared.md + codex.md`，并在生成物头部写 do-not-edit 标记（指明源 + "重新运行本 skill 以重生成"）。
- **skill 即生成器**：扮演 my-agents 里 `sync-instructions.js` 的角色。重跑 skill = re-sync。不给业务项目装 npm 脚本或 pre-commit hook——这是把 my-agents 机制下放但不下放它的重量。
- 收益：`shared.md` 是单一事实源，两文件的共享段零漂移；改 shared 只改一处。

### 增量兼容模式（用于已有手写根指令）

- 目标项目已有手写、非生成的 `CLAUDE.md`/`AGENTS.md`（无 `instructions/agents/` 源、无 do-not-edit 头）→ **尊重现状**：只增量补缺失段、保留既有内容、写前出 diff。
- 可向用户**提议迁移**到生成模式（把现有内容归类拆进 `shared/claude/codex` 源），但不强制、不擅自转换。

## Required Boundary（never break userspace）

- **绝不覆盖或删除**用户手写的内容。生成模式只覆盖**带 do-not-edit 头的生成物**；增量模式只追加缺失段。
- **写前必出 plan/diff**，确认后再落盘。
- 不把 my-agents 的"包仓库家规"（skill/agent/pack schema、catalog、sync 脚本本身）搬进业务项目。
- 不碰 my-agents 仓库自身的根指令。

## Process

### 1. 探测

- 读目标项目根的 `my-agents.project.json`（若有）：`platforms` / `packs` / `skills` / `agents`——"装了什么"的权威来源。
- 核对实际投影：`.claude/skills/`、`.claude/agents/`（claude）与 `.agents/skills/`、`.codex/agents/`（codex）。manifest 缺失时以投影目录反推平台。
- 扫描项目自身：`package.json` / `pyproject.toml` / `go.mod` / `Cargo.toml` 等推断技术栈与 build/test/lint 命令；浏览顶层目录推断结构与领域。
- 检测现有 `CLAUDE.md`/`AGENTS.md`：是否存在、是否已是生成物（有 `instructions/agents/` 源 + do-not-edit 头）、已覆盖哪些段落。

### 2. 选模式与目标文件

- 无根指令、或已是生成物 → **生成模式**；已有手写根指令 → **增量兼容模式**（并可提议迁移）。
- `platforms` 含 `claude` → 产出/补 `CLAUDE.md`；含 `codex` → `AGENTS.md`；两者皆有 → 都处理。无 manifest 时按实际投影目录推断。

### 3. 规划（出 plan，等确认）

- 生成模式：列出将创建/更新的 `instructions/agents/*` 源与生成物，标出 shared 段与各平台段。
- 增量模式：列出"已存在段落（保留）"与"将补全段落（待写）”。
- 展示给用户确认后再写。

### 4. 内容三块

模板与源结构见 [references/instruction-templates.md](references/instruction-templates.md)。

1. **项目自身约定**（→ `shared.md`）：技术栈、build/test/lint 命令、目录与命名约定、领域规则。能扫描确定的填实；不确定的留**显式 TODO 占位**，不臆造。
2. **已装能力**（→ `shared.md`）：从 manifest/投影列出 packs、skills、agents；关键触发与用法、平台分工（Claude 读 `CLAUDE.md`/投影在 `.claude/`，Codex 读 `AGENTS.md`/投影在 `.agents/`+`.codex/`）；装了成对 pack 时指向其搭配说明；执行编排默认引用已装的 `subagent-workflow` + native 子代理（implementer/reviewer/verifier），勿默认套用 `codeagent` 或旧名 `codex-codeagent-workflow`。
3. **可移植骨架**：项目无关、从 my-agents 编排理念移植——`shared.md` 放 Observable Completion（`Execution Summary` 契约）、反熵下沉、开发环境约定（Python→`uv` 防御性默认，其它语言工具链按扫描到的栈补）、项目本地适配指引（`openspec/project-profile.md` / `openspec/glossary.md` / `docs/adr/`）；`claude.md` 放 Claude 特有项（知识域 skill 显式 `/调用` 等）；`codex.md` 放 Codex 按模型纠偏（自然段落写作、禁末尾追问）。

### 5. 生成 / 写入

- 生成模式：`CLAUDE.md = shared + claude`、`AGENTS.md = shared + codex`，加 do-not-edit 头后写盘。共享段天然一致，无需"人工对齐"。
- 增量模式：把对应段落补进现有文件，shared 一致性尽力对齐（并说明这是无源退化方案，建议迁移）。

### 6. 收尾

- 输出：写了哪些源/生成物、新增哪些段、留下哪些 TODO 待人工补。
- 不提交、不推送。

## 与其它 skill 的关系

- 安装命令（`my-agents install pack`）：本 skill 是它的"装后第二步"——前者投影能力，后者补根指令。
- `project-documentation`：管 `docs/` 目录；本 skill 管根 `CLAUDE.md`/`AGENTS.md`。
- `subagent-workflow` 的 `openspec/project-profile.md`：管 workflow 层适配；本 skill 管根指令层。三者同遵"共享放共享层、项目特定放项目本地 living 文件"的理念。

---

设计直接复用 my-agents 自身"单一事实源 → 多平台投影"的范式：把 `shared.md + 平台 fragment → CLAUDE.md/AGENTS.md` 的生成关系下放到目标项目，由本 skill 充当生成器，从而既消除共享段漂移、又不给业务项目强加生成脚本与 hook。
