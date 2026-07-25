---
name: stage-change-pipeline
description: >
  设计文档 → OpenSpec change → subagent 并行审核 → 修复 → 实现就绪 GitHub issue 全流水线，
  tasks 拆为模块边界清晰、适合小 PR 审核的细粒度 issue。用于把设计/阶段文档变成审核过的
  OpenSpec change 加 implementation-ready issues——"开始下一个阶段"、"设计到issue"、
  "run the stage pipeline"。单个 issue 的实现/修复/合并走 subagent-workflow，不需要
  OpenSpec change 与并行审核的普通 issue 不用本流水线。
license: MIT
metadata:
  author: danker
  version: "0.17.0"
---

# Stage Change Pipeline

将开发阶段的设计文档转化为经过审核的、可追踪的 GitHub Issue。流水线分 5 个阶段，每个阶段有明确的输入输出契约，可从任意阶段切入。

**依赖**：`openspec` CLI（npm）、具备并行 subagent 能力的编排器（Claude Code Task subagents 或 Codex subagents）、已认证的 `gh` CLI、包含设计文档的 git repo。可选读取 `IMPLEMENTATION_PLAN.md` 作为阶段上下文。

**支撑 skill**（挂点细节见各 Stage 节）：`clarify`（Stage 1 前澄清）、`blind-spot-pass`（陌生域侦察）、`grill-me`（Stage 1→2 压测门禁）、`grill-with-docs` / `future-aware-architecture` / `implementation-planning`（Stage 2 设计与任务规划）、`risk-adaptive-cross-review`（Stage 3 审查语义）、`project-documentation`（docs 漂移）、`gh-create-issue`（Stage 5 批量创建）。按需复用，不把它们的完整流程复制进来。

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
```

**Stage 执行细则在 [references/stage-flow.md](references/stage-flow.md)——执行任何 Stage 前必须读对应节。** 本文件只保留下面的不可协商项索引与启动入口。

---

## 不可协商项（索引）

完整语义在 `stage-flow.md` 对应节；本索引只保证触发时可见，不替代阅读。

- **Stage 1 压测门禁（EITHER/OR，必须留痕）**：进 Stage 2 前要么用 `grill-me` 逐分支压测并形成凭证，要么 `skipped:<理由>` 留痕跳过。凭证必须在**主会话、启动脚本之前**备好（Workflow 子代理无法与用户交互，脚本内无法补跑）；`full-pipeline.workflow.js` 对缺失/格式不符/裸 `"passed"` 直接拒绝启动。
- **Stage 2**：grill 已拍板分支必须落入 artifact（Stage 3 会拿凭证逐条核对，漂移即 finding）；design.md 必须含 **Sketch seams under test** 清单；收尾 `openspec status` 4/4 complete。
- **Stage 3**：三路并行只读 `reviewer`（设计一致性 / Spec 完整性 / Tasks 可执行性），finding 取 `finding-contract.md` 失败类词表，含糊无锚点条目直接拒收。
- **Stage 4 / 4.5**：P0 + P1 均为阻塞带；每轮修复必过独立验证门（验证者不参与修复、每条 finding 默认未解决、oracle 不可篡改、ack 两行凭据缺失即视为门被跳过）；回环 ≤3 轮由 workflow 脚本硬编码，触顶残留如实标 `needs-followup`。
- **Stage 5 实现就绪契约**：每个子 issue 满足全字段才允许 `Implementation Ready: yes`——单一模块范围、In/Out of Scope、任务清单、验收标准、必读文档与 change 引用、逐行 `Depends on #<dep>`、预期 PR 边界、`Suggested fixture level`、`Minimal mergeable slice`；不得把需求澄清留到实现阶段。
- **Stage 5.5**：issue-change 对齐审核 ≤2 轮，P0 + P1 阻塞，残留如实记录到 Epic。
- **收尾问责**：每次运行把 workflow 返回的 `logEntry` 补 `date` 后 append 到 `docs/stage-pipeline-log.jsonl` 并提交（四步流程见 [references/loop-accountability.md](references/loop-accountability.md)，缺一即漏记）。
- **终局回流（sizing-retro）**：下游 `subagent-workflow` 任何 PR 以 round-ceiling 拆分/放弃/降档收场，都是本流水线切片或契约失败的证据——拆分子项必须重过 Stage 5 契约（不得以裸 PR/裸 fixture 出生）+ 一轮轻量 Stage 5.5（≤1 轮）；每个终局事件落一行 sizing-retro（`slice-error|contract-gap|genuinely-hard`，schema 见 loop-accountability.md）。仅做问责，不做跨 change 学习——sizing-retro 不注入后续 change 的审核 brief。

---

## 启动方式

Stage 1 + 2 由编排器按 `stage-flow.md` 交互式完成后，Stage 3→5.5 通过**一次** `Workflow()` 调用链式执行（回环由脚本硬编码），不要分步手动调用——每多一次手动调用就多一个忘记的触发面：

```
Workflow({
  scriptPath: "<skill-dir>/full-pipeline.workflow.js",
  args: {
    changeName: "<name>",
    designDocs: ["path/to/doc.md"],
    stageLabel: "<optional-stage-label>",
    grillGate: {          // 或 "skipped:<理由>"；缺失/格式不符/裸 "passed" 均拒绝启动
      status: "passed",
      branches: [{ branch: "<决策分支>", decision: "<结论>", decidedBy: "user" }],
      openItems: [],
      userConfirmed: true
    }
  }
})
```

消费仓库有 CI / pre-PR hook 可挂载时，把 `stage-pipeline-log.jsonl` 覆盖检查锚上去（指引见 stage-flow.md 末节）。

---

## 快速参考

> 用时估算与最小命令集见 [references/quick-reference.md](references/quick-reference.md)。

**脚本与协作方**：
- `full-pipeline.workflow.js` — **推荐入口**，Stage 3→5.5 全逻辑 inline（调用方式见上）
- `review-loop.workflow.js` — Stage 3→4→4.5 回环（独立使用）
- `issue-alignment.workflow.js` — Stage 5.5 对齐审核（独立使用，`args: { changeName, epicNumber }`）
- `reviewer` / `verifier` subagent — 由 workflow 脚本 spawn；verifier 不得复用修复者
- `docs/stage-pipeline-log.jsonl`（消费仓库内，已提交）— 跨运行 catch-rate 问责与 kill 标准

**跳过策略**：
- change 已存在 → 跳过 Stage 2
- 不需要 subagent 审核 → 跳过 Stage 3-4.5
- 不需要 GitHub issue → 跳过 Stage 5-5.5
- 用户说"只做审核" → 只执行 Stage 3
- 用户说"只创建 issue" → 执行 Stage 5 + 5.5（对齐审核不可跳过）
