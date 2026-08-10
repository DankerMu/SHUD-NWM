# Design: forcing-provenance-read-fallback（#1203）

## 风险三角与 fixture level

- 风险：**误报恢复**（把真缺产物放行 → 回归 #874/#1160 fail-closed）×**误报阻塞**（继续把缺行当缺产物 → issue 主诉不解）×**repair 通道断链**（新 reason 不被授权通道接受 → 换个 token 继续死锁）。
- fixture level：**expanded**（多模块判定链 + 规格 MODIFIED 改既有 pinned 行为 + p1 生产死锁）。

## 现状基线（fixture 撰写时 explorer + fixture 复审逐点核实，HEAD b0974496）

- 判定链、行号、容器集合见 proposal Why；关键可用事实：
  - `SchedulerCandidateLike`（`scheduler_state_types.py`）携带 `source_id/cycle_time_utc/model_id/basin_id/basin_version_id(None 可)/resource_profile/forcing_version_id` —— sidecar key 可在判定层由候选身份推导。
  - `LocalObjectStore`（`packages/common/object_store.py:35`）有 `exists/read_bytes/read_bytes_limited`；判定层 root 解析先例 `_object_manifest_is_missing`（`scheduler_state_common.py:164`：`resource_profile["object_store_root"]` → env，root 未配置返回 False=「不缺」——该 fail-open 是既有怪癖，sidecar 档**不得**复制：root 未配置 = 档位不可用 = 无法判定）。
  - **关键形状事实（fixture 复审 P1-1）**：producer 的 `forcing_package_uri` 是**目录 URI**（`producer.py:1970-1971` `_directory_uri`，尾 `/`，归一化后 5 段），`validate_object_path` 的 forcing pattern 要求 >5 段（`packages/common/storage.py:142-146/:744-746`）→ 目录 URI 进 `_artifact_uri_missing_status` 必被 `except ValueError` 吞成 missing=True（`scheduler_state_failure.py:505-509`）。**因此 sidecar 档的探针对象必须是文件 key**：record `lineage_json.forcing_package_manifest_uri`（`producer.py:2095`；键名即此，非 `manifest_uri`），缺失时（旧记录）以 producer 既有 `_package_manifest_uri(package_uri, package_manifest_filename)` 同构派生——默认文件名是 **`forcing_package.json`**（`producer.py:435/:1972/:2178/:2339`），不是 `forcing_version_record.json` 的兄弟名，实现须复用/断言同构而非手拼（fixture 复审 R1）。record 顶层 `checksum` 恒为 `None`（`:2092`），evidence 不取。
  - sidecar 写侧 key 前缀 `forcing/{source_segment}/{compact_cycle}/{basin_version_id}/{model_id}`（`producer.py:1970`）；source segment 大小写以写侧为准，实现期与写侧同构造/同断言。
  - `_artifact_state_containers` 固定容器序含 `state["forcing_version"]`（`scheduler_state_failure.py:475/:481-483`）→ D1 物化后 `_first_artifact_uri` 自动命中。
  - 直读档写入方：生产端零；测试端除 `_direct_forcing_context_record`（`tests/test_file_orchestration_journal.py:319-341`）外，`tests/test_production_scheduler.py:28257-28259` 内联播种、`:32127` `_record_journal_forcing_provenance` 写行档——D1 会让 `[s3_missing]` 参数化 case 从空 URI 分支改走探针分支（断言仍应过，B6 名单核对）。

## 决策

### D1 — candidate_state 直读回落（file_orchestration_journal.py）

`candidate_state` 在 `rows.forcing_version is None` 时，调用与 `find_forcing_context` 同一个底层直读（`_forcing_context` 的文件读取层，原始 mapping），命中则以**浅拷贝**物化进 `candidate_state_from_rows(forcing_version=...)`，附 `forcing_version_source="direct"`；journal 行命中附 `"journal"`（拷贝加注，不回写 rows）。两档全空 → `forcing_version=None`（不伪造）。`find_forcing_context` 零改动。

- **错误语义（fixture 复审 P1-3 裁定）**：新直读调用点在 `candidate_state` 的 try 护栏（`:637-668`）之外，而 `_validated_direct_forcing_context_record`（`:5522-5551`）有 5 类 `FileOrchestrationJournalError` 抛点。裁定：**调用点捕获 `FileOrchestrationJournalError` 并退化为 `forcing_version=None`**（档位不可用 = 无法判定，与 D2 sidecar 档同语义；损坏的 direct 文件不得让整趟 scheduler pass 崩溃）。`find_forcing_context` 保持其既有严格语义（包 `OrchestratorError` 抛出，`:786-790`）——两条读路对**损坏文件**的语义有意分裂：context 读是显式查询（调用方要真相），candidate_state 是批量派生（单候选损坏不得殃及全趟）；此分裂记录在案。
- 一致性钉（AC-3）：同一 `(source, cycle, model)` 合法文件下，`find_forcing_context` 与 `candidate_state["forcing_version"]` 的 `forcing_version_id`/`forcing_package_uri` 一致（**B7**）；B7 另含损坏文件分支（不抛、退 None）。

### D2 — 判定点 sidecar 第三档（scheduler_state_failure.py）

空 URI 分支（`:353-363`）改为三段：

```text
uri = _first_artifact_uri(...)             # 既有（D1 后 journal/direct 档物化即命中，探针语义不动）
if not uri:
    sidecar = _forcing_sidecar_provenance(candidate)   # 新 helper
    if sidecar.witness:                     # 档位命中
        probe_uri = sidecar.manifest_uri    # ← 文件 key（P1-1）：lineage_json.forcing_package_manifest_uri
                                            #    缺失时按 producer _package_manifest_uri 同构派生（默认名 forcing_package.json）
        missing, unsafe = _artifact_uri_missing_status(candidate, probe_uri)  # 既有探针，语义不动
        if missing: return blocker(reason="missing_forcing_package_uri", ...,
                                   artifact_uri=sidecar.package_uri,
                                   forcing_provenance={source:"object_store_sidecar", probe:"manifest", ...})
        annotation = {source:"object_store_sidecar", package_uri, manifest_uri}   # 不 block，注记外传
    else:                                   # 三档全空：无法判定
        return blocker(reason="forcing_version_row_absent",
                       error_code="FORCING_VERSION_ROW_ABSENT",
                       stable_classifier="FORCING_VERSION_ROW_ABSENT",
                       artifact_uri=None,
                       forcing_provenance={source:"absent", tier_status:sidecar.status})
```

- `_forcing_sidecar_provenance`：key 由候选身份推导（与 producer 写侧同构造）；`basin_version_id=None`、root 未配置、`exists()` 假、`read_bytes_limited`（上限 64 KiB）抛错、JSON 畸形、`forcing_package_uri` 与 manifest URI 双缺——一律"档位不可用"+status 细因（`sidecar_absent`/`sidecar_unreadable`/`sidecar_malformed`/`store_unconfigured`/`identity_incomplete`），**不抛异常、不 fail-open**。
- **探针对象裁定（P1-1）**：tier-1/2（journal/direct）恢复的 URI 走既有探针路径**零改动**（其目录形 URI 误判属 pre-existing 同源另腿，已立案跟踪，不在本 change 修）；**仅 sidecar 档**探针对象定死为 manifest 文件 key。B1/B2 的 sidecar 播种必须用 producer 同构 record（目录形 `forcing_package_uri` + `lineage_json.forcing_package_manifest_uri`），禁止手写非生产形状（如 6 段文件 URI）绕过——另加一条钉：目录形 `forcing_package_uri` 本身绝不直接进探针。
- **evidence 出口（P2-2）**：guard helper 返回值从"blocker-or-None"扩为"(blocker-or-None, provenance_annotation)"（或等价 out-channel）；失败决策装配处把 `forcing_provenance` 并入**最终产出决策**（blocked 或非 blocked）的 evidence。B8 在 B1 几何（不 block）断言最终决策 evidence 携带 `forcing_provenance.source == "object_store_sidecar"`。
- `_evidence_safe`/redact 复用；sidecar record 原文不整体入 evidence（只取 `forcing_version_id`/`forcing_package_uri`/manifest uri；顶层 `checksum` 恒 None 不取）。

### D3 — repair 通道扩集与 token 配对（scheduler_candidates.py）

- 新 decision/reason 对：`blocked_forcing_version_row_absent` ↔ `forcing_version_row_absent`；既有对不变。
- 授权通道扩集点**四处**（P2-1 补第四处）：reason 硬等判 `:1412/:1443/:1519` + **stable_classifier 硬等判 `:1452`**（`_decision_is_stable_missing_forcing_blocker` 内 `== "FORCING_PACKAGE_URI_MISSING"` → 集合 `{FORCING_PACKAGE_URI_MISSING, FORCING_VERSION_ROW_ABSENT}`）；`_repair_precondition_blocker` `:1432` 的 `rejection.reason or "missing_forcing_package_uri"` 兜底改为透传底层 reason。`:728-754` echo 路径按底层 reason 回显对应 token 对。
- repair 语义核查：`operator_exact_cycle_missing_forcing_repair` 动作为按 cycle 重产 forcing——对 row_absent 同样幂等安全（重产后 sidecar 在场，下轮自动恢复）。**B5** 钉双 reason 均被接受 + `_decision_is_stable_missing_forcing_blocker(row_absent_decision) is True` 直断言。
- **文档消费面（P2-3）**：`docs/runbooks/current-production-ops.md:1480-1481` exact-cycle repair 前置条件"仅被 `missing_forcing_package_uri`/`FORCING_PACKAGE_URI_MISSING` 阻塞"须扩为双 reason/双 classifier（tasks 1.6）。
- **open-change 冲突记录**：`openspec/changes/fix-node22-scheduler-business-concurrency/specs/job-retry-mechanism/spec.md:55/:73/:105` 钉"only ... solely by FORCING_PACKAGE_URI_MISSING"——与本 change 双 reason 语义冲突。裁定：本 change 先行修改已部署 spec；该 open change（node-22 域，未实现）归档前需按新基线重排其 delta，由其 owner 处理；冲突记录入 PR body，不代改其 fixture。
- 消费方审计（实现期逐点核实入 PR body）：`_incident_scheduler_evidence_payload`（`tests/…:23490`）、`_expected_bounded_blocked_candidate_summary`（`:23652`）、db-free repair-preservation tests、runbook、上述 open change。已核实 `schemas/`/`apps/frontend/`/`scripts/` 零消费。

### D4 — 兄弟副本裁定（不改行为）

`chain_forecast_state.py:95,110`/`chain_analysis.py:41,54` 的 `forcing.forcing_package_uri or fallback_forcing_uri`：提交路径为**新生产**构造前瞻 URI（identity-derived，目录形是全系统 canonical 形状），与恢复路径"需要有见证的既存 URI"语义不同——保留现状。若改为三档读回落，会把"无法判定"引入提交路径并阻塞正常首产。判定记录于此，PR body 复述。

### D5 — 测试策略（tmp 双根注入，禁实机制造故障）

| 锚 | 内容 | oracle |
|---|---|---|
| B1 | journal 无行、tmp object store 播种 **producer 同构 sidecar**（目录形 package_uri + lineage manifest uri）、manifest 文件在场 → 决策**非** missing/row_absent、恢复腿推进；最终决策 evidence 带 `forcing_provenance.source="object_store_sidecar"`（AC-1 复现钉） | test_production_scheduler |
| B2 | 同构 sidecar 在场、manifest 缺失 → `missing_forcing_package_uri` + source=object_store_sidecar（AC-2 真缺 fail-closed 反向钉） | 同上 |
| B3 | sidecar 缺失 → `forcing_version_row_absent`/`FORCING_VERSION_ROW_ABSENT`（reason+error_code+stable_classifier 三重）、`artifact_uri=None`、结构 repair-eligible | 同上 |
| B4 | 畸形 JSON / 超 64 KiB / root 未配置 / basin_version None → 同 B3 档位不可用细因，无异常逃逸；**目录形 package_uri 绝不直接进探针**钉 | 同上 |
| B5 | repair 授权双 reason 接受（提交/echo 两侧）+ token 配对 + `_decision_is_stable_missing_forcing_blocker(row_absent) is True` | 同上 |
| B6 | 空 provenance 几何 pins **全名单迁移**（见下），仅换 reason/error_code/classifier 断言，block 方向断言逐字保留；URI 已记录几何 pins 零改动 | 三文件 |
| B7 | candidate_state/find_forcing_context 直读档一致性 + 两档全空 None + **损坏 direct 文件不抛退 None**（P1-3） | test_file_orchestration_journal |
| B8 | evidence `forcing_provenance.source` 四值现形：journal/direct（state 物化标注）、object_store_sidecar（B1 不-block 几何）、absent（B3） | 分摊 |

**B6 全名单（fixture 复审 P1-2 逐条核实的空-provenance 几何，实现期如再发现同类以同规则迁移并记 PR body）**：

- `tests/test_production_scheduler.py`：`:8889`、`:8918`（`_assert_stable_missing_forcing_blocker` `:8830-8848` 三重断言随迁）、`:8932`、`:8954`、`:9945`、`:10047-10051`、`:10591`（repair-rejection 组，`_missing_forcing_retry_state` 默认 `forcing_package_uri=None` `:9489-9505`；`_missing_forcing_repair_rejected_decision` 透传 reason `:1382-1384`）、`:28276-28279`（`absent_uri` 参数化 case）、`:29159`/`:29189`/`:29253`（`_seed_db_free_missing_forcing_blocker` `:29008-29033`）、`:32202`（`forcing_recorded=False` 分支）。
- `tests/test_gateway_reconcile.py:1148`（`_file_cohort_repository` 无 forcing 行、`resource_profile={}`）——**该文件必须加入 Evidence Floor 定向命令**，否则回归到 merge 后才暴露。
- 迁移纪律：名单内仅改 reason/error_code/classifier 与新增 source 断言；block-不-retry 方向断言逐字保留；名单外断言零改动（发现新几何 → 先补名单入 PR body 再改）。

- 播种 helper 复用：`_direct_forcing_context_record`、`_candidate_state`；sidecar 播种新 helper 与 producer 同构造（1.4）。
- **oracle 完整性声明**：B6 迁移由本 change spec MODIFIED 授权（场景细分）；除名单及其随迁 shared helper 外不得改动任何既有断言。

### D6 — 实机证据（只读）与 live drill 偏离

- node-22 只读几何 receipt（evidence task）：任选六流域 replay 窗口一个 cycle，ssh 只读列出 (a) journal `latest/<source>/<cycle>/<model>.json` 的 `forcing_version: null`；(b) object store 对应 sidecar key 存在及其 record 内 manifest uri 指向的文件存在——证明 B1 几何即生产实况且探针对象真实可命中。**不制造失败、不写任何远端文件**。
- issue AC 第 6 条（实机制造候选级失败）**偏离**：生产 replay 窗口制造失败具侵入性且 quarantine 处置在 issue Out-of-scope；以 B1（真实决策代码 + tmp 双根注入）+ 只读几何 receipt 替代，偏离记录入 PR body；完整 live drill 如需由后续运维窗口单独立项。

### 备选方案否决记录

写侧物化（journal `upsert_forcing_version`）：语义上 journal 恢复单一事实源，但需动 journal 写契约 + 幂等/replay 顺序仲裁 + 历史 cycle 回填迁移，且存量 replay 窗口 cycle 仍需读侧回落才能解锁——改动面与风险大、收益不独立，否决（issue 自身权衡一致）。

## Must-preserve

- #874/#1160 真缺产物 fail-closed：sidecar 见证 manifest 缺失 → `missing_forcing_package_uri` 语义、结构 evidence、repair-eligible 契约不变（B2）；URI 已记录几何（tier-1/2 命中）的全部既有行为零改动。
- 三档全空**仍 block**（fail-closed 方向不变，只换可区分 reason）；任何档位错误不得 fail-open 放行 retry（B3/B4）；损坏 direct 文件不得崩掉 scheduler pass（B7）。
- `_artifact_uri_missing_status`/`_object_manifest_is_missing` 既有语义（含 root 未配置 fail-open 怪癖与目录形 URI 拒绝）零改动——sidecar 档以 manifest 文件 key 为探针对象绕开而非修改之。
- manual retry 先于 guard 评估的操作员逃生门不变。
- `find_forcing_context` 行为零改动；forcing producer 写侧零改动；非 forecast restart_stage 分支零改动。

## Seams under test

- tmp journal root + tmp object store root（`resource_profile` 注入，`LocalObjectStore` 真实文件读写，无 mock 网络）。
- 既有 `test_production_scheduler.py` 决策 harness 与 `_candidate_state`/`_direct_forcing_context_record`/`_missing_forcing_retry_state`/`_seed_db_free_missing_forcing_blocker` helpers。
- sidecar 播种与 producer 写侧同构造（复用其 key/record 构造或字面断言一致，防漂移）。

## Evidence mapping

| AC | 证据 |
|---|---|
| AC-1（缺行+包在场不再伪 missing） | B1 |
| AC-2（真缺仍 fail-closed + reason 细分） | B2 + B3 + B4 |
| AC-3（candidate_state/find_forcing_context 一致） | B7 |
| AC-4（evidence 档位来源可读） | B8 |
| AC-5（定向 pytest + ruff） | `uv run pytest -q tests/test_file_orchestration_journal.py tests/test_production_scheduler.py tests/test_gateway_reconcile.py` + `uv run ruff check .` |
| AC-6（node-22 实机） | 只读几何 receipt（D6，偏离记录） |

## Risk packs

- 选用：`fail-closed-semantics`（真缺/无法判定二分方向）、`operator-evidence-contract`（reason/token/classifier/repair 通道契约）、`cross-module-blast-radius`（token 消费方审计 D3 + B6 全名单）。
- 未选：`db-migration`/`display-boundary`（零触面）、`concurrency`（无锁/调度时序变更）、`perf`（sidecar 读仅发生在本就要 block 的失败腿，非热路径）。
