---
name: ask-danker
description: 本仓库 skills 的路由器——按你当前的处境指路：该用哪个 skill、走哪条流、下一步交给谁。手动调用（/ask-danker），模型不会自动触发。
disable-model-invocation: true
invocation_posture: manual
version: 0.1.2
---

# Ask Danker

你不需要记住每个 skill——问这张地图。一条**流**是穿过若干 skill 的路径：大多数工作走一条主流，几条 on-ramp 汇入它，其余是独立工具或跑在底下的治理层。每条路由是"你现在的处境"，不是 skill 简介；细节以各 skill 自己的 SKILL.md 为准。

## 主流：想法 → 交付

1. **方向还没选** → `brainstorming`（探索/对比方向）；**需求含糊、验收标准缺失** → `clarify`。
2. **压测已选的方向**：`grill-me`（对话式逼清决策，不写文档）或 `grill-with-docs`（同时对齐术语、沉淀 `openspec/glossary.md` 与 ADR）。**任务进入陌生领域、计划还不存在**时先跑 `blind-spot-pass` 从代码库挖出你没想到要问的问题。
3. **需要产品文档** → `prd-authoring`（方向已定才进，产出可评审的 PRD）。
4. **设计变交付物** → `stage-change-pipeline`：设计文档 → OpenSpec change → 三路并行审核 → 修复与独立验证门 → implementation-ready GitHub issues。它内部按需调用 `implementation-planning`、`future-aware-architecture`、`grill-with-docs`、`gh-create-issue`。
5. **实现 issue**：`issue-controller`（DAG 主控循环，多 issue 调度）或 `subagent-workflow`（单 issue 全周期：实现 → PR → 交叉评审 → 合并）。
6. **评审，三选一**：单遍 diff/PR/staged 评审 → `review`；高风险、多视角、不变量/状态机聚焦 → `risk-adaptive-cross-review`；只关心一致性漂移、命名/模式重复 → `entropy-review`。

## On-ramps（汇入主流的起点）

- **方向已定、要深执行计划**（跨子系统、迁移、回滚路径）→ `implementation-planning`。
- **架构方向、技术选型、可逆性权衡** → `future-aware-architecture`。
- **只是要把需求变成 GitHub issue** → `gh-create-issue`（含 Epic/子 issue 拆分与宽改造 expand–contract 切法）。
- **出了毛病、原因不明**（难缠 bug、性能回归、修了又复发、CI 挂但本地复现不了）→ `diagnosing-bugs`：先建红色反馈回路后假设，确诊后修复回所在工作流；`subagent-workflow` 的修复环节内部也消费它。原因已确诊只差动手的修复不必进——直接修。
- **多后端 AI 代码任务（Codex/Claude/Gemini 执行）** → `codeagent`。

## 仓库健康（多为手动调用）

- `repo-entropy-audit` — 全仓六轴熵体检，产出优先级清单。
- `entropy-review` — 变更集的一致性/漂移评审（自动触发型，也可点名）。
- `control-plane-auditor` — 控制面审计：CLAUDE.md/AGENTS.md、hooks、生成物、指令一致性。
- `improve-codebase-architecture` — 深模块机会扫描；选中的候选回到主流第 2 步压测。
- `project-documentation` — docs 漂移检查与刷新。
- `project-instruction-bootstrap` — 安装 pack 后对齐目标项目的根指令文件（安装命令会提示这一步）。
- `editorial-review` / `readme-craftsman` — 文档编辑评审 / README 打磨。

## 研究流

- `research-lifecycle` 是研究控制面的路由器，按需编排：`research-profile-bootstrap`（研究档案）→ `research-question-framing`（问题定型）→ `study-design`（实验设计）→ `scientific-evidence-synthesis`（证据合成）→ `theory-to-code-traceability`（理论-代码追溯）→ `research-engineering-handoff`（交给工程）。从 `research-lifecycle` 进，别直接凑叶子。
- `deep-research` — 独立的多源、带引用的深度研究报告（web 检索）。
- `meta-loop` — 昂贵或安全关键的评估/证据循环的元审计。

## 治理 / 元

- `skill-lifecycle-manager` — skill 的创建、验证、评测、投影、发布、审计全周期。
- `agent-lifecycle-manager` / `agent-architect` — agent 合约的同套治理 / 设计。
- `prompt-engineering` — 提示词与系统提示设计（知识型 skill，显式调用最可靠）。
- `hook-development` — Claude Code hooks 开发。
- `agentic-development` — 多 agent 系统的运行时调试与开发方法。
- `git-worktree-workflows` — worktree 并行开发的手动指引与恢复。

## 跨会话

上下文将满时：同一会话内的阶段过渡用原生 compact；**要开新会话/换 agent 续跑 → `/handoff`**——把会话独有状态（已定决策、工作流计数器、已排除路径、下一步）压成交接文档，issue 工作流中写到 `.workplans/` 证据束旁，新会话第一条消息加载接续。并行实现用 `git-worktree-workflows` 隔离工作区。

## User-invoked 清单

以下 skill 设了 `disable-model-invocation: true`——只能由你 `/name` 调用，模型不会自动触发，也不占常驻上下文：

`ask-danker`（本 skill）、`agentic-development`、`git-worktree-workflows`、`handoff`、`project-instruction-bootstrap`、`prompt-engineering`、`control-plane-auditor`、`repo-entropy-audit`。

> 维护约定：新增、改名、删除任何 skill，或改动上述任何流的走向时，必须回查本地图并更新——路由器撒谎比没有路由器更糟。校验器会检查每个 user-invoked skill 都出现在本地图中。
