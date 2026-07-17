---
name: grill-with-docs
description: >
  grill-me 的领域增强变体：在对抗式压测计划/设计的同时，对齐项目术语、用具体场景探边界、
  与代码交叉核对，并把术语 inline 沉淀到 openspec/glossary.md、把够格的长期决策落到 docs/adr/。
  触发词："grill with docs"、"对着领域模型压测"、"对齐术语"、"stress-test against the domain"，
  或用户希望在压测计划的同时统一项目语言、沉淀长期决策。
  不用于不需要术语/决策沉淀的轻量压测（用 grill-me），也不用于纯需求澄清（用 clarify）。
invocation_posture: hybrid
version: 0.2.2
---

# Grill With Docs

`grill-me` 的领域增强变体：在压测一个 plan/design 的同时，**统一项目语言、沉淀长期资产**。当领域概念多、边界模糊、术语易漂、或决策需要跨变更长期追溯时用本 skill；只想快速逼清一次性计划、无沉淀需求时用 `grill-me`。

## 核心铁律（Non-negotiables）

继承 `grill-me` 的六条——

1. 一次只问一个问题。
2. 每个问题附推荐答案 + 一句理由。
3. 能从环境（codebase/文档/数据）查到的**事实**就去查；**决策**必须摆到用户面前等答复，绝不代答——即使本 skill 嵌在 `stage-change-pipeline` Stage 2 内自动运行。
4. 沿决策树逐分支推进。
5. 目标是 shared understanding，不是攒答案。
6. 用户未确认共同理解已达成之前，不得动手落实计划。

——再加三条沉淀纪律：

7. **术语对齐**：对话中浮现的概念，收敛成项目唯一 canonical term。
8. **inline 沉淀**：术语一旦解决就立即写入 `openspec/glossary.md`，不要攒到最后。
9. **ADR 稀疏**：只有"难回退 + 无背景会困惑 + 真实权衡"三条全真，才提议落 ADR。

## 压测时多做的四件事（领域增强）

1. **对照 glossary 挑刺**：用户用词与 `openspec/glossary.md` 已有定义冲突时，立即指出（"glossary 里 'cancellation' 指 X，你这里像是 Y——到底哪个？"）。
2. **收敛模糊语言**：overloaded/含糊术语当场逼成精确 canonical term（"你说的 'account' 是 Customer 还是 User？这是两个东西"）。
3. **具体场景探边界**：发明能触发 edge case 的具体场景，迫使用户说清概念之间的边界。
4. **代码交叉核对**：用户的说法与代码不一致时，引用具体文件/行指出（"代码 `orders.ts:88` 支持部分取消，但你说只支持整单取消——以哪个为准？"）。

## 沉淀落点（本仓库约定，与上游不同）

上游用 `CONTEXT.md` + `docs/adr/`。本仓库改写为：

### 术语表 → `openspec/glossary.md`

项目级 ubiquitous language 的**单一来源**。格式见 [GLOSSARY-FORMAT.md](./GLOSSARY-FORMAT.md)。

- **只放本项目特有术语**，不放通用编程概念（timeout、error type 等）。
- **只做术语定义**，不混实现细节，不当 spec / scratchpad / 决策仓库——那些是 OpenSpec `specs/`、`design.md` 的职责。
- 单 context：`openspec/glossary.md` 一个 `## Language` 段。多 context：同一文件内用 `## Context Map` + 每个 context 一个二级标题，**不分散成多文件**，贴合 OpenSpec 单目录与本仓库的熵治理。
- 与 `openspec/project-profile.md` 的关系：profile 保持 lean，**只引用** glossary，不把术语堆进 profile。
- **懒创建**：第一个术语被解决时才创建 `openspec/glossary.md`。

### 长期决策 → `docs/adr/NNNN-slug.md`

跨变更累积的决策账本。格式与三门槛见 [ADR-FORMAT.md](./ADR-FORMAT.md)。

- **与 OpenSpec `design.md` 的分工**：`design.md` 记**某次 change 的**技术决策，随 change 归档；ADR 记**项目级、跨 change 长期有效**的决策。若某决策已在 `design.md` 充分说明且只作用于该 change，不必再开 ADR。
- 三门槛全真才提议；**懒创建** `docs/adr/`，编号连续递增。

## When To Activate

- 压测计划/设计的同时，需要统一术语或沉淀长期决策
- 领域概念多、边界模糊、术语在 design/specs 间漂移
- 用户说 "grill with docs" / "对着领域模型压测" / "对齐术语"

## When Not To Use

- 轻量计划压测、无术语/决策沉淀需求 → `grill-me`
- 从零把模糊需求变 actionable scope → `clarify`
- 纯方向选型、头脑风暴 → `brainstorming` / `future-aware-architecture`

## 流程

1. **锚定靶子**：确认要压测的 plan/design。先读相关 codebase、`openspec/glossary.md`、`docs/adr/` 建立事实与术语基线（用查，不用问）。
2. **画决策树**：拆出相互依赖的决策点与涉及的领域概念。
3. **逐分支追问**：一次一个问题，每问附推荐答案 + 理由。
4. **顺依赖深入**：用户定一个决策后，顺着新引出的子决策继续。
5. **领域增强 + inline 沉淀**：在追问中执行上面"四件事"；术语一解决就写进 `openspec/glossary.md`，够三门槛的决策当场落 `docs/adr/`。
6. **收敛输出**：每个分支达成共同理解后，给小结——已确定决策、开放项、关键假设，外加**本轮新增/修订的 glossary 术语与 ADR 列表**。本 skill 运行于 `stage-change-pipeline` Stage 2 内部，故该小结是 Stage 2 `design.md`/`specs/` 定稿与 Stage 3 审核的输入；用户确认共同理解已达成之前，不进入 `design.md`/`specs/` 定稿。

## 与本仓库其它 skill 的关系

- `grill-me`：轻量变体，只逼清决策、不沉淀。领域复杂或需长期追溯时升级到本 skill。
- `stage-change-pipeline`：Stage 2 写 `design.md`/`specs/` 时用本 skill 对齐术语、沉淀 glossary/ADR，降低 Stage 3 审核中的术语/边界类 finding。
- `clarify`：从零澄清需求；本 skill 假设已有计划，专做对抗式压测 + 领域沉淀。
- `risk-adaptive-cross-review`：负责对产出（change/代码）的对抗式审查；本 skill 把对抗前移到设计与术语阶段。

---

改编自 [`mattpocock/skills`](https://github.com/mattpocock/skills) 的 `grill-with-docs`（中文参考 [`vinvcn/mattpocock-skills-zh-CN`](https://github.com/vinvcn/mattpocock-skills-zh-CN)）。沉淀落点由上游的 `CONTEXT.md`/`docs/adr/` 本地化为本仓库的 `openspec/glossary.md`/`docs/adr/`，保留原作的 ADR 三门槛纪律。
