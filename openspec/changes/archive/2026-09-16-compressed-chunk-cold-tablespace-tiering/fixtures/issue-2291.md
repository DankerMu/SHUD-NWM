# #2291 reviewed census cardinality fixture

Upstream fixture: expanded. Repair intensity/effective tier: high (shared original
baseline, file evidence and multi-gate acceptance). Project profile: NHMS.
Depends on #2290, merged via PR #2292 at `4615a05fc4f0e74e732433941895fdde82115dab`.
Parent #1895 / #1891. This is one atomic preparation cutover, not a live window.

## Authority and non-goals

The reviewed input N is canonical decimal 1..63 at CLI boundaries and a strict
integer at in-memory/top-level census boundaries; bool is not an integer here.
There is no default, historical-six fallback, observed-count assignment or
oldest-N truncation. The catalog scan can still see at least one extra candidate.

G1 validates its fresh successful private census against the externally supplied
N, reviewed SHA and existing command bracket before freezing authority. Freeze N
and the existing held-reader `facts["sha256"]` in the existing exclusive mode-0600
policy file, alongside the existing semantic `CENSUS_DIGEST`. The original census
is not rewritten to contain its own hash. Existing semantic digest meaning and
receipt/census wire formats remain unchanged.

This supplemental whole-file anchor is required, not speculative hardening:
`node27_cold_residency_census.py:531,572` hashes only ordered key/group_digest pairs;
`:447-460` adds parity, inventory and expansion outside snapshot group_digest.
`node27_issue1895_private_receipt.py:301-306` already computes the complete held
byte digest. Later consumers must use the G1-frozen expected hash, never compute
expected authority from the reopened original or a current observation.

After private-file/provenance and frozen-byte-hash validation, the ORIGINAL
`required_group_count` is the downstream scalar authority. Validate it against
resolved count, config echo, ordered unique keys, raw groups and capacity count
before any later live observation/mutation. Check raw cardinality and duplicates
before constructing maps. Keys must match their group identities and order.
Original GO/head/preimage and existing digest/parity/bracket checks remain intact.
A self-consistent replacement of the original still fails its old external hash.

Capacity policy representation is preserved: status `resolved`, group_count a
canonical decimal STRING, byte values canonical decimal strings. Top-level
required/resolved/config counts are integers. Reuse the shared checked capacity
policy over every approved group's measured expansion/retained values; preserve
E=max(expansion), S=sum(retained), reserves E/E, install_required=S, rollback2E and
installer aggregate S+2E. No multiplication of a sample by N or chunk interval.

Original baseline validation is not a validator for G8 output documents. Original
N groups remain the named identity/parity authority, while current source/target
sets and newly-terminal natural-tick groups keep their distinct existing semantics.
No newly-terminal group is absorbed into or discarded from the baseline to hit N.
PER_TICK_BOUND=1 and all time/row/byte/complexity limits remain unchanged.

N=63 is a supported input, NOT a guarantee every possible 63-group payload fits
existing limits. Prove a complete representative 63-group serialized artifact
through the real loading/acceptance chain, and reject oversized artifacts without
raising limits or truncating. The held input limit is currently1MiB; census output
has its own4MiB ceiling. A dead declaration is not an active resource guarantee.

Non-goals: production SSH/DB/active checkout, G0-G8, service/timer actions,
maintenance scheduling, other issues, #2290 admission redesign, #2293 mixed-session
evidence canonicalization, migrations/lifecycle/lag, C4/display API, receipt schema
changes or extra per-tick mutations. Zero groups or unavailable narrow river proof
cannot be presented as parent live acceptance or filled with legacy. Shared change
stays active and production tasks4.1-4.8 remain unchecked.

## Interface and ownership contract

Use existing owners rather than a second census/eligibility implementation.
Implementation is serial because original authority, callers, runbook and selector
closure form one shared boundary; cross-review remains parallel.

- `node27_cold_residency_census_policy.py`: dependency-light canonical reviewed
  count parsing/validation, shared1..63 limit. Reuse its existing error family;
  keep capacity output shape/formula. No readiness/runtime/probe import from the
  pre-target census through this owner. CLI malformed counts remain stable errors.
- Census CLI: `--require-count` remains required; expose the real1..63 range and
  preserve the independent64 scan capability and extra detection. No count-only
  mode that accidentally runs parity or changes existing publication semantics.
- `node27_issue1895_census_bind.py`: shared original-document validation and frozen
  file loading. Provide `validate_original_census(document, *, expected_count,
  reviewed_sha)` and `load_original_census(path, *, expected_original_sha256,
  reviewed_sha)` returning the validated original document and N. Cosmetic error
  context may be supplied to preserve existing public refusal classifications;
  original hash/count authority is never optional. Use the existing held-reader
  facts and existing durable-key owner; do not move/redefine key serialization.
- `bind_pre_movement_census`: retain existing expected semantic digest/current
  bracket/reviewed SHA and add mandatory expected_original_sha256. Validate the
  original first, then validate current cardinality against original N and retain
  exact ordered keys, inventory/parity, complete-source and capacity comparisons.
- `assert_sequential_tick_receipt`: add mandatory keyword expected_count. Validate
  N and all N ordered unique keys; i is1..N, one migrated Ki, legal prior
  already_cold, exact deferred suffix. Preserve bound1 and planned dry-run mode.
- `persist_baseline_groups`, `assert_exact_cold_groups`, `observe_post_target`:
  add mandatory expected_count and check the whole original N identity set before
  observations. `assert_natural_tick_selection` receives explicit original keys
  and expected_count; retain separate remaining/newly-terminal logic.
- `run_post_target_observation`: add mandatory expected_original_sha256; load and
  validate the original before opening the observation connection, then pass N.
  Preserve #2290's stable ColdRuntimeError adapter and programming-error behavior.
- G5/G6/G8 CLI loaders: require uniform `--original-sha256`; G6 additionally gets
  required `--reviewed-sha`. Migrate every caller, including runbook inline Python,
  dry-run, loop-key extraction, sequential receipt and both post-target calls.
  No unanchored groups-only original loading or default-six compatibility route.
- G3: small `scripts/node27_issue1895_cutoff_count.py` entrypoint. Required original
  path, `--original-sha256` and reviewed SHA. Reuse existing readonly connection,
  watermark/lag and #2290 inventory/ranked/group owners; first validate original
  file and bind fresh inventory to its frozen digest. Observe current catalog at
  the same eligibility cutoff, do not read baseline N as the result. Unexpected
  mixed/incomplete/drifted groups refuse rather than being filtered away. Print
  the bounded observed integer; caller compares it with reviewed N. Extra counts
  or zero are not GO receipts; overflow/bound failures are nonzero refusals. No
  parent/all-table count SQL, legacy fallback or full-row parity for this count.
- Runbook G1 policy freezes `REQUIRE_COUNT` and `ORIGINAL_CENSUS_SHA256` after
  existing successful/bracket/SHA checks; G3 uses the count entrypoint; later
  commands pass the frozen hash and original-derived N. Do not re-freeze these
  from a current observation or auto-retry with observed size. Preserve unrelated
  writer-unit/field counts and label historical six/bytes as historical only.
- `scripts/select_ci_tests.py`: integration owner appends targets to existing
  unique rules; every changed acceptance owner gets asserting suite and independent
  removal proof with all existing targets/marker/meta-guards retained.

## Risk packs considered

| Pack | Selection / evidence |
|---|---|
| Public API/CLI/script entry | selected: mandatory N/hash caller cutover and stable refusals |
| Config/project setup | selected: frozen policy inputs, direct selector ownership |
| File IO/path/overwrite | selected: held original bytes, private/no-clobber policy, replacement refusal |
| Schema/columns/units/field names | selected: strict integer versus canonical string count, unchanged wire |
| Auth/permissions/secrets | selected: existing readonly connections and redacted/no-publication refusal |
| Concurrency/shared state/ordering | selected: immutable original authority and exact sequential suffix |
| Resource/discovery limits | selected: N1/63, extra visibility,1MiB/complexity/byte bounds unchanged |
| Legacy compatibility/examples | selected: no six fallback, legacy/third-table excluded, all callers migrate |
| Error/rollback/partial outputs | selected: malformed/stale authority before observation; E/S/2E preserved |
| Release/dependency compatibility | not selected: no dependency/package change |
| Documentation/migration notes | selected: executable G1/G3/G5/G6/G8 contracts and production HOLD |
| Geospatial/CRS | not selected: no geometry |
| Hydro-met time series/windows | selected: actual mixed ranges, frozen cutoff, newly-terminal semantics |
| SHUD numerical runtime | not selected: no solver |
| PostGIS/TimescaleDB | selected: pinned actual eligible/excluded population and extra origin |
| Slurm lifecycle | not selected: no node22/scheduler |
| External providers | not selected: no provider input |
| Run manifest/QC | not selected: unchanged payloads |
| Published evidence/identity | selected: original SHA/count/keys/parity across producers/consumers |

## Invariant Matrix

Governing invariant: every later acceptance consumes the SAME externally frozen,
validated original census N and identities; no current observation can authorize
itself, a changed original cannot pass, and no population is truncated to N.

| Surface | Owners | Required proof |
|---|---|---|
| Producers | census parser/artifact, G1 policy | external N retained; counts/ordered groups/policy agree; whole-file hash freezes once |
| Validators/preflight | shared original loader, G5 | malformed/count/set/order/hash/SHA/bracket drift refuses before query/mutation |
| Storage/query | G3 count + #2290 catalog | current physical population, actual ranges; excluded legacy/third table; N+1 detected |
| Public entrypoints | census, cutoff-count, G5/G6/G8 CLIs and runbook | complete non-six chain, required frozen hash, stable refusal/no publication |
| Downstream consumers | sequential receipt, post-target, timer | explicit N, exact i/suffix/original keys, distinct newly-terminal groups |
| Failure/rollback/stale state | all original loaders and capacity | self-consistent replacement/oversize/duplicates/partial budgets refuse; E/S/2E unchanged |
| Evidence/readiness | held facts, policy, selector | complete63-group artifacts fit unchanged gates; independent owner-removal proofs |
| Unchanged siblings | #2290/#2224, forcing, C4/installer | physical/origin/role/parity/replay proof stays green; no unrelated operation |

## Scenario evidence and verification

- `tests/test_issue2291_reviewed_census_count.py`: direct public count/loader and
  complete non-six G5→all G6 calls→post-target/natural reconciliation;1/63; invalid
  scalar forms; duplicate/missing/extra/order/identity cases; external old hash
  against a self-consistent replacement; count tampering; oversized held input;
  full measured-value capacity arithmetic and overflow guards.
- Existing storage/c14/publication/census tests migrate to complete original
  fixtures. Frozen test hashes are captured BEFORE mutation, never auto-derived
  by argv helpers from the file being validated. Preserve unrelated refusal,
  programming-error, private-path and natural newly-terminal behavior.
- Existing pinned runtime integration harness invokes a new count discriminator:
  real admitted narrow/forcing with differing ranges, compressed legacy and third
  table excluded, real census and independent count agree for non-six N, extra
  admitted origin is detected then fixture state restored. Retain all #2290/#2224
  invoked physical-parent/role/plan/recompression/rollback/cleanup assertions.
  Add a load-bearing invocation/selector proof per the existing harness contract.
- `tests/test_select_ci_tests.py`: every changed owner→asserting suite→unique route
  and independent removal; all prior legs remain. No duplicate rule whitelist.
- Local: `uv run ruff check .`; `openspec validate
  compressed-chunk-cold-tablespace-tiering --strict --no-interactive`.
- Node27 owned isolated resources only: focused/default regression and
  `NHMS_RUN_NODE27_DOCKER=1 uv run --no-sync pytest -q -m 'integration and
  timescaledb_210 and node27_docker' tests/test_compressed_chunk_cold_runtime_integration.py`.
- Capture executed tests-first semantic red at existing public seams (not missing
  symbol collection), then exact-head green. Temporary cross-version repro glue
  is evidence only and is removed; permanent callers/tests require the new inputs.
- Fixture review and strict PASS before implementation; independent cross-review,
  verifier/final Gap Sweep, exact-head CI and human merge gate. User authorized
  implementation/verification/review of #2291, NOT its merge or production.

## Fixture review closure: explicit boundary checklist

Every original-loading CLI SHALL require BOTH `--original-sha256` and
`--reviewed-sha`: `scripts/node27_issue1895_census_bind.py`,
`scripts/node27_issue1895_sequential_receipt.py`,
`scripts/node27_issue1895_post_target_observe.py`,
`scripts/node27_issue1895_group_reconcile.py`, and the new cutoff-count CLI.
Retain existing reviewed-SHA flags; G6 adds the missing one. G5 bind, G6 preview,
inline dry-run/key extraction/loop, both G8 post-target calls and G8
group-reconcile migrate. Direct unanchored `read_held_private_json` of
ORIGINAL_CENSUS is forbidden after the G1 freeze; observations/receipts remain
separate input kinds and do not become original-baseline authority.

G3 uses the existing `per_table_catalog_limit(N)` extra-slot arithmetic:
per-table ceiling N+1, at most64, with the shipping ranked loader's own overscan
refusal unchanged. A complete extra admitted group produces the observed N+1
(or other bounded surplus) integer; the runbook equality guard rejects it.
Zero likewise fails the equality guard. Mixed/incomplete/drifted admitted groups
or scan/resource overflow produce nonzero refusal and NO integer output. Neither
successful integer output nor a count mismatch is a full GO receipt.

The boundary63 proof SHALL use one complete real-shaped serialized original,
not keys-only or reduced group stubs, under the existing1MiB held-input ceiling.
That SAME frozen file traverses `load_original_census`, G5 binding, all63 G6
calls and G8 original-N named-group acceptance/reconciliation. Separately prove
an oversized original is rejected. No1MiB input,4MiB census output, active
262144-byte performance publisher or complexity limit is raised. The unrelated
post_target.MAX_BYTES declaration is not claimed as an active gate and does not
justify adding a new gate. Boundaries apply to each actual consumer's input kind.

The selector table below is additive to each exact unique existing owner rule.
Q denotes `tests/test_issue2291_reviewed_census_count.py`; every row has a Q
behavioral assertion and an independent removal mutant which removes that leg
only and proves all original selected legs remain. Existing expanded constants
are preserved in full; the table names them rather than restating their members.

| Changed owner | Existing targets to retain | Added assertion/removal leg |
|---|---|---|
| `packages/common/node27_cold_residency_census_policy.py` | NODE27_COLD_RESIDENCY_CENSUS_CLOSURE_TESTS | Q canonical count/capacity |
| `scripts/node27_cold_residency_census.py` | census closure + storage + ORIGIN_CHUNK_PARITY_TESTS + issue2290 | Q public count/extra |
| `scripts/node27_issue1895_cutoff_count.py` (new) | no existing rule; one exact new rule only | Q fresh count/refusal + pinned runtime integration |
| `packages/common/node27_issue1895_census_bind.py` | readiness_storage + runbook_contract | Q original hash/count/G5 |
| `scripts/node27_issue1895_census_bind.py` | ISSUE1895_READINESS_STORAGE_TESTS + runbook_contract | Q CLI original authority |
| `packages/common/node27_issue1895_receipt.py` | readiness_storage + runbook_contract | Q sequential boundaries |
| `scripts/node27_issue1895_sequential_receipt.py` | ISSUE1895_READINESS_STORAGE_TESTS + runbook_contract | Q sequential CLI |
| `packages/common/node27_issue1895_post_target.py` | ORIGIN_CHUNK_PARITY_TESTS (includes readiness_storage) + issue2290 | Q named-group original N |
| `scripts/node27_issue1895_post_target_observe.py` | ISSUE1895_READINESS_STORAGE_TESTS + runbook_contract | Q post-target CLI |
| `packages/common/node27_issue1895_timer.py` | readiness_gates + readiness_c14 + ISSUE1895_READINESS_STORAGE_TESTS + runbook_contract | Q original/new terminal split |
| `scripts/node27_issue1895_group_reconcile.py` | readiness_c14 + readiness_gates + ISSUE1895_READINESS_STORAGE_TESTS + runbook_contract | Q reconciliation CLI |
| `docs/runbooks/tier-node27-timeseries-storage.md` | existing origin/runtime/lifecycle/readiness/runbook/issue2290 targets in full | Q executable frozen-authority commands |

Any additional changed helper, fake or harness owner receives its actual
assertion-bearing suite through its current unique rule, with independent
removal proof. Existing selector tests/meta-guards and support-module routes
remain; a newly added route does not replace coverage of unchanged siblings.

Executed CLI smoke found direct-file Python launch cannot resolve the existing
`scripts` package imports in a clean environment. All five affected runbook CLI
entrypoints use the existing `python -m scripts.<module>` convention from repo
cwd. Real subprocess tests extract those documented launches without PYTHONPATH
and reach original-validation refusal, including both G8 observations. This is
an invocation cutover, not a sys.path shim or packaging/schema change.

Round1 wrapper closure: G3's residual driver/OS failure after a SUCCESSFUL
watermark and at the distinct observation opener/session must produce stable
non-secret refusal with no integer/traceback. Tests must reach that second
operation; a malformed DSN failing in the already-wrapped watermark is not proof.
Retain finally cleanup after acquisition. Use the existing census CLI boundary
pattern at the new G3 entrypoint, not a shared-connection or G8 error-model change.
