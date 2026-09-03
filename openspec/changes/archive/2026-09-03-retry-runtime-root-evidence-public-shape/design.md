# Design — one public shape for manual-retry runtime-root evidence

Line cites against `origin/master` `f9a1345f`; symbol names are authoritative.

## Risk triage

- **Fixture level: compact** (S/M: one leaf module extracted verbatim, one
  branch, one call, tests). Repair intensity: standard. Neither issue carried an
  upstream `Suggested fixture level`; orchestrator triage, recorded as such.
- **Must-preserve behaviour**:
  - DB persisted event details keep real roots and credential-stripped URLs
    (`tests/test_retry.py:1209/:1242/:1245`, `"https://example.com/object-store" in persisted` at `:1801`).
  - Secrets assertions at `tests/test_retry.py:1783-1800` (persisted and body).
  - `runtime_root_contract` flattening: `object_store_root == "[local-path]"`
    scalar (`tests/test_file_orchestration_journal.py:4469`).
  - `resolved.workspace_dir` file-lane shape and value (`:4468`).
  - `_runtime_root_resolution_from_error` / `_runtime_root_contract_from_error`
    (`retry.py:1295-1311`) — durable surface, unchanged.
  - File-lane reader `FileJournalRetryService.submission_runtime_root_resolution`
    (`file_orchestration_journal.py:11517-11596`) returns the persisted mapping
    unchanged; PR #1963's T1 equality (`tests/test_retry.py:3087`) holds.
  - `get_retry_service` (DB lane DI), the `ManualRetryService` Protocol
    (`retry.py:327-346`), the 409 mapping.
  - Every other `_public_evidence` caller (`:11901` row rendering,
    `:12840/:12864/:12877` error evidence, `:12981`, `:13056`, `:13177`,
    `file_orchestration_migration.py:1613/:1618`): behaviour identical except
    where a Mapping sits under a `_path`/`_root` key — repo scan finds only
    `runtime_root_resolution.resolved` (see D2 blast radius).
- **Seams under test** (upstream-declared): `pipeline.py:560` (the single HTTP
  consumer); `RetryService.submission_runtime_root_resolution`;
  `_public_evidence` / `_sanitize_public_field`; the file-lane write sites
  `:11724` / `:12586`.
- **Risk packs selected**: Security light (the DB read side is a public surface
  that today carries absolute roots; the change must not weaken secrets
  redaction — precedence pinned); Legacy compat (module extraction must keep
  every importer resolving; historical bare-string events tolerated); Contract
  (one wire shape for one field across two lanes; the Protocol's docstring
  gains the rendering contract).
- **Not selected**: Error handling (no new fault boundary: `_public_evidence`
  is total on JSON-shaped input; the DB read is inside the existing session
  scope); File IO / path safety (no filesystem access added); Concurrency
  (pure functions); Performance (one extra walk over a bounded mapping —
  `rejected` is capped at `_RUNTIME_ROOT_REJECTION_EVIDENCE_LIMIT` (`:101`), values at
  `_ROOT_EVIDENCE_VALUE_MAX_LENGTH` = 256).

## D1 — leaf module `services/orchestrator/public_evidence.py`

### Why extraction, not a third copy

`retry.py` must call the file lane's renderer to converge by construction. It
cannot import the journal (journal → retry, `file_orchestration_journal.py:79-123`)
nor `scheduler_file_providers` (providers → `scheduler_state` → retry; probed:
importing providers loads `services.orchestrator.retry`). The journal's
renderer depends on three things: `is_sensitive_key` (`packages/common/redaction.py:182`),
retry's `_safe_error_message` (`:1276`, three lines over `redact_payload`) and
providers' `_sanitize_file_provider_scalar` (`:2258-2271`, urlparse
classifier) via `_sanitize_file_provider_evidence_scalar("uri", value)`
(`:2249`), plus `_format_utc` from `scheduler_state` (`:146`; a re-export of
`scheduler_state_common._format_utc`, which pulls neither retry nor providers —
probed). A leaf with imports `{packages.common.redaction, scheduler_state_common, urllib.parse}`
is therefore cycle-free for retry, the journal and providers alike.

### Contents (moved verbatim from `file_orchestration_journal.py:13059-13166`)

`_public_evidence`, `_sanitize_public_evidence`, `_sanitize_public_field`,
`_sanitize_public_scalar`, `_sanitize_public_path_or_uri_scalar`,
`_public_message`, `_sanitize_public_text`, `_sanitize_public_text_tokens`,
`_sanitize_public_text_token`. Two local equivalents replace the imports the
leaf cannot take:

- `_safe_error_message(message) -> str`: `redact_payload(message)` coerced to
  `str` — the same body as `retry.py:1276-1278`.
- `_public_path_or_uri_placeholder(value) -> Any`: the body of
  `scheduler_file_providers._sanitize_file_provider_scalar` (`:2258-2271`):
  non-str → unchanged; blank → unchanged; `urlparse(text).scheme in {"s3", "published"}`
  → `[object-uri]`; any other scheme → `[uri]`; leading `/` or `~` → `[local-path]`;
  else unchanged. Called where the journal called
  `_sanitize_file_provider_evidence_scalar("uri", value)` (`:13110`) and, for
  `_uri` keys, `_sanitize_file_provider_evidence_scalar(key, value)` (`:13086`)
  — both resolve to `_sanitize_file_provider_scalar` for those keys, so the
  substitution is behaviour-preserving.

The journal deletes the block and imports exactly the two names it still
uses: `_public_evidence` (eleven call sites) and `_public_message` (`:11903`,
`:12137`). `file_orchestration_migration.py:34` keeps importing
`_public_evidence` from the journal (an imported name is an attribute of the
importing module). The one external reference to a name the journal no longer
uses — `tests/test_production_scheduler.py:13501`
`file_orchestration_journal_module._sanitize_public_field(...)` — is repointed
to `services.orchestrator.public_evidence._sanitize_public_field` (test edit,
one line; no `noqa`, no `__all__`). The journal's `_sanitize_file_provider_evidence_scalar`
import (`:140`) drops to zero uses after the move and is removed; the
`_safe_error_message` import from retry stays (`:11749`, `:12105`, `:12133`).

Module size: ~130 lines; not near the large-file guard.

## D2 — `_sanitize_public_field`: Mapping under a path-shaped key recurses (#1965)

### Today (`:13077-13087`)

```
if lowered.endswith("_path") or lowered.endswith("_root") or lowered in {"path", "root"}:
    return "[local-path]" if value not in (None, "") else value
```

applies to the whole value. `resolved.object_store_root = {present, source, value, same_as_workspace}`
→ `"[local-path]"`; `resolved.workspace_dir` (no match) → recursion →
`value` → `_sanitize_public_scalar` → `_sanitize_public_path_or_uri_scalar`
→ `[local-path]`, `present`/`source` kept.

### Change

Before the scalar replacement:

```
if isinstance(value, Mapping):
    return _sanitize_public_evidence(value)
```

Ordering inside `_sanitize_public_field` is unchanged: `is_sensitive_key`
first (`[redacted]` precedence), then `message`, then the path-key branch
(now Mapping-aware), then `_uri`, then the generic recursion. Scalar values
under path keys are byte-identical. Sequence values under path keys keep the
whole-value replacement (the issue scopes to Mapping; no repo instance).

### Blast radius

Every `_public_evidence` caller is affected only when a Mapping sits under a
`_path`/`_root`/`path`/`root` key, and `_public_evidence` is applied to
arbitrary payloads (`:11901` every pipeline-event `details`, `:12981` every
scheduler row, `:13056`, `file_orchestration_migration.py:1613` sampled DB
rows), so a tests-only scan cannot establish the negative. Two scans are the
evidence: (a) the fixture review's production-side check — `root_preflight`
has zero hits in the journal, providers and migration;
`packages/common/node27_cold_tablespace_observation.py:64`'s `"path": {…}`
reaches only `node27_cold_tablespace_recovery.py`, never `_public_evidence`;
(b) the implementer's Evidence-Floor scan over `services/ packages/ apps/ workers/`
for dict literals or assignments that place a Mapping under a `*_root` /
`*_path` / `path` / `root` key, with every hit classified as
reaches-`_public_evidence` or not. `runtime_root_resolution.resolved` is the
only known instance. Policy for what the recursion newly lets through: inner
scalars that are not `/`-, `~`- or scheme-shaped (a relative path, a hostname)
survive under a `_root` key exactly as they survive today under every other
key — the renderer's general policy, not a special case; no shape-bound
(`keys ⊆ {present, source, value, same_as_workspace}`) exception is added,
because it would move shape knowledge into the sanitizer. Any assertion that
flips in the four suites is reported as a deviation, not silently edited.

### Legacy shape

Events persisted before this change carry `object_store_root` /
`published_artifact_root` as the bare string `"[local-path]"`. No migration.
The file-lane reader (`:11517`) already returns the persisted mapping
unchanged; its docstring gains one sentence stating both shapes reach the
route. A route test injects such an event and asserts passthrough.

## D3 — DB read side renders public (#1961)

`RetryService.submission_runtime_root_resolution` (`retry.py:736-749`):
`return _redacted_mapping(evidence)` → `return _public_evidence(_redacted_mapping(evidence))`.
The evidence was `redact_payload`-ed at construction (`:1828-1911`, every
scalar through `_bounded_redacted_text` `:1912` and the whole mapping through
`_redacted_mapping` `:1920`); the read-side `_redacted_mapping` re-application
is pre-existing and kept (idempotent). `_public_evidence` on top is exactly the
file lane's write-time pipeline (`_public_evidence(evidence)` at `:11724` /
`:12586` over the same constructor's output), so the two lanes converge by
construction. `retry.py` gains `from services.orchestrator.public_evidence import _public_evidence`.

Rendering per key on the DB evidence (`:1828-1911`):

| key | today (body) | after |
|---|---|---|
| `resolved.*.value` for `_LOCAL_RUNTIME_ROOT_FIELDS` (`:96`: `workspace_dir`, `object_store_root`, `published_artifact_root`) | absolute root | `[local-path]` |
| `resolved.*.value` for `object_store_prefix`, `published_artifact_uri_prefix` | object URI | `[object-uri]` (probed) |
| `resolved.*.present/source`, `object_store_root.same_as_workspace` | kept | kept |
| `rejected[].value` | credential-stripped URL / path | `[uri]` / `[object-uri]` / `[local-path]` |
| `rejected[].field/source/reason` | kept | kept (plain tokens; `source` like `pipeline_event:submission:3:runtime_root_contract` is not path-shaped) |
| `db_free_runtime.resolved.*.value` | absolute root / DSN | `[local-path]` / `[uri]` (a DSN-valued `scheduler_registry_backend` renders `[uri]`) |
| `db_free_runtime.slurm_env`, `candidate_counts`, `required`, `missing`, ids | kept | kept (`is_sensitive_key` false for every key — probed) |

Truncated values (`…` suffix at 256) still start with `/` → `[local-path]`.
Whitespace-bearing roots (round-1 cand-01, CONFIRMED P3): the moved
`_sanitize_public_path_or_uri_scalar` bailed out on any whitespace before
classifying, so a root such as `/home/nwm/nhms data/objects` fell to
token-wise rendering (`[local-path] data/objects`). Pre-change that was
already the file lane's rendering for `workspace_dir.value` and
`rejected[].value`, but for `object_store_root` / `published_artifact_root`
the whole-value key rule had masked it, so D2's recursion would have turned
those two from whole `[local-path]` into partial disclosure. The fix pass
classifies a stripped text starting with `/` or `~` as `[local-path]` before
the whitespace bail-out (URI branches stay behind it); message keys are
unaffected because `_public_message` tokenises first. The resolver and
`_local_runtime_root_safety` admit such roots on both lanes (verifier
evidence), so this is reachable, not hypothetical. Known residual (round-2
D-1, P3, deferred with routing — see Boundary surface): URI-shaped values
(`scheme://`, `s3:`, `published:`) stay behind the whitespace bail-out, so a
URI-shaped root or prefix containing a space still renders token-wise
(`s3://nhms prod/objects` → `[object-uri] prod/objects`); no realistic
configuration produces one (bucket names cannot contain spaces; nobody uses
a `file://` workspace root). Over-redaction side effect of the fix: under a
generic non-message key a `/`-leading prose value collapses to
`[local-path]` whole; `message`/`*_message` keys stay tokenised via
`_public_message`; no repo producer writes leading-slash prose under a
generic key.

The `ManualRetryService` Protocol docstring (`retry.py:327-345`) gains one
sentence: `submission_runtime_root_resolution` returns the public-rendered
mapping (no absolute local roots) on every lane.

## D4 — Tests

Placement: no new test file (selector governance cost). Top-level test
imports of the leaf are fine: the selector's importer index is queried only
by test-module name (`scripts/select_ci_tests.py:184-199`, `:2789`), and
`services/orchestrator/public_evidence.py` alone already routes to the
orchestrator lane including both edited suites (fixture review ran the selector).

- **T1 DB route (red today)** in `tests/test_retry.py`: `RetryService` +
  `_insert_submission_event` with `workspace_dir="/srv/nhms/workspace"`,
  `object_store_root="/srv/nhms/object-store"` + `_RecordingGateway(error=...)`
  → 503; `str` of both roots absent from the body; shape helper; persisted
  event (`_events(store)[-1]`) keeps the real values.
- **T2 DB rejected URI**: candidate `object_store_root="https://user:pw@example.com/object-store"`
  (secret-bearing → `rejected` with reason `url_userinfo`, as in `:1750`):
  body `rejected[].value == "[uri]"`, persisted keeps `https://example.com/object-store`,
  secrets absent from both — extends the existing `:1746` test's assertions
  (additions only; existing lines untouched) or a sibling test; implementer's
  call, recorded.
- **T3 shape helper** `_assert_public_runtime_root_resolution(mapping)`: for
  every `resolved.*` entry: Mapping, `present is True`, `source` non-empty str,
  `value == "[local-path]"` when the field is in `_LOCAL_RUNTIME_ROOT_FIELDS`
  (`retry.py:96`) and `value in {"[object-uri]", "[uri]"}` otherwise
  (`object_store_prefix`, `published_artifact_uri_prefix` are URI-valued);
  `object_store_root` has `same_as_workspace` bool when `workspace_dir` present;
  every `rejected[].value` and `db_free_runtime.resolved.*.value` is one of
  the three placeholders or `[redacted]`; `json.dumps(mapping)` has no `/srv/`
  and no `tmp_path` text (caller passes the roots to forbid). Called from T1
  and from PR #1963's file-lane T1 (`:3065`, addition only) — the "both
  lanes, same assertions" scenario.
- **T4 file-lane write side** `tests/test_file_orchestration_journal.py:4468`:
  add `object_store_root["present"] is True`, `["source"]` non-empty,
  `["value"] == "[local-path]"`; `:4469` untouched.
- **T5 legacy passthrough** (file lane, `tests/test_retry.py`): the reader
  is latest-first (`:11577-11586`) and `attempt_manual_retry` writes the new
  retry job's own `submission` event before the route reads (`:12586`), so a
  legacy event appended before the POST is shadowed or on another job. Inject
  through `_post_file_lane_retry`'s `after_retry` seam (`tests/test_retry.py:2977-3005`):
  append, on `retry_row.job_id` with a higher `event_id`, a `submission` event
  whose `runtime_root_resolution.resolved.object_store_root` is the bare
  string `"[local-path]"` (journal's own append; no hand-edited file);
  POST → 503 with that entry returned as recorded (string, not mapping).
- **T6 leaf unit pins** (in `tests/test_file_orchestration_journal.py`):
  (a) input `resolved` carrying `workspace_dir`, `object_store_root` (with
  `same_as_workspace`) and `published_artifact_root` mappings plus a scalar
  `runtime_root_contract`: all three mappings recurse with `value == "[local-path]"`
  and `present`/`source` kept, the scalar `_root` → `[local-path]`, a
  sensitive key inside the same mapping → `[redacted]` first — pinned with
  keys that are BOTH sensitive and path-shaped (`credential_path` mapping
  value, `secret_path` scalar; round-1 cand-02: the ordering was unpinned by
  `api_token` alone, and the mutant leaked the mapping in clear); (b) idempotency
  `f(f(x)) == f(x)` on a DB-shaped evidence mapping AND on the post-D2
  file-lane output (the recursed form as input), the DB-shaped literal
  carrying a DSN-valued `db_free_runtime.resolved.scheduler_registry_backend`
  (`[uri]`) and a local-path manifest (`[local-path]`) asserted explicitly
  (round-1 cand-06: the shape helper's `db_free_runtime` loop iterates zero
  times on both T1s); (c) classifier parity:
  `_public_path_or_uri_placeholder(v) == scheduler_file_providers._sanitize_file_provider_scalar(v)`
  over `["/srv/x", "~/x", "s3://b/k", "published://p", "https://u:p@h/x", "/srv/my dir", "", "  ", "plain", 7, None]`.
- **T7 whitespace-bearing roots** (round-1 cand-01): unit — `_public_evidence`
  over an evidence mapping whose three resolved roots and one `rejected[].value`
  contain a space → every value exactly `[local-path]`, the tail absent;
  `_sanitize_public_path_or_uri_scalar("/srv/my dir") == "[local-path]"`, same
  for `~/my dir`; a whitespace-bearing URI keeps today's token path (stated,
  not widened). Route — file lane with roots under a tmp directory containing
  a space: `resolved ⊇ {workspace_dir, object_store_root}`, `missing == []`,
  neither the root nor its post-space tail in the 503 body, shape helper
  passes. DB lane pinned at `RetryService.submission_runtime_root_resolution`
  (the exact call the route makes) with `/srv/nhms data/...` roots, durable
  event keeps the real values. The fix's boundary (a `/` anywhere must NOT
  classify) is pinned by pre-existing relative-token expectations in the
  journal suite (`journal/gfs/2026062800.jsonl` at `tests/test_file_orchestration_journal.py:928`
  and four siblings): an over-broad mutant fails ten of them (round-2 review).
- **Existing DB 503 tests** (`:1746`, `:1817`, `:2290`): unchanged lines; the
  `:1746` test's `missing` assertion is a list and unaffected.

## Invariant matrix

| # | invariant | pinned by |
|---|---|---|
| 1 | DB 503 body has no absolute local root | T1, T7 |
| 2 | DB persisted details keep real roots | T1 + `:1209/:1242/:1245` |
| 3 | secrets precedence over path rendering | T2 + `:1783-1800`, T6a (sensitive+path-shaped keys) |
| 4 | `resolved.*` is a Mapping with `present/source/value` on both lanes | T3 on both |
| 5 | `runtime_root_contract` stays flat scalar | `:4469` |
| 6 | file-lane response == persisted (no second scrub) | `:3087` |
| 7 | legacy bare-string entries pass through | T5 |
| 8 | renderer idempotent | T6b |
| 9 | leaf classifier == providers classifier | T6c |
| 10 | every prior importer of the moved names resolves | ruff + the four suites |

## Boundary surface

- New file `services/orchestrator/public_evidence.py`; `.large-file-guard.json`
  unchanged (journal, retry, pipeline.py, the three test files already excluded).
- CI selector: `services/orchestrator/**` selects the orchestrator lane;
  `tests/test_select_ci_tests.py` must stay green (no new test file).
- Migration receipts: `file_orchestration_migration.py:1613/:1618` sample DB
  rows through `_public_evidence`; a `_root` subtree grows from a 12-byte
  string to a four-key mapping. The implementer checks the per-relation
  sample size cap around those lines and reports whether any receipt test
  flips (expected: none — the cap bounds row count, not bytes; verify).
- Residual duplicate: `scheduler_file_providers._sanitize_file_provider_scalar`
  and the whole-value `_root` rule in `_sanitize_file_provider_evidence_scalar`
  (`:2249-2252`) remain; pinned by T6c; reported, not consolidated (module is
  outside the guard exclusions; no runtime-root evidence flows through it).
- Routed follow-ups: #1975 (round-1 side finding: the 503's `error_message`
  and the `GET /api/v1/jobs` read surfaces pass `job.error_message` through
  `redact_payload` only — same body, unrendered path channel; pre-existing);
  #1976 (round-2 D-1, P3: URI-shaped values with whitespace still render
  token-wise behind the bail-out; `tests/test_file_orchestration_journal.py`
  T7 pins today's behaviour for `s3://bucket/my key` explicitly, so the fix
  must rewrite that pin deliberately).
