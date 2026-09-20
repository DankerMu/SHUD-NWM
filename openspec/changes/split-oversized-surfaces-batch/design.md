## Context

10 个越线文件、约 46,000 行，分属三条治理主线，全部已被 `.large-file-guard.json` 豁免。issue body 写于不同时间，其中的行数与模块切分表已 stale（实测 #1101 4033→9614、#1103 1100→6249、#1099 2466→3639、#1102 1500→3218；#1823/#1842 声称「当前无豁免」，实测两条豁免均已存在）。本 design 以 HEAD `e5ea7f788` 实测为准，issue 的模块表只作切分种子，不作完整映射。

## Goals / Non-Goals

- Goals：每个 resulting 文件 < 1,000 行；10 条 exclude 全部删除且零替代 exclude；零行为变更（用机械 oracle 钉死）；定向 CI 选择器对新分区保持完备。
- Non-Goals：任何 retention / registry refresh / entropy audit 的语义、CLI、schema、检查 id、退出码变更；其余 86 条 exclude 与 `maxLines` 阈值；`write_entropy_baseline.py`。

## Decisions

### D1 — facade vs shim：按 monkeypatch 面裁定，不按 issue 文字

#1099 的 issue 提议旧路径降为 2 行 shim。实测 `tests/` 内对本批模块存在大量 module-attribute patch（`tests/test_entropy_audit_script.py:6042-6044/6882/8911-8982` patch `audit_repo_entropy` 的私有 helper；`tests/test_retention_copyback_mutex.py:113/143/190/483/484/625` patch `retention_module` 的 `acquire_copyback_batch_lock` / `release_copyback_batch_lock` / `remove_tree_allow_symlinks`）。

裁定，对每个被 patch 的符号二选一，**不得两者兼有**：

1. 符号与其**全部调用点**留在原模块 → 原模块保持为真实 owner，patch target 不变；
2. 符号与其调用点**一起**搬到新模块 → patch target 在同一 commit 内改指新模块，且**原模块不得 re-export 该符号**。

理由：若调用点搬走而原模块仍 re-export 同名符号，`monkeypatch.setattr(old_module, name, ...)` 会成功（属性存在）却打不到真实调用点——负向用例（如 `:113` 把 acquire 换成 `forbidden`、`:625` 换成 `failing_first_remove`）会**静默 vacuous pass**，证据链失效而 CI 全绿。这是本批最大的正确性陷阱。

对**非 patch 面**的私有 helper / 常量，原模块可自由 re-export 以保 import 兼容（#1910 的 basins facade 先例，`openspec/specs/orchestrator-structural-burndown/spec.md` 的 "Basins package facade split" requirement）。

### D2 — 测试拆分 oracle：suffix 集合逐字节相等

每个被拆测试文件，拆前后取 `pytest --collect-only -q <目标> | grep '::' | sed 's/^[^:]*:://' | sort`，两侧文件必须 byte-identical。文件段（module prefix）允许变化，`::test_name[param-id]` 后缀不允许。baseline 已在拆分前落盘（25 / 32 / 410 / 315 / 59 条）。不保留任何 collectible 兼容 shim（沿用 `spec.md:295-300` 既有做法）；共享定义只进非收集 helper module。

### D3 — `audit_repo_entropy.py` 的 parity oracle：稳定键 + 归因残差

issue #1842 要求「拆分前后报告全量一致」。这条**不可能字面成立**，原因有二：

1. 该 audit 的输入是本仓库自身，而本 PR 正在改仓库结构；
2. 即便把比较缩到「只拆 audit 一个文件」的相邻两 commit，`metadata` 里仍有天然漂移键——实测 baseline（`schema_version: governance-4a.entropy-report.v1`）含 `generated_at`（墙钟 ISO 时间，每次调用都不同）、`repo_root`（绝对路径）、`comparison_base_ref`（经 `HEAD^`/merge-base 解析，相邻 commit 必不同）、`structural_file_budget` / `budget_counted_count` / `finding_count` / `summary_counts`（audit 自身行数变化会移动它自己的 budget-class 条目——正是本条降级的起因）。

实际 oracle：

- **metadata 稳定键白名单**逐键相等，只比这 8 个：`schema_version`、`mode`、`check_family_count`、`executed_check_families`、`skipped_path_families`、`max_scanned_text_file_bytes`、`max_artifact_fingerprint_bytes`、`baseline_path`。其余键（`generated_at` / `repo_root` / `finding_count` / `budget_counted_count` / `gate_eligible_count` / `baseline_exists` / `baseline_written` / `summary_counts` / `structural_file_budget` / `compatibility_facade_guard` / `scoped_agent_context`）显式排除。
- `findings[].check_id` 集合相等（baseline 实测 5 个）。
- `high_spread_patterns` / `module_heatmap` 的 key 集合相等（baseline 23 个 module）。
- 顶层 public 函数集合相等；`--format json` 退出码不变。

并且 #1842 的拆分 commit 必须**单独**跑一次全量 findings diff：在只有 `audit_repo_entropy.py` 被拆、其余 8 组已落地的树上取 before/after 两次 `--format json`。baseline 实测 801 条 findings 中**零条**的 `evidence_path`/`module` 命中 entropy audit 自身路径，因此该 diff 的期望是**空**。若出现残差，必须逐条归因到本次搬运产生/消失的路径，并在 PR body 内列举；**不可归因的残差 = 行为漂移，回退该组而非放宽 oracle**。

### D4 — 执行顺序：共享治理面冲突驱动

`scripts/select_ci_tests.py`、`tests/test_select_ci_tests.py`、`_ScopedAgentContextConfig` 字面量、4 个 scoped `AGENTS.md` 被多组共同触碰。顺序固定为：

`#1611 → #2259 → #1101 → #1099 → #1102 → #1100 → #1823 → #1842 → #1103`

- 测试侧先于生产侧（#1101 先于 #1099、#1102 先于 #1100）：先收窄测试 import 面，生产拆分只需保住已收窄的面（issue #1101 自述的理由）。
- #1823 先于 #1842：两者共同改 `select_ci_tests.py:4172-4181` 的三条 PathTestRule target、selector pin、`_ScopedAgentContextConfig` 四处字面量、4 个 AGENTS.md；测试侧先落，执法侧一次性收口，避免两轮互踩。
- #1103 末位：纯文档，与任何 Python 面无耦合。

### D5 — 新测试分区必须显式进 PathTestRule，且加 tracked-tree 守卫

`tests/test_select_ci_tests.py` 的既有守卫**不足以**接住本批。实测：

- `_tracked_same_name_pairs`（`tests/test_select_ci_tests.py:3165`）只覆盖 5 个后端前缀下的同名 source/test 配对，对「没有同名源文件的孤儿测试分区」不红——而 #1611 / #1101 / #1102 与 #1823 的大部分产出正是这一类。
- 目标文件消失只触发 **WARNING 而非失败**（`test_select_tests_warns_when_a_rule_target_no_longer_exists`，`tests/test_select_ci_tests.py:6131`）。
- 具体到 #1611：`scripts/select_ci_tests.py` 对 `tests/test_scheduler_state_index_copyback_replay.py` **零条 PathTestRule**（grep 无命中），现有路由完全依赖与 `scripts/scheduler_state_index_copyback_replay.py` 的同名推导；无 shim 删除原文件后该配对静默消失，路由降级为一条 warning。

因此每个产出新测试分区的组（#1611 / #1101 / #1102 / #1823 / #2259）必须同时：

1. 在 `scripts/select_ci_tests.py` 增/改一条 `PathTestRule`，**显式枚举**本组全部新分区（不依赖同名推导）；
2. 在 `tests/test_select_ci_tests.py` 增一条 tracked-tree 守卫，断言该语料在受追踪树上**恰好**是「N 个 collectible 分区 + M 个非收集 helper」（先例：`test_registry_partition_tracked_tree_is_exactly_seven_suites_one_helper:16870`、`test_qhh_partition_tracked_tree_is_exactly_three_suites_and_one_helper:14844`，用 `_tracked_python_files` + `is_test_suite_path`）——多一个分区、残留一个兼容 shim、helper 被改成 suite 都必须转红。

单跑 `uv run pytest -q tests/test_select_ci_tests.py` 绿**不构成**路由证据；证据是上面两项落地后它仍绿。

### D5b — 治理面的行号引用一律以模式为准

本 design 与 tasks 内对 `scripts/select_ci_tests.py` 的行号（以及 `tests/test_select_ci_tests.py` 的行号）均取自 HEAD `e5ea7f788` 实测，仅作定位线索。六个组会先后改动同一文件，行号执行时必然漂移——实现与验收一律按**规则模式 / target 字符串 / 函数名**定位，不按行号。

### D6 — 提交纪律：每组一 commit，exclude 与拆分同 commit

guard hook 对 staged **整文件**计行（`.claude/hooks/large-file-guard/large-file-guard.sh:166-187`），且 `git commit -a` 会触发 worktree 全量扫描。因此：每组用显式 `git add <paths>`（禁 `-a`，worktree 根有 untracked `agents/ hooks/ settings.json skills/` 不得入库）；该组的 exclude 删除必须与拆分在**同一 commit**，否则 hook 在拆分未完成时就拒绝，或在 exclude 已删而文件仍超阈值时拒绝。

## Risks / Trade-offs

- **vacuous-pass 风险**（D1）：缓解=拆分后逐条核对每个 `monkeypatch.setattr(<mod>, "<name>"` 的 `<mod>` 确为真实调用点 owner；原模块不得为已搬走的 patch 符号留 re-export。
- **PR 体量**：约 46,000 行搬运在单个 PR 内，人工逐行 review 不可行。缓解=每组各自的机械 oracle（D2/D3）+ 每组独立 commit，review 按 commit 而非按 diff 总量进行。
- **CI 定向选择降级**：diff 面过宽时 `scripts/select_ci_tests.py` 可能选不出而降级为 `--collect-only` 冒烟（零断言执行）。缓解=node-27 真实 DB pytest 作为迭代 oracle，PR body 显式声明 CI 绿 ≠ 全量通过。
- **#1842 parity 的可达性**（D3）：若单独 parity 的 findings 残差无法逐条归因到本次搬运路径，即为行为漂移，回退该组而非放宽 oracle。
- **selector 路由静默失效**（D5）：既有守卫只对同名配对与既存目标发声，孤儿分区与消失目标分别是「不红」与「仅 warning」。缓解=每组显式 PathTestRule + tracked-tree 守卫，二者缺一不算完成。
