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

## 4R. Round-1 评审修复(head 4293b474 的 20 项裁决,见 `.workplans/issue-1164-change2/review/round1-verdicts.md`)

- [x] 4R.1 canonical-readiness guard(A-P1-1/A-P1-2,design D3.5):`scheduler_candidates.py:775-793` 对 decision=`replay_resubmit` 候选——raw-less 腿不 block `nfs_raw_manifest_required`(replay forcing evidence 作替代证据,typed);raw-ready 腿 merge 不降 `restart_stage`/`restart_from_stage`/`fresh_ingestion`;replay 缺省 byte-identical。
- [x] 4R.2 `scheduler_replay.py:348-349` undeterminable 不折叠为 missing(A-P2-5,#1190 invariant)。
- [x] 4R.3 测试:harness readiness 参数化 + fresh-zero-row 两腿用例(A-P2-3;弃 `object_store_root` 误因);首时次用例改真实形态 `terminal_hydro_success` + bootstrap strict payload,断言覆写发生且证据存活(A-P2-4)。
- [x] 4R.4 driver 收敛 oracle 去 created_at(B-P1-1):per (model,source,valid_time=T+12h) 以 reset receipt `removed_entries` checksum 对账判"新条目"。
- [x] 4R.5 driver 首时次绑定回放序列原点 2026070500(B-P1-2,非 `cycles[0]`)。
- [x] 4R.6 键一致断言改真实 manifest 形态(B-P1-3:无 `outputs.variables`,用输出文件名集/段计数)。
- [x] 4R.7 替换 receipt 逐 cycle 原子落盘(B-P1-4)。
- [x] 4R.8 staging 结果门(A-P2-6/B-P2-5 合并):`verified is not True` → typed halt;`source_absent` 仅 REPAIR_CYCLES 豁免;reason 入 receipt schema enum。
- [x] 4R.9 完成判定 baseline=旧终态 job id(B-P2-7);frontier 枚举去界(B-P2-8)。
- [x] 4R.10 `replay_capture` reset-receipt loader 校验 `outcome=="completed"`(B-P2-11 后半:拒绝 commit_uncertain/refused)。
- [x] 4R.11 reset 锁探针递归 `.locks/<source>/<cycle>.lock`(B-P2-6)。
- [x] 4R.12 tiles:root 三分预检(C-F4→refused exit 2);全路径 receipt+显式退出码(C-F3);两者补测试。
- [x] 4R.13 runbook:tile 命令补 `--source-id hydro-national` + `--window-end 2026-07-28T00:00Z` + national digest 前提 note(C-F1/C-F2);6.0 普查 SQL+逐 chunk 调用模板+终止条件+交叉引用(C-F7);`reset-receipt.json` 文件名(B-P2-10);去 `| tee` 改重定向+rc 捕获(B-P2-11 前半);中断表补 `PIPELINE_ALREADY_ACTIVE` 处置行(A-P2-7);两相位姿态与 `--start-cycle 2026070712` 口径一致。
- [x] 4R.14 `compute.replay.env.example:61` `nhms-production`→`nhms-prod`(B-P2-9)。
- [x] 4R.15 复验:定向 pytest 绿 + ruff 绿 + openspec strict 绿。

## 4R2. Round-2 评审修复(head 9836bfe9 的 15 项裁决,见 `.workplans/issue-1164-change2/review/round2-verdicts.md`)

- [x] 4R2.1 repair 腿 guard(A2-1,design D3.5 v5):replay 窗口内 authorized-repair 候选不被 canonical 门第一腿 block,以 raw-manifest-restart **完整合并**放行(restart 降至 `convert`);窗口外 byte-identical;测试:070712 形态 5 repair + 1 override 同 pass,repair 候选 admitted 且 restart_stage=convert。
- [x] 4R2.2 raw-less 腿替代证据验证(A2-3):discovery provenance 为 replay forcing 分支 → 要求 `status=="ready"` 否则 typed block;其他 provenance 落该腿一律 typed block;guard 记录不声称不存在的证据;测试两种 provenance。
- [x] 4R2.3 `_forcing_package_probe` 循环序无关(A2-2,重开 4R.2):空包目录不立即 return,记录后继续;循环后 present > undeterminable > missing;三种排列 + shadow 用例。
- [x] 4R2.4 续跑保真(B2-1):`_load_resume_rows` 导入非 completed 行的 prior/inputs,`_pre_capture_row` 沿用(`prior_source=resumed_receipt`);`--receipt-path`==`--resume-from` 拒跑;测试:两次 attempt 后 prior 仍为原始值且旧 receipt 未销毁。
- [x] 4R2.5 receipt 写韧性(B2-2):循环前预写 `in_progress` receipt,不可写 → `receipt_path_unwritable` refused exit 2 零提交;中途写失败 → typed halt 非零;测试只读父目录。
- [x] 4R2.6 锁探针活性(B2-3,design D4 v5):NB-flock(O_RDWR 无 O_CREAT,EWOULDBLOCK=active,异常=undeterminable=active)+ 内容新鲜度辅信号;测试:持有 flock 的 stale-mtime 锁 → refuse。
- [x] 4R2.7 reset receipt scope 校验(B2-4):装载校验 `scopes[]` 覆盖本次 (source, 全部 model),不覆盖 → `reset_receipt_scope_mismatch` refuse;测试:IFS receipt 跑 GFS config → refused。
- [x] 4R2.8 cohort 形态钉住(B2-6):`terminal_completion_job_ids`/`default_journal_probe` 直接单测(model_id=None 归属、stage/status 过滤、空 job_id、去重)+ 经真 journal repo 的端到端形状测试;修 `:495` docstring。
- [x] 4R2.9 tiles 多 receipt + outcome 门(C2-1):`--from-replacement-receipt` append 并集;装载校验 `outcome=="completed"`;runbook 列全 4 receipt 真实 NFS 路径 + §5 清单改"tile invalidation ×1(全量 scope)";测试:两 receipt 并集、in_progress 拒绝。
- [x] 4R2.10 tiles 收尾三件(C2-3/C2-5/C2-6):`*_truncated` 标志 ×2;commit 独立 try → `db_commit_uncertain_after_file_unlink`+`deleted_rows:null`(迁移既有测试);`except BaseException` 出 receipt 再 re-raise;各补测试。
- [x] 4R2.11 runbook census SQL `to_char` ISO-Z 输出(C2-4)+ 去"逐字"注意语;顺手:§2.1(a) `ifs-reset-dryrun.json`→`.log`(C2-2 DISCARD 并入)。
- [x] 4R2.12 复验:定向 pytest 绿 + ruff 绿 + openspec strict 绿 + 触及共享合并点的附带回归(test_production_scheduler)绿。

## 4R3. Round-3 gate retro 纠正动作(head 4b01348b 的 7 项裁决 + 深度形态收口,见 `.workplans/issue-1164-change2/review/{round-ledger.log,review-failure-retro-r3.md}`)

- [x] 4R3.1 后装配不变量钳(retro 主动作,design D3.5 v6):`build_candidates` 全部 merge(含 post-sync 重建 `:1013`)之后统一终检强制 replay 关键键;偏差修复且记 `replay_invariant_clamp_applied`(列被覆写键与原值);replay 缺省纯 no-op。测试:A3-1 复现场景(`allow_slurm_status_sync=True`,repair 候选 sync 后)经钳恢复 convert;`replay_resubmit` 候选被人为 clobber 后经钳恢复 forecast;`slurm_state_sync` 审计证据保留(不整段跳过 `:1013` merge)。
- [x] 4R3.2 merge 点审计测试:枚举 `scheduler_candidates.py` 全部 `_merge_state_evidence`/`_candidate_with_state_evidence` 调用点,断言各点在钳上游或自带 guard;新增点按构造失败。
- [x] 4R3.3 repair 腿 fresh-zero-row 前提(A3-2):`_replay_repair_raw_restart_evidence` 开头 `_canonical_evidence_is_fresh_zero_row` 不满足 → None 落回 pre-change typed block;测试 `canonical_unavailable` 与 `no_expected_leads` 两形态。
- [x] 4R3.4 续跑语义集中化(B3-1/B3-2,design D5 v6):单一函数定行处置——resume receipt 全行结转;`status ∈ {completed, verified_skip}` ∧ 无断言失败才可 verified-skip 复核;其余重跑沿用结转 prior;receipt 顶层 `resume_from {path, sha256}`(schema 显式加属性)。测试:末位模型 drift 续跑必重跑再停(非 skip);三跳链断裂 cycle 行结转保原始 prior;`resume_from` 在场。
- [x] 4R3.5 替换 receipt mode 0644(C3-1):`replay_driver.py` receipt 写路 mode 参数化为 0o644(reset receipt 等不变);runbook §2.3.2 增 node-27 `test -r` 前置与失败处置;design 措辞已改。
- [x] 4R3.6 tiles 守卫放宽(C3-3):`if not unlinked_paths and not deletable_keys` 两处;commit 期间中断纳入;reason 命名不带 `after_file_unlink` 误义;测试 `--no-file-cache` + commit 失败 → typed receipt。
- [x] 4R3.7 文档口径(C3-2):runbook §2.3.2 + 模块 docstring 注明信号中断按信号默认退出码(SIGINT=130)、receipt 为权威记录。
- [x] 4R3.8 复验:定向 pytest 绿 + ruff 绿 + openspec strict 绿 + `test_production_scheduler` 附带回归绿。

## 4R4. Round-4 gate retro 纠正动作(head 64370e99 的 4 项裁决,消费者侧闭合,见 `.workplans/issue-1164-change2/review/{round4-verdicts.md,review-failure-retro-r4.md}`)

- [x] 4R4.1 钳保护集按消费者推导(A4-1,design D3.5 v7):`_REPLAY_RESUBMIT_CLAMPED_KEYS` 补 `decision` + `durable_shud_output_reused`,`_REPLAY_REPAIR_CLAMPED_KEYS` 补 `decision`(值仍从生效证据捕获,无第二份字面量)。**消费者 oracle**:post-sync clobber 用例断言 `decision=="replay_resubmit"` 并直接驱动 `chain_forecast_orchestrator_cycle._terminal_stage_needs_forced_resubmit`(succeeded forecast job)为 True;`retry_downstream` 形态用例断言钳把 `durable_shud_output_reused` 复位且 `scheduler_candidate_manifest` 不反转 `native_shud_resubmitted`;repair 腿断言 `retry_repair_missing_forcing` 存活。审计 allowlist 注释写明 "upstream of clamp" 只是位置断言。
- [x] 4R4.2 receipt rows 改键控映射(B4-1,design D5 v7):`_ReceiptRows` 以 resume **全部**行按原序播种,仅本 pass 实际产出的键替换;`_halt`/逐 cycle checkpoint/pre-flight 共用同一序列化(单一 owner),`carried_rows()` 静态 in-scope 排除法废止。**消费者 oracle**:三跳 halt 用例——attempt-2 全窗口跑但停在 cycle 1,断言其 receipt 仍含 attempt-1 的 cycle-2 行逐字不变,attempt-3 该行 `prior_source=="resumed_receipt"` 且 `prior.run_manifest_sha256` 等于 attempt-1 原值。runbook §3 同步 `outcome` 为**本 pass** 语义。
- [x] 4R4.3 resume receipt scope 校验(B4-2):`_resume_plan` schema 校验后,payload `source_id`(规范化)≠ config 源、或本次 `model_ids` 未被该 receipt 覆盖(覆盖判据见 4R5.1)→ `resume_receipt_scope_mismatch` 拒跑(exit 2,零提交,details 含两侧 source_id 与缺失 model);子集(窗口/模型收窄)仍放行。测试:IFS receipt 喂 GFS pass 拒跑 + 缺模型拒跑 + 收窄正例仍跑通。
- [x] 4R4.4 runbook exit-4 段落改写(C4-2,doc-only):exit 4 = **可能**已变更(删过文件**或** DELETE scope 已进事务),先看 `failure.reason` 定分支,`unlinked_file_cache_paths` 只是 `_after_file_unlink` 族的权威清单(空列表 ≠ DB 未变);补 `failure: null` + exit 4 时读 stderr `failure_reason=receipt_write_failed_after_commit`;补 dry-run / 未进删除 scope 的中断不写 receipt。附:`interrupted_delete_scope_uncertain` 分支补单测(KeyboardInterrupt 于 DELETE 已 staged、无 unlink 时:receipt 落盘、`deleted_rows: null`、`unlinked_file_cache_paths: []`、异常原样再抛)。
- [x] 4R4.5 复验:定向 pytest 绿 + ruff 绿 + openspec strict 绿 + `test_production_scheduler` 附带回归绿 + 新 oracle 对 base 的 red-proof(6 失败)。

## 4R5. Round-5 local-repair rider(head 1d790d8a 的 1 项裁决,见 `.workplans/issue-1164-change2/review/round5-verdicts.md`)

- [x] 4R5.1 resume scope 覆盖判据改运行时(B5-1,design D5 v7.1):`_assert_resume_receipt_scope_covers` 按 `rows[].model_id` ∪ 顶层 `model_ids` 判覆盖,不再单看顶层声明(声明记的是写 receipt 那个 pass 的 scope,全行结转后二者发散);source 检查原样不动(B4-2 由它独自承载);拒绝 reason/details 字段名不变。修既有缺模型 fixture:同时收窄声明**与**行,使其名副其实。**consumer oracle**:full→收窄→加回三跳,断言第三跳不被拒、真跑,且加回模型的行 `prior_source=resumed_receipt` + `prior.run_manifest_sha256` 等于首跳原值。runbook §3 去自相矛盾(收窄后加回同样续"上一份";拒跑只在真缺行或错源时发生)。
- [x] 4R5.2 复验:定向 pytest 绿(replay driver/admission/tiles/reset/warm-start)+ ruff 绿 + openspec strict 绿 + 加回用例对 1d790d8a 的 red-proof(1 失败,拒跑于旧判据)。

## 5. node-22 实机执行(执行授权后;本分支不合并,直接从分支执行——见 runbook §6,timer 保持停机)

- [ ] 5.1 部署 + replay env 落盘;`replay_state_scope_reset.py` dry-run → 人审输出 → `--enforce`(IFS 6 scope);reset receipt 归档。
- [ ] 5.2 IFS 串行回放 070500→072100(33 cycle);首时次 receipt 行确认 `init_mode=3`/`packaged_calibrated_state`/`packaged_ic_checksum`;全序列替换 receipt。
- [ ] 5.3 GFS 同序(清场→回放,070712 走 repair);替换 receipt。
- [ ] 5.4 负验证:六流域外 12 模型 run/索引条目抽样 sha256 回放前后一致,入 receipt。

## 6. node-27 实机验证(node-22 回放完成后;同样从分支执行)

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
