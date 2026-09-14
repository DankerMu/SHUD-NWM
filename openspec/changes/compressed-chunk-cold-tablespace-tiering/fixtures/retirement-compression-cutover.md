# R2 and dependent R3 atomic compression retirement fixture

Issue #1895 / epic #1891. This merge implements R2.1–R2.3 and only the concrete dependent-R3 closure enumerated below; other R3 work stays in the remaining-deletion fixture. tasks.md remains authority. Expanded accepted, high repair intensity. Independent of R1.4 except if a real retained consumer emerges. No cold rollout or effective deployment authorization.

## Publication closure and invariant

One merge replaces paired budget/preflight/unit/wrapper/runner assembly with normal compression only, and removes every cold runner/wrapper or other caller that consumes the removed API, including tests/config/schema/examples/selectors/manual entries. Never publish R2-only with broken old callers. Unrelated cold base modules may remain until their real issue1895 importers retire; no compatibility aliases, hidden cold launch or renamed cold protocol.

Budget owner becomes compression-owned, with all actual imports migrated. Derive defaults from statement timeout plus cleanup <= wrapper and wrapper plus required systemd margin <= service, accounting for actual tick execution semantics. Preserve configurable validated declarations, never copy old service duration merely because it exists. Current receipt schema already has single-lane budget fields; inspect real consumers and leave it unchanged when evidence shows no change needed.

## Concrete atomic census and preserved bounds

This merge covers only the dependent portions of R3 rows, not all R3.1–R3.5 or
all R3.7. Delete cold runner/wrapper/env example, paired budget declarations and
cold assembly fields, preflight cold options, unit cold ExecStart and old
--cold-env preflight, and the lifecycle test's cold CLI branch. Close all
transitive callers before release, including tests importing the deleted CLI.
Unrelated cold tablespace/governance/census/G0–G8 functions remain for their
later deletion closure only when independent of removed interfaces.

`node27_issue1895_watermark.assert_systemd_invocation_facts` and
`scripts/node27_issue1895_systemd_facts.py` require dual ExecStart and therefore
retire in this merge with their dedicated tests. Do not rewrite G8 acceptance
to approve a single lane. Keep watermark parsing/other functions still used by
unretired display_runtime until their own closure; no alias for the deleted
function. Mixed readiness_storage_publication tests retain unrelated assertions.
Remove obsolete runbook source-text pins instead of repinning their wording.
R1.4 is not a prerequisite for this systemd-facts closure.

Mixed-import closure in this merge:
- `test_node27_connection_attribution.py` and its `_delegated.py` suite remove
  cold CLI imports/registered component/invoker/delegated-map entries, keeping
  ordinary component attribution. Deleted CLI selector loses its attribution
  edge; other component owners keep theirs.
- `test_node27_timeseries_sequential_runner_config.py` migrates to the
  compression-only owner suite; drop cold.config_from_args and cold pair keys,
  remove incidental default pins, preserve ordinary config/refusal behavior.
- Dedicated `test_node27_cold_residency{,_phase2,_publication,_runtime_identity,
  _schema_compat}.py` suites retire with the CLI. In
  `test_compressed_chunk_cold_runtime.py` and its `_integration.py` suite, remove
  CLI RunnerConfig-dependent tests of the retired runner; preserve independently
  exercised runtime engine tests only when they use existing engine-owned
  fixtures/types without the deleted CLI. Do not create a new owner/facade just
  to keep tests of retired runner behavior.
- Mixed readiness_storage_publication W8/systemd test retains cutoff/horizon
  assertions but removes dual-ExecStart assertions; delete watermark
  parse_exec_start/COLD_WRAPPER used only by retired systemd facts. Keep
  observe_current_cutoff/assert_independent_receipt_horizon/parse_systemctl_show
  and their remaining consumers. Remove obsolete G8 runbook source-text pins.

New budget owner: `packages/common/node27_timeseries_compression_budget.py`;
all consumers migrate, old sequential-budget module disappears with no aliases.
Normal statement default3600000ms plus cleanup300s gives wrapper3900s.
Preserve strict existing outer-margin semantics: service > wrapper+40s,
therefore default3941s; TimeoutStartSec and receipt example use3941. Overrides
retain wrapper>=ceil(statement_ms/1000)+300 and strict service inequality.
Default bound4 is unchanged; inspect tick supervision semantics and report any
contradiction rather than silently multiplying/changing policy. Keep retention
timer06:36 unchanged: no cadence retightening or live schedule change.
Receipt schema2.1 already single-lane, remains unchanged; update echoed defaults
in example/current runbook, no new cold fields or version bump.

Public safety regression scenarios:
- mode0600 regular descriptor-bound compression env accepted; quoted inert
  values cannot execute; symlink/nonregular/oversize/duplicate/noncanonical
  declarations refuse before child/DB work;
- absent cold env and absent cold pair keys succeed for actual compression;
  removed cold flags refuse, partial/forged assembly refuses;
- preflight ignores caller PYTHONPATH via -E; final scripts import origin must
  equal intended root; lane-file executable override values stay inert;
- runner arguments remain argv after --, timeout retains TERM and
  kill-after30s with child cleanup, required environment retained and secrets
  absent from failures;
- below-bound wrapper/service refuse, equality at wrapper minimum accepted
  but service==wrapper+40 refused, validated catch-up overrides accepted;
- fixed lifecycle mutex before local/DB lock, override refused, contention
  with surviving compression/retention/replay/PGDATA remains tested.

Selector closure: each compression budget/preflight/wrapper/unit/env producer
selects surviving compression-budget, compression-wrapper, compression,
retention, lifecycle and selector suites; preserve existing additional actual
consumers such as wrapper_pythonpath. Compression timer selects
test_node27_timeseries_compression.py (and any real compression scheduling owner),
never test_node27_timeseries_retention.py. Retention timer selects only
test_node27_timeseries_retention.py. Lifecycle producer selects
test_node27_timeseries_lifecycle_lock.py, test_node27_timeseries_compression.py,
test_node27_timeseries_retention.py and
test_node27_timeseries_decompression_replay.py, dropping cold residency.
Delete cold CLI/wrapper/env rules, update every affected expected map, and add new
producer paths rather than rely on test-import closure. No dangling selected
path; inspect actual shared helper consumers for additional needed edges.

Named evidence suites: test_node27_timeseries_compression_budget.py and
test_node27_timeseries_compression_wrappers.py (migrated behavioral suites),
test_node27_timeseries_compression.py, test_node27_timeseries_retention.py,
test_node27_timeseries_lifecycle_lock.py, test_select_ci_tests.py and
test_node27_wrapper_pythonpath.py, plus surviving mixed consumers.
Actual wrapper smoke uses NODE27_TIMESERIES_COMPRESSION_REPO_ROOT and
NODE27_TIMESERIES_COMPRESSION_ENV_FILE to target owned isolated checkout/private
env and runs `scripts/node27_timeseries_compression_once.sh --enforce`; fixtures
provide disposable DB identities and verify real compression/receipt. Unsafe
env variants run the same entry and must refuse without DB work.

Rewrite current ordinary-maintenance catch-up §4.5, preflight instructions and
env example to one compression env and matching budget/drop-in. Existing
withdrawn rollout sections and immutable receipts remain historical; not current
activation instructions. Apply safe-launch ADDED hypertable-compression and
owner-suite MODIFIED ci-contract-baseline deltas with this source slice.
Live observed TimeoutStartUSec/source pins/env stay under separate R5.2;
template3941 does not claim the currently observed service has changed.

Governing invariant: one finite, origin-verified compression launch from one descriptor-bound private inert configuration cannot activate cold work, exceed admitted budget, leak secrets, or bypass the fixed lifecycle mutex; normal compression/retention/discovery/lag behavior remains intact.

## Invariant matrix

| Surface | Source/seam | Expected behavior and refusal |
| --- | --- | --- |
| Config | sequential budget owner read_lane_env_file/parser, migrated compression owner | Single mode0600 regular nonsymlink descriptor-bound file; quoted inert values remain data, malformed/oversize/duplicate/noncanonical integers refuse |
| Budget | compression runner resolve_runner_budget and preflight | Statement/cleanup/wrapper/service inequalities enforced for defaults and overrides; short wrapper/service refuses, caller cannot forge assembly to bypass checks |
| Entry | timeseries_budget_preflight.py, compression_once.sh, compression.service | Real compression launch needs no cold env; --cold-env/--launch cold unsupported; argv remains argv and Python/module origin bound to intended root |
| Child | preflight subprocess/exec and compression CLI | Finite wall timeout/cleanup, safe error without DSN; no child survives timeout; required environment preserved without caller injection |
| Mutex | node27_timeseries_lifecycle_lock + compression/retention/replay/PGDATA | Fixed lifecycle mutex acquired before local/DB locks; override refusal and surviving contention hold; implementation preserved |
| Consumers | cold runner/wrapper and their tests/config/schema/selectors/manual references | All paired API consumers removed or legitimately migrated same merge; no stale collection import or selected nonexistent suite |
| Data/receipts | ordinary compression/retention/catalog discovery | Existing retention windows, lag, canonical/legacy discovery and ordinary receipt meanings unchanged |
| Deployment | repository templates only | Template diff is not live activation or live absence proof; actual systemd/dropin/env/source pin deferred to separately authorized R5.2 |

Boundary checklist: shared budget exports and transitive callers; descriptor/config read; import-origin probe; child argv/environment/timeout; lock ordering and cleanup; role and catalog siblings; shell/systemd/manual and schema/CI closure. Source names are navigation, not wildcard deletion authority. Keep generic Docker collection behavior; transfer its test owner if affected, never change conftest gate to pass. Do not delete display-cold-waterfall.

## Risk packs

Selected: CLI (launch/refusal), config (single private env), file IO (descriptor identity/path), schema/fields (audit actual receipt effects), auth/secrets (safe error/environment), concurrency (mutex/child cleanup), resources (timeout bounds), legacy/examples (atomic removal), errors/partial outputs (no child/false PASS), release/dependencies (shell/systemd/import/CI closure), documentation (current normal maintenance instructions). Domain selected: Timescale maintenance and time-series lag/retention compatibility. Not selected: geospatial, SHUD/numerics, Slurm, external providers, manifest/QC, published display identity: no behavior changes.

## Evidence floor

Parent validates only after edits complete; workers skip all tests/build/lint/formatters.
- Local Ruff and strict OpenSpec, scoped shell/Markdown checks according to repo conventions.
- Node27 owned isolated checkout/venv/TMPDIR with capacity check; targeted budget/config/wrapper/compression/retention/lifecycle/selector suites and affected surviving consumers.
- Actual compression_once.sh launch with private compression env and absent cold env, against isolated PostgreSQL15.2/Timescale2.10.2. Assert execution, receipt and real normal maintenance result; no --help/all-skipped proof.
- Actual unsafe-config refusal before DB/child work; timeout/cleanup and surviving lifecycle contention regression. Retain existing protective unsafe env/origin tests; remove only tests of intentionally retired contracts and incidental pins.
- Final backend default full and nonempty isolated engine assertions, active-reference disposition audit, exact-head CI, high-risk independent review and final gap sweep.
- Matching hypertable-compression/CI canonical changes and R2/dependent-R3 tasks completion recorded only with evidence. R3 remainder, effective deployment, archive and epic closure remain pending.

Rollback by complete reviewed source closure, never restore old cold production activation. No DROP, live REVOKE, data/private evidence deletion, active service operation, source-pin handoff or node22 action in this slice.
