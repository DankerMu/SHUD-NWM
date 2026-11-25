# Tasks

Fixture level: `standard`. Risk packs: public evidence shape (a wrong cache
key publishes one string's sanitised form for another, or leaks an
unsanitised value); scheduler verdicts (the ladder reads the sanitised state,
so a changed byte can turn `complete` into `gap` and submit backfill);
memory of a long-running process.

Must preserve: for every input, `_public_evidence` and every function it calls
return what they return today, including the type of non-string scalars
(`1`, `True` and `"1"` stay distinct); the public shape rules (`[redacted]`,
`[local-path]`, `[uri]`, `[object-uri]`, dropped `_file_journal_*` keys,
datetimes as UTC text); `packages/common/redaction.py`,
`file_orchestration_journal.py` and every `file_journal_*` module are not
edited; `nfs_raw_manifest_readiness_from_env` outside a scope behaves exactly
as today (computed on every call, same return values including `None`); the
verdict of completion scoring for every journal state; pass evidence and the
submitting path's candidate-state evidence; existing tests stay green
unedited.

## 1. Sanitiser caches (`services/orchestrator/public_evidence.py`)

- [ ] 1.1 The text sanitiser (`_sanitize_public_text`) and the string branch
  of the scalar sanitiser (`_sanitize_public_scalar`) remember their result
  per input string in bounded least-recently-used caches. A string longer
  than a module constant is computed without being remembered. Non-string
  scalars never enter a cache. The uncached implementations are module-level
  functions that the cached path looks up at call time, so a test can replace
  or count them.
- [ ] 1.2 The sensitive-key test used by `_sanitize_public_field` remembers
  its answer per field name in a bounded cache that wraps
  `packages.common.redaction.is_sensitive_key`; a name longer than a module
  constant is tested without being remembered.
- [ ] 1.3 The bounds are module constants. Starting values: 8192 strings of
  at most 1024 characters for each text cache, 4096 names of at most 256
  characters for the key cache. The comment states the memory honestly: an
  entry holds the input and the output; the output is normally about as long
  as the input but can be several times longer (every token of `/ / /`
  becomes `[local-path]`), and a character can take up to four bytes; it
  gives the typical figure (ASCII, output about as long as input) and the
  formula for the worst case. One function clears all three caches (for
  tests).

## 2. Readiness scope (`services/orchestrator/source_cycle_raw_manifest.py`, `scheduler_discovery.py`)

- [ ] 2.1 A context manager opens a readiness scope held in a `ContextVar`
  (set on entry, reset in `finally`). Inside a scope,
  `nfs_raw_manifest_readiness_from_env(source_id, cycle_time)` computes its
  result once per key and returns a deep copy to every caller, the first
  included. The early `return None` for a disabled lane stays ahead of
  everything else: no key is built and nothing is stored (building the key
  first could raise for an unknown source id where the function returns
  `None` today). The key is the normalised source id, the cycle time, and the
  value of every environment variable the function reads (six today). A call
  that raises stores nothing. Outside a scope the
  function body is what it is today. A scope opened inside another one starts
  empty and restores the outer one on exit.
- [ ] 2.2 `_cycle_completion_verdict` scores its cycle inside one scope (the
  whole function body, both branches). Nothing else opens a scope.

## 3. Tests

New file `tests/test_public_evidence_memo.py`:

- [ ] 3.1 Corpus parity. The whole corpus is run in order through one cache
  that is not cleared between strings, twice; for each string the cached
  function returns what the uncached implementation returns, for the text
  sanitiser and for the scalar sanitiser. The corpus
  holds at least: a bearer token, a `password=` assignment, URL userinfo, an
  absolute path, a `~` path, a path containing a space, `s3://` and
  `published://` and `https://` URIs, a URI in the middle of prose, a bare
  `s3:` prefix, a `key=/abs/path` token, quoted and bracketed tokens, the
  empty string, whitespace only, two strings that differ only by leading or
  trailing whitespace and render differently, two that differ only by letter
  case and render differently (not `S3://` against `s3://`, which both become
  `[object-uri]`), non-ASCII text, and a string longer than the length limit.
- [ ] 3.2 Payload parity. A nested payload (mappings, lists, a tuple, a
  datetime, `None`, `1`, `1.0`, `0`, `0.0`, `True`, `False`, `"1"`, a `_file_journal_` key, sensitive
  keys in plain and escaped spelling, `message` / `*_message`, `*_path` with a
  scalar and with a mapping, `*_uri`) renders equal three times: cold, warm,
  and after clearing the caches; and equal to a rendering made with the three
  cached functions replaced by their uncached implementations. Types of the
  non-string scalars are asserted, not only equality, and rendering a list
  of non-string scalars (no mapping, whose keys would enter the key cache)
  leaves the size of every cache unchanged.
- [ ] 3.3 Bounds. After sanitising more distinct strings than a cache holds,
  its size equals its maximum; an over-long string and an over-long key leave
  the cache sizes unchanged and are still rendered correctly; the constants
  have the starting values of 1.3.
- [ ] 3.4 Reuse happens: rendering the same payload twice calls the uncached
  text implementation once per distinct cacheable string (count through a
  wrapper installed by the test).

New file `tests/test_raw_manifest_readiness_scope.py`:

- [ ] 3.5 Scope unit tests with the underlying `nfs_raw_manifest_readiness`
  counted: (a) three calls for one (source, cycle) inside a scope compute
  once and return equal mappings that are distinct objects, and appending to
  a nested list of the first does not show in the second; (b) another cycle,
  another source, or a changed value of an environment variable the function
  reads computes again; (c) outside a scope every call computes; (d) after the
  scope exits, a new scope computes again; (e) a nested scope computes again
  and the outer scope's entry is still served afterwards; (f) a call that
  raises is followed by a second call that computes again; (g) with the
  feature flags off the result is `None` every time and the underlying
  function is never called, inside and outside a scope; (h) the result inside
  a scope equals the result outside it for a real manifest on disk (ready,
  and with one file missing).
- [ ] 3.6 Scoring on the real file journal and the real scheduler wiring.
  The test must drive the branch production runs: the strict/successor branch
  of `_cycle_completion_verdict` (strict provider `None`, successor provider
  `{"ready": True}` for three models, the way
  `tests/test_completion_scoring_event_evidence.py` sets its providers), with
  the raw-manifest lane enabled (`NHMS_SCHEDULER_NFS_RAW_MANIFEST_ENABLED=true`
  and the root pointing at an existing directory), and it counts
  `source_cycle_raw_manifest.nfs_raw_manifest_readiness`. With the providers
  both `None` the verdict is decided by `has_completed_pipeline` and no
  candidate state is read, and with the lane off nothing is computed; in
  either case the count is zero and the test proves nothing. Calling the
  scheduler's `_cycle_completion_status` directly: a cycle completed for
  three models scores `complete` and computes readiness once; scoring a
  second cycle computes once more; a `candidate_state` call made outside
  completion scoring computes on its own. The states handed to the ladder
  for the three models carry equal readiness mappings.
- [ ] 3.7 State parity on the real file journal: for a journal whose rows
  carry content the sanitiser changes (a secret-looking token in an error
  message, an absolute path and an `s3://` URI in job details, URIs in
  `hydro_run` and in the run manifest's initial state), `candidate_state`
  returns equal states with cold caches, with warm caches, and with the three
  cached functions replaced by their uncached implementations.

## 4. Evidence floor

- [ ] 4.1 Red first, in `.workplans/3236/red-runs.log`: 3.3 (bounds), 3.4,
  3.5 (a), (e) and 3.6 fail on the unmodified tree, with the failing line or
  the missing attribute. 3.1, 3.2, 3.7 and 3.5 (c), (g), (h) compare
  behaviour that must not change; where they can run on the unmodified tree
  they pass before and after, and the log says which could not run before.
- [ ] 4.2 Mutations, each applied by hand, the named test run, the failing
  assertion recorded, the file restored and checked with `cmp`:
  M1 the text cache is keyed on the stripped string -> 3.1;
  M2 the scalar cache is keyed on the lower-cased string -> 3.1;
  M3 the scalar cache accepts non-string scalars -> 3.2 (types);
  M4 no length limit -> 3.3;
  M5 the key cache answers `False` for every remembered name -> 3.2;
  M6 the scope hands out the stored mapping itself -> 3.5 (a);
  M7 the scope key drops the cycle time -> 3.5 (b);
  M8 the memo is module state that outlives the scope -> 3.5 (c), (d);
  M9 a raised error is remembered -> 3.5 (f);
  M10 `_cycle_completion_verdict` opens no scope -> 3.6;
  M11 the scope is opened around the whole pass (in `discover_cycles`)
  instead of in the verdict -> 3.6 (the test calls the status function
  directly, so no scope is open and the count is three). A whole-pass scope
  would hand one snapshot to discovery, the adapters, the predecessor gate
  and the submitting path; it must not pass.
- [ ] 4.3 Local: the two new files; `tests/test_file_orchestration_journal.py`;
  `tests/test_completion_scoring_event_evidence.py`;
  `tests/test_scheduler_backfill.py`; `tests/test_source_cycle_raw_manifest.py`;
  every test file that names `public_evidence` or
  `nfs_raw_manifest_readiness` (find with `git grep -l`);
  `tests/test_production_scheduler.py` once;
  `uv run --no-sync ruff check --no-cache .`; the topology hard gate count
  unchanged; `openspec validate memoise-completion-scoring-pure-work --strict --no-interactive`.
- [ ] 4.4 A local timing, recorded in the log and not turned into a test:
  rendering one synthetic candidate state (a few hundred events sharing a few
  dozen distinct messages, a readiness mapping) 500
  times, before and after. A real readiness mapping holds counts and at
  most five file samples; the synthetic one has that shape.
- [ ] 4.5 node-27: the new files and the journal, scoring and raw-manifest
  test files in a temporary detached worktree.
- [ ] 4.6 CI: every shard of the full suite green.

## 5. After merge (evidence owed on the issue, not a task of this change)

- Before the production checkout moves: one plan-only pass of the new code on
  the compute control node, from a separate clone, with its own lock path and
  evidence directory, at a pass boundary with the timer stopped and restored.
  Compare with the neighbouring production pass: status, candidate count,
  source-cycle list and per-cycle completion verdicts must be the same. Record
  the cache statistics of that pass (hits, misses, size) and adjust the
  constants in a follow-up if the caches thrash.
- Then deploy at a pass boundary; idle-pass `timing.pass.total_wall_ms`, pass
  CPU and `journal_read_attribution.totals` before and after; one submitting
  pass that completes normally.
- A new profile of an idle pass decides the journal read path (the issue's
  option 1). The issue stays open for that.
