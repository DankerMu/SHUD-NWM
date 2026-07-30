# Design — six-basin-production-replay

## 风险分级与 fixture 级别

- Suggested fixture level: **expanded**(生产数据受控覆盖 + 调度器准入面改动 + 双端运维执行)。
- 风险轴:生产数据不可逆覆盖(最高)、调度器准入语义(高,env-gated 默认关)、双 lane 索引一致性(高)、node-27 展示正确性(中)、运维执行序(中)。
- Minimal mergeable slice:代码 + 测试 + schema + 驱动器(tasks 1-4)可独立合并;实机执行(tasks 5-7)为 merge 后运维,以 receipt 验收。

## 生产拓扑事实基座(node-22 实机,2026-07-30 两轮只读勘察)

| 事实 | 出处 |
|---|---|
| 12 scope = 6 basin × {gfs, IFS},dg model_ids 见 registry `manifest-last.json`(36 行全 active) | NFS `scheduler/registry/manifest-last.json` |
| 首业务时次 2026070500,frontier 2026072100,00/12 双时次,期望 33 cycle | NFS `runs/`/`forcing/`/journal 三面一致 |
| IFS run 33/33 全六流域;GFS 32/33(070712 缺 run+forcing,huai_main 除外);状态链 12 scope 各 33 条 usable | NFS 实测 |
| 首时次缺陷 12/12:`quality=cold_start_no_state`,`init_mode=1` | 审计 receipt sha256 `088e753b` |
| 完成探针 cohort 级:终态 job `model_id=null`,`run_id=cycle_<src>_<stamp>_convert_cohort_*`,匹配整 cycle 所有候选 | `file_orchestration_journal.py:513-538,8443-8462` |
| journal 读三面:`latest/`、`<cycle>.jsonl`(+轮转)、`pipeline-jobs/`(+`by-cycle/`);`latest/` 会在 append 时从 jsonl 重物化 | `file_orchestration_journal.py:3492-3572,5717` |
| 链 resume 只看 journal job 记录,不看对象在场性 | `chain_forecast_cycle.py:467-478` |
| discovery raw-manifest 门;raw 起 2026070712;070500..070700 五 cycle × 2 源无 raw | `scheduler_core.py:547-600`;NFS `raw/` 实测 |
| repair-missing-forcing 面:`restart_from_stage="forcing"`、需 verified warm state + verified raw manifest + direct-grid 契约 | `scheduler_candidates.py:1340-1349,1505-1600` |
| retention 内嵌 pass,`--submit` 真删,cutoff 按名字中 cycle token;`states/`+`tiles/`+published 受保护;单开关 `NHMS_RETENTION_ENABLED=false` 足以禁用 | `scheduler_runtime.py:1387-1390,1780-1818`;`retention.py:26-63,166-237` |
| `exists_any_generation` 接受任意 valid_time 的 usable 条目 → 有史即无 bootstrap | `state_manager.py:1401-1414` |
| IC 精确选择:valid_time==T、cycle_id==cycle_id_for(T−12h)、lead=12 | `state_manager.py:1150-1257` |
| 覆盖不可逆:同 run_id 原地替换删旧树;索引 upsert 丢旧条目;无版本机制;归档先例 `/ghdc/data/nwm/recovery/<op>-<ts>/`+receipt | `run_tree_copyback.py:309-351`;`state_manager.py:1072-1092` |
| 双 lane 索引:NFS(control-plane 读)+ scratch(Slurm 写),copyback merge 合流 | `compute.scheduler-dbfree.env`;`state_manager.py:1863-1902` |
| node-27 autopipe:runs/ 目录名正则发现;re-ingest 条件 = DB init_state_id ≠ manifest 或产物 mtime > MAX(created_at);parser 删除窗口 [min,max] 后 upsert | `node27_autopipeline.py:76-78,867-933`;`parser.py:741-796` |
| MVT tile 缓存 key 无 run_id/数据 checksum,无任何失效路径 | `services/tiles/mvt.py:135-149` |
| 定向驱动入口既存:`scripts/ops/node22-run-cycle-once.sh` → `plan-production --cycle-time --source --model-id --disable-backfill --plan/--submit`;`--cycle-time` 接受任意历史时次 | `cli.py:312-325,607-634,737-744` |
| timer 已停(is-active inactive,无 next),最后 pass 2026-07-30T08:45Z | systemd 实测 |

## D1 Replay 准入 = 候选级终态覆写,journal 零触碰

**决策**:新增 env 三元组 `NHMS_SCHEDULER_REPLAY_MODE`(bool,默认 false)、`NHMS_SCHEDULER_REPLAY_MODEL_IDS`(逗号分隔封闭集)、`NHMS_SCHEDULER_REPLAY_WINDOW`(`<start10>..<end10>`)。全部齐备且 pass 带 `--cycle-time` 时才生效;任何一项缺失/格式错 → replay 不生效且(若 MODE=true 而其余缺失)fail-closed 报配置错,不得静默降级为普通 pass。

**座位修正(fixture review r1,P1-1)**:生产 env(`NHMS_REQUIRE_FORECAST_WARM_START=true` + `FileOrchestrationJournalRepository` 提供 `candidate_state`)下,`completed_duplicate_pipeline` 短路(`scheduler_candidates.py:358-366`)恒不可达——975 份实机 pass 证据零出现。真正拦截回放候选的是 **strict-warm-start 终态分支**(`scheduler_candidates.py:413-502`):`state_decision=terminal_pipeline_success` 后进入 `_terminal_decision_matches_strict_warm_start`(`:1846-1886`),分流为 mismatch 分支(restart `forecast`,但受 retry budget 降级 `:2101-2113`)或 successor-retry 分支(`:435-443` → `restart_stage="state_save_qc"`——复用旧的错初值 forecast 输出重存 state,恰好把缺陷链再生)。(该段为 r1 叙述留档;触发 token 见下段 r2 修正。)

生效时对候选判定的**唯一**影响:model_id ∈ 集合 且 cycle ∈ window 且 state_decision ∈ **完成型终态族** `_STRICT_WARM_START_TERMINAL_SKIP_REASONS = {terminal_hydro_success, terminal_pipeline_success}`(`scheduler_candidates.py:67`——进入 413-502 分支的门票;**实机形态是 `terminal_hydro_success`**:六流域 run `hydro_run.status="succeeded"` ∈ `DURABLE_HYDRO_SUCCESS_STATUSES`(`scheduler_state_types.py:30`),hydro-success 分支(`scheduler_state_decision.py:186-205`)排在 `terminal_completed_cycle`(:207)与 `terminal_pipeline_success`(:226)之前,fixture review r2 R1 修正)的候选,**整体覆写**该终态分支为 replay 决策:decision token `replay_resubmit`(新,typed)、**不消费 retry budget**(旧史已烧预算不得继承,`_state_retry_attempt` 不参与;实机证据:2026072914 pass 中 36/36 候选 `strict_warm_start_retry_budget_exhausted`)、evidence 记 `replay_terminal_override`(含被覆写的原分支形态)。覆写同时压过 mismatch 与 successor-retry 两条既有分支。

**Evidence 形态(r2 R4)**:覆写点之后 `_upgrade_retry_for_strict_warm_start_manifest`(`scheduler_candidates.py:503-506`,定义 `:1940-1968`)只在 `native_shud_resubmitted is True ∧ restart_stage=="forecast"` 时原样放行,否则改标为 `strict_warm_start_retry_run_manifest_mismatch` 而丢失 token。replay evidence 必须对齐既有 mismatch retry evidence 形态(`:2154-2167`):`decision="replay_resubmit"`、`reason`(typed)、`restart_stage="forecast"`、`restart_from_stage="forecast"`、`native_shud_resubmitted=True`、`durable_output_reused=False`。

**第三个完成型 token 显式表态(r2 R1)**:`terminal_completed_cycle`(`scheduler_state_decision.py:207-208`)**不在**覆写族内——它不进 413-502 分支,直落 `:495-502` 通用 skip,replay 对它 no-op。按实机证据回放窗口 33 时次全部为 hydro-success 形态,该 token 不应出现;若出现,该 cycle 不会重投,驱动器等待超时即停(fail-closed),operator 依 receipt 中断点排查——不为理论形态扩第二个覆写缝。

非完成型终态族的 state_decision(如清场+无旧 run 的 GFS 070712 五流域)不覆写,走原判定链。集合外模型即使出现在同 pass(不应,双保险:driver 同时传 `--model-id` 过滤)也走原判定。

**chain 白名单同步(fixture review r1,P1-3)**:`chain_forecast_orchestrator_cycle.py:781-806` 对终态成功 job 强制重投要求每个 active basin 的 `state_evidence["decision"]` ∈ `_FORCE_TERMINAL_RESUBMIT_DECISIONS`(`:19-26` 封闭集)——`replay_resubmit` 必须加入该白名单(append-only),否则 resume no-op。该文件入 Impact 与测试范围。

**否决的替代**:
- journal 清场:完成记录 cohort 级(`model_id=null` 匹配整 cycle),模型级清场结构性不可能;整 cycle 清场波及 12 无关流域、销毁审计痕迹、且 `latest/` 会从 jsonl 重物化导致部分删除复活。
- 整 cohort 重跑:覆盖 12 个无关流域的正确历史,直接违反指令的"受控覆盖六流域"。
- 一次性 hack(手改 journal 后跑):不可评审、不可重复、无 fail-closed。

**审计痕迹按构造保留**:旧 journal 记录(含旧终态 job)原位不动;回放 pass 追加新记录并重物化 `latest/`;替换 receipt 引用旧 job id 而非复制。

## D2 Discovery 门的 replay 分支(raw 缺失时以 forcing readiness 准入)

**决策**:replay 生效且 pinned cycle 的 raw manifest 缺失时,cycle 准入证据改为:replay 集合内**全部**模型的 dg forcing 包(`forcing/<source>/<cycle>/basins_<b>_vbasins/dg_<hash>/`)经有界 no-follow 检查在场且非空。任一缺失 → 整 cycle 拒绝(typed reason `replay_forcing_evidence_missing`),**不回落**到普通 discovery,也不尝试 convert(raw 已不存在,convert 必败;fail-closed 优于可预见的运行时失败)。raw manifest 在场时(070712 及未来)走原门,replay 分支不介入。

适用面:070500/070512/070600/070612/070700 × 2 源(forcing 全在,NFS 实测)。GFS 070712 raw 在场走原门 + repair 面,不经此分支。

## D3 阶段重启:经 replay_resubmit 决策承载,repair 面处理 070712

**决策**:阶段重启不再作为独立机制,由 D1 的 `replay_resubmit` 决策统一承载(`restart_from_stage="forecast"` + chain 白名单成员资格)。效果:convert/forcing 旧 succeeded 记录被尊重(阶段产物复用——forcing 包即"对应历史 forcing"的字面实现),forecast + state_save_qc 以新 job 重跑。候选按 restart stage 分 cohort(`scheduler_execution.py:836-880`),`chain_forecast_orchestrator_cycle.py:793-806` 的 all-basins 合取按 cohort 求值。

GFS 2026070712(5 流域 forcing 缺、raw 在):driver 对该 cycle 切换参数组 `NHMS_SCHEDULER_REPAIR_MISSING_FORCING=1` + `_CYCLE_TIME=2026-07-07T12:00:00Z`(既有面,`restart_from_stage="forcing"`)。repair 面要求 verified warm state——回放序列到达 070712 时,070700 的回放 state(valid_time=070712)已在索引,天然满足;它与首时次 bootstrap 互斥的既有约束不构成冲突(070500 forcing 在场,不走 repair)。huai_main GFS 070712 forcing 在场,同 pass 内走 replay 常规分支;repair(`forcing`)与 replay(`forecast`)按 restart stage 天然进不同 cohort,互不干扰(fixture review r1 P3-11 已核实分 cohort 机制;保留共存测试)。

**首时次判定不改**:070500 pass 中,状态链已被 D4 清场 → `exists_any_generation=False` → 变更 1 契约自然给出 `PACKAGED_IC_BOOTSTRAP`(两级资格 tier-b object probe,12/12 已实证 qualified)→ runtime 消费包 IC(`init_mode=3`)。replay 模式对首时次分支零介入——这是本设计的核心验收点:新路径的实弹首验。注意首时次的调度形态(round-1 A-P2-4):D4 只清**状态索引**,journal 不动,故 070500 的 state_decision 仍是 `terminal_hydro_success`(`hydro_run.status="succeeded"` 留存于 journal)——覆写照常发生;"零介入"指 strict-warm-start 证据为 bootstrap payload(无 `candidate_state`,含 `packaged_ic_checksum`)且该证据穿过覆写与 `_upgrade_retry_for_strict_warm_start_manifest` 原样存活,不是 decision=None。

## D3.5 候选装配下游门:canonical-readiness fresh-zero-row 合并点(round-1 review A-P1-1/A-P1-2)

D1 覆写发生在终态分支(:413-502),但候选随后仍经 canonical-readiness 合并点(`scheduler_candidates.py:745-810`)。回放窗口内 canonical 产物已被 retention 清除(node-22 实测:scratch canonical 仅 2026071612 起,NFS 无 `canonical/`,readiness 索引 36 条全为 2026-07-20)→ `evaluate_canonical_readiness` 恒 `canonical_incomplete` 零行 → `_canonical_evidence_is_fresh_zero_row=True`,进入两条互斥腿:

- **raw-less 腿**(:783-792):`_source_raw_manifest_restart_evidence` 为 None(raw 已删且 journal 无 `manifest_uri`)时,无条件 block `nfs_raw_manifest_required`——**不看 `required` 标志**,`NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST=false` 只化解 :714-727 的早期门,拦不住这里。未设防时 070500..070700 × 2 源(10 个 cycle-pass,含首时次 packaged-IC 验收点)提交为零。
- **raw-ready 腿**(:776-782):`state_evidence.update(raw_manifest_restart)` last-writer-wins,把覆写的 `restart_stage="forecast"` 降为 `convert`(`raw_manifest_ready_without_canonical`)→ chain 按 `_STAGE_ORDER` 把已 succeeded 的 convert/forcing 也强制重投,违反 D3 阶段复用与 spec"forecast-stage restart"。raw 自 070712 起在场而 canonical 仅 071612 起 → ~18/33 cycle/源命中。

**Guard 决策(单一 guard site,两腿同修)**:当候选 decision 为 `replay_resubmit` 时:(a) raw-less 腿以 D2 的 forcing evidence 作为 raw-manifest 替代证据放行,不 block;(b) raw-ready 腿的 merge 不得覆盖 `restart_stage`/`restart_from_stage`/`fresh_ingestion` 键(或对该候选整体跳过 merge)。guard 位于合并点本地(`scheduler_candidates.py:775-793`),不在 `scheduler_replay.py`;replay 缺省时该合并点行为 byte-identical(must-preserve 3)。测试 harness 必须提供非 None 的 canonical readiness(round-1 A-P2-3:`readiness=None` stub 曾使整个门在全部候选测试中为死代码,两条 P1 腿因此漏网)。

## D4 Scoped 状态链清场(双 lane,归档先行,fail-closed)

**决策**:新 `scripts/replay_state_scope_reset.py`,输入 (model_id, source) 集合。顺序:

1. 前置拒绝(任一命中即 exit 非零,零写):timer active(systemd is-active 探测不可用时以 journal `.locks/` 目录内锁文件 mtime < 10min 判活,**探测失败视为 active**)、任一 lane 索引不可读、归档目标不可写。
2. 归档:两 lane 索引文件全量字节快照;被移除条目 JSON 清单;每条目对应 `states/...` 对象的 stat+sha256(对象缺失/不可读如实三分记录,不阻断——索引条目仍移除,receipt 记 `object_unreadable`);对象字节归档(实测 12×33 个 `state.cfg.ic`,单个 28KB-4.3MB,总量上限 ~3.4GB,归档前 `df` 检查)。目的地 `/ghdc/data/nwm/recovery/six-basin-replay-<ts>/`。
3. 移除:scratch lane 先、NFS lane 后(NFS 为准入读面,后清使"清场未完成即跑"窗口只可能表现为 bootstrap 不可达 = fail-closed 方向);每 lane 原子写回(临时文件 + rename),写回后读回校验条目数=预期。
4. receipt(schema `nhms.replay_state_scope_reset.v1`):per-lane {index_path, snapshot_path, snapshot_sha256, removed_count, removed_entries[], readback_verified};per-object {state_id, object_key, stat 三分, sha256, archived_path};全局 {scopes[], enforced, started/finished_at}。dry-run 输出同 shape(enforced=false,零写)。

错误分类承袭变更 1 不变量:每个 stat/IO 点三分(成功/确证缺失/不可判定),不可判定绝不折叠为负结果;写回后 readback 失败 → receipt 记 `commit_uncertain`(exit 3),不得报成 refused(承 #1190 invariant)。

**范围**:仅 12 dg scope。legacy `basins_*_shud` 条目(13 条,不同 model_id,不挡 dg bootstrap)不动。`states/` 对象文件不删(回放逐 cycle 原地覆盖;pre-image 已归档)。

## D5 串行驱动器与替换 receipt

**驱动器**(`scripts/ops/node22_six_basin_replay.sh` 薄壳 + `scripts/replay_driver.py` 内核):

- 输入:source(gfs|ifs)、cycle 区间(默认 2026070500..2026072100)、model 集合(默认该 source 6 dg id)、`--execute`(默认 dry-run:打印计划 + 前置校验,零提交)。
- replay env 文件(`infra/env/compute.replay.env.example` 模板):在生产 env 基础上叠加 `NHMS_RETENTION_ENABLED=false`(**驱动器启动时断言该值,否则拒跑**)、replay 三元组、其余与生产一致(§8/warm-start 门全开——回放走完整生产语义)。
- 启动时全局步骤(fixture review r1 P2-7/P3-13):
  0a. **存量现场核定**:per-scope 实际 cycle 存量(run 目录、索引条目、journal 终态)由驱动器现场枚举并写入 receipt——不沿用勘察冻结计数(勘察后 frontier 可能已动);回放终点 = 现场核定的 frontier。
  0b. **旧半 state 字段来源 = reset receipt**:清场先于逐 cycle 回放,到达 cycle N 时索引条目已移除——替换 receipt 行的旧 state 字段(state_id/checksum/created_at)一律从 reset receipt 的 `removed_entries[]` 对账引用,禁止读已清场索引(读到空即静默降级,禁止)。
- 每 cycle 步骤:
  1. 预捕获(在该 cycle 提交**之前**,旧 run 树此时仍完整):该 cycle 6 模型旧 run manifest sha256 + 输出清单 sha256 →替换 receipt 行(旧半,state 字段见 0b);旧 run 缺失(GFS 070712 五流域)如实记 `no_prior_run`。**无条件**记录该 cycle 每模型的 forcing 包 manifest checksum 与模型包 checksum(用户指令"输入 checksum"的字面承载,r1 P2-8)。
  2. 预 stage:scratch 缺该 cycle forcing 时从 NFS 复制(sha256 校验后写,receipt 记录);模型包在 `models/`(非 retention 域)无需 stage。
  3. 提交:`plan-production --cycle-time <T> --source <s> --model-id×6 --disable-backfill --submit`(单 pass 语义;`--max-passes` 仅 `--continuous` 下有意义,不传,r1 P3-12)。070712 切 repair 参数组。
  4. 等待:journal 出现 6 模型新 forecast 终态 + 新 cohort `state_save_qc` succeeded + NFS 索引出现 6 条 (valid_time=T+12h) 新条目(created_at > pass 开始);超时(默认 90min/cycle)或任一失败 → **立即停**,receipt 记录中断点,不自动重试不跳过。
  5. 后捕获:新 manifest sha256、新 state checksum、init_mode/quality(070500 行必须 `init_mode=3`+`quality=packaged_calibrated_state`+`packaged_ic_checksum` 非空,否则视为失败停机);同时断言新 run 的 `river_network_version_id` 与 variable 键集合与旧半一致(r1 P2-6,见 R3)→ receipt 行(新半)。
- 串行序:先 IFS 全序列后 GFS(或反之,单序执行,不并行两源——控变量、控 Slurm 负载、失败面清晰)。
- 全局 receipt `nhms.production_replay_replacement.v1`:rows[] 66×6 行 + per-source reset receipt 引用 + 中断/恢复记录(驱动器可从 receipt 断点续跑:已完成 cycle 经完成校验后跳过——校验=新 state 在场且 checksum 与 receipt 一致,非盲跳)。

## D6 node-27 刷新、验证与 timer 重启

0. **TimescaleDB 压缩块前置(fixture review r1,P1-5)**:回放窗口(2026-07-05..07-21)为历史区间;`parser.py:733-755` 替换 DELETE 前经 `check_batch_targets_uncompressed`(`timescale_write_guard.py:121-137`)fail-closed——任一压缩 chunk 与窗口相交即整批失败。re-ingest 之前必须:压缩块普查(受影响 run 的行所在 chunk 压缩状态)→ 命中则以既有 `scripts/node27_timeseries_decompression_replay.py` 面解压 → 普查与解压 receipt 入 Evidence Floor。(本 fixture 勘察未连 node-27,压缩现状待 6.0 实测。)
1. copyback 后 autopipe 自动 re-ingest:32/33 时次旧 manifest 带真实 `init_state_id` 而新 manifest 为回放形态 → `_ingested_run_is_current`(`node27_autopipeline.py:915-933`)的 init_state_id 分支触发;唯首时次旧 manifest `state_id=null` 落到 mtime 分支——`_run_product_mtime`(`:953-973`)看 `input/manifest.json` + 输出文件,回放必改写,亦触发(r1 P3-10 修正)。`--force` 仅作首时次行的兜底并记录。
2. tile 失效(新 `scripts/node27_invalidate_tiles.py`,dry-run 默认):hydro 图层的 `map.tile_cache.source_id` 实为 **run id**(`hydro_display.py:313-321`,deviation 1 实测认定,round-1 已裁 ACCEPT)——scope 天然精确到单个回放 run,域外流域结构性不可及,v2 的"仅按 source_id 会波及域外"论证随之失效(round-1 C-F6)。删除范围 = source_id ∈ **{回放 run_ids} ∪ {`hydro-national`}**(全国聚合层 `source_id="hydro-national"`、valid_time 非空,内容 join 六回放流域,是 `/` 默认视图,round-1 C-F1)且 valid_time ∈ [2026-07-05T00Z, **末次 cycle + FORECAST_HORIZON_HOURS(168h)= 2026-07-28T00Z**](上界取展示跨度而非状态链 lead——末次 cycle 的 168h 预报展示只有 12h 落在旧窗口内,round-1 C-F2;valid_time 窗口在 run-id scope 下已退化为下界防御,取宽不取窄)的 `map.tile_cache` 行 + 对应文件缓存条目;receipt 记删除行数。执行于 re-ingest 完成后。负验证以 DB 行断言而非缓存行断言。注:national cache-key 摘要(`mvt.py:1126-1163`)按每河网**全局最新** run 取键——本次回放安全仅因窗口终点=冻结 frontier(回放 run 即最新 run,ingest 的 `updated_at` 无条件 bump 使 key 轮换);该前提写入 runbook note,timer 重启后不成立。
3. 验证清单(live receipt,C1-C4 风格):
   - DB:12 scope × 33 cycle `hydro_run` 行 manifest 引用为新 sha256;070500 行 `init_mode=3`/`quality=packaged_calibrated_state`;
   - timeseries:parser 删除窗口为**新旧并集**(`parser.py:724-730`,r1 P2-6 修正——旧行超出新 span 也会被删,无窗口残留问题);真正的残留风险是键漂移:断言新旧 run 的 `river_network_version_id` 与 variable 键集合一致(D5 步骤 5 已前置断言,此处 DB 复核:每 (run_id, variable) 无两版本混行);
   - API/前端:display API 抽样返回新数据(对比替换 receipt 中新 manifest 的产物 checksum 链)、`https://test.nwm.ac.cn` 单图 + `/ops` 浏览器验证;
   - 负验证:六流域外任一流域 **DB 行数据** checksum 回放前后不变(缓存行见上,不作为负验证对象)。
4. timer 重启(全部 receipt 绿后):`systemctl --user start nhms-compute-scheduler.timer`;次一自然 pass 证据:frontier 前进到 072112+ backlog、回放时次被新 cohort 完成记录跳过(`completed_duplicate_pipeline`)、无 replay env 残留(生产 env 未被改动,replay env 是独立文件——驱动器退出后无进程持有)。

## Must-preserve

1. 六流域外 12 模型:run/状态/journal/展示零字节变化(负验证入 receipt)。
2. journal 只追加:全程无删改任何 journal 文件。
3. replay env 三元组默认缺省时,调度器全行为 byte-identical(含 §8、warm-start、首时次、completion、discovery、retention 全链;测试以 env 缺省断言零介入)。
4. 变更 1 契约零改动:`PACKAGED_IC_BOOTSTRAP` 判定、runtime 消费、审计工具原样。
5. `states/`/`tiles/` retention 保护、copyback merge 语义(#1190 三窄化)不动。
6. 生产 env 文件不改(replay 走独立 env 文件);timer 单位文件不改。
7. repair-missing-forcing 面语义不改(replay 仅作为参数组消费方)。

## 残余风险(具名)

- R1 双 lane 清场与 copyback 并发:timer 停机 + 驱动器单进程串行下无并发写者;若外部违规启动 pass,NFS 后清顺序保证错误方向为 bootstrap 不可达(fail-closed)。
- R2 GFS 070712 repair 与 replay 常规分支同 pass 共存(huai_main forcing 在场):分 cohort 机制已核实(`scheduler_execution.py:836-880`),风险降级;保留共存测试,driver 仍支持模型子集参数作为逃生门。
- R3 键漂移残留(取代原窗口断言,r1 P2-6):parser 删除并集窗口,无窗口残留;若新 run 的 `river_network_version_id` 或 variable 集合与旧不同,旧键下的行不在删除条件内而残留。驱动器步骤 5 + node-27 验证双重断言键一致;破则人工介入(不自动删旧键行——避免误删无关数据)。
- R4 回放结果与旧结果物理量差异显著(初值不同,本就是目的):display 侧无平滑处理,切换即生效;记录于验证 receipt,无需额外动作。
- R5 `state` (单数) 目录在 scratch root 存在(勘察见 `state/` 与 `states/` 并存):实现时确认其归属,清场工具只动 `states/` 域。

## Retry budget(fixture review r1,P1-4)

回放候选继承旧史已烧的 forecast retry 预算(`_state_retry_attempt` 读持久化 journal 的 `retry_count`/`_retry_<n>` 后缀,`scheduler_state_rows.py:425-479`);实机证据:2026072914 pass 36/36 候选 `strict_warm_start_retry_budget_exhausted`。D1 的 `replay_resubmit` 覆写**不消费预算**(在预算判定之前覆写整个终态分支)。回放期间真实失败的有界性由驱动器承担(任一失败立即停,不自动重试)——预算旁路不引入无界重试。非终态路径(070712 五流域)不受影响(无旧 forecast 史即无烧预算)。

## 修订记录

- v1(2026-07-30):初稿,基于两轮 node-22 只读勘察 + 变更 1 审计 receipt。
- v2(2026-07-30,fixture review r1 修订):P1-1 旁路座从不可达的 `completed_duplicate_pipeline` 重定位为 strict-warm-start 终态分支整体覆写(`replay_resubmit`);P1-2 覆写显式压过 successor-retry(`state_save_qc` 重启=复用错初值输出,必须消除);P1-3 chain `_FORCE_TERMINAL_RESUBMIT_DECISIONS` 白名单入 Impact;P1-4 retry budget 不消费;P1-5 node-27 压缩块前置;P2-6 R3 改键漂移断言(parser 删并集窗口);P2-7 旧半 state 字段来源=reset receipt;P2-8 forcing/模型包 checksum 无条件入 receipt;P2-9 tile 失效收窄 (source, valid_time 窗口);P3-10 re-ingest 触发修正(32/33 走 init_state_id 分支);P3-11 R2 降级(分 cohort 已核实);P3-12 去 `--max-passes`;P3-13 存量现场核定。
- v3(2026-07-30,fixture review r2 修订):R1 覆写触发 token 修正——实机形态是 `terminal_hydro_success`(hydro-success 分支先于 pipeline-success),触发条件改为完成型终态族 `_STRICT_WARM_START_TERMINAL_SKIP_REASONS`,`terminal_completed_cycle` 显式不覆写(通用 skip → 驱动器超时停);R4 evidence 形态对齐 mismatch retry(`native_shud_resubmitted=True` 等,防 `_upgrade_retry_for_strict_warm_start_manifest` 改标丢 token);R2 tasks 4.1/4.2 tile 收窄同步;R3 proposal 3/5 同步;R5 tasks 重编号;R6 proposal 作用面两处。
- v4(2026-07-30,round-1 实现评审修订,verifier 全裁决 head 4293b474):新增 D3.5——canonical-readiness fresh-zero-row 合并点是第三道生产门(A-P1-1 raw-less 腿无条件 block 不看 `required`;A-P1-2 raw-ready 腿 merge 把 restart 降为 convert),guard site `scheduler_candidates.py:775-793`,proposal 作用面两处 → 三处;D3 首时次形态澄清(journal 留存 ⇒ 070500 仍 `terminal_hydro_success` 被覆写,零介入指 bootstrap 证据原样存活,A-P2-4);D6.2 重写——deviation 1(source_id=run_id)裁 ACCEPT,scope 补 `hydro-national`(C-F1),窗口上界改末次 cycle+168h=2026-07-28T00Z(C-F2),v2 收窄论证废止(C-F6);spec 同步新增两 scenario(raw-less 存活装配、raw-ready 保持 forecast restart)并修 node-27 措辞;C-F5 由 D3.5 + spec 修订承载(candidate 级门与两相位 env 姿态入 design 正文)。
