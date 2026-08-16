# Proposal: runtime-root-safety-symlink-loop (#1401)

## Why

`_local_runtime_root_safety`（`services/orchestrator/retry.py:1456-1464`）是
retry 提交面**唯一**的本地 runtime root 安全判据，其产物既参与
workspace/object-store 重叠裁决（`:1410-1417`）、又直接进入提交给 Slurm
的 manifest（`:1406` → `:743-747`）。它的 symlink 环判据押在「非 strict
`Path.resolve()` 遇环抛 `OSError`」上——两条 CPython 臂都不成立（issue
#1401 证据 2/3/4，实测钉死）：

- **3.13+**：不抛，环路 root 被静默收编为合法比较基准并写进提交
  manifest；两个指向同一目标的环路别名比较**不相等**，重叠守卫
  （`resolves_to_workspace_dir`）静默漏判。`unresolvable_local_root` 出口
  成死码（全仓仅 `:1464` 一处定义，零引用零测试，证据 5）。
- **≤3.12（生产臂 3.11.15/3.12.7）**：抛无 errno `RuntimeError`，
  `except OSError` 接不住，逃逸至两条腿的宽 `except Exception`
  （`retry.py:551-559` DB 腿 / `file_orchestration_journal.py` db-free 日
  志腿）——**不是**崩溃，是归因与证据降级：本该走
  `_RetryRuntimeRootResolutionError(RETRY_RUNTIME_ROOTS_UNSAFE)` 携完整
  evidence 的结构化出口，实际回落 `SBATCH_SUBMISSION_FAILED`
  （`_retry_submission_error_code:1167-1173` 无 `.code` 可取），且
  `details["runtime_root_resolution"]` 整块缺席。

#1348 家族第 4 面（#1332 → #1345 → #1347 → #1348/PR #1399 → 本 issue），
在 allowed-roots 曲面之外；**本族唯一 fail-open 值会流进提交 manifest 的
站点**。

## What Changes

- `_local_runtime_root_safety` 换用家族合入范式（PR #1346/#1349/#1399/
  #1422 同款）：`os.path.realpath(expanded, strict=True)` + `except` +
  errno 分流——`ENOENT` → 非 strict 回退 + **loop-filtered 复查**（回退
  值再 strict 解析，仅二次 ENOENT 或干净解析可 admit；`"<missing>/../
  <loop>"` phantom 形态两臂拒收，消除照搬 #1402 残留裁决会引入的 ≤3.12
  fail-closed→fail-open 退化，见 design D0/P1-1）；其余 errno
  （ELOOP/EACCES/ESTALE/ENOTDIR）→ `(None, "unresolvable_local_root")`
  ——**全版本 admit→reject 扩面**（两臂旧行为均静默收编，≤3.12 唯一抛
  型是 ELOOP；与同族 `scheduler_runtime_roots.py:410-416` blocker 判据一
  致），走既有 `unsafe_rejected → RETRY_RUNTIME_ROOTS_UNSAFE` 接线
  （`:1400-1403`/`:767-772`，下游零改动）。
- 签名 `(str | None, str)` 与全部既有 reason 字符串不变；
  `parent_traversal_local_root`/`relative_local_root` 分支不动。
- 同 helper 内 `:1457` `Path(value).expanduser()` 换 `os.path.expanduser`
  （具名边界 rider，见 design D0：#1424 同族抛点、在 issue in-scope 行区
  间 `:1456-1464` 内、PR #1422 root 侧同款处置；`~<未知用户>` 形态从
  RuntimeError 逃逸变为 fail-closed `relative_local_root`）。
- 规格：`job-retry-mechanism` ADD Requirement（runtime-root 规范化不依赖
  symlink-loop-unsafe 解析、跨版本同判决、规范化故障不以异常逃逸、环/权
  限故障 root 拒进 manifest 与重叠基准）。

## Impact

- Affected specs: `job-retry-mechanism`（ADDED Requirement "Retry
  Runtime-Root Safety Survives Symlink Loops"）。
- Affected code: `services/orchestrator/retry.py` `_local_runtime_root_safety`
  一处（模块私有；唯一调用点 `:1399` `_resolve_runtime_root_candidate`；
  db-free 日志腿经 `file_orchestration_journal.py:89` import 同函数，修一
  处覆盖两腿；`published_artifact_root` 同批受益）。
- Affected tests: `tests/test_retry.py`（62 个既有用例全部必须保持绿：
  `:1229` 是 UNSAFE parent-traversal 锁，`:929`/`:974`/`:1270` 是重叠守
  卫锁；`unresolvable_local_root` 出口零既有覆盖，全为新增）+
  `tests/test_file_orchestration_journal.py`（db-free 日志腿新增同形断
  言；UNRESOLVED 供体 `:3802`/`:3985`）。
- 平面边界：db-free 纯本地，local pytest 即 oracle。Out of scope：
  allowed-roots 级三处（PR #1399 已治）、path 级两处 +
  `_db_free_path_identity`（#1400，`:1562` 区域）、parent 级三处
  （#1332/D5 两次裁定 scope out）、重叠守卫自身语义（#1192）、
  `retry.py:1524` `Path(text).expanduser()`（allowed-roots lane 内的
  #1424 同族抛点，解析已由 #1399 治理、expanduser 抛点另路由）。
