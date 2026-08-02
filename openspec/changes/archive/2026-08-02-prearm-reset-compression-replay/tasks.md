# Tasks: prearm-reset-compression-replay

Fixture level: compact · Repair intensity: light · Issue #1088

Triage note: S — one new script + one new test file, fully hermetic
(tmp_path workdirs, fake systemctl script, no docker/DB/node-27).
Suggested fixture level was not stated on the issue; compact chosen
for a single-file additive tool with no production-file mutation.
Fixture-review round 1 (REVISE, 2×P1+2×P2) corrected the issue's own
whitelist — see proposal "Premise corrections" #4; dispositions below
are the reviewed ones. Risk axes: (1) DELETION SAFETY — no delete
call anywhere in the module (`unlink`/`rmtree`/`os.remove`/`rmdir`
absent, asserted by source scan AND behaviorally: every pre-sweep
byte findable under the archive). (2) NEXT-ARM VIABILITY — the sweep
must remove exactly what would kill the next arm (stale
`finalizer-state.json`, supervisor.py:1465-1478; stale
`supervisor-ledger.jsonl`, :269-270 O_EXCL) and keep exactly what
the next arm needs (`run-plan.json` for ConditionPathExists,
.service:3; `terminal-evidence.json` for the expected-stale CAS
check, :1766-1773); both directions get named assertions.
(3) ONESHOT ACTIVATING TRAP — a running oneshot reports `activating`
with nonzero rc, so the gate compares OUTPUT TEXT against
{inactive, failed}; the fake systemctl MUST mirror real systemd
semantics (rc=0 only for `active`; other states print the text and
exit 3) or task 7's first red proof is vacuous. (4) INTENT-FAMILY
INTEGRITY — the intent gate is a single-cycle state machine
(compression_terminal_state.py:1620/:1670/:1796/:1018): unresolved
intent (failure-intent dir/consumed sibling present, or gate state
!= idle, or gate json unreadable) REFUSES before any move; resolved
residue is swept as a whole family, never partially.
(5) PLAN-AUTHORED PATHS — commands carry `artifact_associations`;
captures carry only `output_path` (exact key set enforced at
supervisor.py:573-581; capture trust boundary :891-898); residue
archiving walks both, lstat no-follow, collision-safe naming; missing
plan = sweep-only mode with notice, invalid JSON = refuse.
(6) SELF-SWEEP — `prearm-archive/` excluded; a second run creates a
second timestamped subdir and leaves the first intact.
(7) TRUST-BOUNDARY ZERO-DIFF — supervisor.py refusals at :789-794
and :1050-1058 (current anchors; the issue's :781-786/:1042-1048 are
stale) untouched, proven by `git diff` scope. Single review round.

Must preserve:
- Zero diff outside `scripts/node27_timeseries_compression_prearm.py`
  and `tests/test_node27_timeseries_compression_prearm.py` (+ this
  fixture): supervisor, plan_author, capture, live_evidence, replay,
  `packages/common/**`, `infra/**`, `docs/runbooks/**`, `schemas/**`.
- Whitelist exact set (minimal, reviewed): `run-plan.json`,
  `terminal-evidence.json`. Everything else in the workdir is swept
  once the refusal gates pass — including `finalizer-state.json`,
  `supervisor-ledger.jsonl`, `.finalizer-state.json.*.consumed`, and
  the resolved intent-family residue (`.terminal-evidence.json.
  publish.lock` / `.intent-gate.lock` / `.intent-gate.json`).
- Existing suites untouched and green: capture 14, live_evidence 277,
  supervisor 127 at master 8cda366d.

## Implementation tasks

- [x] 1. `scripts/node27_timeseries_compression_prearm.py`: argparse
  (`--workdir` default `/home/nwm/node27-timeseries-compression-replay`,
  `--replay-env-path` default
  `infra/env/node27-timeseries-compression-replay.env` resolved
  against the repo root via `Path(__file__).parents[1]` — never CWD,
  `--systemctl` default `/usr/bin/systemctl`); strict env parser
  (refuse symlink, refuse mode != 0600, refuse duplicate keys,
  KEY=VALUE only — mirror
  scripts/validate_two_node_docker_runtime.py:808-826); module-level
  `PrearmError`; every refusal exits 1 with the reason on stderr;
  `if __name__ == "__main__": raise SystemExit(main())` (issue AC 1's
  `python -m scripts.node27_timeseries_compression_prearm` form).
- [x] 2. Refusal gates, all before any move, in this order:
  (a) unit-state — run `<systemctl> --user is-active
  nhms-node27-timeseries-compression-replay.service`, accept stdout
  exactly `inactive` or `failed`, refuse anything else including
  subprocess failure/missing binary; NEVER use rc==0 as the sole
  signal (activating trap).
  (b) receipt digest — `NODE27_COMPRESSION_EXPECTED_STALE_SHA256`
  must be present and 64-hex UNCONDITIONALLY (review round 1); when
  `<workdir>/terminal-evidence.json` exists its `terminal_identity`
  sha256 must equal the pin; mismatch/malformed refuses; missing
  receipt proceeds WITH an explicit warning that the arm will refuse
  at the supervisor expected-stale gate (supervisor has no first-arm
  branch — :1767-1773 unconditionally requires the receipt).
  (c) intent family — refuse if `.terminal-evidence.json.
  failure-intent` (dir) or any `.terminal-evidence.json.
  failure-intent.consumed-*` exists, or `.terminal-evidence.json.
  intent-gate.json` exists with state != `idle` or unreadable;
  stderr must direct the operator to resolve the intent (e.g.
  supervisor `--finalize-only`) first.
  (d) run-plan — present but invalid JSON refuses; absent =
  sweep-only mode (workdir sweep runs, association pass skipped with
  a printed notice).
- [x] 3. Sweep: `<workdir>/prearm-archive/<UTC-iso>/` (colon-free
  UTC timestamp); move every non-whitelisted direct child except
  `prearm-archive` (whitelist = `run-plan.json`,
  `terminal-evidence.json` ONLY); then residue pass over the plan's
  command `artifact_associations` values and capture `output_path`s
  (lstat no-follow, skip whitelisted workdir members, collision-safe
  naming under `<archive>/associations/`); `shutil.move` only — no
  delete API anywhere in the module.
- [x] 4. Forensics + UX: `<archive>/prearm-manifest.json` via
  `atomic_write_bytes_no_follow` (UTC timestamp + from→to pairs);
  stdout prints archive path and the exact next-step arm command
  (`systemctl --user start
  nhms-node27-timeseries-compression-replay.service`); a clean
  workdir exits 0 with a "nothing to archive" notice and creates no
  archive dir.
- [x] 5. `tests/test_node27_timeseries_compression_prearm.py`
  (`from scripts import node27_timeseries_compression_prearm as
  prearm` — namespace-package import, zero conftest plumbing): fake
  systemctl as an executable `#!{sys.executable}` script in tmp_path
  that mirrors REAL systemd `is-active` semantics — prints the
  configured state; rc=0 only when the state is `active`, rc=3
  otherwise (precedent shape:
  tests/test_scheduler_file_provider_refresh.py:2410-2440, but note
  that precedent always exits 0 — do NOT copy that part).
- [x] 6. Test coverage minimum: (a) whitelist survives (`run-plan.json`,
  `terminal-evidence.json` named) / non-whitelist archived with byte
  content preserved; (b) NAMED next-arm-viability assertions:
  pre-seeded `finalizer-state.json` and `supervisor-ledger.jsonl`
  land in the archive and are gone from the workdir; (c) out-of-
  workdir residue: schema-dump association AND a capture
  `output_path` under a plan `--root` outside the workdir both
  archived; (d) refusals with workdir-byte-identical + no archive
  dir created: unit `active`, unit `activating`, missing systemctl
  binary, receipt-digest mismatch, invalid run-plan JSON, pending
  intent (`.failure-intent/` present) and gate state `consuming`;
  (e) missing receipt proceeds; missing run-plan sweeps with notice;
  (f) resolved intent residue (gate json state `idle` + stale locks,
  no failure-intent dir) is swept as a family; (g) second run leaves
  the first archive intact; (h) no-delete guarantee (source scan +
  byte-findability); (i) env-file symlink/mode/duplicate-key
  refusals.
- [x] 7. Red proof (scratch mutation, restored, outputs recorded):
  (i) flip the unit-state gate to rc-based (`returncode != 0` =
  safe) → the `activating` refusal case itself goes red (the fake
  systemctl's real-semantics rc makes `activating` exit 3, which the
  mutant misreads as safe); (ii) drop `prearm-archive` from the
  sweep exclusion → the second-run test goes red. Record both
  outputs and name the exact failing tests.
- [x] 8. Oracle: `uv run pytest -q
  tests/test_node27_timeseries_compression_prearm.py` green;
  `uv run python -m scripts.node27_timeseries_compression_prearm
  --help` exits 0 (AC 1 invocation form); `uv run pytest -q
  tests/test_node27_timeseries_compression_capture.py
  tests/test_node27_timeseries_compression_live_evidence.py
  tests/test_node27_timeseries_compression_supervisor.py` → 418
  unchanged; `uv run ruff check .`; `git diff --stat` → exactly the
  two new files + fixture; `openspec validate
  prearm-reset-compression-replay --strict --no-interactive`.

- [x] 9. Review round 1 fix pass (2× verifier-CONFIRMED P2, real-fs
  reproductions): (a) label/capture_id used for archive naming
  validated as single safe path components under gate (d), pre-move
  (absolute label escaped the archive; traversal capture_id wrote
  into the replay workdir — both reproduced); (b) both move loops
  wrapped — OSError/shutil.Error → best-effort partial manifest
  (self-guarded write) then PrearmError, so mid-sweep failure (ENOSPC
  reproduced) exits with the refusal prefix instead of a raw
  traceback and keeps the from→to record; (c) cross-device semantics
  documented + EXDEV-monkeypatch test (content preserved at
  destination before shutil's fallback removes the source; module
  docstring rescoped, source-scan test scope stated precisely);
  (d) missing-receipt premise corrected: unconditional 64-hex pin
  validation, sweep proceeds with an explicit warning + qualified
  next-step, `retained in place` lists only files actually present
  (supervisor :1767-1773 has no first-arm branch); test renamed
  accordingly. Claims-lens P2 fixed by ticking tasks 1-8 (record
  drift).

## Required evidence

- Named next-arm-viability proof (finalizer-state.json +
  supervisor-ledger.jsonl swept; run-plan.json + terminal-evidence
  kept); the refusal matrix (unit states × receipt digest × intent
  family × run-plan validity) with workdir-untouched assertions;
  red-proof outputs for both task-7 mutations naming the failing
  tests; pytest counts (new file + 418 regression); ruff; zero-diff
  proof for supervisor.py; PR body MUST record the AC-2 AND AC-3
  premise corrections as deviations (AC-2: paths are NOT env-driven —
  workdir via flag defaulting to the systemd literal, schema-dump via
  run-plan.json; AC-3: the issue's own whitelist would kill the next
  arm — finalizer trio and consumed markers are now swept, see
  proposal premise-correction #4 with supervisor.py:1465-1478/:269-270
  and compression_terminal_state.py:1018 evidence) and the AC-8
  optional integrations as operator-gated follow-up routing.

## Non-goals

- Supervisor/plan_author/capture/live_evidence/replay changes; systemd
  unit or env example changes; runbook/bringup-checklist edits (the
  issue's optional integrations require a node-27 live rehearsal
  receipt — operator-gated, routed as follow-up); any delete path;
  automatic production invocation; resolving a pending failure
  intent on the operator's behalf; #1089 surfaces.
