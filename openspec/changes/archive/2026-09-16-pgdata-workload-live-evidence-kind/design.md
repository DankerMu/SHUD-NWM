# Design: live evidence kind for the PGDATA workload CLI

Change surface: `scripts/node27_pgdata_workload.py` (`build_parser` `:42-57`, `measure` `:62-118`);
`packages/common/node27_pgdata_workload_io.py` (`validate_sha` `:52-57`, admission helpers `:199-219`);
`docs/runbooks/tier-node27-timeseries-storage.md` (the section that owns this CLI, ~`:698-733`).

Must preserve: the default run is byte-for-byte what it is today — no flag means
`evidence_kind: "isolated"`, `isolated: true`, `live: false`
(`tests/test_node27_pgdata_workload.py:810-812`, `tests/test_node27_pgdata_workload_io.py:114` stay green).
Captured query text, `query_digest`, typed `query.parameters`, EXPLAIN binding, the one-warmup-plus-20-sample
protocol, and `publish_measurement_output`'s staged private sibling / mode-0600 / no-clobber behaviour are
unchanged. The library's existing `INPUT_KIND_INVALID` rejection of an unknown kind
(`packages/common/node27_pgdata_workload.py:89`, covered at `tests/test_node27_pgdata_workload.py:904-905`) keeps
working and is not duplicated.

Must add/change: an explicit `--evidence-kind {isolated,live}` on `measure`, defaulting to `isolated`; a `live`
admission that is checked before any measurement work and, on failure, refuses with a typed code and writes
nothing; `--reviewed-sha` bound to the running checkout's HEAD on the `live` path (today it is only shape-checked,
so the SHA is stamped, not bound); a runbook paragraph stating how a live receipt is obtained.

Governing invariant: a receipt may say `live: true` only when this process proved, in the same run that produced
its samples, that the session was read-only as `nhms_display_ro` and that the reviewed SHA equals the HEAD of the
checkout that executed it. Otherwise no receipt exists at that path.

Sibling surfaces:
- Producer: `measure_workload` (`packages/common/node27_pgdata_workload.py:74-140`) — the only place the triple is
  derived; it already validates the kind.
- Publication: `publish_measurement_output` (`packages/common/node27_pgdata_workload_io.py`, staged sibling +
  exclusive create) — the refusal paths must run before it, so a refused live run leaves no partial file.
- Admission: `prove_readonly_session` / `open_readonly_connection` / `read_private_dsn_file` — already fail-closed;
  the live path adds to them, never relaxes them.
- Consumers: receipts are read as acceptance authority by #1987 task 5.2 and the runbook §4.10.5 gate; the only
  other caller of `measure_workload` is the test suite. No other CLI or service reads `evidence_kind`.
- Absent: no schema, no migration, no service runtime, no scheduler — this CLI is operator-invoked.

Seams under test: the CLI entrypoint `main(argv)` with injected `connection` / `sql_probe` / `api_probe` (the
existing tests' seam, `tests/test_node27_pgdata_workload.py:712-812`), an injected head resolver for the SHA
binding so no test depends on the ambient checkout state, plus the published receipt file on disk. Tests assert
through those, not through private helpers.

Required evidence:
- No `--evidence-kind` → published receipt has `evidence_kind: "isolated"`, `isolated: true`, `live: false`;
  exit 0.
- `--evidence-kind live` with a read-only `nhms_display_ro` session and `--reviewed-sha` equal to the checkout
  HEAD → receipt has `evidence_kind: "live"`, `isolated: false`, `live: true`; every other field is structurally
  identical to the isolated run of the same inputs (only the kind triple and timestamps differ); exit 0.
- `--evidence-kind live` with a session that is not read-only, or whose `current_user` is not `nhms_display_ro`
  → typed refusal (`SQL_NOT_READONLY` / `DSN_ROLE_INVALID`), exit non-zero, and **no file at `--output`**.
- `--evidence-kind live` with a `--reviewed-sha` that is well-formed but is not the resolved HEAD, or with a dirty
  tracked tree → `INPUT_SHA_UNBOUND`, exit non-zero, no file at `--output`.
- `--evidence-kind live` where HEAD cannot be determined (non-git, `git` unavailable, timeout, malformed output,
  or the anchor is not itself the repository root git answered for — nested plain tree, redirecting `GIT_*`)
  → `INPUT_SHA_HEAD_UNAVAILABLE`, exit non-zero, no file at `--output`.
- `--evidence-kind live` where the working `node27_pgdata_workload*` modules resolve outside the anchored
  checkout (published script bytes plus a `PYTHONPATH` checkout) → `INPUT_RUNTIME_UNBOUND`, exit non-zero, no
  file at `--output`.
- `--evidence-kind rehearsal` → argparse rejects it with the CLI's static usage error (no input echoed).

Non-goals: collecting #1987's D11 curve receipts; changing query semantics, thresholds or sample counts;
auto-upgrading to `live` without the flag (rejected: it makes the evidence grade an invisible side effect of the
environment, and a reviewer could not tell from the command what the receipt claims).

Review focus:
1. The isolated path is unchanged, including the receipt bytes and the refusal codes.
2. Every live refusal happens before publication, so no partial or misleading file can exist.
3. The SHA binding reads the HEAD of the checkout that actually executes, not an argument-supplied or
   environment-supplied value, and fails closed when HEAD cannot be determined (not a git checkout, dirty state
   handling stated explicitly).
4. `live: true` cannot be reached through any path that skips `prove_readonly_session`.
5. Secrets: the DSN never reaches argv, the receipt, or a refusal message (`format_refusal` redaction).
