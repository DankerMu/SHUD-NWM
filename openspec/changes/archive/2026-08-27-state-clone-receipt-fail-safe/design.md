## Context

`run_recalibration()` already converts an in-loop exception into an aborted receipt after an earlier pair wrote live canonical/mirror rows, but writes that receipt before re-raising without protecting the original exception. `run()` for `baseline_cutover` writes each warm clone immediately yet only assembles its receipt after the whole nested loop, so any later failure skips evidence entirely. Recalibration also permits omitting `--receipt`, despite its receipt being the declared carry-over artifact.

Fixture level: expanded. Repair intensity: high. Project profile: NHMS.

## Goals / Non-Goals

**Goals:**

- Preserve the governing evidence invariant across both file-index transfer modes.
- Preserve the original operational failure as the primary exception while making a failed receipt attempt visible.
- Keep clean success, clean receipt-write failure, dry-run, first-item refusal, `O_EXCL`, and baseline legacy CLI behavior stable except where explicitly changed.

**Non-Goals:**

- Change clone admission gates, row contents, rollback semantics, state-index schemas, receipt schema versions, or overwrite `O_EXCL` receipts.
- Make `--receipt` mandatory for `baseline_cutover`.
- Add whole-batch transactions or roll back rows already persisted by this CLI.
- Change node-22 deployment or run Slurm/SHUD scheduling validation.

## Decisions

### D1 — One helper owns failure-aware receipt writes

Use one small helper for both modes. It writes with the existing `_write_receipt` implementation. When there is no primary error, any receipt error propagates unchanged. When a primary error exists, an `OSError` from receipt persistence is attached to that primary error with an exception note, after which the primary error is raised. This keeps `O_EXCL` and storage failures visible without replacing the reason the clone stopped.

Alternative rejected: preflight `Path.exists()`. It has a TOCTOU gap and cannot cover permission, capacity, or filesystem failures at the point of write.

### D2 — Baseline captures the whole processing unit, not selected raisers

Wrap each baseline basin/source unit so any exception after earlier live writes records the current location and error, stops further work, builds the normal receipt from completed `decisions`, adds an invocation-level aborted marker and failed location, attempts persistence, then re-raises the original exception. A failure before any state row is written does not consume an `O_EXCL` receipt path. Complete receipts retain their existing fields and values byte-for-byte apart from naturally variable `generated_at`.

Alternative rejected: a broad outer `finally`. It would create empty receipts for preflight failures and blur complete versus partially applied runs.

### D3 — Recalibration receipt is unconditionally required

Add `receipt` to the recalibration per-mode required-flags table. Dry-run must also create its reviewable artifact; the runbook already depends on inspecting that receipt before `--apply`. Baseline remains unchanged because its legacy contract and runbook are distinct.

### D4 — Every owned suite joins the production script's targeted-CI route

Keep baseline-specific fixtures and regressions in `tests/test_state_clone_baseline_cutover_cli.py` rather than making the already-large recalibration CLI suite own a second mode. The repository's 1000-line source guard also requires the recalibration CLI suite to split exactly at the existing `# --- §6.8 --pairs resolution` marker: end-to-end/partial-write tests before that marker remain in `tests/test_state_clone_recalibration_cli.py`, while that marker and every following pair-resolution, registry-payload, and per-mode flag test move without semantic changes to `tests/test_state_clone_recalibration_cli_validation.py`. The moved module imports the real CLI environment helpers it needs rather than replacing dispatch/apply coverage with parser-only stubs. List all four suites in `NODE22_CLONE_CUTOVER_STATES_TESTS` and strengthen the selector contract test, so a future production-only change to the shared CLI cannot omit either mode or validation boundary. Because the recalibration core, both recalibration CLI modules, and the baseline CLI module all directly import `tests/state_clone_recalibration_fixtures.py`, its explicit support-module route lists all four consumers. The baseline suite's direct import of `workers.mapping_builder.rewrite` remains owned by the node-22 script surface, so the selector audit records that derived pair as an `edge-consumer` rather than contaminating every mapping-builder change with the baseline CLI suite.

Alternatives rejected: use a large-file exclusion, which evades a valid maintainability gate; or rely on changed-test self-selection in this PR, which makes this PR green but leaves future production-only diffs without the complete oracle.

## Risks / Trade-offs

- **New aborted fields on partial baseline receipts** → additive only; successful baseline receipt mappings stay unchanged and tests compare their exact existing shape.
- **Exception-note portability** → Python 3.11+ is repository policy and supports `BaseException.add_note`; tests assert both original error and receipt failure are observable.
- **Catching too broadly** → only the established unit-of-work loop is captured; preflight remains outside and the exact exception object is re-raised.
- **Receipt failure with live rows remains unresolved evidence debt** → unavoidable when storage itself refuses the write; the primary error carries a visible note so operators know both facts and can inspect/repair state indexes.

## Invariant Matrix

Governing invariant: if a file-index clone invocation has made any clone row live before aborting, it SHALL attempt a persistent receipt that enumerates every completed write; failure to persist that receipt SHALL be visible without replacing the clone's original abort reason.

Source-of-truth identity/contract: completed decision/pair records and their `state_id`/per-index outcomes, bound to the requested receipt path and original exception object.

Surfaces:

- Producers: `run()` and `run_recalibration()` in `scripts/node22_clone_direct_grid_cutover_states.py`.
- Validators/preflight: `enforce_mode_flags`; existing warm/cold partition, package, fingerprint, and state-compatibility validation.
- Storage/cache/query: `FileStateSnapshotIndexRepository.upsert_state_snapshot`; existing canonical and mirror JSON indexes.
- Public routes/entrypoints: `build_parser`, `enforce_mode_flags`, `dispatch`, and module `main()`.
- Frontend/downstream consumers: operator/runbook receipt inspection; scheduler state-index readers remain unchanged.
- Failure paths/rollback/stale state: later baseline failure after an earlier write; later recalibration refusal; mirror-write failure; first-item failure; dry-run; receipt `O_EXCL`/filesystem failure.
- Evidence/audit/readiness: baseline and recalibration receipt JSON plus exception text/notes, focused pytest evidence, and `scripts/select_ci_tests.py` routing all four owned suites for a production-script diff, all four direct consumers for a base-fixture diff, and both recalibration CLI consumers for a CLI-helper diff.

Regression rows:

1. Baseline `--apply`: first warm source persists, later source/basin fails → receipt lists every completed persisted decision and failed location/reason; original exception propagates.
2. Either mode aborts after a live write and receipt path already exists → original clone/mirror exception propagates and contains an observable note naming `FileExistsError`; existing receipt is unchanged.
3. Clean invocation with an existing receipt path → `FileExistsError` still propagates; no error is silently swallowed.
4. Recalibration apply or dry-run without `--receipt` → parser-style `SystemExit`; with a unique path, successful apply writes JSON equal to the returned payload.
5. Baseline legacy invocation without `--receipt` → parses and behaves as before; complete successful receipt mapping retains all existing fields and meanings.
6. First-item refusal or dry-run with no live row → no abort receipt is created solely because an error occurred.

## Boundary-Surface Checklist

- Shared helper roots: `_write_receipt` plus the new failure-aware wrapper.
- Public entrypoints: parser required-flags enforcement and both `dispatch` routes.
- Read surfaces: unchanged registry/package/index reads.
- Write/delete/overwrite surfaces: canonical/mirror upserts and receipt `O_EXCL`; no delete or overwrite added.
- Staging/publish/rollback surfaces: incremental index publication remains; no rollback is claimed.
- Producer/consumer evidence boundaries: returned payload, persisted JSON, and exception note agree on complete/failed work.
- Stale-state/idempotency boundaries: existing receipt is never overwritten; corrected reruns require a fresh path.
- Unchanged downstream consumers: scheduler admission and state-index parsing.

## Open Questions

None.
