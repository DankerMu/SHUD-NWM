# Worklog: #259 — M23-8 q_down 解析入库 + display artifacts 发布

## Goal

把真实 SHUD 的 q_down 输出解析入库 (`hydro.hydro_run` / `hydro.river_timeseries`) 并发布
node-27 可读的 display artifacts —— 与 flood-frequency 发布解耦。

## 现状评估 (state assessment, 入场前)

已就绪 (复用,不重造):
- **parser** (`workers/output_parser/parser.py`): rivqdown 解析、单位换算 (m3/day→m3/s)、QC、
  PK upsert (`(run_id, river_network_version_id, river_segment_id, variable, valid_time)` → reparse
  幂等)、`mark_run_parsed`/`mark_run_failed` (typed failure: error_code+error_message)。
- **chain** (`services/orchestrator/chain.py`): M3_STAGES 含 download/convert/forcing/forecast/parse/
  frequency/publish 全 stage/job/event 持久化;`_display_contract`/`_publish_quality_state`/
  `_frequency_quality_state` 把 display 与 frequency readiness 分离;`_model_run_stage_evidence`
  identity 已含 run_id/source/cycle_time/model_id/basin_version_id/river_network_version_id/
  forcing_version_id/published_manifest_id;`_assembly_quality_states` 聚合 residual_blockers
  (terminal truthful)。
- **artifacts reader** (`services/artifacts/reader.py`): published://、file://、s3-allowlist URI 解析 +
  private-workspace 拒绝 (`_is_private_workspace`: /scratch、/tmp、.nhms-runs;line 542/770)。

唯一真实缺口:
- **`TilePublisher` 只做 flood-return-period** (要求 `flood.return_period_result` + status∈
  {frequency_done,published})。缺一条**不依赖 flood 的 q_down display 发布路径**。
- `tests/test_tile_publisher.py` **不存在** (verification 命令引用,需新建)。

## Boundaries (YAGNI)

- **不加 DB 列、不做 migration**。q_down manifest 作为 published-root 下 JSON artifact + 既有
  hydro_run 列 (run_manifest_uri/output_uri/status/error_*) 足够。
- **q_down 发布不修改 `hydro_run.status`** —— 让 frequency/flood 独占 'published' 转移,天然满足
  "frequency readiness 与 q_down parsed display readiness 分离" (Req2 scenario 4)。
- 不改 parser 数值/解析逻辑;不改 chain 已有 display-contract;不引入新网络抓取。
- 复用 reader 的 private-path 判定语义;不另造一套 allowlist。

## Design: `TilePublisher.publish_qdown_cycle(cycle_id)`

不依赖 flood,从 'parsed' (及更高终态) run + `hydro.river_timeseries` 的 q_down 行发布:
1. discover: hydro_run (run_type=forecast) JOIN river_timeseries (variable='q_down') by cycle lineage
   (`_cycle_filter`),聚合 segment_count / row_count / time range / units。
2. strict identity manifest: run_id, source, cycle_time, model_id, basin_version_id,
   river_network_version_id, forcing_version_id, station_count, segment_count。
3. frequency-unavailable metadata: 检测 `flood.return_period_result` 缺/空 → unavailable_products +
   residual_blockers (explicit unavailable,**不伪造** return period/warning),q_down 仍发布。
4. URI 安全: manifest/log 写到 published root → 只接受 published:// / publish-root file:// /
   allowlisted object-store URI;private workspace/scratch/非 allowlist → display-boundary blocker。
5. 写 manifest JSON + bounded log 到 object_store/published root。

## Validation matrix (Req → test)

| 场景 | 归属 | 状态 |
|------|------|------|
| parse success | test_output_parser (existing) | ✅ |
| parse mapping failure | test_output_parser (existing) | ✅ |
| duplicate terminal prevention | test_output_parser (reparse upsert) | ✅ (确认 mark_run_parsed 幂等) |
| q_down publish success | test_tile_publisher (NEW) | ⬜ |
| frequency unavailable | test_tile_publisher (NEW) | ⬜ |
| private workspace URI rejection | test_tile_publisher (NEW) | ⬜ |
| strict product identity | test_tile_publisher (NEW) | ⬜ |
| incomplete-stage aggregate status | test_orchestration_chain (确认/补) | ⬜ |

## Progress

- [x] State assessment (Explore + 亲读 publisher/reader/chain/parser)
- [x] 分支 feat/issue-259-qdown-publish
- [ ] publisher.py: publish_qdown_cycle 实现
- [ ] tests/test_tile_publisher.py 新建
- [ ] 验证: ruff + 本地 pytest + node-22 真库 pytest
- [ ] cross-review (6-pack, DB-backed/shared-root) → verify → 直到 clean
- [ ] OpenSpec tasks 6.1-6.6 勾选 + validate
- [ ] PR + CI 绿 → merge
