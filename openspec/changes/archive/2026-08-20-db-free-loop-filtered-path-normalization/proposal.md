# Proposal: db-free-loop-filtered-path-normalization

## Why

`#1348` 家族把「symlink 环判据不能押在 `Path.resolve()` 抛错上」这条口径推到了
allowed-roots 一级（PR #1399）和 runtime-root 一级（PR #1426）。本 change 收尾
**db-free selector 腿剩下的两级**：

- **#1427（allowed-roots 级的 phantom 出口）**：`_db_free_selector_allowed_roots`
  （`services/orchestrator/retry.py:1618`）的 ENOENT 臂**直接采信非 strict 回退值**
  （`:1659`），不做二次 strict 复查。于是同一个物理目标拿到两种相反裁定——直接形态
  `<tmp>/loop_a` 被 ELOOP 臂正确拒收，phantom 形态 `<tmp>/never-created/../loop_a`
  词法折叠后指向同一个环路却静默入选，并成为 selector path 的收容基准。
  **与 CPython 版本无关**：issue 证据 3 在 3.11.14 / 3.12.12 / 3.14.2 三个解释器上
  结果一致，故 CI 3.11 与生产 3.11.15 / 3.12.7 上同样成立。

- **#1400（path 级的孪生面）**：被检查的 path 值这一级仍逐字押在 `Path.resolve()` 抛错上，
  两条 CPython 臂各坏一半。`_db_free_selector_path_rejection`（`retry.py:1665`）的
  `except OSError`（`:1684`）在 ≤3.12 接不住无 errno 的 `RuntimeError`、在 3.13+ 根本
  不产生异常，于是 `db_free_selector_path_unresolvable` 是**双臂死码**——全仓
  grep 只有产生方 `retry.py:1685` 一处，测试面零引用。

两条 issue 同族、同范式、同一腿，且 #1400 的验收面（selector path）**消费** #1427 的
产出（allowed roots），合成一个 change 一次扫完；否则先落哪一个都会让另一个的判别几何
被前一级预占。

## What Changes

四个生产站点（坐标量于基线，实现后按符号名定位）：

| 代号 | 站点 | issue | 现状 | 本 change |
|---|---|---|---|---|
| A1 | `retry.py:1618` `_db_free_selector_allowed_roots` | #1427 | ENOENT 臂 `:1659` 直采信回退值 | 加 loop-filtered admit（复用 `_local_runtime_root_safety` 范式）|
| B1 | `retry.py:1665` `_db_free_selector_path_rejection` | #1400 站点1 | `:1683` `resolve(strict=False)` + `:1684` `except OSError` | 换 strict realpath + errno 分流 + loop-filtered 回退 |
| B2 | `scheduler_config.py:1153` `_db_free_path_check` | #1400 站点2 | `:1200` `resolve(strict=False)` + `:1201` `except (OSError, RuntimeError)` | 同上；环路落 errno 归类的 `db_free_required_path_unsafe` |
| B4 | `scheduler_config.py:939` `_resolve_config_path_for_mode` db-free 臂 | #1400 AC-6 | `resolve(strict=False)` + `except (OSError, RuntimeError)` | 与同函数的 non-db-free 臂**取齐**（strict realpath → 非 strict 回退）|

外加一处 evaluate-only 项 **B3**（`scheduler_config.py:1143` `_db_free_path_identity`，
#1400 AC-5 要求显式裁定留痕）——裁定见 design D5，结论是**改**（同 B4 范式，理由是
去掉版本分歧 + 让新 lane meta-guard 无需开豁免口）。

测试面：

- 翻转 `tests/test_production_scheduler.py:17686`
  `test_tilde_residue_change_leaves_the_issue_1400_resolve_line_in_place` ——
  它是 #1436 立的**范围栅栏**，注释原文「#1400 owns this line」自带退休条件，
  本 change 正是被授权拆它者。PRESENT → ABSENT 是 oracle **增强**（见 design D6）。
- 为 db-free 归一化面补一道 `.resolve()` meta-guard。**形状按 round-2 P1-1 裁定改过**
  （初稿是「补一个 lane 成员元组」，仿 artifact 腿的 `_ARTIFACT_GUARD_LANE_FUNCTIONS`，
  `tests:15938`）：元组式守卫在「元组侧掉名」这一向失明——`lane` 由同一个元组过滤构建，
  摘名时两边同步收缩，完备性断言恒真。**实际交付的是「断言违规者」形状**——
  `test_db_free_normalization_modules_call_resolve_only_where_allowlisted` 枚举
  `retry` / `scheduler_config` 两个模块内所有仍调用 `.resolve()` 的函数，与显式 allowlist
  （`{"_safe_preserve_final_component"}`，design D5 的唯一故意保留项）比对，**没有元组可摘名**。
  范围裁定见 tasks 4.4/4.5：它把 `retry.py` 整模块钉成 `.resolve()`-free，强于两条 issue
  各自所需，系有意采纳。

## Impact

- Affected specs: `job-retry-mechanism`（**ADDED**：selector 腿两级——该腿此前无既有要求，
  全 spec 目录 grep 只有 `:1835` 的 tilde 要求提及本腿 token 家族）、
  `runtime-evidence-and-operations`（**ADDED**：scheduler_config db-free preflight / 构造层）
- Affected code: `services/orchestrator/retry.py`、`services/orchestrator/scheduler_config.py`
- 受影响的第二消费者：`services/orchestrator/file_orchestration_journal.py` 与 retry 腿
  共用同一对 `_resolve_*_candidate`，**修一处即覆盖两腿**（#1400 边界原文）。
- 不涉真实 DB、不涉 Slurm/SHUD 实机：纯文件态逻辑，本地 pytest 闭环。

## Non-Goals

- **A2 = `scheduler_config.py:1097` `_db_free_allowed_roots_and_blockers`** ——
  与 A1 同形缺陷，**本 change 明确不改**。#1427 边界原文：「同形『ENOENT → 非 strict
  回退、不复查』的兄弟副本（未 live 验证，仅代码阅读，本 issue 不修，需要时另行路由）：
  …`services/orchestrator/scheduler_config.py:1093-1096`…注意其中数处的注释把
  『回退产物包含 `<missing>/../<loop>` 形状』写成**有意为之**（如
  `scheduler_runtime_roots.py:509-512`、`scheduler_preflight.py:539-542`），与 PR #1426
  采纳的 loop-filtered admit 是两套相反教条——**家族级口径需要一次显式裁决**，不要在本
  issue 里悄悄改兄弟面。」该家族级裁决在本 PR Phase 8 经 issue-scribe 另行路由（见 tasks 5.2）。
- allowed-roots 级判定三处（PR #1399 已治）、parent 级 `resolve()` 三处（#1332 与 #1399 D5
  两次裁定 scope out）、`_local_runtime_root_safety`（PR #1426 已修）、#1423 config db-backed 臂、
  #1424 `Path.expanduser()` 抛点。
- **artifact 腿的 phantom 姿态不外扩**：`openspec/specs/job-retry-mechanism/spec.md:1484`
  那条要求把 phantom 根记作 artifact 腿的**已知残留**（「a known, recorded residual」），
  与本 change 的 recheck 口径相反且已在 **issue #1402 / PR #1422** 裁定过（该句由
  `03fdcbc7` 落入 live spec，PR #1618 未触碰该文件）；本 change 不触碰该腿，也不改那句话。
- 不改任何出口类型、不改调用方签名。
- **reason 词汇的准确口径**（fixture review P2-3 更正了初稿的过强表述）：
  - **blocker / rejection 的 code 集合不变**——`db_free_selector_path_unresolvable` 是**复活**
    既有死码，`db_free_allowed_root_unresolvable` 与 `db_free_required_path_unsafe` 均为既有 code。
  - **但 B2 的 reason 值集合确实扩大**：`db_free_required_path_unsafe` 今天只带
    `unsafe` / `traversal` / `credential_component`，改后经
    `_scheduler_root_os_error_reason`（`scheduler_runtime_roots.py:410-415`）带上 errno 归类值
    （ELOOP/ENOTDIR → `unsafe_path`，EACCES/EPERM → `not_writable`，其余 → `unavailable`）。
    这是 #1400 AC-2「errno 归类的 blocker」的字面要求，非顺带变更。
  - 兼容性已实测：`db_free_required_path_not_found` 全仓测试面零引用；现有三处
    `unsafe_path` pin（`tests:38861`、`:39852`、`:40413`）属 allowed-roots blocker，不涉 B2。
    但**既有 under-loop 用例的 reason 会从 `unsafe` 翻成 `unsafe_path`**，须刻意钉住（tasks 3.8）。
