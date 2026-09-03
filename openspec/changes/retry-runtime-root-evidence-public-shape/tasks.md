# Tasks

Fixture level: compact · repair intensity: standard · issues: #1961, #1965.
Line cites against `origin/master` `f9a1345f`; symbol names are authoritative.

## 0. Evidence Floor

Oracles: local pytest (macOS) for red/green; node-27 for the real-DB run of
the same suites; CI status read at every head (recorded per round).

- [x] Red proof (pre-change): T1 fails with `/srv/nhms/workspace` present in the 503 body; T4's `object_store_root["present"]` fails with `TypeError: string indices must be integers` (bare string) — scratch `red_proof.txt`, `2 failed in 0.92s` at f9a1345f
- [x] `uv run pytest -q tests/test_retry.py tests/test_retry_cancel_consistency.py tests/test_file_orchestration_journal.py tests/test_production_scheduler.py` green locally (2785 passed / 2 skipped at 7364748a; + `tests/test_file_orchestration_migration.py`: 2848 passed / 2 skipped after the round-1 fix pass)
- [x] `uv run pytest -q tests/test_select_ci_tests.py` green locally (460 passed after rebase onto 57ddc545; no new test file)
- [x] Production-side blast-radius scan (3 hits: `runtime_root_resolution.resolved` reaches; `scheduler_evidence.py:746-802` via `write_evidence` direct artifact write and `node27_cold_tablespace_observation.py:64` do not): dict literals / assignments placing a Mapping under a `*_root` / `*_path` / `path` / `root` key across `services/ packages/ apps/ workers/`, each hit classified reaches-`_public_evidence` or not; result recorded in the PR body (expected: only `runtime_root_resolution.resolved`)
- [x] Migration receipt check (`rows[:8]` bounds row count; inputs flat `dict[str, str]`; 60 passed): `file_orchestration_migration.py:1613/:1618` sample cap semantics read and recorded; no receipt test flips
- [x] `uv run ruff check .` clean
- [ ] node-27 receipt for the same four suites (+ `tests/test_api*.py` if present), command line and counts recorded in `.workplans/pr-NNNN/node27-receipt-SHA.txt` (NNNN = PR number, SHA = head)
- [x] Frozen surfaces diff-empty vs `origin/master` (checked at 7364748a and after the fix pass; `scheduler_state_failure.py` gained a comment-only repoint of the moved symbol): `_runtime_root_resolution_evidence`, `_runtime_root_resolution_from_error`, `_runtime_root_contract_from_error`, `_redacted_mapping`, `_bounded_redacted_text`, `get_retry_service`, `retry_run`'s 503 arm (`pipeline.py:549-568`), file-lane write sites `:11724-11726` / `:12586-12589`, file-lane reader body (`:11517-11596`, docstring-only change allowed), `scheduler_file_providers.py` (untouched)
- [x] CI green on every pushed head, recorded in the round ledger (7364748a run 33701580924: Unit Tests success, 5262 passed; fix head: see ledger)
- [x] `openspec validate retry-runtime-root-evidence-public-shape --strict --no-interactive`

## 1. Leaf module (D1)

- [x] 1.1 `services/orchestrator/public_evidence.py`: the nine functions moved verbatim from `file_orchestration_journal.py:13059-13166`; local `_safe_error_message` (same body as `retry.py:1276-1278`) and `_public_path_or_uri_placeholder` (same body as `scheduler_file_providers.py:2258-2271`); imports limited to `packages.common.redaction`, `services.orchestrator.scheduler_state_common` (`_format_utc`), `urllib.parse`, stdlib; module docstring naming both lanes as consumers and the cycle reason
- [x] 1.2 journal: block deleted; imports exactly `_public_evidence` and `_public_message` from the leaf; `_sanitize_file_provider_evidence_scalar` import (`:140`) removed (zero uses after the move); `_safe_error_message` import from retry kept
- [x] 1.2b `tests/test_production_scheduler.py:13501` repointed to `services.orchestrator.public_evidence._sanitize_public_field` (one line)
- [x] 1.3 import-cycle proof: `uv run --no-sync python -c "import services.orchestrator.retry"` and `... import services.orchestrator.public_evidence` each succeed with neither the journal nor providers loaded (assert via `sys.modules`); recorded in the PR body

## 2. File lane recursion (D2, #1965)

- [x] 2.1 `_sanitize_public_field`: Mapping value under a `_path`/`_root`/`path`/`root` key → `_sanitize_public_evidence(value)`; scalar branch byte-identical; ordering (`is_sensitive_key` → message → path → uri → generic) unchanged
- [x] 2.2 file-lane reader docstring (`:11517`): one sentence on the two persisted shapes (bare string before this change, mapping after) both reaching the route unchanged

## 3. DB read side (D3, #1961)

- [x] 3.1 `RetryService.submission_runtime_root_resolution`: `return _public_evidence(_redacted_mapping(evidence))`; `retry.py` imports `_public_evidence` from the leaf
- [x] 3.2 `ManualRetryService` docstring: one sentence on the public-rendered return on every lane

## 4. Tests (D4)

- [x] 4.1 T1 DB route red→green (both roots absent; shape helper; persisted real values)
- [x] 4.2 T2 DB rejected URI `[uri]` on the wire, credential-stripped URL persisted, secrets absent from both
- [x] 4.3 T3 shape helper (local roots → `[local-path]`, URI-valued prefix fields → `[object-uri]`/`[uri]`) called from T1 and from the file-lane T1 (`tests/test_retry.py:3065`, addition only)
- [x] 4.4 T4 file-lane write-side pin extended at `tests/test_file_orchestration_journal.py:4468` (`:4469` untouched)
- [x] 4.5 T5 legacy bare-string passthrough on the file-lane route, injected via `_post_file_lane_retry`'s `after_retry` seam on `retry_row.job_id` with a higher `event_id`
- [x] 4.6 T6 leaf pins: recursion for `workspace_dir` / `object_store_root` / `published_artifact_root` + scalar `_root` + sensitive precedence; idempotency on the DB shape and on the recursed file-lane shape; classifier parity vs `scheduler_file_providers._sanitize_file_provider_scalar` on the listed corpus
- [x] 4.7 Existing DB 503 tests (`:1746`, `:1817`, `:2290`) and the file-lane tests from PR #1963 unchanged except additions
- [x] 4.8 (round-1 cand-01) `_sanitize_public_path_or_uri_scalar` classifies `/`- and `~`-leading text before the whitespace bail-out; T7 unit + file-lane route + DB service pins (three pins red on the unfixed source)
- [x] 4.9 (round-1 cand-02 / cand-06) T6a sensitive+path-shaped keys; T6b `db_free_runtime.resolved` DSN / local-path assertions (both mutants killed)

## Risk packs

- Security light — selected (public surface loses absolute roots; secrets precedence pinned).
- Legacy compat — selected (module extraction keeps importers; historical bare-string events tolerated).
- Contract — selected (one wire shape, both lanes, same assertions; Protocol docstring).
- Error handling — not selected (no new fault boundary; renderer total on JSON-shaped input).
- File IO / path safety, Concurrency, Performance — not selected (pure functions over a bounded mapping).

## Non-goals

See proposal: persisted DB details, `_runtime_root_resolution_from_error`, `redact_payload`, `runtime_root_contract` flattening, Sequence values under path keys, providers' classifier copy, historical event rewrite, production wiring.
