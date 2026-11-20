## ADDED Requirements

### Requirement: The timer tick SHALL log a one-line digest and the full summary only when the tick is not quiet

`scripts/node27_autopipeline.py` SHALL accept `--summary-mode {full,tick}` with default `full`. In `full` mode stdout SHALL be the single pretty-printed JSON summary. In `tick` mode a pass that reaches its final summary (including a `--seed-only` pass) SHALL first print one line holding a JSON object with schema `nhms.node27.ingest_tick_digest.v1`, serialised with sorted keys and the default `", "` / `": "` separators, carrying: `status`, `return_code`, `quiet`, `discovered_runs`, `declines_active`, `runs` (`already_ingested`, `published`, `processed`, `ingested`, `skipped`, `failed`, `declined`), `seed` (counts `seeded`, `already_seeded`, `failed`), `job_provenance` (`imported`, `unavailable`, `failed`, `skipped_unchanged`, the last one 0 when the summary does not carry it), `stats_guard` (`status`, `authority_status`), `cycle_floor` and `skipped_older_than_floor` as the summary carries them, `object_store_root`, and `ingest` (`role`, `stage_shape`, `config_source`, `database` as the preflight reports it, without secrets). It SHALL then print the full pretty-printed summary if and only if the tick is not quiet. The summaries of the preflight exits, which end the pass before any seed or run work, SHALL be printed in full in both modes without a digest line. The node-27 timer wrapper SHALL pass `--summary-mode tick`.

A tick is quiet only when all of the following hold: `return_code` is 0; `runs.ingested`, `runs.skipped`, `runs.failed` and `runs.declined` are 0; `seed.seeded`, `seed.failed` and `seed.activation_skipped_other_active` are empty; `job_provenance` `imported`, `unavailable` and `failed` are 0; `declines_active` is the integer 0; `stats_guard.status` is `not_triggered` or `skipped`; `stats_guard.authority.status` is `completed` or `skipped` with empty `analyzed` and `deferred`.

#### Scenario: A quiet tick logs one line

- **WHEN** a `tick`-mode pass ingests nothing, fails nothing, declines nothing, seeds nothing, imports no provenance and has no active decline
- **THEN** stdout is exactly one line, a JSON object with `"quiet": true`
- **AND** the line contains the text `"declines_active": 0`

#### Scenario: A failing run keeps its stage and reason in the log

- **WHEN** a `tick`-mode pass has a run that fails or is declined
- **THEN** the digest line has `"quiet": false` and the full summary follows it, recording that run's stage and stable reason

#### Scenario: An unknown decline count is not quiet

- **WHEN** `declines_active` could not be read and is null
- **THEN** the tick is not quiet and the full summary is printed

#### Scenario: Every tick identifies the ingest plane

- **WHEN** an operator reads the digest line of any tick, quiet or not
- **THEN** it names the data-plane ingest role, the stage shape, the object-store root, the database host and port without secrets, the discovered and processed counts and the return code

#### Scenario: A preflight exit is printed in full

- **WHEN** a `tick`-mode pass is blocked by preflight
- **THEN** stdout is the full blocked summary and no digest line

#### Scenario: The default output is unchanged

- **WHEN** the script runs without `--summary-mode`
- **THEN** stdout is one pretty-printed JSON summary document and no digest line

### Requirement: The autopipe log SHALL be bounded by rotation under the tick lock

The node-27 timer wrapper SHALL rotate its log after it holds the tick lock and before it writes `autopipe: start`: when the log is a regular file of at least `AUTOPIPE_LOG_MAX_BYTES` bytes (default 67108864; `0` disables rotation) it SHALL keep at most `AUTOPIPE_LOG_KEEP` rotated generations (default 5) named `<log>.1` (newest) to `<log>.<KEEP>`, dropping the oldest and any generation numbered above `KEEP` that an earlier, larger setting left behind. An `AUTOPIPE_LOG_MAX_BYTES` that is not a non-negative integer and an `AUTOPIPE_LOG_KEEP` that is not a positive integer (including `0`) SHALL fall back to their defaults. A tick that does not obtain the lock SHALL NOT rotate. A rotation error SHALL be logged and SHALL NOT change the tick's exit code.

#### Scenario: A log at the bound is rotated before the tick writes

- **WHEN** the wrapper takes the lock and the log is at or over the bound
- **THEN** the previous content is in `<log>.1`, older generations have shifted by one, no generation beyond `<log>.<KEEP>` exists
- **AND** the new log starts with a rotation line followed by `autopipe: start`

#### Scenario: A log under the bound is left alone

- **WHEN** the log is smaller than the bound, or the bound is `0`
- **THEN** no file is moved and the tick appends to the existing log

#### Scenario: A skipped tick does not rotate

- **WHEN** another tick holds the lock
- **THEN** the wrapper appends its skip line and exits 0 without moving any file
