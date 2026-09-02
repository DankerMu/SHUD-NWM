## Why

`packages/common/timescale_write_guard.py` raises `CompressedChunkWriteError` (subclass: a write really targets a compressed chunk) and `CompressedChunkGuardError` (base: the guard itself failed — partial batch window, unregistered hypertable, or the catalog SELECT timing out under its own 5 s budget). Eight `except CompressedChunkGuardError` arms in `workers/forcing_producer/producer.py:806`, `workers/forcing_producer/cli.py:105,142`, `workers/output_parser/parser.py:275` and `workers/output_parser/cli.py:57,71,98,108` treat both as "compressed chunk blocked" and stamp `FORCING_COMPRESSED_CHUNK_BLOCKED` / `OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED` (DB `error_code`) or the matching CLI stderr prefixes. Runbook §4.3.1 today counts "five" codes, labels the three non-handoff codes **ambiguous** and carries an interim caveat naming #1785 ("the code is a hint, not a verdict"), so an operator must read free text to decide whether a decompression (§4.3.2) is warranted — a transient DB contention can still turn into a needless decompression of a large chunk, and a caller bug can masquerade as a compressed-chunk hit. Four tests pin the confusion in. PR #1784 already split the apply layer (`HANDOFF_APPLY_COMPRESSED_CHUNK_GUARD_FAILED`); this batch mirrors that split at the remaining callers. (#1785)

## What Changes

- Each of the eight arms becomes two: `except CompressedChunkWriteError` first (existing `..._BLOCKED` codes and prefixes unchanged), then `except CompressedChunkGuardError` with new codes `FORCING_COMPRESSED_CHUNK_GUARD_FAILED` (producer `error_code`), `OUTPUT_PARSE_COMPRESSED_CHUNK_GUARD_FAILED` (parser `error_code`) and CLI stderr prefixes `FORCING_PRODUCE_COMPRESSED_CHUNK_GUARD_FAILED:` / `OUTPUT_PARSE_COMPRESSED_CHUNK_GUARD_FAILED:`; the generic `except Exception` stays last (producer's `ForcingProductionError` wrapping contract unchanged).
- The four pinned tests become 16 cases, one per arm per class (subclass → `_BLOCKED`, base → `_GUARD_FAILED`), driving the argparse legs through `_argparse_main(argv)` directly and the output-parser second subcommand with `["parse", "--run-id", …]`.
- Runbook §4.3.1 edited at its six sites (count `:1203`, intro `:1204-1209`, code rows `:1217-1219` + three new `_GUARD_FAILED` rows, `HANDOFF_APPLY_*`-only routing sentence `:1221-1223` generalised, interim caveat `:1225-1236` deleted, triage paragraph `:1238-1241` generalised): only subclass `_BLOCKED` codes route to decompress, `_GUARD_FAILED` codes to DB-health / caller-bug triage. `openspec/changes/tier-node27-timeseries-storage/design.md:1713-1715, :1780-1781, :1792-1794` are superseded by one dated correction bullet pointing at this change.

## Capabilities

**Modified Capabilities**
- `hypertable-compression` — guard-failure codes distinct from compressed-chunk-blocked codes at every caller.

## Impact

- Code: the four modules above; no change to `timescale_write_guard.py`, no terminal-state semantics (#1781's domain), no change to `scripts/node27_river_identity_backfill.py` (neutral stage names, recorded).
- Tests: `tests/test_forcing_producer.py`, `tests/test_output_parser.py`, `tests/test_forcing_producer_cli.py`, `tests/test_output_parser_cli.py`; `tests/test_timescale_write_guard.py` unchanged and green.
- Docs: `docs/runbooks/tier-node27-timeseries-storage.md` §4.3.1; active change `tier-node27-timeseries-storage/design.md` correction.
- No node-27 receipt required (pure exception routing; unit tests are the oracle). The PR gate's `select_ci_tests` does select these suites on this diff (measured `--changed-file` count=29, incl. the four changed suites and both wire-site guard suites; the #1656 claim in the issue body does not hold here) — the explicit pytest run is posted regardless.
