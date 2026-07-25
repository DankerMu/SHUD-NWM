# Changelog

All notable changes to this skill will be documented in this file.
This project adheres to [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.27.0] - 2026-07-17

### Added

- **Fixture artifact 集按 level 缩放**（动机：审查侧早已按 fixture level 缩放人数与风险包，fixture 本身的 artifact 集却始终全套——小 PR 与大改动同价，是 TDD 微循环同款的"过程重量与风险脱钩"病）。`none`/`compact`：`proposal.md` + `tasks.md` + **一个最小 spec delta**（单 requirement + 单 `#### Scenario:` 块），`design.md` 豁免（豁免理由一行写进 proposal，风险包勾选落 tasks.md）；`expanded` 全套；`high`/`broad-expanded` 全套 + Invariant Matrix（现状不变）。分级经 openspec CLI 1.3.1 实测背书：validator 硬性要求 ≥1 delta（首版"compact 豁免 delta"方案被实测否决——delta 恰是唯一被 `archive` 沉淀进主 spec 的 artifact，最不该省），`design.md` 实测可选。省掉的是四件里最重的自由散文，保留的是机器要校验、要归档的部分。落点：issue-risk-contract Fixture Level Rules + compact 模板 + fixture review 输入清单、phase-flow Phase 0 第 6 条。

## [0.26.1] - 2026-07-17

### Fixed

- **`openspec instructions` 调用形式与 CLI 1.3.x 不兼容**：phase-flow Phase 0 与 issue-risk-contract 中的 `openspec instructions --change <name>` 缺失必填的 `<artifact>` 位置参数，实际 CLI 报 "Missing required argument \<artifact\>"。改为 `openspec instructions <artifact> --change <change-name>`（artifact ∈ proposal | design | specs | tasks，按待撰写的 artifact 逐个执行），与 stage-change-pipeline 既有的正确用法对齐。同块的 `openspec new change`/`validate`/`show` 经 1.3.1 实测兼容，未动。

## [0.26.0] - 2026-07-17

### Changed

- **五轮硬顶（成本总闸，CLI 终局锁）**（动机：xagent 实跑单 issue 烧到 12 轮 / 约 $1000——复盘确认这是设计漏洞而非执行违规：三轮门的 post-gate 预算按 retro 计发，retro 链无总量上限，每次再进门"更强的动作"只是散文要求，12 轮字面合规。gate 约束了行为合法性，没有任何东西约束开销）。每 PR comprehensive 轮总数硬顶 5：第 5 轮 not-clean 即终局——`record-round` 拒绝第 6 轮（clean 的第 5 轮同样封顶，越顶尝试记违规行不记轮），任何 retro 不再发预算，仅 `breadth`（拆分计划）可注册且锁不解除；唯余三出口：拆 PR（子 PR 新计数器）、降 scope、用户决策后 `close`。ledger gate 字段新增 `round-ceiling`。经济学：不能在 5 轮内收敛的 issue 是在经济上失败，拆/降/停的决策必须在钱烧完之前到人面前。
- **修复后复审默认钉死核心 lens（单轮成本减半）**：free-slot 轮换从"每轮都转"改为"有信号才转"——仅当上一轮出现 critical/major 或 ledger `repeats prior class: yes` 时自由槽回归，默认只跑 fixture 风险包钉死的 2-3 个 lens（修复回归召回由钉死核心承担，这是 lens 轮换政策自己的论证）。scope 不变：轮仍覆盖全 diff。
- **delta 聚焦（复审轮结构化降本）**：修复后复审每轮恰好 1 个 reviewer 保持全 PR comprehensive scope，其余聚焦修复 delta、爆炸半径与回归面（保留全 diff 访问权）；禁止零全 scope reviewer 的轮。
- 复盘记录：本改动源于对"轮次有门、开销无闸"的反思——此前会话在召回与治理侧持续加码（evidence_check、depth 表单义务均为负/零成本，保留），但从未给单轮定价；同期驳回"降 reviewer 数"时混淆了 scope（必须全 diff）与人头数（可收缩），本条修正后者。三改动合计：最坏情况 ~5 轮 × 半价轮，单 issue 复审开销从无上限压至约 $250-300 量级。

## [0.25.0] - 2026-07-17

### Changed

- **`depth` 表单义务 + 拆分默认升级**（动机：实跑观察三轮门触发后执行模型系统性偏好自称 depth——继续修感觉像进展、拆 PR 感觉像破坏——即使轮次增大、面越来越广也不拆）。设计过程中否决了"无 ledger 同类复发即拒绝 depth"的硬门：failure-class 标签是编排器录轮时自写的输入，粗粒度词汇下同一 invariant 三轮可得三个标签，硬门既可被反向游戏（跨轮复用标签制造复发）、又能强制触发 gates.md 明文禁止的"拆分复发 invariant"。落地版本改为两个不可游戏的机械点：(1) **depth 表单义务**——retro 必须有 `Invariant:` 实名行 + `Recurring findings:` 下 ≥2 条跨轮/兄弟面 finding 映射，CLI 表单级拒绝；ledger 无复发时照常注册但追加 `Depth-without-recurrence` 警告行进 ledger，差异本身成为 grill/Phase 8 证据。(2) **拆分默认升级**——gate 进门次数由 not-clean 轮推导、无法造假：同一 PR 第二次进门起 PR split 为默认矫正动作，depth/noise retro 无非空 `Split rebuttal:`（引 ledger/verdict 证据说明面不可独立拆分）即拒绝；breadth 本身就是拆分无需 rebuttal，converging 豁免（趋势数字即论证）。落点：gates.md obligations/escalation 段 + retro 模板新字段、review_gate.py `depth_form_problems`/`split_rebuttal_present`/警告行、单测 5 例新增。
- **Phase 2 验证消费 implementer 红证明**（配套 implementer 1.5.0 的 TDD 微循环→批量红证明改革）：read-only audit 新增两项——implementer 报告必须含新行为测试的批量红跑证据（旧代码上全红的输出），`git stash list` 不得残留 `red-proof` 条目；缺证明或滞留 stash 即 finding。cause-unknown 修复的先红后绿（Phase 5 诊断门）保持原样不动。

## [0.24.0] - 2026-07-17

### Added

- **证据卫生机械化：打包 `scripts/evidence_check.py`**（stdlib-only、确定性；动机：xagent 实跑 Round 7/8 连续两轮 reviewer 命中同类证据治理缺口——current-head SHA 归属过期、PR 正文轮次状态滞后、manifest 占位符——comprehensive 轮是最贵的 linter，不该用来 lint 编排器自己的文书）。三项检查：模板占位符/TODO/TBD 残留（仅扫 `--file` 指定的编排器自著文件；reviewer 报告可合法引用代码泛型与 TODO 讨论，设计上豁免；autolink/邮箱不误伤）、current/frozen head SHA 声明必须前缀匹配实际 HEAD（`Last clean reviewed SHA` 等历史锚点豁免）、`Round K pending` 类声明与 `.review-gate.json` 已记轮次冲突即过期（无状态文件时降级跳过并明示）。exit 2 即先修簿记再行动——不占轮次、不进 ledger。落点：phase-flow Phase 4 spawn 前置（初审与 6.5 复审共用）、Phase 8 发帖前检查清单首条。分层原则：秒级确定性检查放轮间与发帖前，CI 既有检查不动——这是左移新增，不是搬迁。
- 需求驱动单测 14 例（`tests/test_evidence_check.py`）：占位符命中与代码泛型/autolink 豁免、TODO 仅限自著文件、SHA 全长/前缀匹配与失配、历史锚点豁免、已记轮次 pending 过期与未来轮次放行、无状态文件降级、空输入/缺失目标/HEAD 不可解析的响亮失败。

## [0.23.0] - 2026-07-17

### Changed

- **结构重构：减熵减体量，行为契约零变化**（本会话熵审计第 1 项的偿还：规则面密度是最会咬人的熵，三处 gate 散文重复是矛盾温床——0.22.0 刚修过一个 Phase 8 ci-only × pre-merge SHA 条款的既有矛盾）。所有 gate 语义、阈值、模板、预算与 0.22.0 完全一致，仅改归属与表述：
  - **新增 `references/gates.md`**：ordinary-loop gate 体系唯一事实源——Round Ledger（格式/计数语义/CLI 机制/skip-block）、Gate Table（三轮/working-day/same-invariant：触发→动作→预算）、Review Failure Retro 模板、failure shape 默认动作与 post-gate 预算表、converging 取消资格清单。此前同一套规则散布在 phase-flow Phase 4/5/6.5 三处（约 90 行重复散文），现各留 1-2 行指针。
  - **新增 `references/skill-map.md`**：SKILL.md 的 Supporting Skills 十条 + Related skills 下沉于此（canonical 词汇关系保持约束力，其余为路由建议）。
  - **SKILL.md 从法律附录改回刀**：24.6KB → 11.9KB（-52%）。巨型 bullet（原 pre-merge gate ~300 词、governance ~400 词单条）压成每条 1-3 行；两条不可协商项保留原语义原强度，细节全部指针化。Execution Source 精简为带优先级的引用表（gates.md 列第 3 位）。
  - phase-flow 中两处 "6-review high-risk escalation in SKILL.md" 悬空指针改指 phase-flow Phase 4 本体（六评审升级的实际定义处）。
  - 全包 markdown 体量 125.0KB → 115.2KB（净 -8%，同时新增两个结构文件）。关键规范 token 归属核对：retro 模板/converging 判据/预算/ledger 机制仅存于 gates.md，pre-merge 条款细节仅存于 Phase 8，Seams under test 字段仅存于 issue-risk-contract。

## [0.22.0] - 2026-07-17

### Added

- **Phase 7 修复分级**（复用 Phase 8 `ci-only`/`semantic` 的分级精神）：终审 Gap Sweep 发现的 critical/major 修复先分类——`local-repair`（纯测试/证据补充，或单文件局部修复+覆盖测试，不触合同/共享 helper/Invariant Matrix/auth/path/publish/公共 API；`high`/`broad-expanded` fixture 下仅测试/证据类可入）只重跑 Phase 7 终审于新 head，不占 comprehensive 轮次、round counter 不动，每 PR 至多两次 local 循环，第三次 Phase 7 仍有 finding 即按 `semantic` 处理（gap sweep 反复出 finding 是信号不是噪音）；`semantic` 回 Phase 5-6 走全量 Phase 6.5 comprehensive 轮（计入三轮硬门）后重跑 Phase 7。

### Fixed

- **Pre-merge 硬门 SHA 条款与 ci-only 旁路的潜在矛盾**：原条款要求 clean comprehensive round 与 `Agent Review` 的 Reviewed head SHA 都等于冻结 FULL_SHA，但 Phase 8 `ci-only` 修复（明文"不为此类重跑 cross-review"）落地后 SHA 必然不匹配，字面上永远过不了门。改为：Reviewed head SHA 可为冻结 FULL_SHA **或**记录的 `Last clean reviewed SHA`——前提是其后每个 commit 都被记录为 Phase 8 `ci-only` 或 Phase 7 `local-repair`（存在未分类 commit 即 gate failure）；Phase 4.5 verdict 表按实际跑过的 comprehensive 轮持久化；Phase 7 终审必须完成于冻结 HEAD。SKILL.md Core Rule 同步。

### Added

- **三轮硬门机械化：打包 `scripts/review_gate.py` 状态机 CLI**（stdlib-only、确定性、随 skill 双平台分发）。编排器 Phase 4 开始 `open --pr`，每轮 `record-round`（自动检测跨轮同类复发、追加 ledger 行、锁定时 exit 2 当场反馈），retro 持久化后 `record-retro`（机械校验 `converging` 资格：同类复发/第 3 轮起 critical-major/逐轮趋势单调性/每 PR 一次/第 5 轮排除，任一条即拒绝，并按 shape 装填 post-gate 预算），working-day/same-invariant 触发用 `lock --reason`，合并后 `close`。锁定状态预计算进 `.review-gate.json` 供 hook 消费。非法转移（锁定期间记录的轮次）带违规标记写入 ledger 并保持锁定——拦截在动作瞬间，不是 pre-merge 末端。
- **配套 `review-gate` hook 挂接**（新 hook 包 0.1.0，可选，Claude Code）：锁定期间 `Task` 派发按 `subagent_type` 机械拒绝 implementer/reviewer；hook 零 gate 逻辑，只读 CLI 预计算字段。未安装时纪律回退为编排器自持（与 worktree-guard/monitor/issue-scribe 同一可选模式）。Codex 侧无稳定 spawn 工具名可 match，机械层由 CLI 当场拒绝承担。
- 需求驱动单测 15 例（`tests/test_review_gate.py`，经 `scripts/run_unit_tests.py` 进入仓库共享验证链）：三轮锁定、锁定期违规记录、pivot/converging 预算与重锁、converging 五类取消资格、手动触发、参数与文件校验、开闭生命周期。

## [0.20.0] - 2026-07-17

### Added

- **Round Ledger（强制逐轮簿记，即 round counter 本体）**：每个 comprehensive cross-review 轮（初审或修复后复审）结束时，必须先向证据束追加一行 ledger——轮次 N、SHA、clean/not-clean、已验证 finding 数、最高严重度、failure class 列表、是否与此前任一轮同类复发、gate 判定——才能选择下一动作（fix synthesis / 下一轮 / Phase 7 / CI / merge）。三轮硬 gate 读的是这行的 `N` 与 `gate` 字段；Phase 4.5 验证与失败无报告的调用不占行；未记 ledger 就行动记为 skip block。动机：实跑中执行模型（gpt-5.6）在第 3 轮不 clean 后未触发硬 gate，直接续跑逐条修复——把"记得检查条件"改为"必须执行的簿记步骤"，让 gate 判定成为机械动作而非散文条件。落点：phase-flow Phase 4 review rounds（定义）、Phase 5 gate 入口、Phase 6.5 循环条件。

### Changed

- **反豁免措辞封漏洞**（SKILL.md 不可协商项 + phase-flow Phase 4）：明确"逐轮 finding 数下降"不构成绕过三轮 gate 的豁免——`converging` 是在已持久化的 retro 内部选择的 shape，不是跳过 retro 的理由；第 3 轮出现任一 critical/major 或与任一轮同类复发即取消 `converging` 资格（重申 0.17.0 判据并前置到 gate 触发点）。
- **不可协商项扩展**：由"第 4 轮前必须持久化 retro 并执行纠正动作"收紧为"第 3 轮不 clean 后，任何普通动作（implementer fix / 复审 / Phase 7 / CI 等待 / merge）都不得先于 retro 持久化运行，且 retro 之后的下一动作必须是选定的纠正动作本身"；round counter 的实体绑定为证据束中的逐轮 ledger 行。

## [0.19.1] - 2026-07-16

### Changed

- 描述减重（常驻上下文税）：frontmatter description 从 655 字符压至 ~450——删去为弱触发模型准备的触发词枚举（保留 "implement #XX"/"处理下一个issue" 两个最强触发与全部反触发语义），语义边界不变。未跑 skill-creator 触发评测（无 ANTHROPIC_API_KEY），实际触发率变化需在使用中观察。

## [0.19.0] - 2026-07-16

### Added

- **轮间 lens 轮换（带测量的试运行）**：修复后复审轮的 reviewer 组合从"与 Phase 4 同配置重跑"改为**钉死核心 + 自由槽轮换**——fixture 风险包选中的 lens 每轮必在（承担修复回归召回），其余槽位轮换为本 PR 尚未用过的 reviewer 包（同 lens 轮次共享盲区，轮换以零边际成本买联合召回）。禁止轮换掉任何钉死的风险包 lens。落点：phase-flow Phase 4 review rounds / Phase 6.5、phase-4-cross-review follow-up 规则。
- **日志归因字段**：`review-loop-log.jsonl` 行新增 `round_lenses`（逐轮实际 lens 组合）与 `catches`（每条 net-catch finding 的轮次/产出 lens/失效类/严重度），把"后续轮的捕获来自钉死核心还是轮换进来的 lens"变成可查询事实。
- **轮换 keep/cut 裁决标准**（沿用既有 ADR 人工决策机制，样本 ≥8 个多轮 PR）：后续轮捕获集中于轮换 lens → 留任；几乎全部来自钉死核心对修复区新代码的回归召回 → 轮换无收益，记录 ADR 并回退为 round-1 同配置。试运行政策靠机制论证上线，靠净捕获归因数据留任。

## [0.18.0] - 2026-07-16

### Changed

- **Phase 4.5 verifier 从 per-finding 改为 per-failure-class 批量**：去重后的 candidate 先按 failure class 分组，每个 class 批次一个 `verifier` 子代理，批内至多 5 条（超出拆批；单条 class 退化为单条批，行为同旧版）。成本随 class 聚类因子下降；同 class 兄弟 finding 共享证据基，同一验证者一起裁决还能提高 verdict 一致性、捞出 dedup 漏掉的近重复。
- 逐条裁决语义不变且硬化：每条 candidate 一个独立 verdict（CONFIRMED/PLAUSIBLE/REFUTED）+ 逐条证据，"批级一口价"判定无效、整批重跑；批内发现同一缺陷的两条 candidate 须各自 verdict 并在 note 标注重复，不得静默合并。
- 独立性规则随批量调整：verifier 不得是产出**该批内任一** candidate 的 reviewer；编排器不得代裁不变。verdict 持久化从 `verify-<CANDIDATE_ID>.md` 改为按批 `verify-<CLASS_ID>.md`（逐条 verdict 表）。
- 同步三处：SKILL.md 核心规则、`phase-flow.md` Phase 4.5 步骤 3/4、`phase-4-cross-review.md` verifier 模板（新增 `<CLASS_ID>`/`<CANDIDATE_BLOCKS>` 变量与批量输出表）。

## [0.17.0] - 2026-07-16

### Added

- **Review Failure Retro 新增第四个 failure shape `converging`**，修补 0.16.0 三轮 gate 在健康收敛轨迹上的误触发：运行数据表明多轮 review 每轮都有净捕获，"第 3 轮不 clean"是常态而非病态信号，但 0.16.0 的三个 shape（breadth/depth/noise）没有一个描述健康收敛，默认动作（拆 PR/重构/降级）对它全是错误处方。
- `converging` 判据（须在 retro 中列出逐轮数字作证据）：各轮已验证 finding 无同类复发，数量与最高严重度逐轮不增、至少一项严格下降；第 3 轮出现任一 critical/major 或任何同类复发即丧失资格。
- `converging` 默认动作：**有界延长**——ordinary loop 至多再跑 2 轮 comprehensive cross-review，retro 只写一段收敛趋势、跳过策略章节；每 PR 至多选择一次。第 5 轮仍不 clean 则重进 gate，`converging` 不再可选，必须从 breadth/depth/noise 三选一。实质效果：3→5 轮之间的梯子由收敛证据驱动重建，gate 对病态轨迹仍是硬转向，对健康收敛只收一段话的税。
- Post-gate budget 拆分为两支：pivot 支（breadth/depth/noise，纠正动作后至多一轮、仍有 critical/major 则升级重进）与 converging 支（硬 2 轮上限、round 5 强制重进且排除 converging）。

## [0.16.0] - 2026-07-16

### Changed
- **审查升级 gate 从 5 轮提前到 3 轮，四文档包降为单份 retro**：删除五轮硬 gate 全套（Deep Review Failure Retro + Gate-Level PR Strategy Review + Invariant Surface Inventory + Regression Matrix 四文档包及 post-five budget）——触发太晚、仪式过重。新的唯一硬 gate：第 3 轮 comprehensive cross-review 仍不 clean（不再限定同一 failure class）即停 ordinary loop，持久化一份升级版 Review Failure Retro 后按归因执行纠正动作。
- **Review Failure Retro 升级**：模板新增 PR/SHA/轮次证据行与 `Failure shape` 归因（breadth 分散 / depth 同 invariant 反复 / noise 评审噪音），并绑定默认动作映射——breadth 默认**拆 PR**（子 PR 以新 PR 身份重进工作流、round counter 归零，父 PR 证据束记录拆分方案与 finding 归属）；depth 默认 refactor/redesign 或诊断任务（**禁止拆分反复失败的 invariant**：每个子 PR 都继承同一缺陷）；noise 默认 reviewer-pattern downgrade 并记录理由。偏离默认动作须在 retro 中记录原因。
- Post-gate budget 随 gate 前移：纠正动作后至多一轮 comprehensive cross-review，仍有 critical/major 则带更新后的 retro 重进 gate 并选更强动作，不得回退到逐行修补。
- SKILL.md 不可协商项同步：由"第 6 轮前必须持久化 Gate-Level 包"改为"第 4 轮前必须持久化 Review Failure Retro 并执行其纠正动作"；round counter 不重置规则补充唯一合法例外——gate 选定的 PR split 产生的子 PR。

## [0.15.0] - 2026-07-14

### Added
- Fixture 契约新增 **Seams under test** 字段（compact 与 expanded 模板各一处，`references/issue-risk-contract.md`）：测试将行使的公共边界，由上游（`stage-change-pipeline` Stage 2 design.md 或 issue 作者）**预先声明并附理由**——最少、最高、理想一个，无需人工确认，由 fixture review 检查。Core Rule "OpenSpec is the fixture" 同步列入：实现期只消费、不再谈判，需要但缺失的 seam 是一条须上报的 deviation。Adapted from `mattpocock/skills` v1.1.0 `tdd` 与 `to-spec`（先画缝再写 spec）；上游的用户确认环节有意改为上游自动声明 + 审核监督。

## [0.14.0] - 2026-07-14

### Added
- **诊断闸门（cause-unknown 专用）五个绑定点**，消费新落地的 `diagnosing-bugs` 0.1.0（canonical 诊断词汇；consume-don't-fork，对齐 `risk-adaptive-cross-review` 先例）：
  - Core Rule "Self-repair by delegation" 加限定：失败原因无法从输出确定时，先派**诊断任务**（报告契约 = 红命令+输出、最小复现、确诊假设+证据，不含修复），其报告才使 fix task 成为 "precise"。
  - Phase 2：原因不明显的验证失败先走诊断 brief，禁止凭猜写修复任务。
  - Phase 5：诊断闸门规则——因未知的 finding/失败需先有"已跑过一次、能红"的命令才进 fix list；有精确 file/line 且原因显然的普通 finding 不付诊断税；上一轮修复未关闭的 finding 重新过闸。
  - Phase 6：新增**诊断 brief 模板**（implementer 子代理执行、report-only、orchestrator 内联蒸馏纪律——叶子子代理不触发技能）；因未知类修复的 `Test:` 字段硬化为"先红后绿"的红命令；插桩必须带 `[DEBUG-<tag>]`，提交前 `grep -r "DEBUG-"` 清零；seam 缺失本身就是 finding，走 deferral 路由。
  - 两个 Retro（Review Failure / Deep Review Failure）各加归因行 "Cause never diagnosed (no red repro before fixes)"。
  - Phase 8：本地不复现的 CI 失败先诊断（CI 环境 vs 本地的差分回路）再分类 `ci-only`/`semantic`，不按日志表面形状分类。
- Supporting Skills 新增 `diagnosing-bugs` 条目（Phase 0-8 之外的交互式调试直接用独立技能）。

## [0.13.0] - 2026-07-06
- New core rule "Deviations are recorded, not silent" (adapted from Thariq's implementation-notes practice): Phase 1 implementer briefs and Phase 6 fix briefs must report every departure from the plan (unexpected upstream/API behavior, non-reusable component, switched implementation path, mid-work constraint) as one line each — what/why/impact — with "no deviations" stated explicitly.
- The PR description carries a running `偏离记录` section (seeded at Phase 3, appended by Phase 6 fix passes); Phase 4 reviewer briefs include it so review attention goes first to where the implementer chose in territory the plan did not cover; the Phase 8 Chinese work summary consumes it as a new `计划偏离` section.
- Complements 0.12.0 deferral routing: deferrals track what was found but not done; deviations track what was done differently than planned. Reuses the PR body — no new artifact file.

## [0.12.0] - 2026-07-06

- **Deferrals are routed, not dropped** (new Core Rule + Phase 8 gate bullet): every explicitly deferred verified finding and every 剩余风险/已知限制 entry in the merge-time work summary must carry either a tracked issue URL — delegated to the `issue-scribe` agent when installed (verify → dedup → one structured issue → URL) — or a recorded one-line reason why no issue is filed. An unrouted deferral fails the pre-merge gate instead of silently dropping. This makes issue-scribe's trigger structural (gate-checked at the places deferrals are born: the cross-review deferral rule, the Chinese work-summary template's 已知限制 line, the gate itself) rather than description-driven.
- `issue-scribe` added to the optional-prerequisites line alongside `worktree-guard` and `monitor`.

## [0.11.0] - 2026-07-06

- **Verification matrix becomes a standing project asset** (eng-init alignment): the project profile grows from six to eight fields — Phase 0.0 bootstrap now also records the repo's real command entry points (package scripts / Makefile / justfile / CI steps) and a verification matrix (`surface -> command -> evidence`). Phase 2 executes the matrix rows for the touched surfaces plus the default build+test row instead of improvising a pipeline each run (falling back to discovery only when a row is missing, then writing it back via Phase 0.5 profile-gap maintenance), and the Phase 8 completion self-audit treats a touched-surface matrix row never executed on the final head as an uncovered criterion. Templates in `project-profiles.md` (contributes-list, Generic, authoring block, size budget).
- **Route control-plane bootstrap upstream**: new Supporting Skills entry pointing to `control-plane-auditor` (codebase-stewardship) for repos that lack instruction files, unified command entry points, or verification infrastructure; its bootstrap mode scaffolds what Phase 0.0 consumes.

## [0.10.0] - 2026-07-06

- **Wire in the `worktree-guard` hook (optional)**: worktree-delegation mode is now bracketed by `.worktree-guard.json` at the project root — written on entry with `allowedRoots` covering the delegated worktrees plus orchestrator-owned `.workplans/` and `openspec/`, deleted after integration and `git worktree prune` — so out-of-root file writes are denied mechanically instead of by prompt discipline. Added the entry/exit/session-restart protocol and scope notes (file-edit tools only; `git apply` in the shell unaffected) to `references/parallel-worktree-delegation.md`, the bracket rule to the `SKILL.md` parallel-isolation Core Rule, and a stale-guard-file warning. Without the hook installed everything stays orchestrator-enforced.
- **Wire in the `monitor` agent (optional)**: the Silent-long-waits Core Rule and `phase-flow.md` Phase 8 now prefer delegating harness-external waits (CI runs) to a single `monitor` subagent — ID-based completion detection, one quiet blocking wait, read-only — over orchestrator-side polling loops.
- **Declare both as optional prerequisites** in `SKILL.md` so the wiring is discoverable at install time.

## [0.9.0] - 2026-07-02
- **Realign the standard reviewer set to canonical `risk-adaptive-cross-review` (`reviewer-packages.md`)**: the four standard reviewers are Correctness, Integration, Security/Performance, and Test & Evidence Coverage; the six-reviewer high-risk escalation adds Spec Compliance and Invariant/State-Machine/Compatibility. Fixes the prior inversion in `phase-4-cross-review.md` (roles table + high/broad-expanded selection) and `phase-flow.md` Phase 4 that made Spec Compliance a base reviewer and demoted Test & Evidence Coverage to escalation-only.
- **Carve out legitimately-unreviewed `none`-tier PRs from the pre-merge hard-gate**: the gate now passes with EITHER (a) SHA-matched review artifacts (Agent Review block, Phase 4.5 verdict table, clean latest comprehensive round, Phase 7) OR (b) a persisted "review not required" record (fixture tier `none` + the Phase 2 audit that found no risk, recorded against the final head SHA). Missing both remains a skip block. Removes the contradiction with Phase 4 `none`-tier skip. (`SKILL.md` gate rule + `phase-flow.md` Phase 8 gate bullets.)
- **Fix the report-persistence mechanism for read-only subagents**: reviewer and verifier subagents have no write access, so they now RETURN the complete report/verdict as their final message and the orchestrator persists it to `<REVIEW_DIR>`. Rewrote the `phase-4-cross-review.md` reviewer and verifier brief instructions, added an orchestrator-side collection sentence in `phase-flow.md` Phase 4, and clarified the same return-then-persist contract for the read-only fixture reviewer in `issue-risk-contract.md`.
- **Define one canonical evidence root**: `<REVIEW_DIR>` defaults to `.workplans/<issue-or-pr>/review/` (aligned with `parallel-worktree-delegation.md`'s `.workplans/<issue-or-pr>/`) and is referenced everywhere evidence is persisted (`phase-4-cross-review.md` variable def; `phase-flow.md` Phase 4/4.5/8).
- **Clarify the leaf-task explorer boundary**: worker agent definitions may natively permit spawning an `explorer` subagent for standalone use; inside this workflow's leaf tasks that capability is disabled and the injected boundary overrides the agent definition. Added to the `SKILL.md` Required Subagent Boundary and the `phase-4-cross-review.md` reviewer brief boundary.
- **Record the rollback anchor explicitly**: after each clean comprehensive cross-review round the orchestrator records `Last clean reviewed SHA: <sha>` in the evidence bundle; the Phase 8 corruption-recovery path resets to that recorded SHA. Corrected the `phase-flow.md` Phase 8 paragraph that wrongly called the frozen final HEAD the clean rollback anchor.
- **Always persist the project profile**: Phase 0.0 bootstrap always writes `openspec/project-profile.md`, stamping a pure-Generic result as Generic with a note that nothing project-specific was found. Fixed the Phase 0 lookup-order step that implied an in-memory-only Generic fallback.
- **Fix the phase skeleton both directions**: added Phase 0.0 (profile bootstrap) to the `SKILL.md` skeleton, and split the post-fix rerun out of Phase 6's tail into its own `## Phase 6.5: Repeat Cross-Review After Fixes` heading in `phase-flow.md` (which the Chinese work-summary template already referenced).
- **Complete the verifier-bias sentence in `SKILL.md`**: added the third `expanded` tier (PLAUSIBLE blocks only when it maps to a selected risk pack or Invariant Matrix row) alongside the existing `high`/`broad-expanded` and `low`/`compact` tiers.
- **Stop overclaiming "deterministic"**: reworded the `SKILL.md` gate rule and `phase-flow.md` Phase 8 paragraph so only the SHA-match and artifact-presence clauses are deterministic; the completion self-audit and oracle-integrity clauses are mandatory checkable procedures that require reviewer judgment. A failure of any clause still blocks the merge.
- **Split the garbled Phase 4.5 sentence** into two correct rules: drop every REFUTED candidate (with recorded rationale); a `wontfix` test-coverage exception remains forbidden.

## [0.8.1] - 2026-07-02
- Trim the SKILL.md description from ~1000 to ~640 characters: compress the feature enumeration into one clause while keeping every trigger phrase and the full Do-NOT / cc-cx disambiguation. The old run-on description was diluting the trigger signal and brushing the practical description-length ceiling.

## [0.8.0] - 2026-06-18
- Sharpen Phase 7 into the canonical **Gap Sweep** (defined in `risk-adaptive-cross-review` Synthesis): the independent final reviewer now runs a fresh clean-slate pass with the verified-findings list visible, looking only for real defects the recall-biased rounds systematically miss (removed behavior, contract drift, boundary/error/async/auth/migration/cache/wrapper paths), in addition to test coverage vs `tasks.md` and consumer compatibility. Consumes the canonical definition instead of restating it.
- Add a **completion self-audit (premature-completion guard)** and an **oracle-integrity** check to the Phase 8 pre-merge evidence hard-gate, and reflect both in the always-loaded `SKILL.md` gate rule: before merge the orchestrator re-derives each acceptance criterion and selected task and confirms the diff/tests actually satisfy it (not "the agent said done"), confirms no required edge/error path is unhandled and the changes are internally consistent, and confirms no test/spec/CI was weakened to pass. The frozen final HEAD is the clean rollback anchor — a gate failure blocks the merge (deterministic, no "probably fine"), and a fix round that corrupts a clean reviewed state resets to the last clean reviewed SHA rather than patching a broken head. Adapted from stellarlinkco/skills `harness` (validation-required completion, started-commit rollback) and `code-review` (oracle non-tampering), clean-room with author permission.

## [0.7.0] - 2026-06-18
- De-duplicate the cross-review reviewer definitions (entropy reduction): collapse the six self-contained reviewer briefs in `references/phase-4-cross-review.md` into one assembly template plus a reviewer-role table, and inline each reviewer's checklist and cross-cutting lenses from the canonical `risk-adaptive-cross-review` (`reviewer-packages.md`) instead of restating them. This makes phase-4 match what `SKILL.md` already claimed — that the reference holds only scaffolding, the Invariant Matrix binding, and the Phase 4.5 verifier — and removes the two-copy drift that any future reviewer-checklist or lens change would otherwise have to maintain in both places.

## [0.6.0] - 2026-06-18
- Add a hard pre-merge evidence gate (meta-loop dimension 8): never merge (including pre-authorized auto-merge) unless the PR `Agent Review` block, the Phase 4.5 verifier verdict table, a clean latest comprehensive cross-review round, and Phase 7 all exist and are SHA-matched to the frozen final HEAD. Recommend enforcing it as a host-repo required CI/branch-protection status check so skipping the review/verify loop is a detectable hard action; log every skip block. A portable skill cannot install that check, so absent it the gate stays orchestrator-enforced.
- Add cross-run loop accountability (meta-loop dimension 6): after each merge, append one line to a committed append-only review-loop log (`docs/review-loop-log.jsonl`) recording fixture level, comprehensive rounds, `gate_net_catch` (verified findings the review/verify loop caught that local verification and CI missed), verifier verdict counts, residual deferrals, and pre-merge skip blocks. Define a keep/cut criterion (human call, ~8-PR minimum sample, default-keep since this workflow prioritizes correctness over cost) recorded in `docs/adr/`. Cross-run risk learning already ratchets through the living `openspec/project-profile.md`; this adds the accountability half.

## [0.5.0] - 2026-06-14
- Rename the skill from `codex-codeagent-workflow` to `subagent-workflow` and convert the delegation model from `codeagent-wrapper --backend codex` leaf tasks to native subagents. Implementation/fixes delegate to the `implementer` subagent, cross-review to `reviewer` subagents, and the Phase 4.5 verification gate to the `verifier` subagent. Push and PR ownership stay with the orchestrator (`implementer` is push-free), so the workflow does not modify the shared `coder` agent that Issue Agent OS depends on.
- Make the workflow orchestration-agnostic across Claude Code and Codex: the orchestrator uses its native subagent mechanism (Claude Code Task subagents or Codex subagents) instead of shelling out to an external code-agent CLI. Drop `codeagent-wrapper` from prerequisites and `skill.json` requirements.
- Replace the "Required Delegation Guard" with a "Required Subagent Boundary" and reframe "No nested AI delegation" as "No nested workflow delegation": each spawned `implementer`/`reviewer`/`verifier` is a leaf that must not re-invoke this workflow or spawn further nested workflow subagents. The boundary also requires treating issue/external content as untrusted data, not instructions.
- Rewrite parallel code-writing isolation to use `.worktrees/` (was `.codex/worktrees/`) and an orchestrator-owned manifest; rewrite the Phase 4 cross-review and Phase 4.5 verifier templates as subagent briefs rather than `codeagent-wrapper` heredocs. No change to OpenSpec gates, risk-adaptive review/fix governance, Invariant Matrix, round budgets, or merge-gate behavior.
- Hardening from skill self-audit: detect the default branch instead of hardcoding `master`; drop the codeagent `@file` reference syntax in favor of plain OpenSpec paths; tighten triggers (remove broad "do it"/"run the workflow" phrasings) and add a `cc-cx-workflow` anti-trigger; note that `.worktrees/` should be gitignored.

## [0.4.1] - 2026-06-01
- Add a size budget for `openspec/project-profile.md` (in `project-profiles.md` authoring and Phase 0.0): scaled soft targets (~25 lines simple, ~60 broad) plus two hard rules — never restate core packs/triggers and use short bullets over prose — to keep the per-run profile lean and prevent re-accreting core content.

## [0.4.0] - 2026-06-01
- Make the project profile a living, project-local artifact at `openspec/project-profile.md` instead of a selection from the shared skill. It survives skill reinstalls and never accretes project-specific surfaces back into the shared skill.
- Add Phase 0.0 profile bootstrap: on first run in a project with no `openspec/project-profile.md`, scan the repo and generate one (copying the closest template or Generic). Phase 0 resolves the profile by lookup order: project-local file -> bootstrap -> Generic fallback.
- Add Phase 0.5 profile-gap maintenance: update the project-local profile when an issue exposes a new entry surface, contract, risk axis, or domain pack; ordinary issues that already fit the profile do not touch it.
- Reframe `references/project-profiles.md` as a shared template catalog (Generic plus SHUD/rSHUD/AutoSHUD examples) rather than the place to add per-project profiles.

## [0.3.0] - 2026-06-01
- Decouple the workflow from the SHUD/rSHUD/AutoSHUD project family: extract the three hardcoded profiles into a new pluggable `references/project-profiles.md` and add a **Generic** default profile so the skill applies to any repository.
- Generalize `references/issue-risk-contract.md`: core risk packs and mandatory expanded-triggers are now project-agnostic (added Auth/permissions/secrets and Concurrency/shared-state/ordering); domain packs (geospatial/CRS, time-series/forcing, numerical/conservation, solver/threading) and domain triggers move into the profile that contributes them.
- Phase 0 step 0 now selects a profile from `project-profiles.md` (Generic when none matches) instead of assuming a SHUD project family; fixture templates carry `Project profile: Generic|...`.
- No behavior change for SHUD-family repos: SHUD/rSHUD/AutoSHUD remain first-class profiles with full fidelity.

## [0.2.0] - 2026-06-01
- Add Phase 4.5 independent finding verification gate, borrowing the find/verify separation from the `code-review` skill: Phase 4 reviewers now emit candidate findings, and a separate codeagent verifier adjudicates each deduped candidate as CONFIRMED/PLAUSIBLE/REFUTED before Phase 5.
- Make verification recall/precision bias follow the fixture level (`high`/`broad-expanded` keep PLAUSIBLE merge-blocking; `low`/`compact` block only on CONFIRMED); REFUTED candidates are dropped with recorded rationale and do not count toward the review round budget.
- Add a removed-behavior auditor checklist item to the Integration reviewer and a dedup step ahead of verification.
- Add the Phase 4.5 verifier template to `references/phase-4-cross-review.md`.
- Slim `SKILL.md`: collapse 16 review/fix-governance Core Rules (round budgets, hard gates, escalation, CI-only, large-PR staging) into 3 navigational non-negotiables that defer mechanics to `references/phase-flow.md`; always-loaded skill doc drops from 32 to 18 Core Rules (~20KB to ~13KB) with no loss of executable detail.
- De-duplicate review semantics: `risk-adaptive-cross-review` is now the canonical source for reviewer packs, the finding contract, and failure-class synthesis. This workflow references those definitions instead of hand-forking them; `references/phase-4-cross-review.md` is declared the codex/OpenSpec instantiation only.

## [0.1.0] - 2026-05-25
- Initial canonical package for the Codex + codeagent issue workflow.
- Adds OpenSpec fixture gates, risk-adaptive review/fix loops, parallel worktree delegation rules, PR evidence, CI, and human-gated merge guidance.
- Documents relationship to `risk-adaptive-cross-review` and upstream `stage-change-pipeline`.
- Documents reuse points for `implementation-planning`, `review`, `entropy-review`, `git-worktree-workflows`, and `project-documentation`.
- Keeps requirements clarification and issue-readiness checks out of this automated implementation flow; `stage-change-pipeline` owns those upstream.
