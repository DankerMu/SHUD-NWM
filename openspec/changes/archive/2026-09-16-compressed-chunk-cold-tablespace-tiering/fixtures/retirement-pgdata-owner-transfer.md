# R1 PGDATA owner-transfer implementation fixture

Issue: #1895, epic #1891. Upstream: merged PR #2304 / tasks.md R1.1, R1.2 and
PGDATA portion of R1.5. Suggested fixture: expanded, accepted; repair intensity
high. This fixture authorizes only the first owner-transfer slice, not all R1
completion or R3 deletion.

Minimal mergeable slice: migrate PGDATA host/migrate and their tests away from
cold-family imports, move only consumed command/container/hardware-evidence
behavior to PGDATA owners, update all callers and CI selectors together. Cold
callers that still genuinely use moved implementation must call its real owner
directly; no legacy re-export or copied implementation. Cold-only
planning/installer behavior remains under its existing owner until a later
dependency-closed retirement slice.

## Must preserve and seams

- `Host.command`: bounded argv subprocess, kill/process/pipe closure, output
  limits, nonzero status, stable secret-safe `MigrationError` boundary.
- Moved command raises a PGDATA-owned `CommandError`, not a cold subclass or
  alias. `Host.command` translates it to `MigrationError`; cold
  `node27_cold_tablespace_host` Docker action/inspect boundaries preserve
  `ColdHostError`, and `compressed_chunk_cold_target` internal command callers
  preserve `ColdRuntimeError` through explicit local translation. Audit its
  callers at original lines 395/516, cold-host catches 161/192 and direct
  command tests 774–795: moved direct tests expect the new owner error, cold
  public-boundary tests still expect their existing errors.
- `Host` hardware admission: descriptor-bound root evidence, actual mdadm RAID
  membership and both-member SMART checks; original identity/age/refusal
  semantics.
- `Migration` public plan/prepare/copy/activate/rollback/release behavior:
  activate owns exact rebind; rollback owns pre-write restoration and refusal
  after writes released. Preserve complete copy and supported container config;
  no invented rebind/abort/status CLI actions or live activation.
- `normalize_raw_inspect` / serialization: preserve supported non-default
  configuration or refuse; no cold bind/recreate/rollback machinery copied into
  PGDATA ownership.
- Image pin comes from existing `node27_external_contract_snapshot.json` /
  container contract, not duplicated literal. Keep incompatible old cold
  binds/services as negative admission guards.
- Tests assert real PGDATA behavior, not source imports or prose. Existing
  generic command/container/evidence tests migrate with ownership and remain
  available to real cold callers until those callers retire.
- Each new command/container/evidence producer's explicit PathTestRule selects
  PGDATA migrate and oracle suites plus still-live cold consumers as applicable:
  compressed target, cold host/container/evidence/install/governance suites.
  Update `test_select_ci_tests.py` expected maps in the same merge; test-import
  closure alone does not cover producer edits, and old evidence under-selection
  must not be copied.
- Put moved closures in PGDATA-owned command/container/evidence modules, not
  inflated host/migrate files. Keep each touched file within its existing
  size limit and new modules below 1000 lines, with no new guard exemptions.
- Update withdrawn-rollout runbook imports of moved EvidencePolicy/parsers
  (storage runbook original lines 1376/2015) now; keep their withdrawn status.
  This does not revive rollout authority; no dangling executable import awaits
  the later R1.4/R3 documentation deletion.

## Risk packs

Selected: CLI/script entry (migration public seam); config/setup (container
reconstruction); file IO/path safety (evidence/copy/command); auth/secrets
(private descriptor evidence and redacted errors); concurrency/ordering (command
cleanup and migration fence); resource limits (bounded output/time); legacy
compatibility/examples (container snapshot, protective cold rejection);
error/rollback/partial output (pre-write vs stale post-write);
release/dependency (all imports and CI selection); documentation/migration notes
(retained owner references). Schema/columns/units: selected for existing
normalized snapshot/dataclasses only; no receipt schema change intended. Domain
selected: PostGIS/TimescaleDB (exact-image physical oracle), published artifact
identity (container/evidence binding). Domain not selected: geospatial/CRS,
hydro-met temporal semantics, SHUD numerical/runtime, Slurm, provider snapshots,
run-manifest QC (no changed behavior on these surfaces).

## Invariant Matrix

Governing invariant: changing implementation ownership must not change any
accepted/rejected PGDATA transition, container identity, hardware evidence or
bounded-command outcome. Source of truth: existing migration state/first-write
marker, held file facts, Docker inspect normalization and external-contract
image snapshot.

| Surface          | Public seam / regression row                                                                              | Required evidence                                                                                                                                            |
| ---------------- | --------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Producer/process | Host.command with valid argv, nonzero, timeout, oversized output and inherited pipe                       | Existing/migrated bounded command tests plus safe command smoke; same results and cleanup, no leaked child or raw secret                                     |
| Validators       | Host hardware evidence with valid RAID + both SMART members, stale/mismatched/symlink/nonregular evidence | Migrated behavioral acceptance/refusal tests; no installer path or backup-inventory dependency                                                               |
| Storage/state | Migration plan/prepare/copy/activate/rollback/release | Existing unit transitions and isolated engine oracle: exact copy/rebind, pre-write rollback, refusal after writes released |
| Entry/caller     | PGDATA host/migrate CLI and still-live cold callers of moved primitives                                   | All caller imports updated, targeted selection points to existing assertion-bearing suites, full backend collection/regression remains valid                 |
| Downstream       | Container config, current PGDATA placement and ordinary maintenance                                       | Preserve every supported inspect field and unknown-field refusal; current cold-bind/service rejection tests still pass; no unit or governance config changes |
| Evidence         | Existing receipt/error/publication formats                                                                | No added schema protocol; stable migration error boundary and private evidence stays private                                                                 |
| Error cutover | PGDATA CommandError through Host.command and cold Docker/target callers | PGDATA MigrationError and cold ColdHostError/ColdRuntimeError remain stable at each public boundary; no cold imports in moved owner |

Boundary checklist: command process roots, public migration CLI, descriptor read
surfaces, migration write/rollback state, inspect producer/serializer, stale
first-write marker, unchanged cold callers and CI selectors. Source census
starts with `node27_pgdata_host.py:20-23`, `node27_pgdata_migrate.py:24-25`,
both PGDATA tests. History `cb607d62e`, `e6ed6c2ee`, `bd31bb093`, `bad464651`
requires retaining governance admission, primary env identity, ExecStart
metadata normalization and deployed venv pin — no opportunistic simplification.

## Verification and completion

- Local: Ruff on changed Python; strict active OpenSpec validation; Markdown for
  changed docs.
- Node-27 isolated checkout/venv, owned TMPDIR under `/home/nwm/tmp`; check `/`,
  `/home`, `/data/GHDC` capacity first. Never run tests in active checkout or
  production DB.
- `uv run pytest -q tests/test_node27_pgdata_migrate.py tests/test_node27_external_contract_snapshot.py tests/test_select_ci_tests.py`
  plus moved command/container/evidence suites selected by actual ownership.
- `NHMS_RUN_NODE27_DOCKER=1 uv run pytest -q -m 'integration and timescaledb_210 and node27_docker' tests/test_node27_pgdata_migrate_oracle.py`:
  exact image disposable cluster with real assertions, no empty/all-skipped
  proof.
- `uv run pytest -q`: default backend regression on node-27; orchestrator runs
  verification once per final source state, reviewers inspect evidence only.
- New behavior tests, if needed for uncertain edges, need one batched pre-change
  red proof on node-27; behavior-preserving transfer can retain existing tests
  and use command smoke instead of new source-pin tests.
- R4.1: apply only matching PGDATA command/container/evidence ownership
  requirement with this slice; leave governance placement and retained SQL
  evidence deltas for their matching slices. Main owns canonical OpenSpec
  integration.
- Before merge: independent four-seat high-risk cross-review, verifier
  adjudication, final gap sweep, exact-head CI, Chinese summary, origin-tip
  identity. User preauthorized merge, not production changes.

Non-goals: no actual PGDATA migration, production unit/env changes, DROP/REVOKE,
node-22, compression decoupling, cold-only tree deletion, C4/SQL workload
transfer, governance extraction or archive. Do not close R1.5 globally, #1895 or
epic #1891 on this slice.
