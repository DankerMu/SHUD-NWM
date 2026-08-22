## Context

#1183 已把每个 warm-start basin 的 `init_state_identities` 在预约期写到 accepted-submit cohort master，并在 accounting 终态按 `array_task_id` 回抄到 current-contract per-model candidate row。该单条目包含 `model_id`、`init_state_id` 及可选 checksum/URI/valid-time，且受 frozen evidence gate保护。

读侧仍断裂：candidate-state evidence有意删掉cohort master全图；verdict只向 `terminal_init_state_match` 传 `hydro_run`；`completed_pipeline_init_state_id` 也只读completed hydro row。cohort无hydro identity时，conflict与§8.7 mismatch均不可达。

Risk triage: bugfix；SHUD profile；blast radius high；fixture level **expanded**（orchestrator、persisted/shared state、retry/backfill与血统判定强制触发）；upstream suggested level absent；repair intensity **high**（共享identity accessor与两条state-machine gate，错误方向会静默放过错血统或永久卡住历史cycle）。

## Goals / Non-Goals

**Goals:**

- 为verdict与§8.7提供一个journal-only、cohort-aware、未截断的权威identity读面。
- cohort recorded conflict即使successor ready也保持gap；match无需successor即可complete；pre-change absent仍只在successor ready时complete。
- discovery §8.7、candidate quarantine与verdict使用同一identity selection source，避免hydro/job两套漂移。
- 保留completed hydro、non-journal repositories、legacy rows、different-base/suffix-less/no-accessor的既有行为。

**Non-Goals:**

- 不改#1183 writer、reservation/reconcile copyback、frozen fields、digest、member projection或accepted-submit schema。
- 不扩 `_job_state_evidence` 或 `_CYCLE_SCOPE_JOB_PROJECTION_KEYS`；不把cohort master identity map复制到每个candidate evidence。
- 不改absence tolerance：`absent + successor ready -> complete`、`conflict -> gap`原样保留。
- 不处理#1184边界#2/#3/#4（reclaim映射复用、public URI placeholder、ordinary-upsert覆盖）或其它#1179截断面。
- 不新增Protocol成员、DB accessor、node-22 runtime receipt、journal migration或run-manifest读取。

## Decisions

### D1: 新增完整identity accessor，旧string accessor委托它

新增可选 repository method：

```python
completed_pipeline_init_state_identity(
    *, source_id: str, cycle_time: datetime, model_id: str
) -> dict[str, Any] | None
```

它读取与completion probe相同的memoized `_cycle_rows`内部rows，返回identity mapping的copy且永不写journal。既有 `completed_pipeline_init_state_id` 调用它并只接受 `init_state_id` / `initial_state_id`；bare `state_id` 对§8.7仍是no-judgement。Accessor继续通过 `getattr` 可选消费，不加入 `ActiveCandidateRepository` Protocol；缺method的DB repo/test double行为不变。

否决直接扩 `_job_state_evidence`：会把cohort map复制到18个candidate、进入job_limit/size truncation面，并迫使verdict另写job selection逻辑。窄accessor一次封装identity authority，体积与bounded evidence不变。

### D2: identity authority与latest-row选择

Accessor按以下顺序裁决：

1. matching `hydro_run` 本身completed且含任一init-state identity alias时，返回其identity fields，保持legacy/direct verdict语义；旧string wrapper仍只认两个历史aliases。
2. 否则从内部未截断 `rows.pipeline_jobs` 取满足以下条件的rows：`_job_matches_candidate`；显式current accepted-submit contract；`accepted_submit_row_kind == "candidate"`。Cohort master、marker-free historical/ordinary job、foreign model与畸形versioned row均无authority。
3. 对候选rows复用 `chain_source_cycle._pipeline_job_truth_sort_key` 选**最新一行，再校验**：latest row必须terminal-success，且 `accepted_submit_candidate_immutable_evidence` 规范化后恰有一条绑定其 `array_task_id`/`model_id` 的identity；否则返回None。禁止先过滤success再取max，否则旧success会掩盖更新的failure/empty identity。
4. identity mapping保持writer记录的id/checksum/URI/valid-time；不经 `_public_scheduler_row` redaction与candidate-state truncation。

该规则同时处理retry/多次cohort提交：canonical truth-order是candidate-state已用的单一时序来源；新accessor不发明第二套freshness排序。

### D3: verdict消费完整identity，保留hydro fallback

在strict/successor completion分支已经获得terminal decision后，discovery通过 `getattr(active_repository, "completed_pipeline_init_state_identity", None)` 读取完整mapping。若得到mapping，作为 `terminal_init_state_match` 的observed；method缺失或返回None时，继续使用 `decision.evidence["hydro_run"]`，因此非-journal repository、legacy per-basin row与现有special branches逐字保持。

共享helper仍是纯present-field compare；candidate-side strict wrapper不改。Cold strict resolution无candidate state仍gap；accessor不成为新的admission shortcut。

### D4: §8.7与candidate quarantine自动同源

`completed_pipeline_init_state_id` 委托D1完整accessor后，现有：

- discovery `_journal_predecessor_identity_stale_tokens`
- candidate `_journal_predecessor_identity_quarantine`
- quarantine breaker occurrence调用链

无需改判断逻辑即可读到同一cohort token。Positive same-base/wrong-suffix mismatch继续stale；match、absent、suffix-less、different-base、no-accessor仍no-judgement。两条wire必须用同一real repository fixture锁定，防止未来一侧绕过wrapper。

### D5: 两份规格owner

- `cross-cycle-warm-start-chaining`: 修改completion verdict identity来源与cohort match/conflict/absent真值表。
- `file-state-snapshot-index`: 修改§8.7 accessor从“completed hydro row”到“completed hydro优先、latest current-contract per-model terminal row fallback”的journal-only语义。

`strict-warm-start` helper与`pipeline-job-persistence` writer契约不改。

## Risk Packs

Core packs considered:

- Public API / CLI / script entry: selected — optional repository accessor与shared scheduler gate contract。
- Config / project setup: selected — default与`forecast_state_save_qc` terminal mode均须覆盖；不改config格式。
- File IO / path safety / overwrite: selected — accessor从journal内部rows读取且必须read-only/未截断；不新增path或write。
- Schema / columns / units / field names: selected — 消费既有closed `init_state_identities` entry字段，writer/schema不改。
- Auth / permissions / secrets: not selected — 无权限或credential边界变化；identity URI不发布/记录到新output。
- Concurrency / shared state / ordering: selected — canonical truth-order决定多次提交的最新authority，避免旧success遮蔽新failure。
- Resource limits / large input / discovery: selected — 必须绕开candidate-state job_limit但仍只扫描已bound journal cycle rows；禁止扩大evidence payload。
- Legacy compatibility / examples: selected — hydro-only、no-accessor、pre-change rows、suffix-less/different-base/no-record必须保留。
- Error handling / rollback / partial outputs: selected — unreadable/malformed/failed/latest-empty evidence均fail to absent；不产生partial write。
- Release / packaging / dependency compatibility: not selected — 无依赖、package或deploy shape变化。
- Documentation / migration notes: selected — 两份MODIFIED spec与zero-migration/accessor语义同步。

SHUD domain packs considered:

- Slurm production lifecycle / mock-vs-real parity: selected — accepted-submit cohort reservation/reconcile identity必须由真实journal fixture证明可读；不改Slurm行为。
- Run manifest / QC provenance: selected — accessor明确journal-only，run-manifest backfill不得冒充recorded identity。
- Hydro-met/geospatial/numerical/PostGIS/providers/published-display packs: not selected — 无相关surface。

## Invariant Matrix

Governing invariant: completion verdict、discovery §8.7与candidate quarantine必须从同一journal authority读取当前candidate的recorded init-state identity；recorded conflict永不被successor容忍，recorded absence仍可由successor物理证明兜底。

Source-of-truth identity/contract: completed hydro identity优先；否则canonical truth-order最新的current-contract accepted-submit candidate terminal row的唯一normalized `init_state_identities` entry。

Surfaces:

- Producers: #1183 reservation master + reconcile per-model copyback；unchanged。
- Validators/preflight: accepted-submit normalization/frozen evidence；unchanged且作为read authority gate。
- Storage/cache/query: `_cycle_rows` internal untruncated rows；new full accessor + legacy string wrapper。
- Public routes/entrypoints: optional repository methods via `getattr`；no protocol change。
- Downstream consumers: completion verdict、discovery predecessor filter、candidate quarantine、breaker token path。
- Failure/stale: latest failure/empty/malformed evidence -> absent；same-base wrong-suffix -> stale/conflict；unreadable rows -> no judgement。
- Evidence/audit: real file-journal roundtrip, mutation tests, five-file regression（含 #1735 lineage-scope × cohort-identity mixed-model oracle）。

Regression rows:

- cohort current candidate identity conflicts on id or optional present field + successor ready -> verdict gap。
- cohort identity matches + successor absent -> verdict complete。
- pre-change/no-identity cohort + successor ready -> complete；successor absent/unready -> gap。
- positive same-base wrong-suffix cohort token -> discovery stale True and candidate quarantine retry/blocked per existing breaker；match/no accessor/no record/suffix-less/different-base -> unchanged no judgement。
- completed hydro identity -> current outputs unchanged；bare `state_id` remains unavailable to old string accessor。
- latest current-contract candidate row failure/empty identity overrides older success -> accessor None；latest success with one valid entry -> mapping。
- master-only map, marker-free candidate, other-model row, malformed entry -> accessor None。
- `_job_state_evidence` keep-list与 `_CYCLE_SCOPE_JOB_PROJECTION_KEYS` remain without `init_state_identities`。

## Boundary-Surface Checklist

- Shared helpers: accepted-submit row-kind/normalization and canonical truth-sort reused, not copied。
- Entrypoints/read surfaces: full accessor, legacy string wrapper, discovery verdict wiring。
- Write/delete/overwrite: none changed；read-only byte identity asserted。
- Producer/consumer evidence boundary: reservation→reconcile→internal rows→accessor→three gates。
- Stale/idempotency: latest-row-before-qualify and conflict/absence truth table。
- Unchanged consumers: candidate strict wrapper, writer/digest/projection, DB repository, run-manifest backfill。

## Risks / Trade-offs

- [Risk] 任意取第一条success会消费旧血统。 → canonical latest-first selection；latest不合格即absent。
- [Risk] master全图或bounded candidate evidence造成cross-model/截断污染。 → 只读current candidate row的单条目，internal rows，projection/keep-list不变。
- [Risk] accessor None把placeholder hydro旧行为改写。 → verdict仅在accessor返回mapping时override，否则回退原hydro evidence。
- [Risk] optional URI/checksum redaction误判conflict。 → 读取internal raw rows；shared matcher继续忽略既有redaction placeholders。
- [Risk] §8.7与verdict再次漂移。 → old string accessor委托full accessor，同一real repo fixture双面断言。

## Migration Plan

零migration；pre-change rows自然返回None并走既有absence/no-judgement路径。回滚本PR恢复hydro-only读面，不改变任何持久bytes。

## Open Questions

None. 推荐窄accessor已由当前代码的size/truncation与existing getter约定确认。
