# Proposal: artifact-guard-symlink-loop-roots (#1402)

## Why

调度器失败态本地产物守卫（artifact guard）的 containment 基准规范化押在
一条在两个 CPython 主版本上都不成立的判据上：`Path.resolve(strict=False)`
「遇 symlink 环会抛，且抛 `OSError`」。实测（issue #1402 证据 2）：≤3.12
抛**无 errno 的 `RuntimeError`**，`except (OSError, ValueError)` 接不住，
一路穿透 `_artifact_uri_missing_status` 三条 except 臂、候选构造、
`run_once` 的 `except SchedulerResourceLimitError`，**整趟调度 pass 以
traceback 中止且零 evidence 落盘**，`run_continuous` 循环退出；3.13+ 不抛
了，环路 root 被静默收编为合法 containment 基准——产物在环外误报
`local_artifact_path_outside_allowed_roots`（运维查摆放，查不到），在环下
产出 `(True, None)` 喂给清不掉文件系统故障的授权 rebuild 通道（与 #1365
`artifact_probe_error` doctrine 相悖）。#1348 同族第 9 面，输入源独立
（`resource_profile` 四键 + 三个环境变量，不读 `allowed_storage_roots`），
新语义面零测试锁（既有 outside-roots 出口另有三条回归锁，见 Impact）。

## What Changes

- `services/orchestrator/scheduler_state_failure.py` artifact-guard lane 内
  三处 `Path.resolve(strict=False)`（`:1091` path 侧、`:1120` roots 侧、
  `:1133` `_path_is_relative_to` 双侧）全部替换为
  `os.path.realpath(strict=True)` + `except OSError` + errno 分流范式
  （PR #1346/#1399 已合入同款，参考 `scheduler_preflight.py:516-560`）：
  `ENOENT` → 非 strict `os.path.realpath()` 回退（保留「根尚未创建/NFS 未
  挂载」的既有 admitted 语义）；其余 errno（ELOOP/EACCES/…）→ 按 design
  D2 的 tri-state 出口分流。
- 新增可区分 unsafe reason `local_artifact_root_unresolvable`：root 不可
  规范化且产物未被任何可解析 root 收容时产出，非空故被授权修复通道拒收
  （#1365 doctrine：rebuild 清不掉文件系统故障）。
- 既有三个 reason（`invalid_local_artifact_path` /
  `local_artifact_path_outside_allowed_roots` /
  `local_artifact_path_unresolvable`）语义与 runbook 路由不变；
  `local_artifact_path_outside_allowed_roots` 收严为「根可解析且产物确在
  外」。
- `docs/runbooks/current-production-ops.md` unsafe_reason 路由表补新 reason
  条目。
- 规格：`job-retry-mechanism` ADD Requirement（local 腿 allowed-roots 规范
  化不得依赖 symlink-loop-unsafe 解析、故障产出可区分证据、跨版本同结论、
  绝不以异常逃逸中止 pass）。

## Impact

- Affected specs: `job-retry-mechanism`（ADDED Requirement "Local Artifact
  Allowed-Roots Normalization Survives Symlink Loops"）。
- Affected code: `services/orchestrator/scheduler_state_failure.py`
  （`_local_artifact_path_is_allowed` / `_local_artifact_allowed_roots` /
  `_path_is_relative_to`——后者为模块私有，本模块内唯一消费者 `:1093`，
  lane 内自洽；全仓另有 8 份同名副本无跨模块 import，out of scope）·
  `docs/runbooks/current-production-ops.md`（路由表一行）。
- Affected tests: 既有回归锁三条（`tests/test_production_scheduler.py`
  `:12330`/`:12520`/`:12563`，钉 `local_artifact_path_outside_allowed_roots`
  出口，构造为可解析 roots + 产物在外——改造后必须保持绿，AC-4 语义
  锁）；其余 10 个既有 `symlink_loop` 用例在 preflight/allowed-roots/
  db-free 曲面不受影响；新语义面全部为新增测试，无重判面。
- 平面边界：db-free 纯本地，local pytest 即 oracle；不触 object 腿、
  `_object_manifest_is_missing`、evidence schema。#1394（`:1059`
  `path.exists()` 存在性判据——注意 `:1057` 调用行因签名改造必然被动）、
  #1400/#1401（同族其他曲面）、其他文件的 `_path_is_relative_to` 同名副
  本（8 处，issue 已登记）均 out of scope。
