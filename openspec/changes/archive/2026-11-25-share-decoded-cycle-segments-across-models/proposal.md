# Share one cycle's decoded segment records between its models (#3236, stage B2)

## Why

Completion scoring reads every model of a cycle through `_cycle_rows(model_id=<model>)`.
The cache key includes the model, so each model's first read is a miss, and
the miss path reads the cycle's shared segments again: it probes the four
slots of `journal/<source>` and of `pipeline-events/<source>`
(`_cycle_segment_paths`), then decodes every line of every segment
(`_read_jsonl`), although these files are the same for all models of the
cycle. With 132 models and 8 cycles that is 1,056 full decodes per pass where
8 would do.

On the production profile (12,027 main-thread samples of a 120 s idle pass)
`_read_cycle_segments` holds 1,779 samples (14.8 %): 969 in the slot
existence probes, 808 in decoding.

## What changes

`_cycle_rows`, on a miss outside a write window, takes the decoded records of
each shared segment directory from a small cache on the repository instead of
calling `_read_cycle_segments` again, when an entry stored under **the
fingerprint this very call computed** exists. Otherwise it reads as today and
stores the result under that fingerprint.

## What does not change, and why this shape

- **Every non-owner read still computes its fingerprint** and a hit of the
  new cache needs equality with it (#1567). A fingerprint that observed a
  containment fault neither hits nor stores. No cache skips the fingerprint
  (#1661): the new one is validated by the same value as `_cycle_rows_cache`,
  computed by the same call.
- **The write-window owner does not use it.** Inside a window the owner has
  no fingerprint; it reads the segments as today, neither taking nor storing
  an entry. Nothing has to be cleared when a window opens: an entry is only
  ever served to a call whose own fingerprint equals the stored one.
- **Each model still replays the records itself.** `_apply_journal_record`
  runs for every record on every model's miss, so a malformed record still
  raises for every model that reads the cycle, and the per-model filtering
  and the single-slot merge rules are untouched. Only the bytes-to-records
  step is shared.
- **A segment directory whose decode raises leaves no entry** (segment gap,
  slot limit, unreadable or malformed segment), so every model's read raises
  as today. A directory that decoded stays stored even if the call later
  raises on another directory or while applying a record.
- `_cycle_rows_by_model_unlocked`, `_cycle_journal_records`, the write
  preconditions and every other caller of `_read_cycle_segments` are
  untouched.
- The issue's option 1 as written (one fingerprint per cycle) is not done;
  see stage B1.

## Decisions

- **D1. Bound to the call's fingerprint.** The entry is `(fingerprint,
  records)`. The fingerprint is computed before the segments are read, as for
  `_cycle_rows_cache`: a write that lands between the two is stored under
  the older fingerprint and can only be served to a call that computes that
  same older value again, which a changed file does not produce.
- **D2. Key.** `(surface, source segment, cycle segment)`: the two surfaces
  and the two spellings never share an entry. A read with an overridden
  segment list computes a different fingerprint and replaces the entry; that
  costs a decode when such reads alternate with default ones and never
  serves wrong records.
- **D3. Small and bounded.** Scoring finishes one cycle (its models, then
  the stale-identity loop) before the next, so the cache only has to hold the
  cycle being scored: up to four entries where both spellings of the source
  directory exist. A module constant, 8 entries (two such cycles), oldest
  out first. The comment states what an entry holds (the decoded records of
  one segment directory of one cycle).
- **D4. The shared records are read-only.** `_apply_journal_record` copies
  a record one level deep and does not mutate it; the rows of different
  models now reference the same nested values, as the rows cache and its
  clones already do for one model. A test pins that the shared records are
  unchanged after a full scoring run.
- **D5. Attribution follows the reads.** The segment files are already
  served from the byte cache after a cycle's first model, so a shared hit
  removes byte-cache hits, not file reads: `cache_hit_calls` and
  `cache_hit_bytes` of a pass drop, `calls` and `bytes` stay. The per-lane
  rows still reconcile with the totals.

## Expected effect

Predicted from the profile: `_read_cycle_segments` drops from 1,779 samples
to a few dozen (one decode per cycle and surface instead of one per model),
about 17 s off a pass. In `journal_read_attribution`, `cache_hit_calls`
(560 today) drops by roughly the segment reads saved while `calls` (1,152)
stays. The after-profile on the compute control node tests both.

Not addressed: per-model validation of model-less records (957 samples),
latest-view reads (1,448), the direct-job lane (665).

## Known limits

- The saving needs the models of a cycle to be read while the cycle's files
  do not change. A writer between two models' reads costs one extra decode,
  not a stale read.
- Memory: up to 8 decoded segment directories are kept for the life of the
  repository instance; the per-model rows already held by `_cycle_rows_cache`
  now share their nested values with these records instead of each holding a
  private decode.

## Impact

- Code: `services/orchestrator/file_orchestration_journal.py`.
- Specs: `pipeline-job-persistence` (+1 requirement).
- Evidence: the byte-cache-hit totals of `journal_read_attribution` are
  lower; shape unchanged. No schema, configuration or migration.
