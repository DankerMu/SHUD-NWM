# Tasks: node27-external-contract-snapshot

Fixture level: compact · Repair intensity: light · Issue #1089

Triage note: M — one standalone script + one committed JSON fixture +
hermetic tests + one runbook section; no supervisor/verifier semantics
touched. node-27 needed ONCE, read-only, for the committed baseline
(`--dump`); that use is authorized. Timer/workflow installation is
operator-gated and out of scope. Live recipes re-measured 2026-08-02
(see proposal): the unset-timestamp rendering is deterministically
observable ONLY via a never-existing unit name (the real inactive unit
has run this boot), and `CLIENT_BACKEND_TYPE` has a deterministic
self-witness via `pg_backend_pid()`. Fixture review round 0
(ACCEPT with tightenings, all folded): 2 P1 (exit-code collision
between misalignment and drift on the same tampered input → four-state
ordered exit contract with mutation tests split; never-existing-unit
witness proves rendering only, not the consumer checkpoints' shape →
recorded limitation + escalated pre-existing consumer defect), 6 P2
(provenance-stripping semantics, empty-output classification,
probe-failure spec scenario, on-node --check PYTHONPATH recipe, §4.0
step-3 mount point, never-writes test binding), 5 P3 (absolute
container argv, LoadState=not-found fail-closed, bidirectional
alignment via # MEASURED count, XDG_RUNTIME_DIR, test-list
consistency). Risk axes:

1. READ-ONLY HARD BOUNDARY — the probe must be incapable of mutating
   node-27 state: every spawned argv comes from a frozen whitelist
   (systemctl --user show / docker --version / docker inspect /
   docker exec readlink / docker exec pg_restore --version / psql
   SELECT-SHOW only), `--check` never writes any file, `--dump`
   writes only stdout or an explicit `--output`. A test walks the
   frozen argv table and rejects any verb outside the whitelist; SQL
   strings must match a SELECT/SHOW-only pattern.
2. DETERMINISM VS VOLATILITY — compared fields must be deterministic
   probes; volatile facts (backend_type distribution counts,
   timestamps, hostname, the real recurring unit's state) live in
   `informational` and are NEVER compared. A drift check that flakes
   on autovacuum noise would train operators to ignore it (the G9
   lesson inverted). CONSTRAINT (review P1-2): the never-existing-unit
   witness proves systemd's RENDERING contract only, not the
   loaded-but-never-started whole-dict shape consumed at
   `supervisor.py:1282-1293` / `live_evidence.py:834-845`; the probe's
   in-code comment must name those two consumer sites and state that a
   green check does not imply they pass (their pre-existing timer-era
   defect is escalated separately).
3. FIXTURE↔CONTRACT 1:1 — the fixture `contract` section must equal
   `node27_container_contract`'s three measured constants, enforced
   twice: hermetically in CI (alignment test) and on-node inside
   `--check` (lazy import). No auto-update path anywhere.
4. SEAM HONESTY — host binaries resolve through a pinned-`/usr/bin`
   bin-dir seam (supervisor precedent, `SUPERVISOR_BIN_DIR` style);
   hermetic tests repoint it at stub binaries to execute the real
   probe/diff code paths; a test asserts the production default.
   Container-internal paths stay literal (`/usr/bin/pg_restore` inside
   nhms-db is not this host's binary).
5. EVIDENCE — one real node-27 `--dump` output is the committed
   baseline AND the PR evidence; PG env is sourced from
   `infra/env/node27-timeseries-compression-replay.env` on the node,
   never embedded in repo files (no credentials in fixture/script).

Must preserve:
- `packages/common/node27_container_contract.py` unchanged.
- Hermetic lock scope untouched
  (`tests/test_node27_timeseries_compression_live_evidence.py`).
- Supervisor/verifier/capture/benchmark/replay scripts untouched.
- CI stays host-free green: no new test may require node-27, docker,
  or a live DB (stub-bin seam only).

## Implementation tasks

- [ ] 1. `scripts/node27_external_contract_snapshot.py`: probes +
  `--dump [--output PATH]` / `--check [--fixture PATH]` CLI. Probe set
  (all read-only): unset-timestamp rendering via `systemctl --user
  show <reserved never-existing unit> -p LoadState -p
  ExecMainStartTimestamp` — assert `LoadState=not-found` else classify
  probe-execution failure (fail-closed if the reserved name ever
  becomes a real unit), and document in-code why a real unit cannot
  witness this (P1-2 comment naming the two consumer sites);
  pg_restore entrypoint realpath via `docker exec nhms-db
  /usr/bin/readlink -f /usr/bin/pg_restore` (container argv[0]
  absolute, matching runbook §4.0 and supervisor idiom);
  client-backend self-witness via `psql --dbname nhms --no-psqlrc -At
  -c "SELECT backend_type FROM pg_stat_activity WHERE pid =
  pg_backend_pid()"`; host_context: systemd/docker/pg_restore/server/
  timescaledb versions + nhms-db image ref/id; informational:
  backend_type distribution, measured_at (UTC), hostname, real
  recurring unit ActiveState/SubState/ExecMainStartTimestamp.
  Exit-code contract (four states, judged in order): 0 ok;
  misalignment (fixture.contract vs contract module — decided FIRST,
  before any probe is spawned); drift; probe-execution failure. Probe
  output that is empty or missing the expected key/property is
  probe-execution failure, NEVER drift (a drift report always carries
  a real observed value; a returned property line whose value changed
  IS drift). Structured drift report names every drifted field with
  expected vs observed. `--check` asserts fixture.contract ==
  contract-module constants via lazy import; the dump path must run
  importless so a single copied file works on node-27 without a
  branch checkout.
- [ ] 2. `packages/common/node27_external_contract_snapshot.json`:
  committed baseline from task 5's real `--dump`, with `contract` /
  `host_context` / `informational` sections. Every compared entry is
  `{"value": ..., "_provenance": {command, date, source}}`; comparison
  and alignment read `value` ONLY — `_provenance` never participates
  in any diff, so a provenance-only edit reds nothing. No secrets.
- [ ] 3. `tests/test_node27_external_contract_snapshot.py` (hermetic,
  stub-bin seam): (a) alignment guard, BOTH directions —
  fixture.contract equals the three `node27_container_contract`
  constants AND the count of `# MEASURED`-prefixed constant blocks in
  the contract module's source equals `len(fixture["contract"])` == 3
  (a future 4th measured constant reds this without touching the
  module); (b) drift mutation — tamper one `host_context` value with
  observations stubbed true → drift exit code + report names exactly
  that field; (b2) misalignment mutation — tamper a `contract` value
  (e.g. `systemd_unset_timestamp` → `"-"`) → misalignment exit code
  and NO probe is spawned (stub-bin records zero invocations);
  (c) clean pass — stubbed observations matching the fixture → exit
  0; (d) read-only whitelist — every argv the module can spawn
  matches the frozen whitelist, every SQL string is SELECT/SHOW-only;
  (e) bin-dir production default pinned; (f) informational tampering
  → still exit 0; (g1) probe exits non-zero → probe-failure code;
  (g2) probe exits 0 with empty output → probe-failure code; (g3)
  probe exits 0 missing the expected key/property → probe-failure
  code, and the report names the failing probe, not a drifted field;
  (h) provenance-only edit → `--check` exit 0 and alignment test
  green; (i) after `--check` runs, the fixture bytes are unchanged
  and no new file exists in its directory (never-writes binding);
  (j) dump shape — `--dump` output parses and carries the three
  sections with compared entries in `{"value", "_provenance"}` form.
- [ ] 4. Runbook `docs/runbooks/tier-node27-timeseries-storage.md`,
  two deliverables: (4a) new "host-contract snapshot 漂移处置"
  section — invocation recipe (ssh, source
  `infra/env/node27-timeseries-compression-replay.env`, confirm
  `XDG_RUNTIME_DIR=/run/user/$(id -u)` for `systemctl --user` — unset
  means bus connect fails and is classified probe-execution failure,
  see supervisor.py:176-183 — then `uv run python
  scripts/node27_external_contract_snapshot.py --check`), drift
  handling loop (drift → PR review → accept-new-contract updates
  fixture + contract constants + hermetic-lock mutation-RED tests
  TOGETHER, or roll back the host change; never a silent update;
  name the patch-version-only drift class explicitly — e.g. Ubuntu
  auto security updates bumping docker/systemd patch versions — whose
  handling is "confirm no semantic change, then update fixture via
  PR", so operators are not trained into mindless updates), and the
  operator-gated note for optional weekly scheduling (not installed
  by this change); (4b) revise existing §4.0 step-3 dry-probe
  (:966-985): run `--check` FIRST and gate continuation on exit 0,
  keep the manual trio as script-unavailable fallback, and state
  that until a timer/workflow is installed this step is the sole
  pre-mutation interception point.
- [ ] 5. Real node-27 baseline (read-only, authorized): copy the
  single script file to a node-27 temp dir (NOT a branch checkout of
  /home/nwm/NWM), source the env file, run `--dump`, save output as
  the committed fixture (task 2), attach the dump verbatim to the PR
  as baseline evidence, then run `--check --fixture
  <tempdir>/snapshot.json` on the node with
  `PYTHONPATH=/home/nwm/NWM` (master checkout resolves the lazy
  contract import — this change does not touch
  `node27_container_contract.py`, see Must preserve) → exit 0 (live
  green proof). Note the difference from the runbook recipe (which
  assumes a checked-out tree post-merge). Remove the temp copy
  afterwards.
- [ ] 6. Oracle: `uv run pytest -q
  tests/test_node27_external_contract_snapshot.py` green;
  `uv run ruff check .`; `openspec validate
  node27-external-contract-snapshot --strict --no-interactive`;
  mutation red-green recorded; live `--check` exit 0 receipt from
  node-27; `git diff --stat` → exactly script + fixture + test +
  runbook (+ this fixture).

## Required evidence

- Real node-27 `--dump` output (baseline) + live `--check` exit-0
  receipt; hermetic mutation red proof naming the tampered field;
  alignment-guard red proof (flip a contract constant in-memory →
  test red); read-only argv whitelist listing; pytest/ruff/openspec
  outputs; zero-diff-outside-declared-files proof.

## Non-goals

- Hermetic lock scope, supervisor/verifier semantics, fixture
  auto-update, timer/workflow installation (operator), node-22,
  repo-side `RECOVERY_TARGET_*` host-probing (not host facts).
