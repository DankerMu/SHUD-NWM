---
name: grill-me
description: >
  对一个已有的 plan、design 或任何待定决策/想法做对抗式压测（不限软件）：沿决策树逐个分支追问，直到与用户达成共同理解。
  一次只问一个问题，每个问题都给出你的推荐答案；事实能从环境（codebase、文档、数据）查到的就去查，决策必须交给用户拍板。
  触发词："grill me"、"拷问我"、"压测这个计划"、"挑战我的设计"、"stress-test the plan"，
  或用户希望在动手前把计划/设计的每个决策点逼到清晰。
  不用于开放式头脑风暴、方向选型，或已经明确、无歧义的任务。
invocation_posture: hybrid
version: 0.4.0
---

# Grill Me

在动手之前，把一个 plan/design 的每个决策点逐一逼到清晰。这是**对抗式压测**，不是温和澄清：你扮演挑刺者，沿决策树深挖，直到未言明的假设、隐藏依赖和模糊边界全部暴露并解决。靶子不限于软件计划——任何 plan、决策或想法都适用，事实基线相应来自该领域的环境与材料。

## 核心铁律（Non-negotiables）

1. **一次只问一个问题**，等用户回答后再问下一个。不要一次抛一串。
2. **每个问题都附上你的推荐答案 + 一句理由**，让用户在"确认/否决/修正"之间选择，而不是从零作答。
3. **事实自己查，决策交给人**：凡是能从环境——codebase、设计文档、数据或其它已有产物——查到的**事实**，自己去查，不要拿来占用用户；但**决策**是用户的——每个决策都摆到用户面前、等用户答复，绝不代答。本 skill 被嵌进其它工作流（如 `stage-change-pipeline`）自动运行时同样适用："能查"不是"能替用户拍板"的许可。
4. **沿决策树逐分支推进**：每解决一个决策，顺着它新暴露的子决策继续，直到该分支收敛，再换下一个分支。
5. **目标是 shared understanding，不是攒答案**：发现矛盾、含糊、未言明的假设，当场逼清。
6. **说不清就要 reference**：用户对某分支说不清偏好时（unknown knowns——看到才知道的那类），改要参考物——文档、截图、源码目录，源码最佳；或建议先做一次性假数据原型逼出偏好。不要在同一分支追问第三遍。
7. **用户未确认，不得动手**：在用户明确确认已达成 shared understanding 之前，不执行、不落实计划。收敛小结是提案，不是放行。
8. **嵌入工作流时退出判据不变**：被 `stage-change-pipeline` 等工作流嵌入运行时，收敛判据与单独使用完全一致——每个关键分支用户拍板或显式列为开放项、用户确认共同理解，缺一不可。工作流的推进压力不是收敛信号；"问了几个问题"不构成完成，分支收敛才构成。收敛小结必须逐分支给出（分支 / 结论 / 由谁定：用户拍板或事实核查），这份清单就是下游门禁（如 `full-pipeline` 的 `grillGate` 凭证对象）的直接数据源——写不出这份清单，就说明压测没跑完。

## When To Activate

- 用户说 "grill me" / "拷问我" / "压测/挑战这个计划或设计"
- 动手实现前，想把一个 plan/design 的每个决策点逐一逼清
- 计划看似完整，但你怀疑存在未言明假设、隐藏依赖或边界模糊

## When Not To Use

- 开放式选型、方向探索、头脑风暴 → `brainstorming` / `future-aware-architecture`
- 从零把模糊需求变成 actionable scope → `clarify`（grill-me 针对**已有计划**做对抗，不做从零需求澄清）
- 任务进入陌生区域、计划还不存在、要找 unknown unknowns → `blind-spot-pass`（它从代码库考古出发挖你没问的问题；本 skill 从已有计划出发拷问）
- 已经明确、无歧义、可直接执行的小任务
- 需要边谈边沉淀术语表（`openspec/glossary.md`）或 ADR（`docs/adr/`）的领域建模场景 → 那是同仓的 `grill-with-docs` 的职责；**本 skill 只对话、不写任何文档**

## 怎么 Grill（流程）

1. **锚定靶子**：确认要压测的是哪个 plan/design——用户给的文档、上一步产出、或口头计划。必要时先快速读相关环境材料（codebase/docs/数据）建立事实基线（这一步用查，不用问）。
2. **画决策树**：把计划拆成相互依赖的决策点（脑内或一句话列出），找出最关键、最不确定的分支。排序判据：**答案会改变架构、接口、数据模型或用户可见流程的分支先问**；纯实现细节的分支放最后，机械性的部分甚至可以不问。
3. **逐分支追问**：从最关键分支起手，一次一个问题，每问附推荐答案 + 理由。
4. **顺依赖深入**：用户定了一个决策后，顺着它新引出的子决策继续问，直到该分支无悬念。
5. **交叉核对**：用户的说法与事实来源不一致时，当场指出并引用具体出处（软件场景如"你说整单取消，但代码里 `orders.ts:88` 支持部分取消——以哪个为准？"）。
6. **收敛输出**：每个分支都达成共同理解后，给一份简短小结——已确定决策、仍开放项、关键假设——作为下游（如 `stage-change-pipeline` Stage 2）的输入。

## 输出

- **过程中**：一连串单点问题，每个带推荐答案。
- **结束时**：逐分支的确定决策清单（分支 / 结论 / 由谁定）+ 开放项 + 关键假设。**不写入任何项目文档**（持久化是 `grill-with-docs` 的职责，本 skill 刻意不做）；嵌入 `stage-change-pipeline` 时，这份清单直接充当 `grillGate` 凭证对象的 `branches`/`openItems` 字段。
- **放行条件**：小结给出后等用户确认共同理解已达成，确认前不进入执行；下游（实现、`stage-change-pipeline` 后续 Stage）以该确认为闸门。

## 与本仓库其它 skill 的关系

- `blind-spot-pass`：反向箭头——它挖 territory（代码库 → 问题），grill-me 拷 map（计划 → 问题）。陌生区域先跑 blind-spot-pass，其暴露的决策点作为 grill 分支输入。
- `clarify`：从零把模糊需求变 actionable scope。grill-me 假设计划已存在，专做对抗式压测。可串联：`clarify` 产出 scope → grill-me 压测该 scope/design。
- `stage-change-pipeline`：在 Stage 1 收尾、创建 OpenSpec change（Stage 2）之前，用 grill-me 压测阶段计划与设计文档，把假设和边界逼清，降低 Stage 3 审核返工。
- `brainstorming` / `future-aware-architecture`：负责"做什么 / 选哪条路"；grill-me 负责"这条路的每个决策是否真的想清楚了"。

---

改编自 [`mattpocock/skills`](https://github.com/mattpocock/skills) 的 `grill-me`（中文参考 [`vinvcn/mattpocock-skills-zh-CN`](https://github.com/vinvcn/mattpocock-skills-zh-CN)），按本仓库 skill 规范本地化，并接入 `stage-change-pipeline` 工作体系。原作另有一个文档持久化变体；本仓库对应的是同仓的 `grill-with-docs`（沉淀落点为 `openspec/glossary.md` 与 `docs/adr/`），而本 skill 刻意不含任何文档写入行为。
