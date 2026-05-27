---
name: stage-change-pipeline
description: >
  设计文档 → openspec change → codex 并行审核 → 修复 → GitHub issue 全流水线。
  将 tasks 拆为细粒度、模块边界清晰、适合小 PR 审核的 GitHub issue。
  触发词："开始下一个阶段"、"stage change pipeline"、"设计到issue"、"阶段实施"、
  "openspec审核"、"创建 M* change"，或用户指定一个开发阶段要求生成审核过的 issue。
license: MIT
metadata:
  author: danker
  version: "1.0"
---

# Stage Change Pipeline

将开发阶段的设计文档转化为经过审核的、可追踪的 GitHub Issue。

整个流水线分 5 个阶段，每个阶段有明确的输入输出契约。可以从任意阶段切入——如果 openspec change 已存在，直接跳到审核；如果审核已完成，直接跳到创建 issue。

**依赖**：需要 `openspec` CLI（npm）、已配置 codex backend 的 `codeagent-wrapper`、已认证的 `gh` CLI、包含设计文档的 git repo。可选读取 `IMPLEMENTATION_PLAN.md` 作为阶段上下文。

**支撑 skill**：按需要复用本仓库已有 skill，不把它们的完整流程复制进来。

- `clarify`：阶段目标、验收标准、范围边界或设计文档优先级不清时，在 Stage 1 前先澄清。
- `future-aware-architecture`：Stage 2 的 `design.md` 涉及架构方向、技术选型、可逆性或长期演进风险时，用它形成决策输入。
- `implementation-planning`：Stage 2 的 `tasks.md` 或 Stage 5 issue 分组需要复杂依赖、回滚、验证矩阵或分阶段交付时，用它补执行计划。
- `risk-adaptive-cross-review`：Stage 3 的三路审核按 OpenSpec Review 模式组织 finding contract 和失败类汇总。
- `project-documentation`：设计文档、实施计划或 docs 导航陈旧，影响 Stage 1 上下文可信度时，用它做 docs drift 检查或刷新。
- `gh-create-issue`：Stage 5 需要批量创建 Epic/子 issue 时可调用；否则直接使用 `gh` CLI。

## When Not to Use

- 不用于单个 GitHub issue 的实现、修复、PR review、CI 或合并；这些属于 `codex-codeagent-workflow`。
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
Stage 3: Codex 并行审核 (3 路)
    ↓
Stage 4: 审核修复
    ↓
Stage 5: GitHub Issue 创建
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

## Stage 3: Codex 并行审核

**目标**：从 3 个独立视角审核 change 质量，发现错项和漏项。

**关键约束**：通过 `codeagent` skill 以 `--parallel --full-output` 模式同时发起 3 个审核任务，不能串行。`--full-output` 确保返回完整审核文本而非摘要。

**审核契约**：使用 `risk-adaptive-cross-review` 的 OpenSpec Review 模式作为审查语义参考：三路审核分别对应 Design Consistency、Spec Completeness、Tasks Executability；P0/P1 问题必须包含失败类型、证据、影响、修复方向和需要回归检查的相邻 artifact。

**步骤**：

1. 构建 3 个审核 prompt，每个约 200-400 字，包含：
   - 明确的审核范围和检查项
   - `@file` 引用指向 change 文件和设计文档
   - 期望的输出格式

2. 三路审核的标准分工：

   | 审核 | 视角 | 核心检查项 |
   |---|---|---|
   | Review 1: 设计一致性 | change 文件 vs 设计文档 | 表名/字段/ENUM 拼写一致性、API 端点覆盖完整性、ID 规范合规、manifest 字段对齐 |
   | Review 2: Spec 完整性 | 各 spec 之间 + 对照实施计划 | Requirement-Scenario 完备性、WHEN/THEN 可测试性、边界条件覆盖、跨 spec 一致性、功能点遗漏 |
   | Review 3: Tasks 可执行性 | tasks.md vs design + specs | 任务粒度、依赖顺序、spec 覆盖率、多余任务、验证方法明确性、技术决策落地 |

3. 通过 `codeagent` skill 执行（skill 参数格式：`backend=codex mode=parallel prompts=[...]`）。底层命令：
   ```bash
   codeagent-wrapper --parallel --full-output --backend codex <<'EOF'
   ---TASK---
   id: review-design-consistency
   backend: codex
   workdir: <project-root>
   ---CONTENT---
   <Review 1 prompt with @file references>
   ---TASK---
   id: review-spec-completeness
   backend: codex
   workdir: <project-root>
   ---CONTENT---
   <Review 2 prompt with @file references>
   ---TASK---
   id: review-tasks-executability
   backend: codex
   workdir: <project-root>
   ---CONTENT---
   <Review 3 prompt with @file references>
   EOF
   ```

4. `--full-output` 模式会返回每个 task 的完整消息文本（而非仅摘要），直接从输出中提取审核意见。

5. 汇总三路审核的交叉验证结果，按 P0（必须修复）/ P1（建议改进）分类。

---

## Stage 4: 审核修复

**目标**：根据审核意见修改 change 文件，解决全部 P0 问题。

**步骤**：

1. 从三路审核中提取去重后的 P0 问题清单，识别共性问题（如命名不一致、字段遗漏、计数矛盾）。

2. 按文件分组修改。典型的 P0 问题类型：
   - **命名不一致**：目录名、文件名、表名在 spec/tasks/design 之间不统一
   - **字段遗漏**：OpenAPI schema 缺少数据库表中的字段
   - **计数矛盾**：表数量、ENUM 值数量在不同文件中不一致
   - **结构错误**：如 JSON Schema 的 required 应该是嵌套结构而非扁平
   - **覆盖不全**：tasks 缺少 spec requirement 对应的实现任务

3. 修改文件多时（>3），用并行 agent 分批修复，确保互不交叉。

4. 修复后验证 `openspec status --change "<name>"` 仍然 4/4 complete。

5. P1 问题如果修改成本低（<5 分钟），一并处理；否则记录到 issue 中作为后续优化。

---

## Stage 5: GitHub Issue 创建

**目标**：将 change 的 tasks 细粒度拆分为 GitHub Issue（Epic + 子任务），使每个子 issue 预期对应一个小而可审阅的实现 PR。

**前置**：`gh auth status` 确认已认证。

**规划门禁**：如果 tasks 无法自然映射到小 PR issue，或存在跨模块依赖链，先用 `implementation-planning` 明确分阶段交付、依赖顺序、验证和回滚；不要为了减少 issue 数量合并模块边界。

**实现就绪契约**：Stage 5 创建的每个子 issue 都必须可被 `codex-codeagent-workflow` 自动执行，不得把需求澄清留到实现阶段。每个子 issue 必须具备：

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

## 快速参考

**完整流水线用时**：约 30-60 分钟（取决于 capability 数量和 codex 审核耗时）

**最小命令集**：
```bash
# Stage 2
openspec new change "<name>"
openspec status --change "<name>" --json
openspec instructions <artifact> --change "<name>" --json

# Stage 3（通过 codeagent skill 调用）
codeagent-wrapper --parallel --full-output --backend codex <<'EOF'
...
EOF

# Stage 5
gh label create ...
gh issue create --title "..." --label "..." --body "..."
```

**依赖的其他 skill**：
- `codeagent` — Stage 3 审核执行
- `gh-create-issue` — Stage 5 可选调用（或直接用 gh CLI）

**跳过策略**：
- change 已存在 → 跳过 Stage 2
- 不需要 codex 审核 → 跳过 Stage 3-4
- 不需要 GitHub issue → 跳过 Stage 5
- 用户说"只做审核" → 只执行 Stage 3
- 用户说"只创建 issue" → 只执行 Stage 5
