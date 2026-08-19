# Design: shud-attempt-csv-and-checkpoint-alignment (#1491 + #1317)

Fixture level: **expanded**
Project profile: SHUD（NHMS/NWM 双端，node-22 计算 / node-27 数据+display）

## Change surface

- `workers/shud_runtime/runtime.py`
  - `_stage_standard_shud_forcing` 拷贝循环 `:1104-1120`（Lane A 删除点）
  - `_validate_direct_grid_station_filename_target` `:3208-3224`（Lane A：
    **力争一字不动**；仅当机制 (ii) 需要才改签名，届时
    `tests/test_shud_runtime.py:3239` 的直接调用点跟着改）
  - `_recover_missing_state_checkpoints` `:767-920`（Lane B 半(a) cfg 注入）
- `services/orchestrator/chain_manifests.py` `:486` 与 `:643`（Lane B 半(b)，
  **两份兄弟副本**）
- `tests/test_shud_runtime.py`、`tests/test_warm_start.py`

## Must preserve

- **本次 attempt 已 staging 的同名文件仍 fail-closed**（model package 成员等），
  其字节不改写 —— round-0 审证明这是第二分支今天真正保护的东西。
- 保留名 fail-closed 一字不变，但**在 helper 层**（`.csv` 门使其在 staging
  层不可达，e2e 侧仍是 `DIRECT_GRID_STATION_FILENAME_INVALID`）。
- 同一行集内重复 filename 仍 fail-closed。
- 非 direct-grid staging 分支（`:1121`）行为逐字不变。
- 默认 cycle 配置 `0,12` 与等间距 `0,6,12,18` 行为逐字不变。
- recovery rerun 既有契约：per-hour outcome 记账、共享 deadline、一小时失败
  不中止其余小时/不跳过 `write_manifest`/不改调用方
  `STATE_CHECKPOINTS_MISSING`；主 run cfg 在 rerun 后被恢复。

## Must add/change

- Lane A：**按来源/时序锚定**的 station-CSV 定向删除（no-follow +
  containment）——只清早于本次 staging 的残留，本次 attempt 自己 staging 的
  东西仍 fail-closed；机制（提前读行集 vs 记录本次 staging 路径集）由实现者
  裁定。删除失败复用 `DIRECT_GRID_FORCING_RESIDUE_CLEANUP_FAILED`。
- Lane B：(a) recovery rerun cfg 注入 `Update_IC_STEP = hour*60`；
  (b) manifest 两处产地加 `hour*60 % step != 0` 的 typed fail-closed preflight。

## Seams under test

- `prepare_workspace(manifest, input_dir)` 连续两次调用（Lane A 的唯一红证
  载体；公共方法边界，编排者已在 master 实测红形）。
- `_recover_missing_state_checkpoints` **写出的 cfg 文本**（Lane B 半(a) 的
  可观测面——断言文本而非行为副作用）。
- `chain_manifests.py` 两处 manifest 组装的返回/抛出（Lane B 半(b)），
  由 `NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC` 驱动。

## Selected risk packs

- **File IO / path safety / overwrite**：新增的是**删除**动作 —— no-follow、
  containment 于 `model_input_dir`、不跟随 symlink 删到别处、**只删早于本次
  staging 的声明目标残留**；且**所有删除必须先于任何 station CSV 拷贝**。
- **Error handling / rollback / partial outputs**：Lane A typed 删除失败码
  （不复用 collision 码）；Lane B typed preflight 码；均不得搅浑
  `STATE_CHECKPOINTS_MISSING` 语义。
- **Config / project setup**：Lane B 行为由 `NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC`
  驱动，默认值行为必须逐字不变。
- **Legacy compatibility / examples**：既有 collision e2e 用例钉的是
  `DIRECT_GRID_STATION_FILENAME_INVALID`（`.csv` 门先截断），**不拆、不改断言**；
  真正的 legacy 风险是「本次 attempt 已 staging 的同名文件」从 fail-closed
  退化为覆盖写。

## Risk packs considered (core)

- Public API / CLI / script entry: **not selected** — 无入口签名变化。
- Config / project setup: **selected** — 见上。
- File IO / path safety / overwrite: **selected** — 见上。
- Schema / columns / units / field names: **not selected** — preflight 只拒绝，
  不新增/改名任何 manifest 字段。
- Auth / permissions / secrets: **not selected** — 无涉。
- Concurrency / shared state / ordering: **not selected** — `input/<project>/`
  确是跨 attempt 共享可变状态（#1491 的病根），但同 `run_id` 单写者由编排器
  保证（沿 #1355 先例）；recovery rerun 串行逐小时。理由是**单写者假设**，
  不是「该目录不共享」。
- Resource limits / large input / discovery: **not selected** — 删除量与
  station 数同阶，无新扫描面。
- Legacy compatibility / examples: **selected** — 见上。
- Error handling / rollback / partial outputs: **selected** — 见上。
- Release / packaging / dependency compatibility: **not selected** — 无依赖变更。
- Documentation / migration notes: **not selected** — 无对外迁移语义。

## Domain packs

- **SHUD 数值运行时 / conservation / NaN: selected** — `Update_IC_STEP` 是求解器
  restart cadence；注入必须只影响该次 scratch rerun，不得污染主 run cfg。
- **Run manifest / QC provenance: selected** — manifest 是契约面，两份兄弟
  副本必须同改。
- Geospatial / CRS / basin geometry: not selected — 不动几何。
- Hydro-met 时间序列 / forcing 窗口: not selected — 不改 forcing 内容或窗口，
  只改 staging 起点卫生。
- PostGIS / TimescaleDB: not selected — 无 DB 面。
- Slurm 生命周期 / mock-vs-real parity: not selected — 不动 sbatch/调度；
  recovery rerun 是既有本地子进程路径。
- 外部气象源 / snapshot 可复现: not selected — 无涉。
- Published NHMS artifacts / display identity: not selected — 无涉。

## Required evidence

- `prepare_workspace` 连跑两次（同 run_id/同 manifest）→ 第二次成功，
  `input/<project>/` 每个 station CSV 内容 == 本次 staging 产物
  （pre-fix 逐字红：`DIRECT_GRID_STATION_FILENAME_COLLISION | ... : forcing.csv`）。
- model package 携带同名成员 → 仍抛 collision 码，其字节未改写（可构造，
  审者实测 pre-fix 即红）。
- 保留名 → 仍抛 collision 码（**helper 层**断言；staging 层是
  `..._INVALID`）。
- 同一行集内两行同名 → 仍 fail-closed。
- 残留 CSV 删除失败（只读目录/unlink 抛错）→
  `DIRECT_GRID_FORCING_RESIDUE_CLEANUP_FAILED` 终止，**无部分 staging 产物**。
- recovery rerun 写出的 cfg 文本含 `Update_IC_STEP = hour*60`
  （构造须使 pre-fix 值缺失或 ≠ `hour*60`；断言由 stub 在 rerun 进行中捕获）；
  rerun 后主 run cfg 文本逐字回到 rerun 前。
- `NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC="0,5"` / `"0,5,12"` 驱动
  `chain_manifests.py` **两处**产地 → typed 拒绝（pre-fix 静默放行为红）。
- `"0,12"` / `"0,6,12,18"` → 行为逐字不变。

## Invariant Matrix

- **Governing invariant**：一次 SHUD attempt 的**起点是它自己声明的东西**——
  staging 只保证「本次声明的成员就位」（此前留下的同名产物不构成冲突），
  而 restart cadence 必须使**每一个被声明的 checkpoint 小时在结构上可达**
  （`hour*60 % Update_IC_STEP == 0`）。
- Source-of-truth identity/contract：本次 `.tsd.forc` 的行集（Lane A）；
  manifest `runtime.state_checkpoint_hours` × `runtime.update_ic_step_minutes`
  （Lane B）。
- Producers：`_stage_standard_shud_forcing`（:1104-1120）；
  `chain_manifests.py:486` / `:643`；`_forecast_state_checkpoint_hours`
  （`chain_manifest_contracts.py:418`）。
- Validators/preflight：`_validate_direct_grid_station_filename_target`
  （:3208）；Lane B 新增的对齐 preflight（两处产地）。
- Storage/cache/query：`input/<project>/` 工作区（跨 attempt 复用，
  `_model_input_dir`:2881 恒定）；recovery scratch
  `workspace/state_checkpoint_recovery/f<hhh>/`。
- Public routes/entrypoints：`SHUDRuntime.prepare_workspace` /
  `.execute` / `.run_shud`。
- Frontend/downstream consumers：**none** —— 两条车道均不产出对外 artifact。
- Failure paths/rollback/stale state：`retry.py:27-39` `TRANSIENT_ERROR_CODES`
  （collision 码与新 typed 码**均不入内**）；`STATE_CHECKPOINTS_MISSING`
  （:740）；recovery per-hour outcome 记账。
- Evidence/audit/readiness：recovery rerun 的 per-hour outcome、
  `state_checkpoint_recovery_f<hhh>.{out,err}.log`。
- Regression rows：
  - 同 run_id 第二次 `prepare_workspace`（合法重试）→ 成功，CSV 为本次产物。
  - model package 携带与声明 station CSV 同名的成员（越界输入，**staging 层
    可构造**）→ 稳定 collision 码，`input/<project>/` 里那份已 staging 的
    拷贝字节不变。
  - 保留名目标（`{project}.sp.att` 等）→ 稳定 collision 码，
    **仅 helper 层**可构造（staging 层先抛 `..._INVALID`）。
  - 非 direct-grid staging（未改动的兄弟消费者）→ 行为逐字不变。
  - `NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC="0,12"`（默认）→ 行为逐字不变。
  - `="0,5"`（越界配置）→ 稳定 typed 拒绝，不产 manifest。

## Non-goals

见 proposal「Non-Goals」：不动保留名分支、不动 #1355/#1330 的卫生面、
不动非 direct-grid 分支、不做 `input` quarantine-and-recreate、不动 #1315/#1316
机制、不动 SHUD C++ `PrintInit`、不动 analysis 侧 `_analysis_update_ic_step_minutes`。

## Review focus

1. Lane A 的删除是否**只清早于本次 staging 的残留**、no-follow/containment
   不可绕过；有没有制造出「删掉别人文件」的新越界面。
2. **本次 attempt 已 staging 的同名文件是否仍 fail-closed**——这是本 change
   最容易被顺手放宽的地方，round-0 审已证明按名字锚定会静默毁掉它。
   保留名分支是否一字未动。
3. 同一行集内重复 filename 是否仍 fail-closed（非受信输入面）。
4. Lane B 半(a) 的 `Update_IC_STEP` 注入是否**只作用于 scratch rerun**、
   主 run cfg 恢复契约是否仍成立。
5. Lane B 半(b) 是否**两处产地都改且都被独立测试驱动**（单点覆盖 = 漏网）。
