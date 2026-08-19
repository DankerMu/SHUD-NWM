# Proposal: shud-attempt-csv-and-checkpoint-alignment (#1491 + #1317)

## Why

两单同域（SHUD run 的 attempt 生命周期卫生：一个治 staging 起点，一个治
restart cadence 的结构可达性），合批处理（用户裁定「剩余 issue 可以合并的
合并处理」）。**两者都是确定性缺陷、都有确定性红**，本 change **不设**
De-batch 出口条款（无 investigation 车道）。

**#1491（direct-grid staging，不可重试地锁死 run_id）**：
`_validate_direct_grid_station_filename_target`
（`workers/shud_runtime/runtime.py:3208-3224`）的**第二个分支**拒绝目标路径上
**任何**既有普通文件，与「保留文件名」分支同码同错误消息。而
`prepare_workspace`（:557-583）的 attempt 起点卫生只清 packaged IC，
`input/<project>/` 下上一次 attempt 写的 station CSV 原样留着；`run_id`
确定性（`chain_forecast_state.py:85`）、重试原样继承 `run_id`
（`retry.py:979/:1008`）、且 `DIRECT_GRID_STATION_FILENAME_COLLISION`
**不在** `TRANSIENT_ERROR_CODES`（`retry.py:27-39`）——于是任何 direct-grid
run 只要有一次 attempt 跑过 station-CSV 拷贝（:1113）之后才失败，该 run_id
的后续每一次 attempt 都永久卡死，只能人工进 node-22 删文件。尤其恶劣的是
前置失败可以是**可重试**的（`SLURM_TIMEOUT` 等），编排器按设计自动重试，
重试却撞上一个不可重试的码，把瞬时故障升级为需人工介入的永久故障。

**编排者在当前 master（`094caea4`）实跑复现**（只读探针，未改仓库；
`tests.test_shud_runtime` 的既有 fixture 连跑两次 `prepare_workspace`）：

```
PASS1_STAGED_CSVS ['forcing.csv', 'forcing_002.csv', 'forcing_003.csv']
PASS2_CODE DIRECT_GRID_STATION_FILENAME_COLLISION | ... collides with a staged
           model/runtime file: forcing.csv
```

即 **#1355（PR #1490，已合并）的 declared-member 卫生确实不覆盖本缺陷**——
成员集合不相交（`SHUD_FORCING_INDEX_MEMBERS` vs `.tsd.forc` 行 filename），
issue 的「两者不重叠」在 master 上仍成立。

**#1317（checkpoint 与 `Update_IC_STEP` 无对齐守卫）**：SHUD 只在整除时写
restart（`SHUD/src/ModelData/MD_update.cpp:226-229`
`if (t_long % CS.UpdateICStep) return 0;`），跑到 END 也走同一个
`PrintInit`，所以「跑到 END 就一定落盘」不成立。而 manifest 侧唯一的对齐来源是
`chain_manifests.py:486` 与 `:643` 同样的一行
`update_ic_step_minutes = min(checkpoint_hours) * 60`，checkpoint 小时集合由
`NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC` 的相邻 cycle 间隔集导出
（`chain_manifest_contracts.py:418`）。非等间距配置下会产生互不整除的间隔：
issue 实测 `"0,5,12"` → hours `[5,7,12]` / step 300，3 个里 2 个（含 T+12
这个 warm-start 主力）**结构上永远不可能落盘**；穷举含 0 的 2~4 元配置
2047 个里 939 个（46%）misaligned，最小反例 `"0,5"`。当前生产配置 `0,12`
恰好对齐——是运气不是设计。

失败表现是 `STATE_CHECKPOINTS_MISSING` 硬失败，与「solver 坏了 / 采样竞速输了
（#1315）」在现场证据上不可区分；且 #1316 合入后每 cycle 还会为这个注定失败
的小时白烧一次 timeout-bounded recovery rerun。行为 fail-safe（不产坏状态、
不静默错误暖启动），故 P2。

**编排者已核实的实现现状**（`094caea4`）：
`_recover_missing_state_checkpoints`（:767-920）是**逐小时** rerun
（`for hour in checkpoint_tracker.missing_hours():`，每小时独立 END + 独立
scratch dir），只写 `END`/`END_TIME`/`OUTPUT_DIR`，**不写** `Update_IC_STEP`
——issue 的半(a) 处方仍未落地，且因为是逐小时，注入 `hour*60` 即恰好整除，
**不需要 gcd**（这条是本 change 的一个明确 seam，见下）。

## What Changes

### Lane A（#1491）：按**来源/时序**锚定的 station-CSV 卫生

**不变量（本车道唯一要钉的东西）**：只有**早于本次 attempt staging** 的同名
残留可被清除；**本次 attempt 自己 staging 出来的东西**（model package /
forcing package / IC 成员）落在声明的 station CSV 目标路径上时，**仍必须
fail-closed**。

round-0 审推翻了本 proposal 初稿的「行集名字集合」锚定，理由（编排者已独立
复核）：

- 谓词唯一的生产调用点是 `runtime.py:1113`，`target_csv = model_input_dir /
  filename` —— 目标路径上的文件按定义**名字就在行集里**，所以「不在本次行集
  内的既有文件」这个条件在调用点上**结构上恒假**，按名字收窄 = 删掉该分支。
- 而该分支今天保护的是真实场景：`prepare_workspace` 先
  `_stage_artifact(model_package)`（:572）、再 staging forcing 包（:573-577）、
  最后才 `_prepare_shud_project_forcing`（:582）走 CSV 拷贝 —— **本次 attempt
  刚 staging 的 model package 成员**若与声明的 station CSV 同名，今天是
  fail-closed 的（审者实测：往 model package 放一个 `forcing.csv` →
  `DIRECT_GRID_STATION_FILENAME_COLLISION`）。按名字锚定会把它变成静默覆盖写。

因此：

- 残留清理**按不变量落地，机制留给实现**。已核可行的两条路（择一即可，
  实现者裁定并记进 tasks）：(i) 把行集从 object store 侧提前读出
  （`_prepare_forcing_package_context`:1166-1190 纯读，checksum 已验），
  在 model package staging 之前做起点卫生；(ii) 记录**本次 attempt staging
  产生的路径集合**，谓词只对该集合 fail-closed。
- **谓词 `_validate_direct_grid_station_filename_target`（:3208-3224）力争
  一字不动**；若不变量要求它改签名，`tests/test_shud_runtime.py:3239` 的直接
  调用点必须跟着改（round-0 审点名的未列站点）。
- 保留名分支（`{project}.sp.att` / `.cfg.ic` 等）**一字不动**。注意
  round-0 审核实的层级事实：`_direct_grid_station_filename`（:3134-3150）要求
  filename 以 `.csv` 结尾，而保留名无一以 `.csv` 结尾，**保留名分支从
  staging 路径结构上不可达**（先被 `DIRECT_GRID_STATION_FILENAME_INVALID`
  截断，既有 e2e 测试 `tests/test_shud_runtime.py:3301` 断言的正是这个码）。
  故该分支只在 **helper 单测层**可测。
- **同一行集内重复 filename 仍 fail-closed**：`_read_shud_forcing_station_rows`
  （:3104-3122）不查重，`.tsd.forc` 是 object-store 来的非受信输入；今天两行
  同名由 collision 门挡下，修复后不得退化为静默 last-write-wins。
- 删除失败**不降级**：**复用**既有码 `DIRECT_GRID_FORCING_RESIDUE_CLEANUP_FAILED`
  （`runtime.py:1978`，#1355 引入，同一函数族/同一删除原语/同一 retry 语义，
  且已有「不入 `TRANSIENT_ERROR_CODES`」的测试托底
  `tests/test_shud_runtime.py:1946`）。**不新铸**同类第二个码。
- **不**把 `DIRECT_GRID_STATION_FILENAME_COLLISION` 加进 `TRANSIENT_ERROR_CODES`
  （拿重试掩盖脏工作区，且重试也不会清理）；**不**做 `input` 全目录无差别清空。

### Lane B（#1317）：checkpoint 与 `Update_IC_STEP` 的对齐成为结构保证

两半都做，缺一不可（只做半(a) 等于把配置错误长期转化为每 cycle 一次额外
SHUD 全量 rerun 的算力税，且诊断里持续出现「莫名其妙的 miss」误导运维）：

- **(a) recovery rerun 侧局部自洽**：在 `_recover_missing_state_checkpoints`
  改写 `END`/`OUTPUT_DIR` 的同一段里，加
  `_replace_or_append(content, "Update_IC_STEP", str(hour * 60), separator=separator)`。
  因为该循环是**逐小时**的，`hour*60` 对本次 rerun 的 END 恒整除，
  「recovery 一定能兑现请求的小时」成为**局部正确**的保证，与 manifest 配置解耦。
- **(b) manifest 侧前置门**：对 `hour*60 % update_ic_step_minutes != 0` 的组合
  以**稳定 typed error code 拒绝**（fail-closed）。
  `chain_manifests.py:486` 与 `:643` 是**同一模式的两份兄弟副本**，
  **必须两处一起**，否则留一条漏网路径。

**(b) 的设计分叉已在此裁定**：选 **fail-closed 拒绝**，**不选**「把 step 自动
降到各小时的 gcd 后放行」。理由：typed-error 拒绝符合仓内既有惯例（#1164
先例，Lane A 亦沿用），而 gcd 自动纠正会**静默改变 IC 写盘 cadence**——那是
把一个运行时行为变更伪装成校验混进来，且改变了 SHUD 的写盘频率与 IO 量。
gcd 方案作为「考虑过并否决」的备选记录在此。

## Non-Goals

- 不动保留名分支的 fail-closed 语义（`{project}.sp.att` 等）。
- 不动 #1355 的 station-index 成员卫生（成员集合不相交）、不动 #1330 的
  `output` 侧卫生。
- 不动非 direct-grid staging 分支（`runtime.py:1121` `_copy_staged_file_no_follow`
  本就覆盖写）。
- 不把 `input/<project>` 改成 `output` 那样的 quarantine-and-recreate（issue
  的备选方案）：能根除整个跨 attempt 残留缺陷家族，但每次 attempt 需在 NFS 上
  重拷全量 model package + forcing 包，失去「复用 staging 加速重试」的现有性质，
  规模 S → M+。**声明不做**，如需另立。
- 不动 #1315 的轮询竞速本身、不动 #1316 的 recovery 机制设计、不动
  `NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC` 的取值策略。
- 不动 SHUD C++ 侧 `PrintInit` 语义（上游模型代码）。
- analysis 侧 `chain_manifests.py:693-696` / `_analysis_update_ic_step_minutes`
  （:729）**不设** `state_checkpoint_hours`，不在本缺陷面内；**声明不改**，
  但如将来加 checkpoint 需同样约束（写进 spec 场景的边界句）。

## Risk triage

- Fixture level: **expanded**（两条缺陷车道，跨 `workers/shud_runtime` 与
  `services/orchestrator` 两个模块；Lane A 动的是一条 fail-closed 安全门的
  谓词语义，Lane B 动的是 manifest 契约 + SHUD cfg 注入）。
  Repair intensity: medium。
- Selected risk packs:
  - **File IO / path safety / overwrite**（Lane A：新增的是**删除**动作，
    必须 no-follow + containment 约束、不得越出 `model_input_dir`、不得跟随
    symlink 删到别处；删除失败必须 fail-loud typed 码而非两分支降级）。
  - **Error handling / rollback / partial outputs**（Lane A 的 typed 删除失败码
    与 attempt 终止；Lane B 的 preflight 稳定 error code；两者都不得把
    `STATE_CHECKPOINTS_MISSING` / 既有码的语义搅浑）。
  - **Config / project setup**（Lane B：行为由
    `NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC` 驱动，默认 `0,12` 必须逐字不变）。
  - **Legacy compatibility / examples**（Lane A：既有 e2e 用例
    `test_runtime_direct_grid_station_filename_collision_fails_without_overwriting_sp_att`
    钉的是 `DIRECT_GRID_STATION_FILENAME_INVALID`（`.csv` 门先截断），
    **不是** collision 码——round-0 审已纠正初稿的误读；本 change **不拆它**，
    只在谓词签名变动时跟着改传参。真正的 legacy 风险在别处：修复不得让
    「本次 attempt 已 staging 的同名文件」从 fail-closed 退化为覆盖写）。
  - **SHUD 数值运行时 / conservation**（domain，Lane B：`Update_IC_STEP` 是
    SHUD 求解器的 restart cadence，rerun 侧注入必须只影响该次 scratch rerun，
    不得污染主 run 已发布的 cfg——注意 :860 附近的 inner try/finally 恢复契约）。
  - **Run manifest / QC provenance**（domain，Lane B：manifest 是契约面，
    两份兄弟副本必须同改）。
- Not selected：
  - Public API / CLI / script entry — 无入口签名变化。
  - Schema / columns / units / field names — 无 schema 变更（Lane B 的
    preflight 只拒绝，不新增字段）。
  - Auth / permissions / secrets — 无涉。
  - **Concurrency / shared state / ordering — not selected**：`input/<project>/`
    确实是**跨 attempt 共享的可变状态**（这正是 #1491 的病根），但同一
    `run_id` 的单写者由编排器保证（沿 #1355 先例）；recovery rerun 串行逐小时。
    故不选——理由是「单写者假设」，**不是**「该目录不共享」（round-0 审纠正）。
  - Resource limits / large input / discovery — 删除量与 station 数同阶，
    无新扫描面。
  - Release / packaging / dependency compatibility — 无依赖变更。
  - Documentation / migration notes — 无对外迁移语义。
  - Geospatial / CRS、Hydro-met 时间序列窗口、PostGIS/Timescale、Slurm 生命周期、
    外部气象源、Published artifacts / display identity — 均无涉。

## Must preserve

- **本次 attempt 已 staging 的同名文件仍 fail-closed**（本车道最容易被顺手放宽
  的地方）：model package 携带一个与声明 station CSV 同名的成员时，仍抛
  `DIRECT_GRID_STATION_FILENAME_COLLISION`，其字节不变。
- **保留名 fail-closed 一字不变**（helper 层）：直接调
  `_validate_direct_grid_station_filename_target` 传入保留名目标时仍抛
  `DIRECT_GRID_STATION_FILENAME_COLLISION`；staging 层该输入仍先被
  `DIRECT_GRID_STATION_FILENAME_INVALID` 截断（既有 e2e 断言不变）。
- 同一行集内重复 filename 仍 fail-closed，不得退化为静默 last-write-wins。
- 非 direct-grid staging 路径行为逐字不变。
- **默认 cycle 配置（`0,12`）与等间距配置（`0,6,12,18`）行为逐字不变**：
  `tests/test_warm_start.py:162-184` 与 `tests/test_shud_runtime.py` 相关用例
  全绿零断言改动（**无例外**——tasks A.7 已裁定既有 collision 用例
  **不拆分、不改断言**）。
- recovery rerun 的既有契约不变：per-hour outcome 记账（`recovered` /
  `timeout` / `gate_rejected(...)` 等）、共享 deadline 预算、
  「一小时失败不得中止其余小时、不得跳过 `write_manifest`、不得改掉调用方的
  `STATE_CHECKPOINTS_MISSING` 错误码」。
- 主 run 已发布的 cfg 在 rerun 后必须被恢复（既有 inner try/finally 契约）。

## Seams under test

- **Lane A 红证（确定性）**：同一 `run_id` / 同一 manifest 连跑两次
  `prepare_workspace` —— pre-fix 第二次抛
  `DIRECT_GRID_STATION_FILENAME_COLLISION`（编排者已在 master 实测，红形逐字
  在 Why 段），post-fix 第二次成功且 `input/<project>/` 每个 station CSV
  内容等于**本次** staging 产物。
- Lane A 边界缝：(i) **model package 携带同名成员** → 仍 fail-closed、其字节
  不变（**这是替换掉初稿那条不可构造场景的可测缝**，审者已实测 pre-fix 红形
  `DIRECT_GRID_STATION_FILENAME_COLLISION ... : forcing.csv`）；
  (ii) 保留名仍拒（**helper 层**直接调谓词，staging 层不可达）；
  (iii) 同一行集内两行同名 → 仍 fail-closed；
  (iv) 残留 CSV 删除失败（只读目录 / unlink 抛错）→
  `DIRECT_GRID_FORCING_RESIDUE_CLEANUP_FAILED` 终止 attempt，不静默继续。
- **Lane B 红证（确定性）**：
  - (a) 断言 recovery rerun **写出的 cfg 文本**含 `Update_IC_STEP` = 该次 rerun
    的目标分钟数；**该 seam 已核实为逐小时循环**，故断言用 `hour*60`，不引 gcd。
    round-0 审补的两条约束（缺一红证失效）：**(i)** 构造必须保证 pre-fix cfg 的
    `Update_IC_STEP` **缺失或 ≠ `hour*60`** —— `generate_cfg_para`（:619-624）
    在 project 模式下已按 manifest 写入该键，若用 `hours=[12]`/step 720 恢复
    hour=12，pre-fix 文本恰好相同、**红不了**（例如改用 hours `[6,12]` 恢复 12）；
    **(ii)** 断言必须在 rerun **进行中**由 stub solver 捕获 cfg 文本
    （既有 stub 从 `sys.argv[1]` 读 cfg，见 `tests/test_shud_runtime.py:4486-4517`）
    —— cfg 在 inner `finally`（:884-893）被逐字还原，rerun 之后再读只看得到原文。
  - (b) 用 `NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC="0,5"`（最小反例，
    hours `[5,19]` / step 300，`19*60 % 300 = 240`）与 `"0,5,12"` 驱动
    `chain_manifests.py` **两处**产地，断言以稳定 typed error code 拒绝；
    pre-fix 该断言必红（当前静默产出结构上不可达的 checkpoint 小时）。
- 主 run cfg 恢复：rerun 后原 cfg 文本逐字回到 rerun 前（防止 (a) 的注入泄漏
  到主 run）。

## Evidence mapping

- #1491 验收 5 条 anchor → Lane A 红证 + 四条边界缝用例。
- #1317 验收 4 条 → Lane B (a) cfg 文本断言 + (b) 两处产地的 preflight 用例 +
  `"0,5"` / `"0,5,12"` 回归驱动 + 默认/等间距配置零回归。
- Verification：`uv run pytest -q tests/test_shud_runtime.py tests/test_warm_start.py
  tests/test_orchestration_chain.py` + `uv run ruff check .` +
  `openspec validate shud-attempt-csv-and-checkpoint-alignment --strict --no-interactive`；
  merge 后 node-27 receipt（**直接在 `umask 022` 下跑**——见 #1513，默认 umask
  会有 80 条 file-provider 预置红淹掉真实回归）。
