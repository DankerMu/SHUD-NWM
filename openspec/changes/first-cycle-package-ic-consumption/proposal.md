# Proposal: first-cycle-package-ic-consumption

## Why

Closes #1164(变更 1/2;变更 2 为六流域生产回放,依赖本变更)。新注册流域的首时次运行静默冷启动,不消费模型包内标定好的 `*.cfg.ic`:2026-07-05 同批上线的 6 个流域(dth_ls/dth_zj/hhe/huai_main/jialingjiang/lh_gl)× GFS/IFS 共 12 个首轮任务全部踩中,SHUD 全状态变量清零起算,spin-up 失真延续至今(实机证据:`fcst_gfs_2026070500_basins_dth_ls_shud` manifest `quality=cold_start_no_state`/`init_mode=1`,而其 variant 包内 cfg.ic 128KB 非零;18 个 variant 包全部携带非零 IC)。

机制(A1,实机验证):`workers/shud_runtime/runtime.py:842` 的包内 IC 消费门要求 `runtime.init_mode == "3"`,而 `services/orchestrator/chain_manifests.py:460` 只在 `init_state_id/uri` 非空(即 state index 已有 state)时才设 3——首时次恒 1 → SHUD `MD_initialize.cpp:30-45` 清零。该分支结构性不可达、零测试、唯一调用方是手工脚本 `scripts/create_qhh_shud_manifest.py`。现行 spec(`openspec/specs/forecast-warm-start/spec.md:17-34`)甚至把静默冷启动写成了规格。

## What Changes

1. **首时次决策契约**(选择层):generation-aware 路径 `exists_any_generation=False` 时不再无条件 `COLD_NEW_MODEL`——gate 层做**包 IC 资格判定**(机器可判:包 manifest `included_files` 含 `*.cfg.ic` 条目且 `sha256` ≠ 空文件摘要、`size_bytes>0`)并注入纯 evaluator:合格 → 新决策 `PACKAGED_IC_BOOTSTRAP`(强制消费);不合格或 manifest 不可读 → fail-closed block(typed reason);无注册包引用(legacy/gate bypass)→ 保持现行 labeled cold start(显式 carve-out)。禁止一切未记录原因的静默 cold-start fallback。显式 cold-start 审批机制**不在本变更**(YAGNI,follow-up 承接)。
2. **runtime 消费管道打通并收紧**:manifest 声明 `quality=packaged_calibrated_state`(scheduler 产出与 legacy 手工两种形态)时,staging 必须找到非空且 header 可解析的包内 cfg.ic 并 consume-or-raise;带 `packaged_ic_checksum` 时端到端 sha256 比对;任何失败 fail-closed(新错误码 `PACKAGED_IC_CONSUMPTION_FAILED`),**不得**自动降级冷启动,分支不得 fall-through。
3. **运载与证据面**:cold-seed 通道(`chain_forecast_cycle`)扩入新 evidence mode `db_free_packaged_ic_bootstrap` 并写 basin 标记(strict/非 strict 双模式同契约);run manifest `initial_state.quality=packaged_calibrated_state` + `packaged_ic_checksum`;journal `hydro_run` 行新增 quality 字段;M24 receipt quality 枚举扩 `packaged_calibrated_state`。`init_state_id` 保持 None(不进 cohort identity map,不扰动 #1183/#1184 语义)。
4. **只读存量审计工具**(新 script):逐已注册流域×source 比对"包内 IC 资格"与"首个业务 run 的实际决策证据",列出"有合格 IC 但首时次冷启动"的存量,schema 化 receipt;必须能复现 6 个存量流域,零写。
5. spec 修订:`forecast-warm-start` 静默冷启动场景收窄 + 新增首时次决策/fail-closed 消费/审计三条 requirement + `Init State Validation`/`Run Manifest Init State Fields` 适配。

## Impact

- Affected specs: `forecast-warm-start`(修订)+ 新 `first-cycle-initial-state` requirement(并入 forecast-warm-start delta)
- Affected code: `services/orchestrator/scheduler_generation.py`(决策枚举 + 纯 evaluator 新参数)、`scheduler_generation_gate.py`(资格判定 IO + evidence)、`chain_forecast_cycle.py`(cold-seed 集合扩展 + basin 标记)、`chain_manifests.py`(init_mode/quality 推导)、`workers/shud_runtime/runtime.py`(两个 packaged 分支统一 consume-or-raise + 残差 helper 抽取)、`services/m24_live/receipt.py`(枚举)、`services/orchestrator/file_orchestration_journal.py`(quality 字段)、`scripts/`(新审计工具)、tests
- 不动:warm_continue 选择与三方时间一致性、#982/#1081 state-clone 语义与错误码、有历史后的合法 cold-start 回退路径(stale/lineage/QC/对象损坏——对象损坏回退仅限 state-snapshot 来源运行)、cohort `init_state_identities` 语义、direct-grid fingerprint gate、包发布格式(资格判定用既有 manifest `sha256`/`size_bytes` 字段,不加新发布字段)、`tests/test_scheduler_generation.py` 直方图基线(信号缺席 carve-out 保证零重基线)
- 生产协调:node-22 timer 已停(用户指令),本变更合并部署后由变更 2(六流域回放)消费,回放完成并经 node-27 display 验证后恢复 timer
