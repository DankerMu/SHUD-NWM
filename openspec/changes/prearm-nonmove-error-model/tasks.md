# Tasks: prearm-nonmove-error-model

Fixture level: compact · Repair intensity: light · Issue #1252

Triage note: S — single-file hardening + 3-4 test cases, fully local
hermetic (the prearm suite already runs on fake filesystems under
tmp_path). Issue is implementation-ready with orchestrator-verified
current line refs. Fixture review round 0 verdict REVISE → repair
iteration 1 folded all findings: 3 P1 (nested :597 write already in
the model — zero diff there, move freeze self-consistent; forensics
must ride INSIDE the PrearmError message, never pre-raise prints —
prefix contract and `_refusal_case` startswith stay valid; post-move
forensics widened from terminal-write-only to ANY post-move refusal
incl. associations makedirs and collision exhaustion, with
best-effort partial manifest to keep the hypertable-compression
mid-sweep MUST), 4 P2 (`lexists` never raises — dead wrapper refused
as recorded deviation; injection targeting pinned for (c)/(d);
exception chaining + underlying error text asserted; capability
relation to hypertable-compression declared in proposal), notes
(red-proof mechanism via stash, no Traceback-string discriminator;
sibling-caller one-line survey; PrearmError docstring ride-along).
Round-1 re-review: ACCEPT-with-tightenings — 3 P2 folded (terminal
write never retries the partial manifest and 3(a) injects on every
post-sweep write call asserting manifest absence; forensics attach
at specific sites, never a broad `except PrearmError` around the
association loop; risk axis 4 narrowed to archive-dir-only early
refusal) plus a Why-section note. The decisive trap is the exception hierarchy:
`SafeFilesystemError(RuntimeError)` is NOT an `OSError`, so every new
handler must catch BOTH, and the test set must include the
SafeFilesystemError variant explicitly to kill an OSError-only fix.
Risk axes: (1) FORENSICS NEVER LOST — terminal-write failure happens
AFTER a successful sweep; the refusal must carry every completed
`from -> to` pair + archive_dir + the partial-manifest status
(written, or explicitly NOT written), or the out-of-workdir
association original paths become unreconstructable (the exact state
round 1 eliminated for moves). (2) PREFIX CONTRACT —
every refusal exits 1 via `pre-arm reset refused: ` on stderr, no bare
traceback (operator/runbook contract). (3) MOVE-PATH FREEZE — the
round-1 partial-manifest semantics (:594-618) are byte-identical;
the existing 38 tests pass unmodified (they are the oracle; do not
edit any existing test). (4) UNTOUCHED-WORKDIR EARLY REFUSAL —
only archive-dir creation (:430) happens before any move; the
archive-ROOT makedirs failure asserts a recursively byte-identical
workdir via `_refusal_case` (:164). The inner TIMESTAMPED `os.mkdir`
failure (cross-review round 1) also refuses pre-move with the
prefix, but the empty archive root may already exist — byte-identity
and `_refusal_case` apply to the root-creation case only. The
associations-dir creation (:630) is POST-move: no `_refusal_case`;
it is covered by the post-move forensics axis (1).

Must preserve:
- `_archive_move` and its partial-manifest path: zero diff.
- All existing tests in
  `tests/test_node27_timeseries_compression_prearm.py` unmodified and
  green (38 passed baseline).
- `packages/common/safe_fs.py` untouched.
- Whitelist/gate semantics (four pre-move gates) untouched.
- Refusal exit code stays 1; no new exit codes.

## Implementation tasks

- [x] 1. `scripts/node27_timeseries_compression_prearm.py`: wrap the
  non-move FS sites in the unified error model —
  `_create_archive_dir` (:428-438), associations `os.makedirs`
  (:630), the association destination re-probe `os.lstat` in the
  association loop (folded from cross-review round 1: non-ENOENT
  OSError there escaped as a bare traceback post-move; keep
  `except FileNotFoundError: continue` first, then OSError → the
  post-move refusal of task 2 — os.lstat cannot raise
  SafeFilesystemError, so OSError alone is correct at that site),
  and the TERMINAL `_write_manifest` call site (:643).
  The nested partial-manifest write at :597 is ALREADY inside the
  model — `_archive_move` catches `(OSError, SafeFilesystemError)` at
  :611-613 and still raises the move's own `PrearmError` at :614 — so
  it takes ZERO diff (move-path freeze). Recorded deviation: when
  that nested write fails, the refusal text at :615-616 still claims
  "the partial sweep and its manifest are under {archive_dir}"
  although no manifest exists; correcting that wording would touch
  the frozen move path — record as known limit, defer.
  `_collision_safe_destination` (:441-447): NO exception wrapper —
  CPython `os.path.lexists` swallows `OSError`/`ValueError` and
  returns `False` (verified: unreadable parent and lstat-EIO both
  return False), so a permission failure merely defers to
  `shutil.move`, which the frozen move path already handles; the real
  gap at this site is the exhaustion `PrearmError` at :447 firing
  MID-SWEEP, covered by task 2's widened scope. Recorded as an
  intentional deviation from the issue's in-scope list (dead wrapper
  refused). Handlers catch `(OSError, SafeFilesystemError)` and
  `raise PrearmError(...) from error`, message naming the failed
  operation, the path, and the underlying error text
  (`error.strerror` for OSError, `type(error).__name__: {error}`
  otherwise), matching the existing convention at :412/:424/:617.
  Ride-along: fix the `PrearmError` docstring (:77) — "working
  directory is left byte-identical" is already false for post-move
  refusals; reword to "…except post-move refusals, which report what
  already moved".
- [x] 2. Post-move refusal forensics — applies to ANY refusal raised
  after at least one move completed. The best-effort partial-manifest
  attempt (swallowing `(OSError, SafeFilesystemError)` on that nested
  write) applies ONLY to the non-write post-move sites (associations
  `os.makedirs` at :630, collision exhaustion at :447); the terminal
  manifest write at :643 does NOT retry — it IS the manifest write,
  so its failure means "manifest was NOT written", full stop. In ALL
  cases the forensics ride INSIDE the `PrearmError` message
  (multi-line) — NOT printed before the raise — so `main()`'s single
  `print(f"pre-arm reset refused: {error}", file=sys.stderr)`
  (:708-709) keeps stderr starting with the prefix and
  `_refusal_case`'s `startswith` (tests:178) stays valid. Message
  shape: line 1 names the failed operation + path + underlying error
  text; then archive_dir; then the partial-manifest status (written,
  or explicitly NOT written); then every completed `from -> to` pair,
  one per line, absolute paths (associations' ORIGINAL absolute path
  included) so an operator can manually restore. Attach the forensics
  at the SPECIFIC non-move sites (the `os.makedirs` call at :630,
  the `_collision_safe_destination` call at :640, the terminal write
  at :643) — NEVER as a broad `except PrearmError` around the
  association loop: that would re-wrap `_archive_move`'s own refusal
  (:614) and let a second partial-manifest attempt overwrite the
  move's failure record — a behavioral move-freeze violation
  `git diff` alone cannot catch.
- [x] 3. Tests (append-only; existing 38 untouched):
  (a) ENOSPC at the terminal manifest write (monkeypatch the
  module-level `atomic_write_bytes_no_follow` to raise
  `OSError(ENOSPC)` on EVERY call issued after the sweep — there
  must be exactly ONE, since the terminal write does not retry)
  after a sweep with >=1 workdir residue AND >=1 out-of-workdir
  association → rc 1,
  `captured.err.startswith("pre-arm reset refused: ")` AND every
  completed `from -> to` pair appears in `captured.err` after that
  first line, including the association's ORIGINAL absolute path,
  AND `No space left on device` appears in the refusal, AND
  `prearm-manifest.json` is ABSENT under the archive dir;
  (b) same shape with `SafeFilesystemError` raised instead — same
  refusal path (kills an OSError-only fix); assert
  `SafeFilesystemError` appears in the refusal text and
  `prearm-manifest.json` is absent;
  (c) `_create_archive_dir` failure — inject at the OUTER
  `os.makedirs(archive_root)` (:430) ONLY via monkeypatch (NOT
  chmod — a no-op under root), so `prearm-archive/` never exists and
  `_refusal_case` (tests:164-179) applies verbatim → rc 1, prefixed
  refusal naming `archive_root`, workdir recursively byte-identical;
  (d) associations `os.makedirs` failure after workdir residues
  already moved — inject SELECTIVELY (raise only when the target
  path ends with `/associations`, keeping `_create_archive_dir`
  real); `_refusal_case` NOT applicable (residues already moved) —
  assert rc 1 + prefix + the associations directory path named in
  the refusal + every already-moved workdir pair in `captured.err` +
  either a partial manifest on disk or an explicit
  "manifest was NOT written" line (per task 2);
  (e) association re-probe `os.lstat` failure after workdir residues
  already moved — inject SELECTIVELY (raise `OSError(EACCES)` only
  for the association target path, delegating everything else to the
  real `os.lstat`) → rc 1 + prefix + "Permission denied" in the
  refusal + every already-moved workdir pair + partial-manifest
  status line (folded from cross-review round 1).
- [x] 4. Oracle: `uv run pytest -q
  tests/test_node27_timeseries_compression_prearm.py` → 38 existing
  (unmodified) + new all green; `uv run ruff check .`; `openspec
  validate prearm-nonmove-error-model --strict --no-interactive`;
  red proof: capture the red by running the new tests with the
  script hunk stashed (`git stash push --
  scripts/node27_timeseries_compression_prearm.py`) — the red
  signature is the raw `OSError`/`SafeFilesystemError` ESCAPING
  `prearm.main()` into pytest (the suite calls main in-process, so
  no "Traceback" string lands in capsys; rc/prefix/pairs are the
  discriminators, never a Traceback-absence assert); record at least
  the (a) and (b) reds, then `git stash pop` and record green;
  move-path freeze proof: `git diff` shows no hunk inside
  `_archive_move` (:578-618), no `except PrearmError` introduced
  around the association loop, and
  `test_a_failed_move_writes_a_partial_manifest_and_refuses`
  (tests:599) unmodified and green; `git diff --stat` → exactly the
  script + test file (+ this fixture).

## Required evidence

- Red-then-green for (a) and (b) (raw exception escape before,
  prefixed refusal + pairs + errno text after); untouched-workdir
  proof for (c); mid-sweep forensics proof for (d) and (e) — (e)'s
  red recorded against the fix commit in isolation (1 failed, 42
  passed with the fix hunk stashed);
  38-existing-tests-unmodified statement with pass count; move-path
  freeze diff proof; ruff; zero-diff-outside proof; one-line survey
  of other `atomic_write_bytes_no_follow` callers (file a follow-up
  issue if any catches only `OSError` — no code change here).

## Non-goals

- Move path, safe_fs semantics, other atomic_write callers,
  incremental-manifest redesign, node-27 live runs, supervisor/gate
  logic.
