# Design: raw-manifest-probe-unification (#1393)

## Risk triage

- Fixture level：**compact**（issue-scribe 产出无 suggested level；S/M 规
  模、单 lane 两调用点、#1365 家族续面、可复用已交付探针层——偏离记录
  在案）。
- Risk packs：oracle-integrity（红证 + fail-open 复现 + byte-compat）、
  terminal-state-semantics（unsafe 弃权后的梯子终态 + pass 存活）。未选
  全量 spec-compliance：单 requirement delta，映射 D3 内联。
- **对 issue 解决思路的具名偏离（fixture 两轮重裁终态）**：issue 建议臂
  「降级为带 reason 的 blocked/manual 通道」被弃用，改为 **unsafe 时两腿
  弃权（return None）**。理由（round-2 P1 实测）：repair 腿在 unsafe 下
  剩余门极弱，fail-closed 人工证据会把几乎整个 cycle 的瞬时失败
  （SLURM_TIMEOUT/NODE_FAILURE/PREEMPTED，梯尾 `:390-400` 通用 retry
  rung `retry_failed_candidate` 今日自动重试）改判人工 blocked——一次
  NFS 抖动冻住整批；且 repair 腿门集是 downstream 腿门集的真子集、rung
  序 repair 在前，unsafe 下 repair 腿必然吞并 downstream 腿。弃权 = 「腿
  只有拿到真探针裁决才能 claim 候选」的极限收窄臂；对 restart 几何不触发
  既有 guard 的瞬时子集可用性零回归，而被 fail-open 遮蔽的 forecast-
  restart（forcing guard）与 permanent-remedy-permitted（`:371` guard）
  几何则**去遮蔽**到各自既有 fail-closed blocked 终态——是既有 guard 终
  态非新终态（round-1 C1 双反例实测 + repaired=False 对照组同终态）。

## D1 — 修法（弃权设计）

**腿层**（`scheduler_state_failure.py:1346`、`:1406`），决策层零改动：

| 腿 | 旧 | 新 |
|---|---|---|
| repair `:1346` | `if not _object_manifest_is_missing(...): return None` | `missing, unsafe = _artifact_uri_missing_status(candidate, str(manifest_uri))`；**`unsafe` 非 null → `return None`（弃权）**；`unsafe` 为 null 且 `not missing` → `return None`（照旧）；`unsafe` 为 null 且 `missing` → 照旧走修复构造（permanence 咨询 `:1354` 原位不动） |
| downstream `:1406` | `if _object_manifest_is_missing(...): return None` | 同上取 `(missing, unsafe)`；**`unsafe` 非 null → `return None`（弃权）**；`unsafe` 为 null 时照旧（`missing` → None；否则走自身其余结构门 `:1408/:1411/:1416` 与证据构造，逐字节不变） |

- 弃权后候选落入既有决策梯子（`scheduler_state_decision.py` `:364` 模型
  包刷新 retry → `:371` permanent guard blocked → `:382` cancelled
  blocked → `:390-400` forcing block（root 未配置时本就 fail-closed 带
  reason，#1365）→ `:398` 通用 `("retry", "retry_failed_candidate")`）。
  瞬时失败保留自动重试；forcing 几何的可区分 reason 由 forcing rung 继续
  提供；#1313 permanent 拒绝面经 `:371` 原样。
- **决策层 `scheduler_state_decision.py:318-328` 零改动**：弃权设计下腿
  只在真 claim（null-unsafe 裁决）时返回非 None，rung 硬编码 retry 保持
  正确。无新证据形态、无新 decision/reason 值、无 runbook 路由行。
- 结构门与探针相对顺序零改动；探针仍在结构门之后——健康/running 候选不
  新增探针 IO。ObjectStoreError 由探针层 `:1032-1047` 容器化为
  `(True, artifact_probe_error)` → 腿弃权，异常不逃逸（缺陷 2 的容器）。
- `_object_manifest_is_missing` 与 `_artifact_uri_missing_status` 本身零
  改动；`:1027-1028` 豁免注释改真（raw-manifest 腿已接入，该函数仅余探
  针层一个调用方）。

## D2 — 终态表（`(missing, unsafe)` × 腿，完整枚举）

| 探针结果 | repair 腿（manifest 应缺才修） | downstream 腿（manifest 应在才重试） | 候选最终终态 |
|---|---|---|---|
| `(False, None)`（真探针判存在） | `return None`（照旧） | 照旧发 `manifest_exists: true` + 自动重试（逐字节不变） | downstream 自动重试（不变） |
| `(True, None)`（真探针判缺失） | 照旧发修复证据（fresh 全链 reingestion，逐字节不变） | `return None`（照旧） | repair 自动重试（不变） |
| `(True, "object_store_root_unconfigured")` | **弃权 `return None`**——与今日逐字节同（今日 fail-open「存在」→ not missing → 本就 None） | **弃权 `return None`**——行为变化本体：不再发假 `manifest_exists: true` + 自动重试 | 既有梯子按 rung 序：permanent（含 remedy-permitted 码，C1(b)）→ `:371` blocked；cancelled → `:382`；forecast-restart/copyback 几何 → `:390-400` forcing blocked 带 unsafe reason（#1365，C1(a)）；guard 不触发的瞬时 → `:398` 通用自动重试。guard 命中格为**去遮蔽**（repaired=False 对照组 master 同终态），非新终态 |
| `(True, "artifact_probe_error")`（ObjectStoreError 被探针层容器化） | 弃权 `return None`（异常不逃逸） | 同左 | 同上；邻座照常评估、整趟不中止（缺陷 2 修复本体） |
| `(True, None)` 经探针层 `(OSError, ValueError)` 残余臂 | 视同「真探针判缺失」发修复——#1365 D4 既有裁决：rebuild 会 re-record 该引用（fixture review 2(b) 复核成立），具名沿用不新裁 | `return None` | repair 自动重试 |

- **具名限制（AC-2 偏离）**：root 未配置 × manifest 真缺失几何下，repair
  腿弃权 = 修复通道在 root 配好前不触发（与今日行为一致，非新增回归）；
  候选走通用 retry 烧尽预算后按既有 exhaustion 路径浮出。不发明「无探针
  裁决的修复」也不发明「无归属的 blocked」。
- **具名限制（AC-1 偏离）**：可区分 reason 不出现在决策终态（弃权无证
  据），钉死方式 = 测试 spy 断言两腿咨询了统一探针且拿到该 reason 后弃
  权（D3 seam 6），加 forcing rung 对其几何的既有 reason 面。
- 生产可达性（issue 观察）：缺陷 1 在 db-free 车道被 root preflight 遮
  蔽、非 db-free 可达；缺陷 2 与 root 配置无关，NFS stale handle 即触发。

## D3 — seams under test

1. root 未配置 × downstream 几何：不再发 `manifest_exists: true`/
   `automatic_retry_allowed: true`；候选决策来自后续梯子（AC-1 行为面）。
2. root 未配置 × 瞬时失败码（如 SLURM_TIMEOUT 预算内）× guard 不触发的
   convert-restart：决策仍为自动重试（`retry_failed_candidate`）——
   **round-2 P1 可用性回归锁**。**去遮蔽钉格**（round-1 C1）：同几何 ×
   forecast-restart → `blocked/missing_forcing_package_uri`；× INVALID_
   MANIFEST（permanent-remedy-permitted）→ `blocked/permanent_failure_
   guard`（两 unsafe reason 各钉）。限定（实测）：forcing guard 的
   `artifact_guard.unsafe_reason` 仅在 root 未配置臂出现（进程级故障波
   及 forcing key 自身探针）；probe-error 是单叶故障，forcing key 自身
   正常裁决、`unsafe_reason: None`——弃权腿不留证据的 AC-1 具名限制在
   此可见。
3. root 未配置 × repair 几何（manifest 真缺失）：repair 腿弃权、无存在断
   言；D2 具名限制钉一格（AC-2 偏离臂）。
4. `ObjectStoreError`（symlink 探针目标构造 `SafeFilesystemError`）×
   两腿：异常不逃逸；`ProductionScheduler.run_once()` 级用例断言邻座照常
   提交（`submitted_count`）+ 整趟不中止 + 故障候选拿到既有梯子终态而非
   崩溃（AC-3）。
5. byte-compat：配置真实 root 的两腿全部既有几何逐字节不变——真锚点
   `:20338`/`:20417` run_once 用例保绿 + 两腿探针结果等价断言（AC-4；
   F3 换算，弃用 issue 漂移行号）。
6. 探针接线 pin：spy/monkeypatch `_artifact_uri_missing_status` 断言两腿
   各自以 `(candidate, str(manifest_uri))` 咨询统一探针，且 unsafe
   reason（两值各一）导致弃权（AC-1 偏离的钉死面）。
7. `(True, None)` 残余臂：repair 腿照旧发修复（具名沿用裁决钉一格）。
8. **测试几何迁移**（F3 + round-2 P2-3）：`:4657` +
   `_raw_manifest_decision` 调用者族（约 10 个）为「无 root fixture +
   monkeypatch 裸探针」几何——统一探针下 root 未配置在 `:1019` 短路、
   monkeypatch 不再被调，必红。迁移约束：(i) **只做 per-test 覆写**，不
   改共享 `_scheduler_candidate_fixture`（179 处调用共享，
   `tests/test_production_scheduler.py:9663-9666` 断言空 root）；(ii)
   root 值必须是真实 `tmp_path`（或 blanket monkeypatch 探针内部）——假
   路径会走真探针得 `artifact_probe_error` 反而弃权；(iii) monkeypatch
   `_object_manifest_is_missing` 经探针内部 `:1031` 仍生效，**断言零改
   动**；任何断言需改 → 停下重裁（一半是 #1313 permanence 钉子）。迁移
   前后分别全绿作为迁移正确性 oracle。
9. unsafe × permanent-refused：弃权后经梯子落 `:371` permanent guard
   blocked（#1313 面回归锁，经梯子而非腿内咨询）。

## D4 — 红证

- R1：两处调用回退裸 `_object_manifest_is_missing` → seam 1 红（假
  `manifest_exists: true` 复现）+ seam 4 红（异常逃逸复现）。
- R2：容器化冒充修复（调用点裸 try/except ObjectStoreError 吞掉、root
  未配置仍走裸探针 fail-open）→ seam 1/6 红——判别「统一探针 + 弃权」
  而非「不抛」。

## Task 0 探针（实现前）

- (a) 复现缺陷 1：unconfigured root 直调 downstream 腿 →
  `manifest_exists: true` + `automatic_retry_allowed: true` 在案。
- (b) 复现缺陷 2：symlink 探针目标（或 monkeypatch store.exists 抛
  `ObjectStoreError`）× 决策路径 → 异常逃逸至 run_once 外。
- (c) 梯子终态复证：unconfigured root × 瞬时失败码、两腿弃权后 → 决策
  落 `:398` 通用 retry（`retry_failed_candidate`）；同几何 × permanent
  refused → `:371` blocked（D2 末列断言的实测底座）。
- (d) 复证 raw manifest URI（裸 key `raw/.../manifest.json` 与
  `s3://.../raw/.../manifest.json`）走 `_artifact_uri_missing_status`
  object 分支（fixture review 已实测一轮，实现前复证）。
- (e) 复证 seam 8 迁移几何：monkeypatch `_object_manifest_is_missing`
  在「per-test 配置真实 root」下经探针 `:1031` 生效（换桩实测），且
  `:9663-9666` 空 root 断言不受扰动。
- 任一探针与 design 断言不符 → 停下报告重裁。

## Non-goals

见 proposal Out of scope。
