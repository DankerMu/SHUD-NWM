# Tasks — restore node-27 maintenance services (#2285, #2425, #2360)

Issues: #2285 (installed unit drift), #2425 (compression work deleted by
retention), #2360 (canonical retention identity). Operator decisions: design
"Context". Fixture level: **expanded** — production deletion lanes (DB
`drop_chunks` ordering, NFS `rmtree` identity), a new root-installed system
unit, and live node-27 writes; not high/broad because no schema, API or data
contract changes and every live step has a recorded rollback.

Divergence from upstream: none of the three issues carried a
`Suggested fixture level` (all `needs-triage`); expanded is set here.

## Change surface

- `scripts/node27_timeseries_compression.py` — `_classify` only.
- `scripts/node27_raw_retention.py` — lane gate in config, `collect_targets`,
  summary field.
- `scripts/node27_unit_failure_alert_once.sh` — journal scope switch.
- `scripts/node27_canonical_retention_install.sh` — new, root, once.
- `infra/systemd/nhms-node27-raw-retention.service` — `OnFailure=`.
- `infra/systemd/system/nhms-node27-canonical-retention.{service,timer}`,
  `infra/systemd/system/nhms-node27-system-unit-failure-alert@.service` — new.
- `infra/env/node27-timeseries-compression.example` (bound 2 + comment),
  `infra/env/node27-raw-retention.example` (lanes, identity paragraph).
- Tests: `tests/test_node27_timeseries_compression.py`,
  `tests/test_node27_raw_retention.py`, alert-handler and unit-file tests.
- Docs: `docs/runbooks/tier-node27-timeseries-storage.md`,
  `docs/runbooks/current-production-ops.md`, receipt dir
  `docs/runbooks/receipts/2026-09-18-issue-2285-2425-2360-service-restore/`.

## Must preserve

- MP1 `_CHUNK_QUERY` SQL text and its `ORDER BY` unchanged (catalog-only,
  deterministic); lag rule `range_end < now − lag` unchanged.
- MP2 budget chain legs 1–3 and the preflight unchanged. Intended exception:
  `DEFAULT_COMPRESSION_PER_TICK_BOUND` 4→2 (design D2), with
  `tests/test_node27_timeseries_compression_runner_config.py` updated with it.
- MP3 raw-retention with `NODE27_RAW_RETENTION_LANES` unset: identical plan,
  deletions, skips, lock behaviour, exit code (summary gains only the lanes
  field).
- MP4 copyback lock contract (`0600`, owner == euid == root owner, never create)
  and `lockf` primitive unchanged.
- MP5 alert handler with `NHMS_UNIT_FAILURE_JOURNAL_SCOPE` unset: identical
  output and exit codes.
- MP6 no change to the retention runner or its window.

## Seams under test

- `compression._classify(all_chunks, now_utc=, lag_seconds=, per_tick_bound=)`
  (pure).
- `raw_retention.config_from_env(args)` + `collect_targets(config, now=)` +
  `main()` with injected watermark (existing seams in
  `tests/test_node27_raw_retention.py`).
- Alert handler via subprocess with a stub `journalctl` on `PATH` and a stub
  sendmail (existing pattern if present; otherwise that shape).
- Unit files as text (existing unit-file assertions pattern).

## Risk packs

Selected: correctness (selection order, lane gate fail-closed), integration
(systemd units, install script, env contract), security (root install script,
secret env handling, lock identity), test evidence (overlap fixture, MP3
byte-identity). Not selected: performance (no hot path; capacity is D2
arithmetic, evidenced live), spec-compliance-only (covered by spec deltas +
validate), frontend/API (untouched).

## Implementation tasks

- [x] 1.1 `_classify`: newest-first within hypertable per design D1; update the
      docstring and the runbook's "selection is table-major … range_end" text.
- [x] 1.2 Tests: overlap fixture (spec scenario 1, #2425 AC5), table order
      preserved, free slot to next hypertable, deferred order deterministic;
      update existing `_classify`/receipt tests whose expected order encoded
      oldest-first (each change named in the report).
- [x] 1.3 Template bound 4→2 with the D2 derivation comment, and
      `DEFAULT_COMPRESSION_PER_TICK_BOUND` 4→2; pin tests updated.
- [x] 1.4 Runbook per-tick capacity section: new dated re-derivation (narrow
      geometry, 55 s/GB, wall/throughput/retention-overlap, legacy #1988 limit
      and its date-bound check); fix stale "table-major range_end" wording.
- [x] 2.1 `NODE27_RAW_RETENTION_LANES` per design D3 + summary `lanes`.
- [x] 2.2 Tests for every spec scenario of the lane requirement, plus MP3
      (unset → same summary as before apart from `lanes`).
- [x] 2.3 `infra/env/node27-raw-retention.example`: lanes variable, per-unit
      values, identity paragraph (split replaces the "counts.failed paused"
      text).
- [x] 2.4 The documented summary `jq` check covers both summary dirs
      (`/home/nwm/node27-raw-retention-logs/` and
      `/var/log/nhms-node27-canonical-retention/`): canonical `*_unsafe` skips
      and stale summaries are rc=0 and never reach `OnFailure=`.
- [x] 3.1 System units + system alert template per D4/D5; user unit
      `OnFailure=`; unit-file tests.
- [x] 3.2 Alert handler scope switch + test (MP5).
- [x] 3.3 Install script per D6 (+ `bash -n`, shellcheck if available, a
      test that runs it in a precondition-failure path without root effects).
- [x] 3.4 `current-production-ops.md` canonical identity section: split
      deployment, operator sudo step, rollback, check commands.
- [x] 3.5 `select_ci_tests.py` rules for new files if its path rules require.
- [x] 4.1 Stage B live deployment (design D8) + receipts (2026-09-19, receipt
      README "Stage B"; E6/E7 are post-merge postings to #2425).

## Required evidence

- E1 `uv run pytest -q tests/test_node27_timeseries_compression*.py
  tests/test_node27_raw_retention*.py` + new tests — green, run locally and on
  node-27 (TMPDIR=/home/nwm/tmp).
- E2 `uv run ruff check .`; `openspec validate restore-node27-maintenance-services
  --strict --no-interactive`.
- E3 Stage A receipt: before/after unit diffs, bound line count, preflight rc,
  retention catch-up `Result=success` + dropped chunk list, manual compression
  run under bound 2 `Result=success` + receipt `selected`.
- E4 Stage B receipt: compression unit installed file equals the repo file
  (`diff ~/.config/systemd/user/X infra/systemd/X`) and `systemctl --user show
  -p DropInPaths,WorkingDirectory,ExecStart` shows no drop-in and `/home/nwm/NWM`;
  live env `REPO_ROOT=/home/nwm/NWM` (grep count 1); compression run on repo code
  `Result=success`, `per_tick_bound=2`, `selected` newest-first, receipt
  `head_sha` == `git -C /home/nwm/NWM rev-parse HEAD`;
  `nwm` raw-retention run `Result=success`, `lanes=["precip-cache","raw"]`,
  `failed=[]`; canonical system unit `Result=success`, `User=frd_muziyao`,
  canonical `deleted[] > 0`, lock failures 0; both summaries carry equal
  `retention_days` and `sources` (`cutoff` follows the watermark of each run); `systemctl is-active
  nhms-node27-canonical-retention.timer` = active with a next elapse in
  `list-timers`; system alert template started by hand logs `SENT` and
  `SMTP-ACCEPTED` in the system journal, and its mail body contains real
  `nhms-node27-canonical-retention.service` system-journal lines (not the
  placeholder or a permission error); lock file still
  `-rw------- frd_muziyao`; `systemctl --user --failed` without the three units.
- E5 #2285 acceptance: `diff ~/.config/systemd/user/X infra/systemd/X` empty and
  `DropInPaths` empty for raw-retention service, retention timer, compression
  service (`systemctl cat` output carries a `# path` header line, so it is not
  compared directly).
- E6 #2425 acceptance 1/3 (two consecutive daily ticks; compressed count
  non-decreasing; selected ∩ dropped = ∅; size drop persists): posted to #2425
  after merge from the next two ticks.
- E7 #2425 acceptance 4: after Stage B, `scripts/node27_pgdata_workload.py measure
  --evidence-kind live` (tier runbook §D11 invocation) for a run whose forecast
  window overlaps a compressed `hydro.river_timeseries` chunk; `PASS` within the
  D11 bounds, posted to #2425 before it is closed (design Risks).
## Non-goals

See design "Goals / Non-Goals"; each has its reason there.
