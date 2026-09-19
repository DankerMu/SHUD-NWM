# Review Failure Retro

PR: #2456，第 3 轮所审 head SHA: cf400794e（历史锚，非现时 head）
Issue: #1627（本 issue 的第一个 PR，无跨 PR 上限记忆）

Failure classes: duplicate-prose-drift, unverified-claim, citation-line-rot

Rounds affected:
- Round 1 — SHA `e1889fde3`，席位 invariant-state / spec-compliance，not clean，
  8 条已验发现，最高 critical。类：normative-clause-unenforced、ci-selection-gap、
  citation-line-rot、overstated-claim。
- Round 2 — SHA `ee2be0243`，席位 invariant-state / spec-compliance，not clean，
  12 条已验发现，最高 major。类：unverified-claim、citation-line-rot、
  duplicate-prose-drift、undocumented-deviation。
- Round 3 — SHA `cf400794e`，席位 spec-compliance / test-evidence，not clean，
  5 条已验发现，最高 major。类：duplicate-prose-drift、unverified-claim、citation-line-rot。
  **席位 test-evidence 给出 Approve（零 P0/P1）**；三条 P1 全部来自 spec-compliance 席位，
  且全部落在同一份文档集的内部一致性上。

Failure shape: **depth**

判据（照 gates.md 的定义逐条核）：
- 不是 `converging`：`converging` 要求「无失败类跨轮复发」且「已验数与最高严重度逐轮非增、
  至少一项严格下降」。实测三轮已验数 8 → 12 → 5（第 2 轮**上升**），且三个类均有复发
  （duplicate-prose-drift 轮 2/3；unverified-claim 轮 2/3；citation-line-rot 轮 1/3）。两条都不满足。
- 不是 `breadth`：三轮发现并非散布在互不相干的面上，而是收敛到同一条根因。
- 不是 `noise`：无一条被 REFUTED；每条都附了可复现的实测。

Depth evidence:
- Invariant: 任何闭合集合与任何事实陈述都必须从其权威回算，手工维护的第二份抄本必然漂移——这正是本仓反复出现、且本 ADR 自己要交付的那条不变量。
- 这条不变量在三轮里都是同一个根因，我每轮修的都是它的某一次**表现**（某几句话、某个计数），从未移除**产生表现的结构**（两份抄本并存）。

Recurring findings:
- Round 1：普查表的行锚在本 PR 自己的 diff 内腐烂（citation-line-rot）——抄本与源码之间的漂移。
- Round 2：主论点更正只落了 ADR 与 proposal，`design.md` 的抄本原样留着，而偏离记录 DEV-21 声称三处已改（duplicate-prose-drift + unverified-claim）——抄本与抄本之间的漂移，外加一条未现证的完成断言。
- Round 3：ADR 与 `design.md` 的 19 行普查表仍逐字两份，且终态里已**再次**漂移（`scheduler_preflight.py` 的 NOT_VISIBLE 块，ADR `:652-661` 正确、design `:651-661` 错误）；三条从句、裸 `except OSError` 定性、TOCTOU 段、`_safe_preserve_final_component` 定性、8 个 root 字段、D3 守卫论证同样仍是两份。
- Round 3：`proposal.md:25` 仍写「15 个 admit 站点」，而规范正文与守卫要求全部 19 个——同一条退回声明的**第三份抄本**，第 2 轮修正时又漏了。
- Round 1/2/3：未现证的全称断言连续三次——「十三处」→「二十二处」→「指向源码符号的锚已全部改为符号锚」；末者机械可证伪（ADR 内仍有 23 处 `*.py:NNN`，其中两处是烂的），且它是 EF-6 不再追行号的**全部依据**。

Split rebuttal:
- 第 3 轮 test-evidence 席位对**全部代码面**给出 Approve 且零 P1：守卫两条断言、19 条站点标记、selector 接线、两条 `edge-consumer` 处置、拼法测试、零运行时改动，均经独立试破未破——没有一个代码面在失败。
- 三条 P1 全部落在 `docs/adr/0009-*.md` ↔ `openspec/changes/enoent-fallback-family-ruling/**` 这**一份**文档集的内部一致性上；切分只会把这份文档集切成两半，而它的失败模式恰恰就是「被切成两半」。
- 切分无法把「ADR」与「ADR 的 fixture」分开：fixture 就是该 ADR 自己的评审件，随它一起归档；分 PR 交付会让 fixture 与它评审的对象落在不同的合并点上。
- issue #1627 未声明 `Minimal mergeable slice`；即便按「规范先行、守卫后补」切，也会复制第 1 轮那条 P0——一条无守卫背书的 SHALL 在引入之日即被违反。
- 本 PR 不可切分，因为要切的东西不存在：存在的是一份该**删**的重复抄本。

Why Phase 5/6 did not close it:
- Fixture scope gap: **no** —— fixture 覆盖到了这条不变量，D3 的论证本身就是它。
- Fix prompt too narrow: **yes** —— 第 2 轮 P1-3 说的是「存在两份抄本」，
  而我给自己的处置是「替换席位点名的那四**句**」。我把一个结构问题当成了四处措辞问题。
- Reviewer finding contract vague/inconsistent: **no** —— 三轮发现均附 file:line 与可复现实测。
- Missing regression evidence: **no**（代码面）；文档面**无门可言**：仓内没有任何断言校验
  ADR 与 fixture 的一致性，这正是它能连漂三轮的原因。
- Cause never diagnosed (no red repro before fixes): **yes** —— 直到第 3 轮席位 1 亲手跑
  `diff <(ADR 区段) <(design 区段)`，才第一次有人**机械地**测量这两份文档的差异。
  前两轮（含我自己）都是靠读。**根因没被诊断过，所以修的是症状。**
- PR too broad / should split: **no** —— 见上。

Next corrective action: **refactor/redesign —— 删除第二份抄本，而非再替换一次**

1. 从 `design.md` 与 `proposal.md` **删除**每一段与 ADR 重复的论证块：19 行普查表、
   三条从句正文、裸 `except OSError` 定性、TOCTOU 段、`_safe_preserve_final_component` 定性、
   8 个 root 字段、D3 守卫论证。每处只留一行指向 ADR 对应小节的指针。
   fixture 只保留本次变更特有的内容：D1 小标题、F3「为什么第三条从句是被逼出来的」、
   继承自 #1626 的三条硬约束表、风险分诊表、D2 的「权威怎么取」那段方法论。
2. 修正 DEV-26 与 ADR 的自相矛盾：普查表归 ADR（长期件），design 指过去。
3. **删除全称断言**，不是把它换成更小的全称：ADR、`tasks.md` EF-6、`design.md` 三处的
   「已全部改为符号锚」一律删掉，改为有界表述，EF-6 的依据落在回执文件上。
   顺带修两处烂锚（`path_modes.py:117-119` → `:122-123`；`design.md:651-661` 随表删除）
   与 `tasks.md:10` 的 `:206-217` → `:212-222`。
   **执行留档（该子项的目标值当场被证伪）**：`:212-222` 实测仍错，`:220-222` 已跨进下一个 Scenario。
   同批还测出另外两条烂锚：`slurm-array-runner-integration/spec.md:106-112`（两端断在句中，
   论证延续到 `:128`）与 `design.md` 引的 `scheduler_runtime_roots.py:681-688`（实际 `:679-683`，
   `:688` 落在下一个函数体内）。**三条一律改为 Scenario / 符号锚，不做第三次追行号**
   ——这正是本 retro 的不变量：追着补行号是症状级处置，换锚种类才是结构级处置。
4. 折入第 3 轮 test-evidence 席位的 P2（我关于自己流程的第四条错误陈述）：
   `tasks.md:75`、DEV-27、commit 正文都写着「PR 定向 CI 抓不到那条回归」——**实测为假**，
   `tests/test_select_ci_tests.py` 在本分支每个提交上都被选中（任何新增测试文件都会累积
   selector 元守卫）。真实陈述对本 PR 更有利：该类回归**是** PR 可见的。
   同时把「每个 diff hunk 都是注释」改为「无非 docstring 的 AST 差异」（两个文件有 `__doc__` 改动）。

Required proof（机械、可复核，写进验收）:
- 对每一个 design 曾复述的 ADR 小节，取该节一句承重措辞，`grep` design.md 与 proposal.md → 0 命中。
- `grep -n '已全部' docs/adr/0009-*.md openspec/changes/enoent-fallback-family-ruling/*.md`
  **排除本文件自身**（retro 必须引用被撤回的原文，把它算进来会让判据在自己身上为假——
  post-gate 审查实测抓到这一点），其余命中全部落在「」内、是被撤回的原文，无一处是断言。
  精确判据：该 grep 排除 retro 后再过滤掉 `「…已全部…」` 形 → **0 命中**。
  **本条只给文件名不给行号**：初稿在此写死了三个行号，其中两个在后续编辑里就腐烂了
  （post-gate 审查实测），而本 retro 的不变量恰恰是「别写会漂的第二份坐标」。
  （本条在执行时收紧过两次：原写「0 命中」，而撤回记录本身要引用原文；随后又排除了本文件。
  两次收紧都留档，不是事后放宽——放宽会是同一条不变量的又一次复发。）
- `grep -n '15 个' openspec/changes/enoent-fallback-family-ruling/proposal.md` → 0 命中。
- `uv run openspec validate enoent-fallback-family-ruling --strict --no-interactive` 仍 valid
  （确认校验器不要求被删掉的小节）。
- **追加（执行中长出来的判据）**：`uv run python scripts/cite_check.py <ADR + 四份 fixture>` **exit 0**。
  第 3 轮 P2-1 指出旧回执没有生成器；现在生成器随 PR 入库，EF-6 的勾因此落在一个
  **可复跑的退出码**上，而不是任何一句散文。这条是本 retro 的不变量在证据侧的对偶：
  一份不能被重新计算的证据，和一份手抄的清单是同一种东西。

Post-gate plan: 纠正动作 → 一次推送 → **一轮 post-gate 审查，范围锁定上面那四条 required proof**
→ 该 head 上的 Phase 7 终审 → CI 绿 → 合并。
