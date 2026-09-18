# Review Failure Retro #2（post-gate 预算耗尽）

PR: #2456，第 4 轮所审 head SHA: c9fb8128（历史锚，非现时 head）（第一份 retro 见 `review-failure-retro.md`）
Issue: #1627（本 issue 的第一个 PR；`ceilingPrs` 为空，`issueEscalated` 未置位）

Failure classes: unverified-claim, duplicate-prose-drift, dangling-pointer, evidence-gap

Rounds affected:
- Round 1 `e1889fde3` — 8 条已验，critical。
- Round 2 `ee2be0243` — 12 条已验，major。
- Round 3 `cf400794e` — 5 条已验，major。→ 第一份 retro，shape depth，budget 1。
- Round 4 `c9fb8128`（post-gate，席位 spec-compliance / invariant-state）— 9 条已验，major。
  两席均 Request changes，且**两席的 P1 是同一条**：第一份 retro 点名要删的两个块没删干净，
  而 ADR 与 commit 正文已据此写下「全仓因此只剩一份」。

Failure shape: **depth**（同一根因的更深一层，不是新面）

第一份 retro 诊断的根因是「两份手抄件必然漂移」，纠正动作是**删除抄本**。它做对了：
第 4 轮实测 21 条 ADR 承重措辞在 fixture 里 0 命中，普查表只剩一份。
但第 4 轮同时暴露：**删除动作自己又生产了一批新缺陷**，而且是同一类。
所以根因还在下面一层。

Depth evidence:
- Invariant: 写进被评审件的、关于本次评审过程自身的陈述，是一条没有权威可回算的断言——它随每一次修复而新增，并且每一次都必须再被评审一次；于是修复机制就是失败机制。
- 第一份 retro 删掉了**抄本**；这一份必须删掉**抄写者**。四轮下来，绝大多数已验发现不是裁定错了，而是我对「我自己这次改了什么、抓到过几次、覆盖了多少」的陈述错了——那些陈述在仓里没有任何权威可以回算，只能靠下一席人读。

Recurring findings:
- Round 1/2：两个关于本 PR 自身的总数（「十三处」「二十二处」）都数不出来——权威是我的记忆。
- Round 3：「指向源码符号的锚已全部改为符号锚」为假（ADR 内仍有 23 处 `*.py:NNN`），而它是 EF-6 的全部依据。
- Round 3：「PR 定向 CI 抓不到那条回归」为假——我对自己 CI 选择逻辑的陈述，没测就写了。
- Round 4：「TOCTOU 段已整块移除」为假（该段一字未动）；「全仓因此只剩一份」据此也为假。
- Round 4：8 个 root 字段那段写着「本节不复述」，紧接着把被指内容复述了一遍——自指的段落自己否证自己。
- Round 4：同一次提交造出一条指向 change 目录的死指针，而那次提交的文本正在谴责这种指针。
- Round 4：EF-6 写「每一条 `#N`、归档路径、file:line 引用」，而它所依的脚本只覆盖带路径的形（另有 48 条裸续锚、49 条 `#N` 不在其中）。
- Round 4（**少数派，必须照实记**）：不是过程散文，而是**裁定本身**的自相矛盾——ADR「子族定性」把 `_optional_config_path` 判给从句 1，而同一份 ADR 的普查表与 `services/orchestrator/scheduler_runtime_roots.py:653` 的站点标记都判从句 3，且它援引的正是普查表记为「已被实测证伪」的那条理由；同表还把 `_optional_config_path_for_mode` 写在 `config.py`，实际在 `path_modes.py:157`。这两条从第 1 轮活到第 4 轮，三轮人读没抓到。**因此不得写「裁定内容一直是干净的」——那会是第五条未现证的全称断言，而且会写在专门记录前四条的那一节里。**

Split rebuttal:
- 连续两轮（3、4）中，代码面席位与 invariant-state 席位对守卫两条断言、19 条站点标记、selector 接线、拼法测试、零运行时改动均零 P1；第 4 轮的两条 P1 全部是「关于散文的散文」。
- 切分不减少失败类，它**乘以**失败类：两个子 PR 各自需要一份 fixture，而 fixture 正是缺陷密度最高的那一层；每个子 PR 还要新增一段解释「为什么切」的过程散文——正是本 retro 要删的那种。
- 要切的对象不存在：ADR 与它的 fixture 是同一件评审品，fixture 随 ADR 归档；第 1 轮的 P0（一条无守卫背书的 SHALL 在引入之日即被违反）正是「规范先行、守卫后补」这种切法的产物。
- issue #1627 未声明 `Minimal mergeable slice`。
- 正确的操作是**减一层**（删掉元层），不是**切一刀**（把元层分成两半）。

Why Phase 5/6 did not close it:
- Fixture scope gap: **no**。
- Fix prompt too narrow: **no**（本轮不是这条：第一份 retro 的处置范围是对的，执行时漏了两块）。
- Reviewer finding contract vague: **no**，两席均附命令与输出。
- Missing regression evidence: **yes，而且是本条的核心** —— 仓内对「过程散文」这一整类内容**没有任何机械判据**。cite_check 管引用，守卫管站点，`openspec validate` 管结构；「我说我删了」没有任何东西管。
- Cause never diagnosed: **no**（第一份 retro 诊断对了一层，只是不够深）。
- PR too broad / should split: **no**，见上。

Next corrective action: **descope —— 把 ADR 与 fixture 收回到「裁定 + 它的强制」，删掉全部关于本次评审过程自身的叙述**

1. ADR：删掉每一段讲本 PR 自身评审过程的文字——「已知限制」1 的多轮叙事、以及散落在普查 /
   守卫 / 子族定性里的撤回式插入语（**保留被更正后的事实，删掉更正的故事**）。
   给下一个作者的三条情报 (a)(b)(c) 保留，但改写为三句不含任何计数与 PR 沿革的有界陈述。
2. 删掉每一个关于本 PR 自身产物的数字：「253 → 155 行」「23 处」「21 条」「38 条引用」之类。
   它们全部是没有权威可回算的自述。
3. design.md / tasks.md / proposal.md：同样剥掉本轮新加的那批撤回式插入语。
   **fixture 记决定，retro 记沿革，`.workplans/`（gitignored）记流水账**——三者不互相抄。
4. EF-6 的判据保持「脚本 exit 0」，**不把解析条数誊进散文**。
5. 修掉第 4 轮那条裁定级矛盾（`_optional_config_path` 的从句号、普查表的模块名），
   **只留更正后的事实**。

Required proof（机械、可复核）:
- `grep -nE '初稿|交叉审查|post-gate|复发|评审席|fixture 评审|第 ?[一二三四五六七八九十0-9]+ ?[轮次]|本 PR|我自己|我在|抓到|证伪'`
  跑 ADR + 四份 fixture（**排除两份 retro**）→ 每一处命中都是缺陷，逐条清零或改写为不含自指的陈述。
- 21 条 ADR 承重措辞 grep fixture → 仍 0 命中（不回退第一份 retro 的成果）。
- `uv run python scripts/cite_check.py <ADR + 四份 fixture>` → exit 0。
- `uv run openspec validate enoent-fallback-family-ruling --strict --no-interactive` → valid。
- `_optional_config_path` 的从句号在三处（ADR 子族定性、ADR 普查表、站点标记）一致。
- **以上四条的结果数字一律不写进任何被评审件。**

Post-gate plan: 纠正动作 → amend 同一提交 → 一次推送 → 本 retro 授予的预算内一轮审查
→ Phase 7 → CI → 合并。**第 5 轮若仍不清即触及 round ceiling（terminal）**，
届时只剩切分 / descope / 用户裁决三条路，且必须由人来选。
