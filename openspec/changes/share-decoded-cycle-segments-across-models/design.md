# Design

## Today

```text
_cycle_rows(model) outside a window
  fingerprint = _cycle_rows_source_fingerprint(...)        # every call
  hit of _cycle_rows_cache[(source, cycle, model, segs)] if fingerprint equal
  miss:
    for each source segment:
      latest view(s) of the model
      for record in _read_cycle_segments(journal/<seg>, cycle):        apply
      for record in _read_cycle_segments(pipeline-events/<seg>, cycle): apply
    direct pipeline jobs (own cache)
    filter, dedupe, store under the fingerprint unless it faulted
```

`_read_cycle_segments` is eager: it probes the slots, decodes every segment
and returns a list before the first record is applied. An error in it
(`file_journal_segment_gap`, `file_journal_segment_limit_exceeded`,
`file_journal_unreadable`, a malformed line, a probe containment failure)
therefore surfaces before any record of that surface is applied.

## Change

The two `_read_cycle_segments(...)` calls of the miss path go through one
helper:

```text
_shared_cycle_segment_records(surface, source_segment, cycle_segment,
                              fingerprint, shareable)
  if not shareable:                       # owner, or faulted fingerprint
      return _read_cycle_segments(directory, cycle_segment)
  with _cache_lock: entry = cache.get(key)
  if entry is not None and entry[0] == fingerprint:
      return entry[1]
  records = _read_cycle_segments(directory, cycle_segment)   # may raise: nothing stored
  with _cache_lock: store (fingerprint, records), evicting the oldest past the cap
  return records
```

`shareable` is true exactly when the call is outside its write window and
its fingerprint is not the containment-fault marker (the same condition under
which `_cycle_rows_cache` may be hit and stored by a non-owner).

The order of surfaces, of source segments and of records is unchanged, so
errors keep their precedence: a failure of the journal surface is raised
before the events surface is read, as today.

## Why equality with the call's own fingerprint is enough

The fingerprint covers every slot of both surfaces for every source segment
of the call. A hit therefore proves that the files the entry was decoded
from have, now, the same `(mtime_ns, size, inode)` in every slot as when a
call computed that fingerprint before reading them. This is the argument
`_cycle_rows_cache` already rests on; the new cache holds an earlier product
of the same read.

## Invalidation

None is needed. Outside a window equality with the call's own fingerprint
decides, and the owner of a window never consults the cache. This differs
from `_cycle_rows_cache`, whose wipe on window entry is a correctness
precondition because the owner's hits there are unvalidated. Entries simply
age out of the eight slots.

## Aliasing

`_apply_journal_record` builds `payload = dict(...)` and upserts one-level
copies; `_clone_cycle_rows` copies one level. Nested values (evidence maps,
lists) of a decoded record are therefore referenced by the shared entry, by
the stored rows of every model that read it, and by every clone handed to a
caller. The rule that cached rows are never mutated in place already forbids
writing into those nested values; the change widens the blast radius of a
violation from one model's cache entry to the cycle. The test in tasks 3.8
runs completion scoring over a journal and compares the shared records with
a fresh decode of the same files. Rows that come from a model's latest view
are copies of that file, not of a shared record, so the test journal must
carry records that win over the latest view (pipeline events with a higher
sequence) for the oracle to see anything.

## Alternatives rejected

- **Cache the merged `model_id=None` rows and filter per model**: the merge
  keeps single slots for hydro/forcing/model context, so it is lossy
  (stated in the issue).
- **Partition the decoded records by model once per cycle**: removes the
  per-model replay too, but then a malformed record of another model no
  longer raises for this model's read unless the failure is replayed
  separately. Left for a later stage, to be decided on the next profile.
- **Reuse `_cycle_rows_by_model_unlocked`**: never caches, excludes direct
  records and assumes the write lock (#1661).
