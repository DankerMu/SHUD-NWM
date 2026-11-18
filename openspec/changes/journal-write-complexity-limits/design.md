## Design — journal-write-complexity-limits (#3197)

Change surface:
- `services/orchestrator/file_orchestration_journal.py`: `_validate_outgoing_record`, a shared complexity helper, and the
  `_LATEST_VIEW_DEPTH_HEADROOM` mapping;
- `services/orchestrator/file_orchestration_migration.py`: the historical-import pre-scan;
- possibly the cluster-map fixture and the runbook line;
- tests.

Must preserve:
- every currently round-tripping record is unchanged;
- write ordering and locking;
- a refusal writes zero bytes;
- restore lanes stay unchecked.

Must change: no journal write appends a record whose own files (the jsonl line and the direct row) a reader with the same
instance limits would refuse, nor one whose latest-view depth exceeds the reader's limit.

Governing invariant: for every write, either nothing is written, or:
- the record's own files decode under the instance limits;
- its latest view does not exceed the depth limit.

The latest view's node count remains read-side.

Sibling surfaces:
- read-side `_decode_mapping`, and the readers for jsonl, direct and latest;
- the writer caches, which can mask a block on same-instance reads, hence the fresh-instance read-back;
- `ROW_CONTENT_SKIP_REASONS`, which is read side only;
- the historical import, which gets a pre-scan;
- the batch lanes;
- archive restore and `_restore_derived_master_direct_unlocked`, both unchecked on purpose;
- the cluster map and first-import guard.

Seats:
- correctness: headroom values, the closed lane list, zero-byte refusal, the import pre-scan;
- test evidence + spec compliance: the E3 pairs per headroom class, and the E4 mutations;
- invariant: cache masking, restore lanes untouched, error-code consumers, the residual risk stated.

## Measured depth offsets (task 1.1, E2)

Probe: per record type, write one record whose deepest branch is a nest of depth n, on the pre-change source (no
write-side check). The probe captures the built record at `_validate_outgoing_record` and computes the JSON depth
(root = 1, as `_validate_json_complexity` counts it) of the record, of its jsonl line, of its direct row and of the
latest view `latest/gfs/2026062800/model_a.json` that embeds it.

- Each file is cross-checked with a **fresh** repository at `max_json_depth` = its measured depth (accepted) and at one
  less (refused with `file_journal_json_depth_exceeded`); every check printed `ok`.
- Lanes: historical lanes for `hydro_run`, `forecast_cycle`, `pipeline_job` (nest under `exit_code`) and
  `pipeline_event` (nest under `details`).
- `forcing_version` and `model_context` have no write lane in this repository. The probe hand-appends a raw journal
  record of each type through `_append_journal_record_unlocked` (after `_validate_outgoing_record`), then has the
  repository materialize the view.

| record type | n | record | jsonl | direct | latest |
|---|---:|---:|---:|---:|---:|
| hydro_run | 6 / 11 / 17 | 8 / 13 / 19 | 8 / 13 / 19 | - | 8 / 13 / 19 |
| forecast_cycle | 6 / 11 / 17 | 8 / 13 / 19 | 8 / 13 / 19 | - | 8 / 13 / 19 |
| forcing_version | 6 / 11 / 17 | 8 / 13 / 19 | 8 / 13 / 19 | - | 8 / 13 / 19 |
| model_context | 6 / 11 / 17 | 8 / 13 / 19 | 8 / 13 / 19 | - | 8 / 13 / 19 |
| pipeline_job | 6 / 11 / 17 | 8 / 13 / 19 | 8 / 13 / 19 | 8 / 13 / 19 | 9 / 14 / 20 |
| pipeline_event | 6 / 11 / 17 | 9 / 14 / 20 | 9 / 14 / 20 | - | 10 / 15 / 21 |

Offset (file depth − record depth), constant per record type across all three n:

| record type | jsonl | direct | latest |
|---|---:|---:|---:|
| hydro_run, forecast_cycle, forcing_version, model_context | 0 | - | 0 |
| pipeline_job | 0 | 0 | +1 |
| pipeline_event | 0 | - | +1 |

These are the values of `_LATEST_VIEW_DEPTH_HEADROOM`.

- A current accepted-submit master `pipeline_job` is filtered out of the latest view, so headroom 1 is conservative for
  masters (What 7).
- `_strip_internal_fields` drops `_file_journal_*` keys, so a view can only come out shallower than the bound, never
  deeper.
