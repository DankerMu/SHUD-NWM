# Proposal: forcing 证据读侧三档回落——"缺行"与"缺产物"可区分（#1203）

## Why

Issue #1203：DB-free 化后 `forcing_version` 记录只存在于 forcing producer 进程内 dict 与 object-store sidecar（`forcing/<source>/<cycle>/<basin_version>/<model>/forcing_version_record.json`），**从未**写入 file-orchestration journal；journal 写面（`ensure_forecast_cycle`/`create_hydro_run`/`upsert_pipeline_job`/`insert_pipeline_event`，`services/orchestrator/file_orchestration_journal.py:1177/:1235/:1428/:3246`）没有 forcing 对应项，生产代码零写入 `<journal_root>/forcing/**`。而失败态复判只认 journal 行：

1. 读侧口径分裂：`find_forcing_context` 在 `rows.forcing_version is None` 时回落直读 `<root>/forcing/<source>/<cycle>/<model>.json`（`:775-785` → `_forcing_context` `:5476-5492`），但 `candidate_state` 不回落，`forcing_version=rows.forcing_version` 原样交出（`:746` → `chain_repository_state.py:828`）。
2. 判定点把"缺行"当"缺产物"：`_missing_upstream_forecast_artifact_evidence`（`services/orchestrator/scheduler_state_failure.py:329-415`）空 URI 分支（`:353-363`）直接 `missing_forcing_package_uri`/`FORCING_PACKAGE_URI_MISSING`；存在性探针 `_artifact_uri_missing_status`（`:501-518`）只在 URI 非空时被调用——"包物理在场"这一事实在该腿上零观测机会。
3. 实机后果（#1164 六流域 replay，node-22 live）：任何候选级预报失败后每条恢复腿都被伪 `missing_forcing_package_uri` 拦死，只能人工 quarantine 失败行解锁（quarantine 同时抹掉失败证据）。

**基线核实（HEAD b0974496，explorer 逐点复核）**：直读档 flat path 生产端无写入方（测试端有 `_direct_forcing_context_record` helper 及 `test_production_scheduler.py:28257-28259` 内联播种），live 仅有一个人工播种 cycle 目录；sidecar 是唯一有真实写入方的档位（`workers/forcing_producer/file_store.py:656-665`，best-effort）；**producer 的 `forcing_package_uri` 是目录形 URI，过不了既有探针的 `validate_object_path`（5 段拒绝）——sidecar 档探针对象必须是 record 内的 package manifest 文件 key**（fixture 复审 P1-1，见 design 基线）；`_artifact_state_containers`（`scheduler_state_failure.py:471-494`）已含 `state["forcing_version"]`——一旦物化该容器，`_first_artifact_uri` 即命中并进入既有探针路径。issue 引文两处失实：`six-basin-production-replay` change 在仓库不存在（open/archive 均无）；#874 无 inline test pin（仅 openspec 文档引用），#1160 pins 在 `tests/test_production_scheduler.py:8765-9061`。

## What Changes

采纳 issue 推荐方案（读侧回落，改动面小、可回滚）：

1. **candidate_state 直读回落对齐**（`file_orchestration_journal.py:746`）：`rows.forcing_version is None` 时复用 `_forcing_context` 同源直读档，物化进 `state["forcing_version"]` 并标 `forcing_version_source="direct"`（journal 行命中标 `"journal"`）；两条读路对同一 `(source, cycle, model)` 口径一致。两档全空维持 `None`（不伪造）。
2. **判定点 sidecar 第三档**（`scheduler_state_failure.py` 空 URI 分支）：block 前按候选身份推导 sidecar key（`forcing/<source>/<cycle>/<basin_version>/<model>/forcing_version_record.json`，与 producer 写侧 `producer.py:1970` 同构），经既有 `LocalObjectStore` 口径（`resource_profile`/env root）`read_bytes_limited` 限量读取解析 `forcing_package_uri`：
   - sidecar 命中 → 以 record 见证的 **manifest 文件 key** 进既有 `_artifact_uri_missing_status` 探针（目录形 package_uri 绝不直接进探针）：在场 → **不 block**（恢复腿推进）；缺失 → 既有 `missing_forcing_package_uri`（#874/#1160 真缺 fail-closed 语义原样保留）。
   - sidecar 缺失/不可读/畸形/root 未配置/`basin_version_id` 为空 → 新 reason **`forcing_version_row_absent`**（"无法判定"，error code 与 stable classifier `FORCING_VERSION_ROW_ABSENT`），与"判定为缺"可区分，同样 fail-closed block、同结构 repair-eligible。
3. **repair 通道扩集**（`scheduler_candidates.py:728-754` echo、`:1412/:1443/:1519` reason 判、`:1452` stable_classifier 判、`:1432` 兜底）：single-cycle repair 授权通道接受两组 reason/classifier 对（repair 即重产 forcing，幂等，修复后 sidecar 在场、下轮判定自然恢复）；decision token 与 reason 1:1 配对（新增 `blocked_forcing_version_row_absent`）。runbook exact-cycle repair 前置条件同步扩集（`current-production-ops.md:1480-1481`）。
4. **evidence 档位可读**（AC-4）：state 层物化标注 `forcing_version_source`（journal/direct，D1）；决策 evidence 携带 `forcing_provenance.source ∈ {journal, direct, object_store_sidecar, absent}`（D2 出口，B8 四值钉）。
5. **兄弟副本裁定不改行为**：`chain_forecast_state.py:110`/`chain_analysis.py:54` 的合成 fallback URI 是**提交路径**为新生产构造前瞻 URI，与恢复路径需要"有见证的 URI"语义不同——判定保留，理由记 design D4。
6. **规格**：`job-retry-mechanism` MODIFIED（missing-upstream demotion requirement：null-provenance 场景细分 + sidecar 见证档 + repair 双 reason 接受）+ ADDED（forcing 证据读档对齐与来源标注 requirement）。

## Non-Goals

- 写侧物化（journal `upsert_forcing_version` 写面 + 历史回填迁移）——issue 备选，改动面/风险大且救不了存量 cycle，不采纳（理由见 design 备选否决）。
- `missing_forcing_package_uri` 真缺产物时的 fail-closed 语义变更（#874/#1160 裁定，必须保留）。
- #1164 replay campaign 执行与临时处置（quarantine 等）；DB 模式路径。
- 在 node-22 实机**制造**候选级失败的 live drill（生产 replay 窗口侵入性操作；决策逻辑由注入测试覆盖，实机以只读几何 receipt 佐证，偏离记录见 design D6）。

## Impact

- Affected specs: `job-retry-mechanism`（MODIFIED ×1 + ADDED ×1）。
- Affected code: `services/orchestrator/file_orchestration_journal.py`（candidate_state 回落）、`services/orchestrator/scheduler_state_failure.py`（sidecar 档 + reason 细分）、`services/orchestrator/scheduler_candidates.py`（repair 扩集 + echo 路径）、`chain_repository_state.py`（source 标注透传，若需）、`docs/runbooks/current-production-ops.md:1480-1481`（repair 前置条件扩集）；测试 `tests/test_file_orchestration_journal.py`、`tests/test_production_scheduler.py`、`tests/test_gateway_reconcile.py`（B6 名单）。
- 不触 DB/display/前端；node-22 仅只读 ssh 取几何 receipt；forcing producer 写侧零改动。
