# Tasks — node-22 运维待办列举面（#1186 / PR-B）

父 PR #2398 拆分方案：`.workplans/pr-2398/review/split-plan.md`（gitignored 工作底稿；本文件与 `design.md` 是仓内的可复核件，不依赖它）。

## A — 恢复列举面

- [x] A1 从 `ca22d18f9` 恢复 `services/orchestrator/operator_action_listing.py` 与 `tests/test_operator_action_listing.py`（`53c39b99c` 为拆分把它们移出 PR-A）。
- [x] A2 重新挂上 `cli.py`：import 块、click 命令注册、argparse 子解析器、`LIST_OPERATOR_ACTIONS_COMMAND` 分派共**四处**（fixture 审查更正：第一版写的"三处"漏了 import 块），与 `53c39b99c` 删除的是同一批挂点。

- [x] A3 **CI 选择器 importer-gap 定性**（EF-0 当场兑现）。恢复 `tests/test_operator_action_listing.py` 后立即实跑 `uv run pytest tests/test_select_ci_tests.py -q -k "directory_rule_importer_gaps_are_dispositioned"`，实测变红，三条未定性的 importer 对：`services/orchestrator/__init__.py`、`services/orchestrator/cli.py`、`services/orchestrator/scheduler_evidence_payload.py` → `tests/test_operator_action_listing.py`。按 PR-A `863f48ea6` 已立的先例处置：subject 归自己的 stop rule / broad `services/orchestrator/**` 规则，边缘依赖记 `edge-consumer`，并在每个规则点写明实测运行时长。**审计断言本身一字不改**，不得 skip/xfail/删除；若动到既有的 exact-equality 计数 pin，必须留对照腿不动以证明不是"改断言凑绿"。

## B — round-5 未闭合的三条

- [x] B1（r5-01，可判为 P1）范围完整性。按 `design.md` D1/D2 实现三态：范围完整且可判定 → 清零；`TRANSPARENT_PASS_STATUSES` → 保持；**范围被收窄 → 保持**；其余 → 置位。判据是**过滤值**（`basin_ids` 空、`model_ids` 空、`expression` 为 null）加 `backfill.enabled is True`，**不是** `operator_filters` 这个 mapping 的有无或大小——D2 的 169 趟实测说明写反即线上 exit 3。收窄的 pass 仍列出自己的 action，并以 reason `scope_narrowed` 计入 `non_evaluating_passes`。round 1 A2 补上第三个收窄维度：顶层 `sources`（`scheduler_evidence.py:268` 无条件写）不是权威全集 `("gfs", "IFS")` 时同样是 `scope_narrowed`——判据是集合比较，不是空/非空（`cli.py:421` 让它恒非空）。`backfill.enabled`、`operator_filters` 的 `basin_ids` / `model_ids` / `expression` 与顶层 `sources` **六个字段有任一不在** → reason `scope_unknown` 并**置位**隐藏标志（走第四态的默认动作，不豁免；语义与取舍见 design.md D1 的缺键段，判别行是 D3 第 9 行）。分界线是**键的有无**不是值：键在而值收窄才是 `scope_narrowed`；Phase 2 探针实测第一版只查两个顶层键时，`operator_filters={}` 会被判成范围完整并给出 `exit 0`。
  两条实现纪律，写死免得当成"顺手改"：(a) **status 判在 scope 判之前**（`spec.md:54`）——透明 pass 结构性无 `backfill` 键，顺序写反就会被判成 `scope_unknown` 置位，D3 第 7 行必红；(b) `_non_evaluating_entry`（`operator_action_listing.py:209`）现签名 `(name, status, limit)` 判不了 scope，**必须改签名**接收整个 payload，改完把全部调用点一起改。
- [x] B2（r5-00，覆盖）排序表补 `[D, size_fallback(原 status preflight_blocked)] → 3` 一行，并让「把 size-fallback 分支改成按 `limit.pre_limit_status` 置位」这条变异**变红**。
- [x] B3（r5-03，覆盖）排序表补 `design.md` D3 表里其余各行。**第 2 行 `[D, SF, scoped] → 3` 是整张表里唯一锁得住 r5-01 的一行，不得省**（fixture 审查 F-03：拆分计划原给的六行对 r5-01 变异零判别力）。拆分计划里的 `[SF, scoped(backfill disabled)]` 与 `[SF, scoped(basin filter)]` 两行**故意不做**——真实 `bounded_evidence_payload` 丢掉两个 scope 键，那种形状 writer 造不出来（F-04）。**第 8、9 行也不得省**：第 9 行 `[缺键, D] → 0` 是全表唯一区分「缺键=位置相关置位」与「缺键=全局否决」两种实现的一行，实现若写成全局否决它必红。
- [x] B4（fixture 审查 F-05，实现前已知）测试构造器 `_write_pass`（`tests/test_operator_action_listing.py:123-150`）当前**不写任何 scope 键**，而规格要求"可求值但缺 scope 键 → 不可判定"。因此现存全部 `exit 0` 断言会翻——受影响处已定位：`:616-622`（7 行参数）、`:634-637`（4 行参数），以及 `:200`、`:517`、`:809` 三个测试。给 `_write_pass` 加上**默认的范围完整** scope 键（`backfill.enabled: True` + 四键空 `operator_filters`），让既有断言保持原义，再由新增用例显式传入收窄/缺键形状。**这批改动必须当作实现的一部分排期，不是"顺手改的测试"**——它是缺键规则唯一的显形处。
  **默认值只对可求值 status 生效**：`TRANSPARENT_PASS_STATUSES`（`lock_contended` / `preflight_blocked`）的 pass 默认**不写** scope 键，因为生产里这类 pass 是在候选构造之前落盘的、结构性没有 `backfill` 键（`spec.md:54`、D3 第 7 行的前提）。若默认值无条件铺到所有 status，既有透明 pass 用例写出的就是生产造不出的形状——与 F-04 删掉两行 `[SF, scoped]` 是同一类错误，只是方向相反。

## C — runbook

- [x] C1 `docs/runbooks/node22-control-plane-manual-recovery.md`：把 PR-A 留下的过渡口径（手写 jq 直读 evidence 的 `blocked_candidates` + not-selected `source_cycles`）整段替换为 `list-operator-actions` 的用法与退出码表。必须写明 `3` 的含义是**无法判定**而非"出错"，以及窄范围 pass 不构成"没有待办"的证据。

## Evidence Floor

> **EF-0（承自 PR-A 的教训，本 PR 开工即生效）**：新增/恢复测试文件的 PR 必须把 `tests/test_select_ci_tests.py` 算进本地测试集——本 PR 恢复 `tests/test_operator_action_listing.py`，它会给 CI 选择器的 directory-rule 审计带进新的 importer 对，与 PR-A 红了五轮的是同一个缺陷类。**且 CI 结果必须每轮实读**：不读的 CI 结果不是证据。

- [x] EF-1 本地：`uv run pytest -q tests/test_operator_action_listing.py` 全绿。
- [x] EF-2 本地：`uv run pytest -q tests/test_select_ci_tests.py` 全绿（见 EF-0）。
- [x] EF-3 本地：`uv run ruff check .` 与 `openspec validate node22-operator-action-listing --strict --no-interactive` 通过。
- [x] EF-4 红证据。两条变异，**各自指名哪条测试必须变红**，不接受"某条会红"这种说法：
  - **r5-01**：把「收窄 → 保持」改成「收窄 → 清零」→ 必须让 **D3 第 2 行 `[D, SF, scoped] → 3`** 那条用例变红。fixture 审查 F-03 实测：表里其余各行在此变异下**全部不变**（行 3/5 由 `evaluating_count < 1` 决定、行 4 变异后仍为 0、行 6 在 `:202` 提前返回 1），所以这条红证据**只能**由第 2 行产出。若第 2 行没写，EF-4 无法满足——不得以"其他测试也红了"充数。
  - **r5-00**：把 size-fallback 分支改成按 `limit.pre_limit_status` 置位 → 必须让 D3 第 1 行变红。
  - 两次变异均须还原并 `cmp` 对 pre-mutation 副本校验，`-k` 切片随新增测试同步扩；不得用旧切片报新数。
- [ ] EF-5 node-27 后端 oracle：在 `/home/nwm/tmp/` 下开一次性隔离 worktree（`umask 022`、`TMPDIR=/home/nwm/tmp`、共享 checkout 只 `git fetch`、共享 venv 经 `UV_PROJECT_ENVIRONMENT` + 全程 `--no-sync` 只读复用、带 import-origin 证明、跑完移除），跑本分支改过的测试文件。
- [ ] EF-6 **node-22 现场收据（EF-8，本 PR 独有）**：在活动证据根上实跑 `list-operator-actions`，记录退出码与 receipt。
  - **代码投送路径（fixture 审查 F-06：第一版漏了这一条）**：`/scratch/frd_muziyao/NWM` 是**正在跑生产调度器**的活动 checkout，**绝不在它上面 `git pull` 本分支**。做法是：该 checkout 只 `git fetch`，然后在 `/scratch/frd_muziyao/` 下开一次性隔离 worktree（`git -C /scratch/frd_muziyao/NWM worktree add --detach <路径> <sha>`），在隔离树里运行，跑完 `worktree remove` + `prune`。
  - **解释器纪律**：维护窗口前禁止 `uv sync` 与裸 `uv run`（会在 3.11 pin 下删掉并重建活动的 3.12 `.venv`，实测会打断成半拆状态）。只用绝对路径 `/scratch/frd_muziyao/NWM/.venv/bin/python`，控制台入口用 `-m`，并把 `PYTHONPATH` 指向隔离树以保证跑的是本分支的代码（**须留 import-origin 证明**，与 EF-5 同形）。解释器缺失即 fail-closed，不得触发环境创建。
  - 不连任何 DB；不 `kill`/`pkill`。D2 / D2b / D2c 的三次只读探查已按此纪律完成（未开 worktree，因为只读了 JSON、没有执行本分支代码）。
- [ ] EF-7 `design.md` D2 / D2b / D2c 的实测数字在**本 PR 的 head 上复核一次**。证据根内容随时间滚动，所以这条的通过判据是**重跑同样的三条只读探查并把 design.md 里的数字改成重测值**，不是"看一眼差不多"。
  - 节点：node-22；命令：`/scratch/frd_muziyao/NWM/.venv/bin/python <脚本>`，读 `/scratch/frd_muziyao/nhms-prod/workspace/scheduler/evidence`；三组数各自要重测的是：`operator_filters` 四键取值分布与 `backfill.enabled`（D2）、文件体积 min/median/max 与占 `MAX_EVIDENCE_BYTES` 的比例（D2b）、最近 20 趟 blocked candidate 的 `error_code` 与 `retry_policy` 四字段分布（D2c）。
  - 当前写进文档的数字（169 趟 / 170 个文件 / 760 条）**是 2026-09-16 的快照**，复核时若变了就改文档并在此注明变了什么，不得保留旧数字。

## 已知限制（写明，不假装闭合）

- 本面只读最近 `--passes` 趟。窗口之外的待办它看不见，这不是缺陷而是定义；`3` 的存在就是为了不把"窗口内没看见"说成"没有"。
- D2 的实测覆盖**输入形状**，不覆盖端到端退出码——后者由 EF-6 承担，在它产出前不得声称线上行为已验证。

## 开工基线（实测，2026-09-16，branch head = master `8d165f15f` + 恢复的两个文件）

- `uv run pytest -q tests/test_operator_action_listing.py` → **51 failed / 3 passed**。原因**单一，且是实测不是推断**：把全部失败的 `E ` 行归类，**51/51 条是同一条** `click.exceptions.UsageError: No such command 'list-operator-actions'`，无第二种错误——即 `cli.py` 的挂载还没恢复。特别地，PR-A 改过 `scheduler_evidence_payload.py`（新增 `refused_restart_stage` pull），而本测试在 `:222`/`:877` 直接调 `_bounded_candidate_summary`，本可能引入第二类失败；实测**没有**。即恢复的模块与测试本身是自洽的，A2 做完即应转绿——若届时仍有失败，那是**新**信息，不得当作"预期内的遗留红"。
- `uv run pytest -q tests/test_select_ci_tests.py -k directory_rule_importer_gaps_are_dispositioned` → **变红**，三条未定性 importer 对（见 A3）。
