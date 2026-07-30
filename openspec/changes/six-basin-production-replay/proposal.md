# Six-Basin Production Replay(变更 2/2,#1164)

## Why

变更 1(PR #1194)落地了首时次 packaged-IC 消费契约,并以 node-22 实机审计 receipt(sha256 `088e753b`,2026-07-30T16:31:19Z)确证缺陷普查:dth_ls、dth_zj、hhe、huai_main、jialingjiang、lh_gl 六流域 × GFS/IFS 共 12 个 dg scope 的首业务时次(2026070500)全部 `cold_start_with_qualified_ic`——包内标定 IC 合格却被静默冷启动,此后 33 个时次的整条状态链、当前续跑基线与 node-27 展示数据全部承袭错误初值。

用户指令(2026-07-30):对六流域 GFS、IFS 执行正式生产回放——从各自首个业务 cycle 起,使用对应历史 forcing、校准参数和经验证的 package IC 启动,按完整 checkpoint 串行重跑至最新时次(timer 停机冻结基线 = 2026072100);回放结果受控覆盖历史运行、状态链和当前续跑基线;保留被替换版本的可追溯证据、输入 checksum、运行 ID 与替换记录;完成后在 node-27 验证 ingest/display 均为最新回放结果;随后重启 node-22 业务化服务。

现状阻塞(node-22 实机核实,两轮只读勘察):

1. **完成探针 cohort 级**:`state_save_qc` 终态记录 `model_id=null`,按 `run_id` 前缀匹配整个 cycle 的全部 18 模型(`file_orchestration_journal.py:8443-8462`)——无法通过 journal 做模型级 un-complete;删 cohort 记录会波及 12 个无关流域且销毁审计痕迹。
2. **discovery raw-manifest 门**:`NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST=true` 下 cycle 准入要求 `raw/<source>/<cycle>/manifest.json`(`scheduler_core.py:547-600`);raw 仅存 2026070712 起,070500/070512/070600/070612/070700 × 2 源今日不可达——但这些 cycle 的 per-basin dg forcing 包在 NFS 完整在案。
3. **首时次判定被历史状态挡住**:`exists_any_generation` 对 (model, source) 接受任意 valid_time 的 usable 条目(`state_manager.py:1401-1414`)——12 个 scope 各有 33 条 usable 索引条目,不清场则 `PACKAGED_IC_BOOTSTRAP` 不可达。
4. **retention 内嵌于每个 scheduler pass**(`scheduler_runtime.py:1387-1390,1780-1818`),`--submit` 即真删,cutoff 按目录名中的 cycle token 判定——回放产出的旧时次 scratch 产物会被同 pass 或下一 pass 自毁。
5. **覆盖不可逆**:`deterministic_run_uri` ⇒ 同 run_id 原地替换并删除旧树;索引 upsert 丢弃同 identity key 旧条目;全仓无版本/tombstone 机制——被替换版本证据必须在回放前显式归档。
6. **node-27 MVT tile 缓存无 run 级失效**(`services/tiles/mvt.py:135-149`,全仓无 `DELETE FROM map.tile_cache`):re-ingest 后同 valid_time 的旧瓦片会继续从缓存返回。
7. **GFS 2026070712**:5/6 流域 run 与 forcing 均缺(仅 huai_main 有),但 raw manifest 在——需经既有 `--repair-missing-forcing` 修复面从 raw 重导 forcing。

## What Changes

1. **Scheduler replay 准入模式**(env-gated,默认关,行为零变化):`NHMS_SCHEDULER_REPLAY_MODE` + `NHMS_SCHEDULER_REPLAY_MODEL_IDS`(封闭集)+ `NHMS_SCHEDULER_REPLAY_WINDOW`(cycle 上下界)。生效仅当三者齐备且 pass 以 `--cycle-time` pin 单时次。作用面恰两处(r2 R6):
   a. 候选级终态覆写:仅对 model_id ∈ replay 集合、cycle ∈ window 且 state_decision ∈ 完成型终态族 `_STRICT_WARM_START_TERMINAL_SKIP_REASONS`(实机形态 `terminal_hydro_success`,兼收 `terminal_pipeline_success`;`terminal_completed_cycle` 显式不覆写)的候选,把 strict-warm-start 终态分支(`scheduler_candidates.py:413-502`;含 mismatch 与 successor-retry 两支——后者会以旧的错初值 forecast 输出重存 state,必须压过)整体覆写为 `replay_resubmit` 决策:restart forecast、不消费 retry budget(旧史已烧预算不得继承)、evidence 形态对齐既有 mismatch retry(含 `native_shud_resubmitted=True`,防下游改标丢 token);`replay_resubmit` 加入 chain `_FORCE_TERMINAL_RESUBMIT_DECISIONS` 白名单(`chain_forecast_orchestrator_cycle.py:19-26`,append-only)。journal 全程零触碰,旧记录按构造保留为替换审计痕迹。
   b. discovery raw-manifest 门的 replay 分支:pinned cycle 无 raw manifest 时,改以"replay 集合内全部模型的 dg forcing 包 readiness"作为该 cycle 的准入证据(任一缺失即整 cycle fail-closed 拒绝,理由 typed);raw manifest 在场时走原门不变。
   (forcing 包缺失时不在 replay 模式内处理——由既有 `--repair-missing-forcing` 面承担(GFS 2026070712 专用,raw manifest 已核实在场;与 replay 候选按 restart stage 分 cohort 天然隔离)。
2. **Scoped 状态链清场工具**(新 `scripts/replay_state_scope_reset.py`,dry-run 默认,`--enforce` 显式):按 (model_id, source) 集合在**双 lane**(NFS `NHMS_SCHEDULER_STATE_INDEX` + scratch `NHMS_SLURM_SCHEDULER_STATE_INDEX`)移除索引条目;先归档(两 lane 索引全量快照 + 被移除条目清单 + 每个被替换 `state.cfg.ic` 对象的 sha256 与字节归档)至 `/ghdc/data/nwm/recovery/six-basin-replay-<ts>/`,产出 schema 化 receipt;timer active 或 journal 锁新鲜时 fail-closed 拒绝。
3. **替换可追溯 receipt**(新 schema `nhms.production_replay_replacement.v1`):按 (model_id, source, cycle) 记录旧 {run manifest sha256、输出清单 sha256、state 条目(state_id/checksum/created_at,**来源为 reset receipt 的 removed_entries**——清场先于回放,禁止读已清场索引静默降级)、journal 终态 job id} 与新 {manifest sha256、state checksum、init_mode、quality、首时次 packaged_ic_checksum、键一致断言结果},并**无条件**记录每行的 forcing 包与模型包 checksum(用户指令"输入 checksum"),含 replaced_at 与 replay pass id;回放前捕获步骤 + 回放后回填,由驱动器自动维护。
4. **串行回放驱动器**(新 `scripts/ops/node22_six_basin_replay.sh` + python 内核):按 source 分序——先清场该 source 的 6 scope,再 070500→072100 逐 cycle:预 stage NFS forcing→scratch(retention 已删的旧 cycle)、以 replay env(**`NHMS_RETENTION_ENABLED=false` 强制**)调 `plan-production --cycle-time --model-id×6 --submit --disable-backfill`、等待 6 模型 cohort journal 终态 + NFS 索引出现 (valid_time=T+12h) 新条目后才进入下一 cycle;任一失败即停(fail-closed,不自动跳过);GFS 序列在 070712 处切换 repair-missing-forcing 参数组。
5. **node-27 刷新与验证**:回放产物经既有 copyback 与 autopipe 自动 re-ingest(manifest `init_state_id`/mtime 变化触发);re-ingest 前先做 TimescaleDB 压缩块普查、命中则以既有 decompression-replay 面解压(parser 替换 DELETE 对压缩 chunk fail-closed);新增 scoped MVT tile 缓存失效脚本(`scripts/node27_invalidate_tiles.py`:删 (source_id, valid_time ∈ 回放窗口) 的 `map.tile_cache` 行 + 对应文件缓存条目,dry-run 默认);验证清单:12 scope 首时次 `hydro_run` 显示 `quality=packaged_calibrated_state`/`init_mode=3`、timeseries 键一致断言(parser 删除并集窗口,无窗口残留;残留风险在键漂移)、display API + `https://test.nwm.ac.cn` live receipt。
6. **timer 重启**(全部验证绿后):`systemctl --user start nhms-compute-scheduler.timer`,并以次一 pass 证据确认:frontier 正常前进、回放时次被新 cohort 完成记录正常跳过、无历史重触发。

## Non-Goals

- 不修改 journal 任何文件(不删、不改写、不搬移)——终态覆写在候选判定层,不在存储层。
- 不触碰六流域外 12 个模型的 run/状态/journal/展示数据。
- 不修改首时次 packaged-IC 判定逻辑(变更 1 契约原样消费)。
- 不引入通用回放框架/审批机制;replay 模式为封闭集合 + 窗口 + 显式 env 的一次性运维面,默认关闭。
- 不处理 2026072100 之后的 backlog 时次(timer 重启后由业务化调度自然消化)。
- 不改 retention 语义(仅回放 env 关闭开关)。

## Impact

- Affected specs: 新 capability `production-replay`(ADDED);`forecast-warm-start` 不改(消费方)。
- Affected code: `services/orchestrator/scheduler_candidates.py`(终态覆写座 + retry budget 旁路)、`services/orchestrator/chain_forecast_orchestrator_cycle.py`(`_FORCE_TERMINAL_RESUBMIT_DECISIONS` 白名单 append)、`services/orchestrator/scheduler_core.py` 或 discovery 邻接(raw-manifest replay 分支)、`services/orchestrator/scheduler_config.py`(3 个 env)、新脚本 ×3、新 schema ×2(reset receipt + replacement receipt)、runbook。node-27 侧:re-ingest 前压缩块普查/解压走既有脚本,无新代码面(tile 失效脚本除外)。
- 生产影响:回放期间 timer 保持停机;六流域历史数据被受控覆盖(归档先行);其余 12 流域零扰动;node-27 展示切换到回放结果。
