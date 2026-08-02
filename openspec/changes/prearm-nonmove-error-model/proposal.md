# Bring the prearm tool's non-move FS operations into the error model (#1252)

## Why

PR #1251 (#1088) hardened only the MOVE path: `_archive_move` converts
`(OSError, shutil.Error)` into a partial manifest + `PrearmError`, and
`main()` renders every `PrearmError` with the `pre-arm reset refused: `
prefix operators and the runbook key off. The remaining filesystem
operations are outside that model (current master line refs verified
2026-08-02): the terminal `_write_manifest` (:492-499, called :597 and
:643), `_create_archive_dir` (:428-438), the associations
`os.makedirs` (:630), and `_collision_safe_destination` (:441-447)
(:597 and the collision probe turn out NOT to need work — see What
Changes point 1).
The terminal manifest write is the ONLY space-consuming write and runs
LAST — after a fully successful sweep — so ENOSPC/quota/read-only
failures hit precisely there: the sweep has completed, out-of-workdir
associations have been renamed to `associations/<label>-<basename>`,
their original absolute paths exist ONLY in the manifest that never
got written, stdout is empty (all reporting prints run after the
write), and stderr is a bare traceback with no refusal prefix. That is
the same unrecoverable-forensics state round 1 eliminated for the move
path, left intact on the terminal write. Additional trap: the write
seam `atomic_write_bytes_no_follow` raises `SafeFilesystemError`,
which is a `RuntimeError` subclass, NOT an `OSError` subclass
(packages/common/safe_fs.py:10) — a fix that catches only `OSError`
is structurally wrong and must be killed by a dedicated test variant.

## What Changes

Single file `scripts/node27_timeseries_compression_prearm.py` plus its
test suite (issue's recommended route; the append-only incremental
manifest alternative is rejected — cost exceeds benefit at this move
count):

1. Extend the `except (OSError, SafeFilesystemError) → PrearmError`
   model (with `from error` chaining and the underlying error text,
   matching the file's :412/:424/:617 convention) to
   `_create_archive_dir`, the associations `os.makedirs`, and the
   TERMINAL `_write_manifest` call site. Two sites the issue listed
   take no wrapper, as recorded deviations: the nested :597 write is
   already inside the model (`_archive_move` swallows
   `(OSError, SafeFilesystemError)` at :611-613 and raises the move's
   own `PrearmError`), and `_collision_safe_destination`'s
   `os.path.lexists` cannot raise (CPython swallows OSError/ValueError
   → returns False; verified), so a wrapper there would be dead code —
   its real gap is the mid-sweep exhaustion refusal, covered by
   point 2.
2. POST-MOVE refusal forensics (any refusal after ≥1 completed move:
   terminal manifest write, associations makedirs, collision
   exhaustion): carry the forensics INSIDE the multi-line
   `PrearmError` message (archive_dir, the partial-manifest status —
   written, or explicitly NOT written — and every completed
   `from -> to` pair with the associations' original absolute paths)
   so `main()`'s single stderr print keeps the
   `pre-arm reset refused: ` prefix first and associations remain
   manually recoverable. The best-effort partial-manifest attempt
   applies only to the non-write sites (associations makedirs,
   collision exhaustion); the terminal manifest write does not retry
   — it IS the manifest write, its failure means the manifest was
   NOT written.
3. Early failures (`_create_archive_dir`, before anything moved)
   refuse with the prefix while the workdir is untouched — asserted
   recursively in the tests.
4. Move-path behavior byte-identical: zero diff inside
   `_archive_move` (:578-618); all 38 existing tests stay green
   unmodified.

Spec relation: the prearm refusal semantics currently live in the
`hypertable-compression` capability (openspec/specs/
hypertable-compression/spec.md:161 — "A failure in the middle of the
sweep MUST surface as the script's own refusal message and leave a
manifest covering what already moved"). This change's
`prearm-error-model` capability REFINES that mid-sweep clause — the
non-move paths violating it today are exactly what points 1-2 close —
and does not conflict with it; the mid-sweep scenario here binds that
MUST on the non-move paths.

## Non-goals

- The move path (round-1 hardening stands untouched).
- `packages/common/safe_fs.py` / `atomic_write_bytes_no_follow`
  semantics (exception hierarchy stays as is).
- Replay supervisor / upstream gates; node-27 live deployment.
- Sibling `atomic_write_bytes_no_follow` callers elsewhere in the
  repo (issue explicitly scopes to this one file; other finds are
  separate issues).
- The append-only incremental-manifest redesign (recorded alternative,
  rejected).
