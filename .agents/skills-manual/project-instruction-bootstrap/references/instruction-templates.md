# CLAUDE.md / AGENTS.md 源与骨架模板

目标项目的两份根指令由**共享段**（逐字一致）+ 各自**平台差异段**组成。推荐用生成模式：把内容拆进 `instructions/agents/` 三个源，由本 skill 拼接生成两文件。已有手写文件时走增量模式，逐段比对、只补缺失段。能从扫描确定的填实，不确定的留 `<!-- TODO -->`，不臆造。模板默认中文，可按目标项目语言调整。

## 源文件结构（生成模式）

```
instructions/agents/
  shared.md   # 两平台共享主体（项目速览/已装能力/本地适配/反熵/Observable Completion）
  claude.md   # Claude 平台增量（Claude Code Notes）
  codex.md    # Codex 平台增量（Codex Notes + <对话风格>）
```

生成关系：

```
CLAUDE.md = shared.md + claude.md
AGENTS.md = shared.md + codex.md
```

生成物头部统一加 do-not-edit 标记（识别"是否已是生成物"也靠它）：

```md
<!--
Generated from instructions/agents/shared.md and instructions/agents/{claude,codex}.md
by the project-instruction-bootstrap skill. Edit those sources, then re-run the skill.
Do not hand-edit this file.
-->
```

## instructions/agents/shared.md（两平台共享，逐字进 CLAUDE.md 与 AGENTS.md）

```md
# {{Project Name}} — Agent 指南

## 项目速览

- 技术栈：{{stack}}
- 关键命令：build `{{build}}` · test `{{test}}` · lint `{{lint}}`
- 目录约定：{{layout}}
<!-- TODO: 领域规则 / 不变量 / 禁区（扫描无法确定，需人工补） -->

## 开发环境约定

- **Python 一律用 `uv`**（`uv run`、`uv pip` 等），禁止裸 `python` / `python3` / `pip`，避免污染 macOS / 系统 Python 环境。**无条件默认**——即使项目当前无 Python 也写入（防御性约定，项目长出 Python 工具时不踩坑）。
<!-- 其它语言工具链按扫描到的栈补：C++ 编译/构建纪律、Node 包管理器、Rust toolchain 等 -->

## 已装能力（来源：my-agents.project.json + 实际投影）

- Packs：{{packs}}
- Skills：{{skills}} — 投影在 `.claude/skills/`（Claude）或 `.agents/skills/`（Codex）
- Agents：{{agents}} — 投影在 `.claude/agents/`（Claude）或 `.codex/agents/`（Codex）
- 关键触发与用法：
  - `{{skill-a}}` — {{一句触发/用途}}
  - `{{skill-b}}` — {{一句触发/用途}}
<!-- 若装了成对 pack（如 agentic-issue-delivery + codebase-stewardship），在此指向其搭配说明 -->
<!-- 执行编排：装了 `subagent-workflow` 时写明"执行交 native 子代理（implementer/reviewer/verifier），编排见 subagent-workflow"；**不要**默认套用 `codeagent` 或旧名 `codex-codeagent-workflow`（codeagent 仅限确需 codex CLI 执行的场景）。 -->

## 项目本地适配（living 文件，按需创建）

- `openspec/project-profile.md` — workflow 适配（入口/契约/风险轴）；`subagent-workflow` 首次运行可自动 bootstrap。
- `openspec/glossary.md` — 领域 ubiquitous language 单一来源；由 `grill-with-docs` / `improve-codebase-architecture` 维护。
- `docs/adr/NNNN-slug.md` — 长期架构决策账本（三门槛：难回退 + 无背景会困惑 + 真实权衡）。

## 反熵约定

根指令保持精简。包/能力的操作细节下沉到各自 `SKILL.md` / pack `README.md` / `CHANGELOG.md`，不在本文件展开；子树需细化时就近新增 scoped 指令文件。

## Observable Completion

完工附一行 `Execution Summary: agents=…; skills=…; tools=…; verification=…; limits=…`；保持事实、不展开隐藏推理。
```

## instructions/agents/claude.md（Claude 平台增量，仅进 CLAUDE.md）

```md
## Claude Code Notes

- 知识域类 skill（如 `prompt-engineering`、调试方法论）自动触发率低，优先显式 `/skill-name` 调用。
- 安装重叠 skill 时剪枝旧/被取代项，保持技能列表清晰。
<!-- 仅保留与本项目实际装入能力相关的条目 -->
```

## instructions/agents/codex.md（Codex 平台增量，仅进 AGENTS.md）

```md
## Codex Notes

- 仓库级指令集中在根 `AGENTS.md`；子树需细化时新增 scoped `AGENTS.md`，勿膨胀根文件。
- Codex runtime 安装：skills → `.agents/skills/`，agents → `.codex/agents/`；改 canonical 后重装，勿编辑投影副本。

<对话风格>
自然段落写作，克制标题、列表与加粗。禁止在结尾进行"如果你…/需要我…/可以的话…"式追问。
</对话风格>
```

## 段落归属速查

| 段落 | 源文件 | CLAUDE.md | AGENTS.md |
|---|---|---|---|
| 项目速览 / 已装能力 / 本地适配 / 反熵 / Observable Completion | `shared.md` | ✓ | ✓ |
| Claude Code Notes | `claude.md` | ✓ | — |
| Codex Notes + `<对话风格>` | `codex.md` | — | ✓ |

## 增量模式（已有手写根指令，无源）

不强制建源。逐段比对现有 `CLAUDE.md`/`AGENTS.md`，只**追加缺失段**、保留既有内容；shared 段尽力对齐两文件并注明这是无源退化方案。可向用户提议把现有内容拆进上述三源、转入生成模式。
