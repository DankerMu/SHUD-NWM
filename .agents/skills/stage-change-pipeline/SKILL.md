---
name: stage-change-pipeline
description: >
  设计文档 → openspec change → subagent 并行审核 → 修复 → GitHub issue 全流水线。
  将 tasks 拆为细粒度、模块边界清晰、适合小 PR 审核的 GitHub issue。
  触发词："开始下一个阶段"、"stage change pipeline"、"设计到issue"、"阶段实施"、
  "openspec审核"、"创建 M* change"，或用户指定一个开发阶段要求生成审核过的 issue。
license: MIT
metadata:
  author: danker
  version: "0.8.0"
---

# Stage Change Pipeline

将开发阶段的设计文档转化为经过审核的、可追踪的 GitHub Issue。

整个流水线分 5 个阶段，每个阶段有明确的输入输出契约。可以从任意阶段切入——如果 openspec change 已存在，直接跳到审核；如果审核已完成，直接跳到创建 issue。

**依赖**：需要 `openspec` CLI（npm）、具备并行 subagent 能力的编排器（Claude Code Task subagents 或 Codex subagents）、已认证的 `gh` CLI、包含设计文档的 git repo。可选读取 `IMPLEMENTATION_PLAN.md` 作为阶段上下文。

**支撑 skill**：按需要复用本仓库已有 skill，不把它们的完整流程复制进来。

- `clarify`：阶段目标、验收标准、范围边界或设计文档优先级不清时，在 Stage 1 前先澄清。
- `grill-me`：Stage 1 收尾、创建 OpenSpec change 之前，对阶段计划/设计文档做对抗式压测——沿决策树逐分支追问、一次一个问题，把未言明的假设和模糊边界逼清，降低 Stage 3 审核返工。
- `grill-with-docs`：Stage 2 写 design/specs 时，对领域复杂、术语易漂的 change 做领域压测——对齐术语并 inline 沉淀到 `openspec/glossary.md`，够格的长期决策落 `docs/adr/`。
- `future-aware-architecture`：Stage 2 的 `design.md` 涉及架构方向、技术选型、可逆性或长期演进风险时，用它形成决策输入。
- `implementation-planning`：Stage 2 的 `tasks.md` 或 Stage 5 issue 分组需要复杂依赖、回滚、验证矩阵或分阶段交付时，用它补执行计划。
- `risk-adaptive-cross-review`：Stage 3 的三路审核按 OpenSpec Review 模式组织 finding contract 和失败类汇总。
- `project-documentation`：设计文档、实施计划或 docs 导航陈旧，影响 Stage 1 上下文可信度时，用它做 docs drift 检查或刷新。
- `gh-create-issue`：Stage 5 需要批量创建 Epic/子 issue 时可调用；否则直接使用 `gh` CLI。

## When Not to Use

- 不用于单个 GitHub issue 的实现、修复、PR review、CI 或合并；这些属于 `subagent-workflow`。
- 不用于纯头脑风暴、需求澄清或架构选型；先用 `clarify`、`brainstorming` 或 `future-aware-architecture`。
- 不用于没有设计文档、阶段目标或实施计划的临时小改动。
- 不用于只想创建一个普通 issue、且不需要 OpenSpec change 和并行审核的场景。

---

## 流水线概览

```
Stage 1: 上下文收集
    ↓
Stage 2: OpenSpec Change 创建
    ↓
Stage 3: 并行 Subagent 审核 (3 路)
    ↓
Stage 4: 审核修复 (P0 + P1 均阻塞)  ←────────┐
    ↓                                         │ 仍有 P0/P1 未解决/回归且轮次 < 3
Stage 4.5: 独立验证门 ───────────────────────┘
    ↓ (P0 + P1 全部 resolved，或触顶 3 轮)
Stage 5: GitHub Issue 创建
    ↓
Stage 5.5: Issue-Change 对齐审核 (≤2 轮)

推荐用 full-pipeline.workflow.js 一次调用完成 Stage 3→5.5，
回环由 review-loop / issue-alignment workflow 硬编码执行。
```

---

## Stage 1: 上下文收集

**目标**：确定目标阶段，收集必读文档清单。

**澄清门禁**：如果目标阶段、范围、验收标准、必读文档或阶段完成定义不清，先调用 `clarify`。如果问题不是澄清而是方向选择，先交给 `brainstorming` 或 `future-aware-architecture`，不要直接创建 OpenSpec change。

**步骤**：

1. 查找实施计划文档。按优先级搜索：`IMPLEMENTATION_PLAN.md`、`implementation_plan.md`、`docs/**/implementation*`、`docs/**/roadmap*`、`**/PLAN.md`。如果找到，读取目标阶段的任务包和必读文档列表。
2. 如果没有实施计划文档，从用户描述中提取阶段目标，然后搜索项目中的设计文档（用 `find . -name "*.md" -path "*/docs/*" | head -30` 发现文档结构，不假设固定路径）。
3. 并行读取目标阶段涉及的核心设计文档（通常 3-6 个文件），记录关键实体：表名、API 端点、ENUM 值、ID 规范等。
4. 输出一份简要的阶段上下文摘要，确认后进入 Stage 2。

**压测门禁（可选但推荐）**：在创建 OpenSpec change 之前，如果阶段计划或设计文档存在未言明假设、隐藏依赖或模糊边界，先用 `grill-me` 沿决策树逐分支压测，把决策逼清后再进入 Stage 2，可显著降低 Stage 3 审核返工。

**判断切入点**：如果 `openspec/changes/<name>/` 已存在且 `openspec status` 显示 artifacts complete，跳到 Stage 3。

---

## Stage 2: OpenSpec Change 创建

**目标**：生成 proposal → design → specs → tasks 四个 artifact。

**前置**：确认 `openspec` CLI 可用（`which openspec`），项目已初始化（`openspec/` 目录存在，否则执行 `openspec init --tools claude`）。确认 `openspec/` 在 `.gitignore` 中。

**步骤**：

1. 创建 change：
   ```bash
   openspec new change "<stage-name>"
   ```

2. 获取构建顺序：
   ```bash
   openspec status --change "<name>" --json
   ```

3. 按依赖顺序生成 artifact：

   **proposal.md**（第一个，无依赖）：
   - 获取指令：`openspec instructions proposal --change "<name>" --json`
   - 基于设计文档写 Why / What Changes / Capabilities / Impact
   - Capabilities 部分的每个 capability 用 kebab-case 命名，后续会生成对应 spec

   **design.md**（依赖 proposal）：
   - 获取指令：`openspec instructions design --change "<name>" --json`
   - 写技术决策（选型理由、备选方案）、风险和缓解
   - 如果技术决策还没有稳定依据，先用 `future-aware-architecture` 形成架构决策输入
   - 领域概念多、术语易漂时，用 `grill-with-docs` 对齐术语并 inline 沉淀到 `openspec/glossary.md`/`docs/adr/`，再定稿 design/specs

   **specs/**（依赖 proposal，可与 design 并行）：
   - 获取指令：`openspec instructions specs --change "<name>" --json`
   - 为 proposal 中列出的每个 capability 创建 `specs/<capability>/spec.md`
   - 每个 spec 包含 `## ADDED Requirements`，每个 Requirement 下有 `#### Scenario:` 用 WHEN/THEN 格式
   - capability 数量多时（>4），用并行 agent 分批写入

   **tasks.md**（依赖 design + specs）：
   - 获取指令：`openspec instructions tasks --change "<name>" --json`
   - 按 capability 分组，每个任务用 `- [ ] X.Y 描述` 格式
   - 任务粒度：单个 session 可完成
   - 任务顺序：按依赖关系排列
   - 如果任务依赖、验证或回滚路径复杂，先用 `implementation-planning` 产出执行计划，再写 tasks

4. 验证：
   ```bash
   openspec status --change "<name>"
   ```
   确认 4/4 artifacts complete。

---

## Stage 3: 并行 Subagent 审核

**目标**：从 3 个独立视角审核 change 质量，发现错项和漏项。

**关键约束**：通过编排器的原生并行 subagent 机制（Claude Code Task subagents 或 Codex subagents）同时发起 3 个审核任务，不能串行。每个 `reviewer` subagent 必须返回完整审核文本而非摘要。

**审核契约**：使用 `risk-adaptive-cross-review` 的 OpenSpec Review 模式作为审查语义参考：三路审核分别对应 Design Consistency、Spec Completeness、Tasks Executability;P0/P1 问题必须包含失败类型、证据、影响、修复方向和需要回归检查的相邻 artifact。失败类型从 `finding-contract.md` 的 Failure-Class Vocabulary（含 `design-consistency`/`spec-completeness`/`task-executability` 等 spec 类）取一个标签;含糊、无锚点、纯风格的条目按 Reject 精度门降级为 note，不进 P0/P1。

**步骤**：

1. 构建 3 个审核 brief，每个约 200-400 字，包含：
   - 明确的审核范围和检查项
   - 指向 change 文件和设计文档的 OpenSpec 路径（如 `openspec/changes/<name>/proposal.md`）
   - 期望的输出格式（候选 finding 列表，含失败类型/证据/影响/修复方向）

2. 三路审核的标准分工：

   | 审核 | 视角 | 核心检查项 |
   |---|---|---|
   | Review 1: 设计一致性 | change 文件 vs 设计文档 | 表名/字段/ENUM 拼写一致性、API 端点覆盖完整性、ID 规范合规、manifest 字段对齐 |
   | Review 2: Spec 完整性 | 各 spec 之间 + 对照实施计划 | Requirement-Scenario 完备性、WHEN/THEN 可测试性、边界条件覆盖、跨 spec 一致性、功能点遗漏 |
   | Review 3: Tasks 可执行性 | tasks.md vs design + specs | 任务粒度、依赖顺序、spec 覆盖率、多余任务、验证方法明确性、技术决策落地 |

3. 用编排器的原生并行 subagent 机制同时 spawn 3 个 `reviewer` subagent（每路一个 brief），并行执行、互不通信。每个 subagent 是只读 leaf：只审核、不修改 change 文件，也不再嵌套发起本流水线。建议的 task id：`review-design-consistency` / `review-spec-completeness` / `review-tasks-executability`。

4. 等三路 subagent 全部返回完整审核文本，从中提取各自的候选 finding。

5. 汇总三路审核的交叉验证结果，去重后按 P0（必须修复）/ P1（建议改进）分类。

---

## Stage 4: 审核修复

**目标**：根据审核意见修改 change 文件，解决所有 P0 和 P1 问题——两者均为阻塞带。每轮带着 Stage 4.5 验证门反馈的未解决/回归 finding 进入；首轮直接消费 Stage 3 的去重 finding。

> **执行方式**：Stage 3→4→4.5 回环由 `review-loop.workflow.js` 的 `while` 循环硬编码执行，不依赖编排器自觉遵守散文回环指令。

**步骤**：

1. 从三路审核中提取去重后的 P0 + P1 问题清单，识别共性问题（如命名不一致、字段遗漏、计数矛盾）。P0 和 P1 均为阻塞带，全部必须修复。

2. 按文件分组修改。典型的 P0 问题类型：
   - **命名不一致**：目录名、文件名、表名在 spec/tasks/design 之间不统一
   - **字段遗漏**：OpenAPI schema 缺少数据库表中的字段
   - **计数矛盾**：表数量、ENUM 值数量在不同文件中不一致
   - **结构错误**：如 JSON Schema 的 required 应该是嵌套结构而非扁平
   - **覆盖不全**：tasks 缺少 spec requirement 对应的实现任务

3. 修改文件多时（>3），用并行 agent 分批修复，确保互不交叉。

4. 修复后验证 `openspec status --change "<name>"` 仍然 4/4 complete。

5. 记录本轮改动的文件清单和每条 finding 的修复证据（文件 + 行/片段），交给 Stage 4.5 核销。不要在 Stage 4 自行宣告"已修复"——结论由独立验证门给出。

---

## Stage 4.5: 独立验证门（有界回环）

**目标**：由不参与修复的独立视角确认 Stage 4 真正解决了 P0/P1，且修复没有引入新的不一致；未达标则带证据回到 Stage 4，最多 3 轮。

**关键约束**：
- 验证者必须是**不参与本轮修复**的独立 subagent（只读 leaf：只核销、不改 change 文件，也不再嵌套发起本流水线）。建议 task id：`verify-review-fixes`。
- 对抗式判定：每条 finding **默认"未解决"**，必须有修复证据才判 `resolved`。证据不足、含糊或对不上即判 `unresolved`。
- **Oracle 完整性**：源设计文档、实施计划、Stage 1 的阶段目标与验收标准是不可篡改的 oracle——是判定 finding 的标准，不是为了让 change 过审而拧的旋钮。只修 change 文件，不改判定它的东西；确需改源设计的，按"方向选择"回 `brainstorming`/`future-aware-architecture`，并显式记录,不得静默改写来消 finding。

**触发与确认（不可跳过）**：
- 禁止从 Stage 4 直接跳到 Stage 5——每完成一轮 Stage 4 修复，必须经过本验证门。
- 每轮进入验证门时编排器显式输出 ack：`Stage 4.5 round <N>: started`；退出时输出 `round <N> verdict: <resolved>/<total> resolved, residual P0=<n> P1=<n>`。这两行是该轮验证门确实运行的可审计凭据，缺失即视为门被跳过，不得进入 Stage 5。
- **可靠性限制（诚实声明）**：本 skill 是可移植的文档方法论，无法替消费项目安装 pre-commit/pre-PR hook，因此触发可靠性依赖上述 ack 凭据 + 编排器纪律。若消费仓库存在可挂载的硬动作（如 PR 创建前钩子），应把本门锚定其上。各项目真实 skip-rate 为开放实测项，需在使用中审计，不可假设"写了就一定会跑"。

**步骤**：

1. 用编排器原生 subagent spawn 一个 `verifier`，输入：本轮去重后的 P0/P1 清单 + Stage 4 改动的文件列表 + 各 finding 的修复证据。
2. 逐条核销：每条 finding 标 `resolved` / `unresolved` / `regressed`（修复引入的新问题），附文件 + 行/片段证据。
3. Delta 一致性扫描：只对**被改文件触及**的跨 artifact 关系（命名、计数、覆盖、ID 规范）复查，不重跑全量三路审核。仅当修复改动了实体名、表/ENUM 计数等会全局扩散的项时，升级为 Stage 3 三路全审。
4. 汇总：残留的 P0/P1（`unresolved`）+ 新增的 P0/P1（`regressed`）。

**回环判定**：

**band 划分**：P0 和 P1 均为 **blocking 带**——两者都驱动回环，必须全部修到 `resolved` 才放行。回环逻辑由 `review-loop.workflow.js` 的 `while (activeFindings.length > 0 && round < MAX_ROUNDS)` 硬编码执行，编排器无法跳过。

- **退出（进 Stage 5）**：P0 + P1 全部 `resolved` **且** `openspec status` 4/4 complete **且** 验证门本轮无回归 **且** 完成自审通过——对照 Stage 1 收集的阶段目标与验收标准逐条核对，每个要求都有对应的 spec requirement + task 覆盖、无遗漏的设计目标或边界、change 内部无相互矛盾的修复;任一要求未覆盖则回 Stage 4，不靠"看起来修完了"放行。
- **继续**：仍有 `unresolved`/`regressed` 的 P0 或 P1 **且** 已完成轮次 < 3 → workflow 脚本自动带验证门证据回 Stage 4 再修，轮次 +1。
- **触顶（已完成 3 轮）**：把残留 P0/P1 如实写入对应 issue 正文并标 `needs-followup`，不假装干净；残留 P0 必须在 Epic 中显著标注阻塞风险。
- **收敛停止（提前退出）**：若某轮净新增 finding 不再下降，可在 3 轮内提前停，按"触顶"方式记录残留，避免空转。

---

## Stage 5: GitHub Issue 创建

**目标**：将 change 的 tasks 细粒度拆分为 GitHub Issue（Epic + 子任务），使每个子 issue 预期对应一个小而可审阅的实现 PR。

**前置**：`gh auth status` 确认已认证。

**规划门禁**：如果 tasks 无法自然映射到小 PR issue，或存在跨模块依赖链，先用 `implementation-planning` 明确分阶段交付、依赖顺序、验证和回滚；不要为了减少 issue 数量合并模块边界。

**实现就绪契约**：Stage 5 创建的每个子 issue 都必须可被 `subagent-workflow` 自动执行，不得把需求澄清留到实现阶段。每个子 issue 必须具备：

- `Implementation Ready: yes`
- 单一模块或 ownership 范围
- 明确的 In Scope / Out of Scope
- 可执行任务清单
- 可验证验收标准
- 必读文档和 OpenSpec change 引用
- 依赖 issue 列表
- 预期 PR 边界
- 不需要实现阶段再做产品决策、范围判断或需求澄清

**分组原则**：
- 不设置子 issue 数量上限；数量由模块边界、依赖关系和可审阅 PR 体量决定。用 Epic 保持总览，不通过合并子 issue 来压低数量。
- 每个子 issue 只覆盖一个模块、包、服务、目录边界或 ownership 边界。禁止一个实现 issue 同时修改数据库、后端 API、前端 UI、CI、文档生成等多个模块。
- 按"可独立实现和验证的模块内交付物"分组。一个子 issue 应包含 1-3 个紧密相关的 tasks；如果 tasks 会形成大 PR，继续拆分。
- 跨模块能力必须拆成多个子 issue，用 `Dependencies` 串联：先创建共享契约或接口准备 issue，再分别创建各模块实现 issue，最后创建必要的集成验证 issue。
- 只有当多个 task 位于同一模块、同一 owner、同一验证路径，且拆开会制造无意义阻塞时，才允许合并。
- 不以 capability 名称机械合并 issue；同一 capability 可以拆成多个模块 issue，不同 capability 也只有在同一模块内强耦合时才可合并。
- 每个 issue 必须包含：任务清单、必读文档表、验收标准

**步骤**：

1. 创建标签（如 `epic`, `sub-task`, `priority:high`, 阶段标签如 `m0`）：
   ```bash
   gh label create <name> --color <hex> --description "<desc>" --force
   ```

2. 创建 Epic issue，包含：
   - 概述和交付物表
   - 设计文档引用
   - 子任务占位（后续更新）
   - 依赖关系图

3. 从 `tasks.md` 生成子 issue 分组：
   - 标注每个 task 的目标模块、主要文件/目录、依赖 task、验证方式
   - 将跨模块 task 拆成模块内 task；如果无法拆分，在 issue 正文说明原因和预期 PR 边界
   - 检查每个分组是否能由一个小 PR 完成；不能则继续拆分

4. 为每个分组创建子 issue，包含：
   - `Part of #<epic>` 链接
   - `**Dependencies:** #<dep1>, #<dep2>` 依赖声明
   - `**Module / Scope:** <module-or-path>` 单一模块或路径范围
   - `**In Scope:**` 本 issue 明确包含的行为、文件或交付物
   - `**Out of Scope:**` 相邻模块、后续能力或明确不做的工作
   - `**PR Boundary:**` 预期修改范围和明确不包含的相邻模块
   - 任务清单（从 tasks.md 提取，保留 checkbox 格式）
   - 必读文档表（从 IMPLEMENTATION_PLAN.md 或 Stage 1 收集的文档清单中提取，标注优先级和重点章节）
   - 验收标准
   - `**Implementation Ready:** yes`，仅当上述契约全部满足时允许创建

5. 回填 Epic 的子任务列表和依赖关系图，按模块分组展示，避免子 issue 数量增加后失去总览。

6. 输出创建结果汇总（issue 编号、标题、模块范围、依赖关系）。

---

## Stage 5.5: Issue-Change 对齐审核

**目标**：验证创建的 GitHub Issue 与 OpenSpec change 完全对齐——覆盖完整、边界正确、依赖一致、内容不漂移。

> **执行方式**：由 `issue-alignment.workflow.js` 的 `while` 循环硬编码执行，最多 2 轮。调用方式：`Workflow({ scriptPath: "<path>/issue-alignment.workflow.js", args: { changeName: "<name>", epicNumber: <N> } })`

**审核维度**：

| 维度 | 检查内容 |
|---|---|
| missing-coverage | tasks.md 中的 task 未被任何 issue 覆盖 |
| wrong-boundary | 单个 issue 混合了多个模块或 ownership 范围 |
| wrong-dependency | issue 间依赖链与 task 依赖顺序不一致 |
| scope-mismatch | issue 的 In Scope / Out of Scope 与 task 实际内容不符 |
| missing-reference | issue 缺少 change 中的 spec 或设计文档引用 |
| content-drift | issue 内容（任务清单、验收标准、PR 边界）与 change artifact 矛盾或偏离 |

**流程**：

1. 审核 agent 读取所有 change artifact 和已创建的 issue，逐条比对，输出结构化对齐缺口（P0/P1）。
2. 修复 agent 通过 `gh issue edit` / `gh issue create` 修复缺口。
3. 独立验证 agent 确认修复。
4. P0 + P1 均阻塞，未清则继续，最多 2 轮。残留如实记录到 Epic。

---

## 触发锚定（Trigger Anchoring）

> 对应 meta-loop rubric dim 8（触发可靠性）。workflow 脚本解决了"回环内部不可跳过"，本节解决"workflow 本身会不会忘记调用"。

**推荐做法：单次调用 `full-pipeline.workflow.js`**

Stage 1 + 2 由编排器交互式完成后，Stage 3→5.5 应通过 **一次** `Workflow()` 调用链式执行，不要分步手动调用 `review-loop` 和 `issue-alignment`——每多一次手动调用就多一个忘记的触发面。

```
Workflow({
  scriptPath: "<skill-dir>/full-pipeline.workflow.js",
  args: {
    changeName: "<name>",
    designDocs: ["path/to/doc.md"],
    skillDir: "<skill-dir>"
  }
})
```

**消费仓库锚定指引**：

- 如果仓库有 CI 或 pre-PR hook 可挂载：在 PR 创建前检查 `docs/stage-pipeline-log.jsonl` 最新条目是否覆盖本 change（change name + 日期），缺失则阻塞 PR。这把触发从"编排器记得调"变成"不调就过不了 CI"。
- 如果没有可挂载的硬动作：依赖 SKILL.md 指令 + 上述单次调用模式；定期审计 skip-rate（检查哪些 change 有 openspec 产物但 `stage-pipeline-log.jsonl` 无对应条目）。
- **诚实声明**：本 skill 无法替消费仓库安装 hook。per-project skip-rate 是开放实测项，需在使用中审计，不可假设"写了就一定会跑"。

---

## 跨运行问责（Loop Accountability）

> 对应 meta-loop rubric dim 6（跨运行记忆）。Stage 4.5 的回环只解决"单次运行内"的记忆；本节解决"这道验证门长期是否还值得跑"。**仅做问责，不做跨 change 学习**——不把某个 change 的 finding 带进另一个 change 的审核 brief，避免跨 change 偏见。

**1. catch-rate 日志（提交进仓库）**

每次流水线跑完 Stage 4.5（无论 clean 还是 residual），向消费仓库里一个**已提交、append-only** 的日志追加一行。默认 `docs/stage-pipeline-log.jsonl`（或该项目存放运维记录的既有位置）。一行 schema：

```json
{"change":"<name>","date":"<run-date>","rounds":<n>,"gate_net_catch":<n>,"p0":{"in":<n>,"resolved":<n>,"residual":<n>},"p1":{"resolved":<n>,"carried":<n>},"regressions":<n>,"approx_subagent_calls":<n>,"verdict":"clean|residual"}
```

- `gate_net_catch`：**本节核心指标**——验证门独有的价值。统计"修复者声称已解决、但独立验证者判为 `unresolved`/`regressed` 的 finding 数"，即若没有 Stage 4.5 这道门就会漏过去的问题数。Stage 3 审核和 `openspec status` 已经抓到的不计入。
- 其余字段供横向看趋势（轮次、残留、回归、成本）。

**2. kill 标准**

验证门要持续证明自己值得那份成本。判定（最小样本 = 5 次运行，不足不下结论）：

- 若连续 **≥5 次** 运行 `gate_net_catch` 都 ≈ 0（验证者从不推翻修复者、也不抓回归），说明它在橡皮图章 → **收窄**（改成只在修复触及实体名、表/ENUM 计数等全局扩散项时才跑）或**退役**。
- 收窄/退役的决定连同日志证据记入 change 或 `docs/adr/`，不悄悄关掉。

**3. ratchet（把复发问题棘轮成永久校验）**

当同一 finding 类（如"表/ENUM 计数不一致"、"OpenAPI schema 缺字段"）跨 **≥2 次** 运行复发，就把它从"每次靠人审抓"提升为**永久自动校验**——加一条 openspec validation / lint / CI 检查。已解决的问题从此变成不变量，不再消耗回环预算，也顺带降低 dim 9 成本。

**非目标**：不做 Reflexion 式跨 change 学习（不把历史 change 的 finding 模式注入新 change 的审核）。catch-rate 日志是**组织级问责**，不是循环内记忆。

---

## 快速参考

**完整流水线用时**：约 30-60 分钟（取决于 capability 数量和 subagent 审核耗时）

**最小命令集**：
```bash
# Stage 2
openspec new change "<name>"
openspec status --change "<name>" --json
openspec instructions <artifact> --change "<name>" --json

# Stage 3：用编排器原生并行 subagent 同时 spawn 3 个 reviewer subagent
#   review-design-consistency / review-spec-completeness / review-tasks-executability

# Stage 4.5：spawn 独立 verifier 核销 P0/P1，未清且 < 3 轮则回 Stage 4
#   verify-review-fixes

# Stage 5
gh label create ...
gh issue create --title "..." --label "..." --body "..."
```

**依赖**：
- `full-pipeline.workflow.js` — **推荐入口**，Stage 3→5.5 单次调用链式编排（Claude Code Workflow），调用：`Workflow({ scriptPath: "<dir>/full-pipeline.workflow.js", args: { changeName: "<name>", designDocs: [...], skillDir: "<dir>" } })`
- `review-loop.workflow.js` — Stage 3→4→4.5 回环（由 full-pipeline 内部 `workflow()` 调用，或单独使用）
- `issue-alignment.workflow.js` — Stage 5.5 对齐审核（由 full-pipeline 内部 `workflow()` 调用，或单独使用）
- `grill-me` skill — Stage 1→2 之间的设计压测（可选）
- `reviewer` subagent — Stage 3 三路并行审核执行（由 workflow 脚本 spawn）
- `verifier` subagent — Stage 4.5 独立验证门核销（由 workflow 脚本 spawn，不得复用修复者）
- `docs/stage-pipeline-log.jsonl`（消费仓库内，已提交）— 跨运行 catch-rate 问责与 kill 标准
- `gh-create-issue` skill — Stage 5 可选调用（或直接用 gh CLI）

**跳过策略**：
- change 已存在 → 跳过 Stage 2
- 不需要 subagent 审核 → 跳过 Stage 3-4.5
- 不需要 GitHub issue → 跳过 Stage 5-5.5
- 用户说"只做审核" → 只执行 Stage 3
- 用户说"只创建 issue" → 执行 Stage 5 + 5.5（对齐审核不可跳过）
