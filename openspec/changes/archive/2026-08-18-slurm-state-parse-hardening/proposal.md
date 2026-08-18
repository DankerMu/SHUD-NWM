## Why

两条同族 sacct 解析缺口（PR #1508 cross-review 发现，均 master 既有）：

- #1509：`SLURM_STATE_MAP` 缺 `REVOKED`/`SPECIAL_EXIT` 终态键，而
  `reconcile.py:1224` 的 file-cohort task 投影是全仓唯一无 default 的读法——
  这两态的 array task 会让整 cohort 卡 `task_accounting_incomplete`
  （#1508 为 BOOT_FAIL 修掉的同形残留）。`TERMINAL_SLURM_STATES` 早已枚举两态，表间口径分叉。
- #1510：`_normalize_slurm_state` 对空/纯空白 state `split()[0]` 裸 IndexError，
  逃逸 `SlurmParseError` 契约穿透 `get_job_status`；
  `production_closure/slurm_validation.py:1480` 有独立同形副本。

## What Changes

- `SLURM_STATE_MAP` 增 `REVOKED`/`SPECIAL_EXIT` → FAILED（沿 BOOT_FAIL 注释体例，
  注明与 `map_slurm_error_code` 具名不映裁决正交——错误码侧继续落 `SLURM_JOB_FAILED`，
  design D1 不变）。
- `_normalize_slurm_state` 守空：空/纯空白 → `UNKNOWN`（与非法字符 state 同一兜底语义）；
  `slurm_validation.py` 同形副本对齐。
- 测试：两态 cohort 投影方向用例（照 BOOT_FAIL 形态）；映射网格补空串/纯空白两格；
  三条 sacct 解析腿的空 State 用例；`SLURM_STATE_MAP` ⊇ `TERMINAL_SLURM_STATES` meta 断言。

## Non-Goals

- 不改 `map_slurm_error_code` 映射结论（两态继续 `SLURM_JOB_FAILED`）。
- 不给 `reconcile.py:1224` 加 default 兜底（保留 unverified 的"没见过就停下"信号）。
- 非终态形全集审计（RESIZING/SIGNALING/STAGE_OUT）；#1462 / #1282 不相交。

## Risk triage

- Fixture level: compact。Repair intensity: medium（slurm 生产生命周期面，但改动是加法映射+守空）。
- Risk packs: Slurm production lifecycle selected（cohort 记账投影方向测试 + 网格补格）；
  test-evidence selected（meta 断言防第三次同形复发）；其余 not selected（无 IO/DB/发布面）。

## Must preserve

- `tests/test_real_slurm_gateway.py:1012-1014,1058-1062` 两态 → `SLURM_JOB_FAILED` 既有断言（D1 守卫）。
- 兄弟读法 :728/:977/real_backend:1463 的 FAILED 兜底行为不变。
- 非法字符 state → UNKNOWN 既有兜底不变。
