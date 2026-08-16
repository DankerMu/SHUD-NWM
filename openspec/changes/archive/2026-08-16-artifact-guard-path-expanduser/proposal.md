# Proposal: artifact-guard-path-expanduser (#1424)

## Why

失败态本地产物守卫 `_local_artifact_path`
（`services/orchestrator/scheduler_state_failure.py:1083-1089`）的两处
`Path.expanduser()` 押注「路径规范化只抛 `OSError`/`ValueError`」，而
CPython 在**无法确定家目录**时（`~<不存在的用户>/...`，或 uid 无 passwd
条目且 `HOME` 未设时的普通 `~/...`）抛**无 errno 的 `RuntimeError`**
（3.11.14 / 3.14.2 双臂 issue 实测一致）。调用点 `:1054-1063` 的
`except (OSError, ValueError)` 接不住；`_looks_like_local_uri_or_path`
（`:1079-1080`）又显式把 `~` 开头的值接纳进这条腿。逃逸链与 #1402 逐帧
同构（issue 证据 3，AST 静态核对链上无宽 except）：`RuntimeError` 一路
逃出 `run_once` → 整趟 pass traceback 中止 → 零 evidence 落盘 → 同批邻
座候选全不被调度 → `run_continuous` 循环退出（daemon 停摆）。root 侧孪
生已被 PR #1422 用 `_realpath_or_none`（首行 `os.path.expanduser`，不
抛）修掉，path 侧被单独留下，同一 lane 两侧对同一 `~unknown` 输入行为
不一致。

## What Changes

- `_local_artifact_path` 两处 `~` 展开改用 `os.path.expanduser(...)` 再
  包 `Path(...)`，与同文件 root 侧 `_realpath_or_none:1112` 写法逐字一
  致。`os.path.expanduser` 对未知用户/无家目录**原样返回**输入串——
  `~unknown/...` 成为普通相对路径，继续走既有
  `_local_artifact_path_is_allowed` containment 判定，落到既有
  fail-closed reason（预期 `local_artifact_path_outside_allowed_roots`；
  以 task 0 探针实测终态为准）。
- except 臂 `:1062` **不扩宽**（issue 备选被拒：`RuntimeError` 宽类型会
  把未来任何缺陷静默塌缩成 `local_artifact_path_unresolvable`，且保留
  两侧不一致的根问题）。
- 钉测扩面（**receiver 判别式**，非 attr 名全禁——fixture review F1：
  `.resolve()` 钉测靠 attr 名在 lane 内彻底消失成立，本修法保留
  `expanduser` attr 名只换 receiver，attr 名全禁会在正确修复码上红）：
  lane 内 `expanduser` 调用**当且仅当** receiver 形如
  `os.path.expanduser`（AST：`Attribute(value=Attribute(value=Name('os'),
  attr='path'), attr='expanduser')`）才允许；任何其他 receiver 的
  `.expanduser()`（即 `<Path 表达式>.expanduser()`）一律禁止。既有
  `_realpath_or_none:1112` 的 `os.path.expanduser` 在该判据下合法。
- 不动任何 reason 码语义之外的决策逻辑；可解析 `~` 的正常形态行为逐字
  节不变（`os.path.expanduser` 与 `Path.expanduser` 成功臂语义一致）。

## Out of scope

- `Path.resolve()` symlink 环面（#1402 / PR #1422 已修）。
- `_artifact_uri_missing_status` 目录 fail-open（#1394）。
- copyback redaction placeholder（#1367）。
- 兄弟副本 `scheduler_preflight.py:534/:587`（输入源是配置不是 state
  artifact uri，触发面不同；#1424 issue 已登记留待 triage，本 change 不
  修——若实现中发现该副本被本 lane 测试牵连则停下重裁）。

## Impact

- Affected specs: job-retry-mechanism（ADDED 1 requirement）
- Affected code: `services/orchestrator/scheduler_state_failure.py`
  （2 行核心），`tests/test_production_scheduler.py`（新用例 + 钉测扩面）
- 共用探针的三条腿（sidecar `:520` / forcing tier `:600` / copyback
  `:644`）一处修复全覆盖。
