# Tasks — six-basin-production-replay

## 1. Scheduler replay 准入模式

- [x] 1.1 `scheduler_config.py`:replay 三元组 env 解析(MODE bool / MODEL_IDS 封闭集 / WINDOW `<start10>..<end10>`);MODE=true 而其余缺失或格式错 → 配置错 fail-closed(不静默降级);默认缺省 → 全部 None,零介入(独立可落,低风险)。
- [x] 1.2 replay 准入语义(合并原 1.2/1.4,fixture review r1 P1-1..P1-4 + r2 R1/R4):
  - `scheduler_candidates.py:413-502` strict-warm-start 终态分支整体覆写为 `replay_resubmit`(仅 model ∈ 集合 ∧ cycle ∈ window ∧ state_decision ∈ `_STRICT_WARM_START_TERMINAL_SKIP_REASONS`——**实机形态 `terminal_hydro_success`**,兼收 `terminal_pipeline_success`):压过 mismatch 与 successor-retry(`state_save_qc`)两支,不消费 retry budget,evidence 形态对齐 `:2154-2167`(`restart_stage`+`restart_from_stage="forecast"`、`native_shud_resubmitted=True`、`durable_output_reused=False`——否则被 `_upgrade_retry_for_strict_warm_start_manifest:1953-1957` 改标丢 token);
  - `chain_forecast_orchestrator_cycle.py:19-26` `_FORCE_TERMINAL_RESUBMIT_DECISIONS` append `replay_resubmit`;
  - 附实测决策形态表(**三 token 各一行**:`terminal_hydro_success` 覆写、`terminal_pipeline_success` 覆写、`terminal_completed_cycle` 不覆写→通用 skip→驱动器超时停)+ mismatch/successor-retry/budget-exhausted 覆写前后对照,入测试注释或 commit message。
- [x] 1.3 discovery replay 分支:pinned cycle 无 raw manifest 且 replay 生效时,以集合内全模型 dg forcing 包有界 no-follow 在场性作准入;任一缺失整 cycle 拒绝(typed reason `replay_forcing_evidence_missing`);raw 在场走原门。
- [x] 1.4 测试:env 缺省零介入(must-preserve 3,断言判定链 byte-identical);覆写命中 `terminal_hydro_success` 实机形态(测试形态必须用它,不得只测 `terminal_pipeline_success`);`terminal_completed_cycle` 不覆写;覆写仅命中集合内模型/window 内 cycle;successor-retry 形态被覆写(不得出现 `restart_stage="state_save_qc"`);budget-exhausted 形态被覆写(不出现 `strict_warm_start_retry_budget_exhausted`);evidence 过 `_upgrade_retry_for_strict_warm_start_manifest` 不被改标;集合外模型同 pass 原判定;forcing 缺失 fail-closed;非完成型 state_decision 不覆写;首时次分支零介入(070500 形态走变更 1 契约原样);repair 与 replay 分 cohort 共存(R2)。

## 2. Scoped 状态链清场工具

- [x] 2.1 `scripts/replay_state_scope_reset.py`:dry-run 默认 / `--enforce`;前置拒绝(timer 探活三分,探测失败视为 active;索引可读;归档可写+df);双 lane 快照+条目清单+对象 stat/sha256 三分+字节归档;scratch 先 NFS 后;原子写回+读回校验;receipt。
- [x] 2.2 schema `schemas/replay_state_scope_reset_receipt.schema.json`(`nhms.replay_state_scope_reset.v1`),含 commit_uncertain 出口(exit 3,承 #1190 invariant)。
- [x] 2.3 测试:dry-run 零写;enforce 双 lane 移除+归档完整;对象缺失/不可读三分不阻断;readback 失败 → commit_uncertain 非 refused;legacy `basins_*` 条目不动;非目标 scope 条目 byte-identical。

## 3. 串行驱动器与替换 receipt

- [x] 3.1 `scripts/replay_driver.py` + `scripts/ops/node22_six_basin_replay.sh`:dry-run 默认;RETENTION_ENABLED=false 启动断言;启动时存量现场核定(P3-13);逐 cycle 预捕获(旧 run manifest/输出 sha256 + **无条件** forcing 包与模型包 checksum,P2-8;旧 state 字段从 reset receipt `removed_entries[]` 对账,P2-7)→预 stage(NFS→scratch forcing,sha256 校验)→提交(无 `--max-passes`,P3-12)→等待(journal 终态+NFS 索引新条目)→后捕获(070500 bootstrap 强断言 + `river_network_version_id`/variable 键一致断言,P2-6);失败即停;断点续跑经完成校验;GFS 070712 repair 参数组切换。
- [x] 3.2 schema `schemas/production_replay_replacement_receipt.schema.json`(`nhms.production_replay_replacement.v1`):rows 旧半/新半、`no_prior_run`、forcing/模型包 checksum 字段、070500 行 bootstrap 强断言字段、键一致断言结果、中断记录、reset receipt 引用、存量核定快照。
- [x] 3.3 测试:receipt 行完整性(含输入 checksum 无条件在场);070500 新半非 bootstrap 形态 → 驱动器停;键漂移 → 驱动器停;等待条件(索引 created_at > pass 开始);断点续跑不盲跳;repair 参数组仅 070712;旧半 state 来源为 reset receipt(读空索引不静默降级)。
- [x] 3.4 `infra/env/compute.replay.env.example` 模板 + runbook `docs/runbooks/six-basin-replay.md`(执行序、中断处置、回滚指引=归档恢复步骤)。

## 4. node-27 失效与验证工具

- [x] 4.1 `scripts/node27_invalidate_tiles.py`:dry-run 默认;删除范围 **(source_id, valid_time ∈ 回放窗口)** 的 `map.tile_cache` 行 + 对应文件缓存条目(r2 R2);receipt(删除行数/条目数)。
- [x] 4.2 测试:dry-run 零写;窗口外同 source 行不被删;其他 source 行不被删。
- [x] 4.3 通用验证:`uv run ruff check .`;`uv run pytest -q`(定向文件);`openspec validate six-basin-production-replay --strict --no-interactive`。

## 5. node-22 实机执行(merge 后,timer 保持停机)

- [ ] 5.1 部署 + replay env 落盘;`replay_state_scope_reset.py` dry-run → 人审输出 → `--enforce`(IFS 6 scope);reset receipt 归档。
- [ ] 5.2 IFS 串行回放 070500→072100(33 cycle);首时次 receipt 行确认 `init_mode=3`/`packaged_calibrated_state`/`packaged_ic_checksum`;全序列替换 receipt。
- [ ] 5.3 GFS 同序(清场→回放,070712 走 repair);替换 receipt。
- [ ] 5.4 负验证:六流域外 12 模型 run/索引条目抽样 sha256 回放前后一致,入 receipt。

## 6. node-27 实机验证(merge 后)

- [ ] 6.0 TimescaleDB 压缩块普查(回放窗口 × 受影响 run);命中则 `scripts/node27_timeseries_decompression_replay.py` 解压;普查+解压 receipt(P1-5)。
- [ ] 6.1 确认 autopipe re-ingest 全部 12 scope × 33 cycle(32/33 走 init_state_id 分支,首时次走 mtime 分支;`--force` 仅首时次兜底并记录);ingest receipt。
- [ ] 6.2 tile 失效执行(scoped:source × 回放窗口 valid_time)+ receipt。
- [ ] 6.3 live 验证:DB 断言(070500 行 bootstrap 形态、manifest sha256 对账替换 receipt)、timeseries 键一致断言(R3:`river_network_version_id`/variable 无两版本混行)、display API + `https://test.nwm.ac.cn` + `/ops` 浏览器 e2e、负验证(其余流域 DB 行不变);live receipt 回贴 #1164。

## 7. 业务化恢复

- [ ] 7.1 timer 重启;次一自然 pass 证据:frontier 前进、回放时次 `completed_duplicate_pipeline` 正常跳过、无 replay env 泄漏;证据回贴 #1164 并关闭 issue。

## Evidence Floor

1. 定向 pytest 绿(replay 模式/清场/驱动器/失效工具测试文件)。
2. `uv run ruff check .` 绿。
3. `openspec validate six-basin-production-replay --strict --no-interactive` 绿。
4. must-preserve 3 的零介入断言测试(env 缺省判定链 byte-identical)在案。
5. reset receipt ×2(IFS/GFS,含归档路径与 sha256)。
6. 替换 receipt(66 cycle-pass 行,12 首时次行 bootstrap 形态,输入 checksum 无条件在场)。
7. node-27 压缩块普查/解压 receipt(6.0)。
8. node-27 live receipt(DB+API+前端+负验证)回贴 #1164。
9. timer 重启后次一 pass 证据回贴 #1164。
