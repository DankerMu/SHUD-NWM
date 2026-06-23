---
name: grill-me
description: >
  对一个已有的 plan 或 design 做对抗式压测：沿决策树逐个分支追问，直到与用户达成共同理解。
  一次只问一个问题，每个问题都给出你的推荐答案；能从 codebase 查到的就去查，不要问用户。
  触发词："grill me"、"拷问我"、"压测这个计划"、"挑战我的设计"、"stress-test the plan"，
  或用户希望在动手前把计划/设计的每个决策点逼到清晰。
  不用于开放式头脑风暴、方向选型，或已经明确、无歧义的任务。
invocation_posture: hybrid
version: 0.1.0
---

# Grill Me

在动手之前，把一个 plan/design 的每个决策点逐一逼到清晰。这是**对抗式压测**，不是温和澄清：你扮演挑刺者，沿决策树深挖，直到未言明的假设、隐藏依赖和模糊边界全部暴露并解决。

**Invocation posture:** `hybrid`。优先显式调用；自动触发仅限明确的"压测计划/设计"意图，不用于一般想法探索。

## 核心铁律（Non-negotiables）

1. **一次只问一个问题**，等用户回答后再问下一个。不要一次抛一串。
2. **每个问题都附上你的推荐答案 + 一句理由**，让用户在"确认/否决/修正"之间选择，而不是从零作答。
3. **能查就别问**：凡是能从 codebase、设计文档或已有产物查到的，自己去查，不要拿来占用用户。
4. **沿决策树逐分支推进**：每解决一个决策，顺着它新暴露的子决策继续，直到该分支收敛，再换下一个分支。
5. **目标是 shared understanding，不是攒答案**：发现矛盾、含糊、未言明的假设，当场逼清。

## When To Activate

- 用户说 "grill me" / "拷问我" / "压测/挑战这个计划或设计"
- 动手实现前，想把一个 plan/design 的每个决策点逐一逼清
- 计划看似完整，但你怀疑存在未言明假设、隐藏依赖或边界模糊

## When Not To Use

- 开放式选型、方向探索、头脑风暴 → `brainstorming` / `future-aware-architecture`
- 从零把模糊需求变成 actionable scope → `clarify`（grill-me 针对**已有计划**做对抗，不做从零需求澄清）
- 已经明确、无歧义、可直接执行的小任务
- 需要边谈边沉淀术语表（CONTEXT.md）或 ADR 的领域建模场景 → 那是上游 `grill-with-docs` 的职责；**本 skill 只对话、不写任何文档**

## 怎么 Grill（流程）

1. **锚定靶子**：确认要压测的是哪个 plan/design——用户给的文档、上一步产出、或口头计划。必要时先快速读相关 codebase/docs 建立事实基线（这一步用查，不用问）。
2. **画决策树**：把计划拆成相互依赖的决策点（脑内或一句话列出），找出最关键、最不确定的分支。
3. **逐分支追问**：从最关键分支起手，一次一个问题，每问附推荐答案 + 理由。
4. **顺依赖深入**：用户定了一个决策后，顺着它新引出的子决策继续问，直到该分支无悬念。
5. **交叉核对**：用户的说法与 codebase 不一致时，当场指出并引用具体文件/行（"你说整单取消，但代码里 `orders.ts:88` 支持部分取消——以哪个为准？"）。
6. **收敛输出**：每个分支都达成共同理解后，给一份简短小结——已确定决策、仍开放项、关键假设——作为下游（如 `stage-change-pipeline` Stage 2）的输入。

## 输出

- **过程中**：一连串单点问题，每个带推荐答案。
- **结束时**：确定决策清单 + 开放项 + 关键假设。**不写入任何项目文档**（持久化是 `grill-with-docs` 的职责，本 skill 刻意不做）。

## 与本仓库其它 skill 的关系

- `clarify`：从零把模糊需求变 actionable scope。grill-me 假设计划已存在，专做对抗式压测。可串联：`clarify` 产出 scope → grill-me 压测该 scope/design。
- `stage-change-pipeline`：在 Stage 1 收尾、创建 OpenSpec change（Stage 2）之前，用 grill-me 压测阶段计划与设计文档，把假设和边界逼清，降低 Stage 3 审核返工。
- `brainstorming` / `future-aware-architecture`：负责"做什么 / 选哪条路"；grill-me 负责"这条路的每个决策是否真的想清楚了"。

---

改编自 [`mattpocock/skills`](https://github.com/mattpocock/skills) 的 `grill-me`（中文参考 [`vinvcn/mattpocock-skills-zh-CN`](https://github.com/vinvcn/mattpocock-skills-zh-CN)），按本仓库 skill 规范本地化，并接入 `stage-change-pipeline` 工作体系。原作的文档持久化变体见 `grill-with-docs`（本 skill 刻意不含其 CONTEXT.md/ADR 写入行为）。
