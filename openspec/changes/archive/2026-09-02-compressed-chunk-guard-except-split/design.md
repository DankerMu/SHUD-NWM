## Context

Issue #1785 — diagnostics only: the guard remains fail-closed, no data path changes, but the operator route on the codes is wrong. Fixture level `expanded` (error-code contract across four modules + runbook), repair intensity `medium`. Repo facts (fixture review, revision 1): the eight base arms are confirmed at `workers/forcing_producer/producer.py:806`, `workers/forcing_producer/cli.py:105,142`, `workers/output_parser/parser.py:275`, `workers/output_parser/cli.py:57,71,98,108`; a ninth literal `except CompressedChunkGuardError` sits in a comment at `producer.py:812` (and `parser.py:276-278` carries the same now-false routing comment); the existing codes are inline literals (`producer.py:821`, `parser.py:281`, `cli.py` sites), not constants; both CLIs' `main()` prefer the click leg whenever click imports (`forcing_producer/cli.py:150`, `output_parser/cli.py:118`) and every existing test calls only `main()`, so the argparse arms (`forcing_producer/cli.py:142`; `output_parser/cli.py:71,98,108`) are unreached by the current suites; no file under `openspec/specs/` contains `FORCING_COMPRESSED_CHUNK_BLOCKED`, `FORCING_PRODUCE_COMPRESSED_CHUNK_BLOCKED` or `OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED` (hence ADDED, not MODIFIED), while `openspec/specs/hypertable-compression/spec.md:1235-1241` pins the apply layer's `HANDOFF_APPLY_*` split; the runbook §4.3.1 already carries an interim caveat naming #1785 (`docs/runbooks/tier-node27-timeseries-storage.md:1204-1209`, `:1225-1236`) and a "five codes" count at `:1203`; `openspec/changes/tier-node27-timeseries-storage/design.md:1713-1715,1780-1781,1792-1794` carry the superseded base-arm contract; both `error_code` columns are plain `TEXT` (`db/migrations/000005_met.sql:21`, `000006_hydro.sql:18`).

## Goals / Non-Goals

**Goals**: a guard-internal failure is never reported as a compressed-chunk hit at any of the eight arms; the existing `_BLOCKED` contract is byte-stable; the runbook routes on the new codes.

**Non-Goals**: the guard's exception hierarchy; terminal-state/decline semantics; the backfill script's tuple catches (`scripts/node27_river_identity_backfill.py:1233,1521-1522,1743`, neutral stage names); the apply layer and its `HANDOFF_APPLY_*` codes (done in #1784, pinned by the existing spec scenario).

## Decisions

### D1 — mirror #1784's shape at every arm, subclass first, generic last
Order is a hard constraint (producer :814-816 comment): `except CompressedChunkWriteError` → `except CompressedChunkGuardError` → `except Exception`. The base arm does exactly what the old arm did (stamp/prefix + re-raise) with the new code; nothing else in the arm body changes, but the routing comments at `producer.py:812-816` and `parser.py:276-278` are rewritten so they no longer describe the base arm as a compressed-chunk hit. The new codes follow the existing style — inline string literals at the arm, no new constants (a constants refactor of the old literals is out of scope), so the evidence-floor grep counts literals; the comment at `producer.py:812` is reworded so `grep -c "except CompressedChunkGuardError"` reports exactly 8 code arms.

### D2 — tests per arm, both CLI legs
Each pinned test becomes a pair: raise the subclass → assert `_BLOCKED`; raise the base → assert `_GUARD_FAILED`. Explicit inputs per arm: producer and parser via their existing fake-store fixtures (2 arms × 2 = 4 cases); forcing CLI click leg via `_click_main` and argparse leg via `_argparse_main(argv)`, both called directly so the leg is deterministic (2 arms × 2 = 4); output-parser CLI both subcommands × both legs, the second subcommand driven with `["parse", "--run-id", …]` on `_click_main` and on `_argparse_main` (4 arms × 2 = 8). Total 16 cases, one per arm per class. A mutation check recorded in the PR body: swapping the two arms' order in each module makes the 8 subclass cases report `_GUARD_FAILED` (red).

### D3 — runbook routes on the code, design.md corrected in place
§4.3.1 as it stands: `:1203` "five" → "eight" codes; the intro `:1204-1209` drops the "other three … ambiguous … tracked as #1785" classification; rows `:1217-1219` rewritten as subclass-only `_BLOCKED` rows plus three new `_GUARD_FAILED` rows; `:1221-1223` "for the two `HANDOFF_APPLY_*` codes the routing is exact" generalised to all four families; the interim caveat `:1225-1236` deleted; the triage paragraph `:1238-1241` generalised beyond the handoff code. Evidence floor gains the negative grep anchored on §4.3.1's own strings (`therefore **ambiguous**`, `hint, not a verdict`, `#1785`); bare `ambiguous` at `:626` and `:3353` is unrelated prose and must survive. The active `tier-node27-timeseries-storage/design.md` gets one dated bullet under its "post-review corrections" list stating the base-arm contract at `:1713-1715`, `:1780-1781`, `:1792-1794` is superseded by this change; evidence floor greps that bullet.

## Invariant Matrix

- Governing invariant: a `..._COMPRESSED_CHUNK_BLOCKED` code is produced only by an arm that caught `CompressedChunkWriteError`; every other guard exception surfaces as `..._GUARD_FAILED`; the generic wrapper never sees either.
- Source-of-truth identity/contract: the four `error_code` literals + four CLI prefix literals (inline, matching existing style); `met.forecast_cycle.error_code`; `hydro.hydro_run.error_code`.
- Producers: producer `_mark_failed`, parser `_mark_run_failed_preserving_error`, the two CLIs.
- Validators/preflight: the 16 paired cases incl. direct `_argparse_main` calls; order-mutation check; runbook negative grep.
- Storage/cache/query: the two `error_code` columns (values only).
- Public routes/entrypoints: forcing CLI (click + argparse), parser CLI (two subcommands × two legs).
- Frontend/downstream consumers: operators via §4.3.1; ops dashboard reading `error_code`.
- Failure paths/rollback/stale state: both arms re-raise as before; retryability unchanged (`failed_forcing`, `hydro_run` failed).
- Evidence/audit/readiness: explicit pytest run posted; `grep -n "except CompressedChunkWriteError\|except CompressedChunkGuardError"` output showing 8+8 code arms in order (comment reworded); `grep GUARD_FAILED` on the runbook and the negative grep; the design.md supersede bullet.
- Regression rows:
  - producer raises `CompressedChunkWriteError` -> `FORCING_COMPRESSED_CHUNK_BLOCKED`; raises base -> `FORCING_COMPRESSED_CHUNK_GUARD_FAILED`; raises `RuntimeError` -> `ForcingProductionError` wrap as before.
  - parser: same pair with `OUTPUT_PARSE_*`.
  - CLIs: stderr prefix pair, exit 1, both legs/subcommands.
  - unchanged sibling: `forcing_domain_handoff_apply.py` `HANDOFF_APPLY_*` codes and the spec scenario at `hypertable-compression/spec.md:1235-1241` untouched; backfill tuple catches untouched.

## Boundary surfaces

Error-code contract (four modules, two DB columns, two CLIs). Docs: runbook + active change design. Unchanged downstream: guard module, apply layer, backfill.
