# orchestrator-structural-burndown Specification

## Purpose
Keep the orchestrator, scheduler and test corpora below the 1,000-line structural guard by splitting
oversized modules and test files without behaviour drift: extractions preserve mutation fences, patch
seams and CLI facades, and every physically partitioned test corpus keeps its collection identity.

## Requirements

### Requirement: Scheduler lease extraction SHALL preserve mutation fences

Scheduler lease extraction SHALL preserve mutation fences even when lease code
is moved to a dedicated module.

#### Scenario: scheduler pass starts
- **WHEN** the production scheduler starts a pass
- **THEN** lock-root preflight, lease acquisition, runtime-root preflight,
  startup reconcile, candidate discovery, lease-lost fence, pre-execution
  evidence reservation, and mutation occur in the same order as before

#### Scenario: lease renewal races with another holder
- **WHEN** lease renewal, CAS, live-holder detection, or cross-host TTL behavior
  is exercised
- **THEN** only the current valid holder can renew or mutate, and lease loss
  prevents orchestrator construction and submission

### Requirement: Candidate-state extraction preserves legacy compatibility

Candidate-state and identity validation helpers SHALL be extracted without
changing evidence fields, legacy aliases, retry decisions, or terminal-state
guards.

#### Scenario: legacy candidate state row is evaluated
- **WHEN** a candidate state row lacks full M23 identity fields but has legacy
  compatible source/cycle/model evidence
- **THEN** the same authoritative, legacy-non-authoritative, or filtered
  decision is produced as before extraction

#### Scenario: manual retry or active Slurm evidence is present
- **WHEN** candidate-state validation sees manual retry, active Slurm sync,
  terminal success, permanent failure, cancelled, or stale failure evidence
- **THEN** the extracted helper preserves the current decision and evidence
  payload

### Requirement: Discovery and backfill extraction preserves candidate ordering

Cycle discovery, completion checks, and backfill selection SHALL be moved only
when candidate ordering and legacy fallback behavior remain unchanged.

#### Scenario: backfill gaps are present
- **WHEN** backfill is enabled and multiple gaps exist
- **THEN** the oldest eligible gap is selected first and later gaps are deferred
  exactly as before

#### Scenario: model discovery returns empty
- **WHEN** backfill is enabled but no models are available
- **THEN** scheduler behavior falls back to the legacy newest-cycle mode

### Requirement: Candidate construction extraction preserves gating

Candidate construction SHALL preserve canonical readiness, fresh full-chain,
zero-canonical, active Slurm sync, duplicate candidate, and blocked-cycle
behavior.

#### Scenario: canonical readiness is unavailable
- **WHEN** canonical readiness provider raises or returns blocked evidence
- **THEN** the candidate is blocked/deferred with the same reason and evidence
  fields as before extraction

#### Scenario: active Slurm sync is allowed
- **WHEN** active Slurm status sync updates stale active rows
- **THEN** candidate inclusion, defer, or skip decisions remain unchanged

### Requirement: Scheduler execution extraction preserves pass ordering

Scheduler execution extraction SHALL preserve forcing, cohort grouping,
concurrent submission evidence, and scheduler pass mutation ordering.

#### Scenario: forced production run is scheduled
- **WHEN** a forced production run enters execution after candidate selection
- **THEN** the extracted execution helper preserves the current forcing
  decision, cohort grouping, submission behavior, and evidence payload

#### Scenario: concurrent candidates are submitted
- **WHEN** multiple eligible candidates are submitted in one scheduler pass
- **THEN** concurrent submit evidence, candidate ordering, and mutation fences
  remain the same as before extraction

#### Scenario: published artifact root is absent during planning
- **WHEN** runtime root preflight runs with a missing `published_artifact_root`
  and all other required roots valid
- **THEN** the preflight reports `published_artifact_root.allow_create=true`,
  does not emit `SCHEDULER_ROOT_PUBLISHED_ARTIFACT_ROOT_NOT_FOUND`, and dry-run
  planning remains non-mutating without creating the directory

#### Scenario: temp root is absent at pass start
- **WHEN** a scheduler pass runs its runtime root preflight with a missing
  `temp_root` whose parent is an existing, writable, non-symlink directory,
  the path inside the approved roots, and all other required roots valid
- **THEN** the preflight reports `temp_root.allow_create=true` and does not
  emit `SCHEDULER_ROOT_TEMP_ROOT_NOT_FOUND`
- **AND** the pass creates only the final `temp_root` component without
  following symlinks, with mode 0700, before the preflight verdict is acted on
- **AND** the pass evidence records `root_preflight.checks.temp_root.created=true`
  and the pass is not `preflight_blocked` by `temp_root`

#### Scenario: an absent temp root cannot be created safely
- **WHEN** the missing `temp_root` is outside the approved roots, is itself a
  symlink, or its parent is missing, unwritable, or a symlink
- **THEN** the scheduler reports the existing `SCHEDULER_ROOT_TEMP_ROOT_<REASON>`
  blocker before registry, adapter, active-repository, or submission work
  begins, and creates no directory

#### Scenario: any other required runtime root is absent
- **WHEN** runtime root preflight runs with a missing workspace, object-store,
  runtime, lock, or evidence root
- **THEN** the scheduler reports the existing root blocker before registry,
  adapter, active-repository, or submission work begins

### Requirement: Scheduler evidence extraction preserves reservation proof

Scheduler evidence extraction SHALL preserve pass evidence assembly,
pre-execution reservation proof, bounded serialization, and evidence keys.

#### Scenario: pre-execution evidence is reserved
- **WHEN** a scheduler pass reaches the pre-execution evidence reservation step
- **THEN** reservation proof is written after lease-lost fencing and before
  mutation/submission with the same evidence fields as before extraction

#### Scenario: pass evidence is serialized
- **WHEN** scheduler pass evidence is assembled or bounded for persistence
- **THEN** status, reason, schema version, evidence keys, and serialization
  limits remain unchanged

### Requirement: Chain type and catalog extraction preserves static contracts

Chain type and stage-catalog extraction SHALL preserve existing stage
definitions, stage ordering, context/result type contracts, and `chain.py`
import compatibility.

#### Scenario: stage catalog is moved
- **WHEN** static chain stage definitions move to `chain_stages.py`
- **THEN** stage IDs, order, script names, success/failure states, array flags,
  and downstream dependencies remain unchanged

#### Scenario: chain types are moved
- **WHEN** shared contexts, result dataclasses, or type aliases move to
  `chain_types.py`
- **THEN** existing imports from `chain.py` keep working through compatibility
  re-exports until callers migrate

### Requirement: Chain stage extraction preserves reservation protocol

Forecast-chain stage execution SHALL preserve reserve-before-sbatch and
bind-after-submit semantics for array and non-array stages.

#### Scenario: reservation is lost
- **WHEN** a stage reservation reports an already-inflight owner
- **THEN** no sbatch submission is made by the losing pass

#### Scenario: reservation is won
- **WHEN** a stage reservation is created or reclaimed
- **THEN** sbatch uses the same deterministic idempotency comment and the
  reservation is bound only after a real Slurm job id is returned

#### Scenario: startup reconcile finds reserved-unbound rows
- **WHEN** startup reconcile runs before planning/submission
- **THEN** dead reserved-unbound rows are handled without causing duplicate
  Slurm submissions

#### Scenario: active stage is resumed after startup
- **WHEN** an existing non-terminal stage job is found during chain execution
- **THEN** the chain polls that Slurm job to terminal, records the same
  status/event/log evidence, and does not submit a replacement job

#### Scenario: stage polling times out
- **WHEN** a submitted stage does not reach a terminal Slurm state before the
  configured timeout
- **THEN** the pipeline job and forecast cycle are marked with the stable
  `SLURM_JOB_TIMEOUT` failure evidence and no fabricated accounting is emitted

#### Scenario: terminal log publication fails
- **WHEN** terminal log persistence fails during submit, resume, or timeout
  handling
- **THEN** durable job status evidence is still recorded and the chain does not
  advertise a missing or failed published log URI

#### Scenario: manual retry targets a terminal stage
- **WHEN** manual retry metadata targets a failed, permanently failed,
  submission-failed, cancelled, or partially failed stage
- **THEN** the chain submits a new retry attempt identity instead of reusing the
  old terminal job id or idempotency key

### Requirement: Chain manifest extraction SHALL preserve published evidence

Chain manifest extraction SHALL preserve published evidence, schema versions,
and evidence keys when model-run assembly, manifest index writing, runtime
manifest safe writes, array aggregation, partial status, or publish-stage
evidence moves to dedicated modules.

#### Scenario: model run manifest is written
- **WHEN** a model run manifest or manifest index is produced
- **THEN** schema version, identity fields, quality states, residual blockers,
  and safe-write behavior remain stable

#### Scenario: array stage partially fails
- **WHEN** array accounting reports partial task failure
- **THEN** aggregate status, task outcomes, downstream manifest reduction, and
  publish behavior remain the same as before extraction

### Requirement: Compatibility shims protect downstream callers

Structural extraction SHALL keep old import or method surfaces available until
callers and tests migrate.

#### Scenario: tests import private scheduler or chain helpers
- **WHEN** existing tests or downstream code import helper names from
  `scheduler.py` or `chain.py`
- **THEN** the first extraction PR keeps compatibility shims or re-exports so
  behavior-preserving tests continue to pass

### Requirement: Object-store copyback preserves complete run products

Production copyback to `NHMS_OBJECT_STORE_COPYBACK_ROOT` SHALL validate source
and target paths without following symlinks and SHALL publish only complete
`runs/<run_id>` trees.

#### Scenario: configured copyback path contains a symlink component
- **WHEN** tile publication prepares `NHMS_OBJECT_STORE_COPYBACK_ROOT`
- **THEN** the raw configured path is checked before path resolution, symlink
  components are rejected, and no target `runs/<run_id>` tree is created

#### Scenario: copyback root equals object-store root
- **WHEN** `NHMS_OBJECT_STORE_COPYBACK_ROOT` exactly equals `OBJECT_STORE_ROOT`
- **THEN** physical copyback is skipped only after every `runs/<run_id>` source
  tree passes no-follow traversal plus manifest, output, and log completeness
  validation

#### Scenario: copyback roots overlap without equality
- **WHEN** the copyback root is inside the object-store root, or the object-store
  root is inside the copyback root
- **THEN** publication fails with a normalized object-store copyback error before
  creating target run-product trees

#### Scenario: source or target object-store operation fails
- **WHEN** source reads, target writes, unsafe run ids, or filesystem validation
  fail during copyback
- **THEN** the failure is reported as a copyback publish error with run id,
  object key, object-store root, copyback root, error type, and error details

### Requirement: Copyback replacement is rollback-safe

Replacing a canonical copyback `runs/<run_id>` tree SHALL avoid deleting or
partially corrupting the previous complete tree when promotion fails.

#### Scenario: promotion fails after staging succeeds
- **WHEN** a complete staged run tree exists but replacing the canonical
  `runs/<run_id>` directory fails
- **THEN** the previous canonical tree remains available and no partial new tree
  is exposed at the canonical path

### Requirement: q_down display publication waits for required copyback

q_down display publication SHALL not advance stable object-store, published
artifact, cycle manifest, or map-layer references until required run-product
copyback has succeeded.

#### Scenario: first publish copyback fails
- **WHEN** q_down publication requires copyback and copyback fails before any
  previous q_down cycle manifest exists
- **THEN** no new q_down per-run manifest, publish log, cycle manifest, or
  display layer is visible at the stable published locations

#### Scenario: republish copyback fails
- **WHEN** a q_down cycle has already published and a later republish fails
  during copyback
- **THEN** the previous q_down cycle manifest remains unchanged and the failed
  run's new per-run manifest/log artifacts are not exposed at stable URIs

### Requirement: Scheduler configuration package split preserves all contracts

The production scheduler configuration owner SHALL be split into responsibility-
focused Python modules that are each below the repository's 1,000-line limit.
The historical owner-package and scheduler-facade imports, constructor and
frozen-dataclass contract, module helper attributes used by compatibility tests,
normalization behavior, exceptions, and DB-free evidence/blockers SHALL remain
unchanged. The deleted monolithic path SHALL no longer be excluded from the
large-file guard, and no replacement module SHALL receive an exclusion.

#### Scenario: existing callers import and construct scheduler configuration

- **WHEN** callers import `ProductionSchedulerConfig` through either
  `services.orchestrator.scheduler_config` or `services.orchestrator.scheduler`
  and construct it from direct values or environment defaults
- **THEN** the class signature, dataclass fields/defaults, normalized attributes,
  methods, exceptions, evidence, and downstream behavior are equivalent to the
  pre-split owner
- **AND** every package module is below 1,000 lines and the old exact guard
  exclusion is absent.

#### Scenario: compatibility and safety oracles inspect the package

- **WHEN** tests access historical private scheduler-config attributes or inspect
  which scheduler-config functions call `Path.resolve`
- **THEN** the package barrel exposes the same attribute surface and
  `_functions_calling_resolve` scans one module file or every `.py` beneath the
  owner package rather than only `__init__.py`
- **AND** `test_db_free_normalization_modules_call_resolve_only_where_allowlisted`
  reports scheduler-config callers exactly `{_safe_preserve_final_component}`
  and retry callers exactly `set()`
- **AND** temporarily adding an unallowlisted `.resolve()` to non-barrel
  `path_modes.py` or `db_free.py` makes that test fail before the mutation is
  reverted.

### Requirement: Basins package facade split preserves runtime and artifact contracts

The repository SHALL split `workers.model_registry.basins_package` into the six
responsibility owners defined by this change. The historical facade and every new
owner MUST remain below 1,000 lines, while `.large-file-guard.json` remains
byte-identical. Every pre-split facade attribute and callable signature MUST remain
available: eight callable seams use runtime facade forwarding, other private helpers
use plain re-export, and object/class attribute patches use the same imported module
or class objects. Public entrypoints, source/package identity, manifest and package
bytes/checksums, object-store effects, forcing/calibration policy, typed failures,
idempotency, and cleanup MUST remain equivalent.

#### Scenario: existing callers import and publish through the facade

- **WHEN** an existing CLI, reingest, registry, QHH, production-validation, scheduler,
  or test caller imports the historical module and publishes or plans a valid model
- **THEN** its imports/signatures/types resolve without a cycle
- **AND** source identity, package manifest/bytes/checksums, object keys/effects,
  forcing/calibration material, and success payload are identical to baseline.

#### Scenario: a historical dynamic dependency is patched

- **WHEN** a test patches one of the eight governed facade callables and invokes the
  existing high-level path that consumes it
- **THEN** its direct/transitive wrapper forwards the facade's current runtime
  binding rather than a stale leaf-local callable
- **AND** when a test patches a governed `os` or class attribute, the leaf observes
  the same shared module/class object, while coordinated limit values observe the
  facade binding
- **AND** the existing failure/race/limit oracle produces its baseline result.

#### Scenario: invalid and stale publication paths retain failure behavior

- **WHEN** existing malformed, symlink, ancestor-race, stale source, conflict, lock,
  verification, or output-write failure fixtures are run through the facade
- **THEN** the same typed error/details occur before the same partial-output boundary
- **AND** lock/temp/object/local cleanup and prior immutable objects remain equivalent.

#### Scenario: structural guard evaluates the finite owner set

- **WHEN** the six production files and guard configuration are evaluated
- **THEN** each file is strictly below 1,000 lines, no seventh owner or replacement
  exclusion exists, the guard digest is unchanged, and an ordinary verified commit
  passes the hook.

### Requirement: Guard exclusion burndown removes ten oversized surfaces without replacement

The repository SHALL split the ten guard-excluded oversized surfaces named by this
change so that every resulting file is below the 1,000-line limit, and SHALL remove
exactly those ten entries from `.large-file-guard.json` in the same commit as each
split. No replacement exclusion SHALL be added for any file a split produces. Where a
split forces an edit to a file that already exceeded the limit before this change and
was never excluded, the change MAY add one exclusion for that pre-existing surface,
and SHALL record the deviation and file a separate split issue for it. Both `maxLines`
and every unrelated exclusion SHALL remain byte-identical.

#### Scenario: the ten exclusions disappear and nothing takes their place

- **WHEN** `.large-file-guard.json` is compared against its pre-change content
- **THEN** exactly the ten named entries are absent, the only added entry is a
  recorded pre-existing oversize surface that a split had to edit, and `maxLines`
  plus every remaining exclusion is byte-identical
- **AND** every file produced by the ten splits is below 1,000 lines.

#### Scenario: a split lands without its exclusion removal

- **WHEN** a split commit leaves its source path in the exclusion list
- **THEN** the change is incomplete, because the guard signal for that surface
  stays closed and the burndown direction is not restored.

### Requirement: Relocated monkeypatch seams keep their negative oracles live

A relocated patch seam SHALL keep its negative oracle live: when a symbol that tests
patch through `monkeypatch.setattr(<module>, <name>, ...)` moves to a new owner
module, the change SHALL move every call site of that symbol with it, SHALL repoint the patch target to the new owner in the same commit, and
SHALL NOT re-export that symbol from the original module. Symbols whose call sites
remain in the original module SHALL keep their existing patch target unchanged.

#### Scenario: a negative case still reaches the real call site

- **WHEN** a test replaces a relocated seam with a forbidden or failing stub
- **THEN** the stub is installed on the module that actually performs the call,
  and deliberately breaking the real call site turns that test red
- **AND** the original module does not expose the relocated name, so a patch aimed
  at the stale target raises instead of passing vacuously.

### Requirement: Scheduler registry refresh surfaces split behind stable CLI facades

The repository SHALL split the scheduler file-provider refresh script, the registry
publish script, and their two pytest corpora below 1,000 lines. Both historical script paths
SHALL remain executable facades: `python -m` entrypoints, argparse surface, help
output, environment variables, flags, exit codes, `schema_version` and audit field
semantics SHALL be equivalent to the pre-split owners.

#### Scenario: the CLI contract is byte-stable across the split

- **WHEN** `python -m scripts.scheduler_file_provider_refresh --help` and
  `python -m scripts.publish_scheduler_file_registry --help` run after the split
- **THEN** both exit zero and emit stdout byte-identical to the pre-split baseline.

#### Scenario: both refresh corpora collect identically

- **WHEN** the refresh and publish suites are collected after partitioning
- **THEN** the sorted suffix sets equal the pre-split baselines of 315 and 59.

### Requirement: Entropy audit enforcement and its corpus split without report drift

The repository SHALL split `scripts/governance/audit_repo_entropy.py` and
its former single test module (now the fifteen `tests/test_entropy_audit_*.py` partitions of
`ENTROPY_AUDIT_TESTS`) below 1,000 lines with shared definitions in
support modules. The audit's check identifiers, module heatmap keys, public function
set, exit codes, and the stable subset of `metadata` SHALL be unchanged. The stable
subset is exactly `schema_version`, `mode`, `check_family_count`,
`executed_check_families`, `skipped_path_families`, `max_scanned_text_file_bytes`,
`max_artifact_fingerprint_bytes` and `baseline_path`; the remaining `metadata` keys
are excluded because they embed wall-clock time, the absolute repository root, the
resolved comparison base ref, or counts of the audit's own tracked lines, all of
which drift between any two invocations. The split SHALL also land the two deferred #1809
items: five expected-command literals migrated to the `tests/test_gateway_reconcile_*.py`
glob and the two frozen pre-#1809 provenance lines removed from the compatibility
inventories. Verification-command literals in `_ScopedAgentContextConfig`, the scoped
`AGENTS.md` files, the governance inventories and the triage document SHALL name the
new partitions.

#### Scenario: the audit report is unchanged by its own split

- **WHEN** `--format json` runs immediately before and after the enforcement split,
  with no other repository change between the two runs
- **THEN** the eight stable `metadata` keys are key-for-key equal, the check-id set,
  the `module_heatmap` and `high_spread_patterns` key sets, and the public function
  set are equal, and the exit code is unchanged
- **AND** the full `findings` diff is empty, or every residual entry is attributed to
  a path this split created or removed and is enumerated in the pull request body;
  an unattributable residual is behavior drift and the split is reverted.

#### Scenario: the corpus collects identically and the deferred items land

- **WHEN** the entropy audit suites are collected after partitioning
- **THEN** the sorted suffix set equals the pre-split baseline of 410
- **AND** no frozen pre-#1809 guard literal remains in either compatibility
  inventory, and the five expected commands name the gateway-reconcile glob.

### Requirement: Production ops runbook becomes an index over sub-runbooks

`docs/runbooks/current-production-ops.md` SHALL be split into
`docs/runbooks/production-ops/` sub-runbooks each below 1,000 lines, with the
historical path retained as an index landing page below 1,000 lines. Commands, jq
expressions and reproduction steps SHALL be preserved verbatim, and every existing
inbound reference and anchor SHALL still resolve.

#### Scenario: inbound links and anchors survive the split

- **WHEN** the references to `current-production-ops.md` are resolved after the split
- **THEN** every referencing file still resolves, the anchor
  `#311-pipeline-job-provenance-sidecar-and-recovery-2420` still resolves, and
  markdown lint passes.

### Requirement: Guard exclusion burndown batch 2 retires three exclusions and unfreezes two model-registry owners

The repository SHALL split `tests/test_node22_refresh_timer_health.py`,
`tests/test_direct_grid_display_cutover_flip.py`,
`workers/model_registry/basins_discovery.py`,
`workers/model_registry/qhh_production_bootstrap.py` and
`workers/model_registry/basins_registry_import.py` so that every resulting file is
below 1,000 lines, SHALL reduce `workers/model_registry/basins_package_source_io.py`
to at most 900 lines, and SHALL remove exactly the three corresponding exclusions
from `.large-file-guard.json` in the same commit as each split. No exclusion SHALL be
added, and `maxLines`, `enabled` and every other entry SHALL remain byte-identical.
Oversized non-excluded consumers of these modules SHALL remain unmodified.

#### Scenario: the guard ledger only shrinks

- **WHEN** `.large-file-guard.json` is compared against its pre-change content
- **THEN** exactly the three named entries are absent and no entry was added
- **AND** every file the splits produce is below 1,000 lines.

#### Scenario: a split would force an edit to a frozen consumer

- **WHEN** a candidate cut would require editing an oversized non-excluded consumer
- **THEN** that cut is rejected in favour of one whose facade keeps the consumer's
  imports and patch seams valid, instead of adding an exclusion.

### Requirement: Model-registry facade splits keep patch seams, write surfaces and lock order

When a model-registry module is split behind a facade, every caller on the call path
that an existing monkeypatch exercises SHALL stay in the patched module and resolve the
patched name through its module globals, and each such seam SHALL be proven live by a
mutation that breaks the real call site. `_backfill_output_segment_geometry`, `_lock_river_network_version`
and `_seed_output_segment_rows` SHALL stay in their current modules, the
river-segment write-surface scan constants SHALL be unchanged, and ADR 0009 member
disposition markers SHALL stay inside their member function bodies.

#### Scenario: a spy still intercepts the real import path

- **WHEN** `tests/basins_registry_import_helpers.py` spies one of the eleven patched
  `basins_registry_import` names and the import orchestration runs
- **THEN** the spy records the call, and deliberately breaking the real call site
  turns a committed spy-count assertion red or, for a spy no committed assertion
  discriminates, a split-time seam probe red.

#### Scenario: the write-surface scan still finds its single writers

- **WHEN** `tests/test_river_segment_write_surface_scan.py` runs after the split
- **THEN** the only `UPDATE core.river_segment` and `geometry_generation` write
  remain in `basins_registry_import.py` and the only upsert in
  `qhh_production_bootstrap.py`, with the scan constants unchanged.

### Requirement: Model registry store splits behind a stable facade without drift

The repository SHALL split `packages/common/model_registry.py` so that the facade and
every owner module are below 1,000 lines without adding any `.large-file-guard.json`
exclusion. `PsycopgModelRegistryStore` SHALL remain importable from
`packages.common.model_registry` and SHALL expose every attribute it exposed before,
with each method's source unchanged. Class-attribute and module-level monkeypatch
seams SHALL still reach the real call path, source-pin checks SHALL cover the facade
and every `model_registry_*` owner module, and `from_env`, `_transaction`,
`_PsycopgTransaction` and `_attribution_connect_kwargs` SHALL stay in the facade.

#### Scenario: a class-attribute patch still intercepts a mixin method

- **WHEN** a test patches `PsycopgModelRegistryStore._transaction` (or any other
  patched method) and exercises a store method that now lives in a mixin module
- **THEN** the patched attribute is the one the method calls, and breaking the real
  call site turns that test red.

#### Scenario: oversized consumers stay untouched

- **WHEN** the split lands
- **THEN** every oversized non-excluded consumer of the module has zero diff and
  still passes.

### Requirement: Orchestrator modules SHALL import cleanly as the first import, except an exact allowlist
For every module under `services/orchestrator` discovered from disk, importing it first in a fresh interpreter SHALL succeed. The only exceptions are the modules in the first-import guard's allowlist. The allowlist SHALL be exact: a listed module that imports cleanly fails the guard until it is removed. The scheduler and chain facades SHALL bind the same non-dunder names to the same objects whether they are imported first or after a split module.

#### Scenario: Split module imported first
- **WHEN** a fresh interpreter runs `import services.orchestrator.scheduler_config.path_modes`
- **THEN** the import succeeds, and `services.orchestrator.scheduler.ProductionSchedulerConfig.__module__ == "services.orchestrator.scheduler"`

#### Scenario: A new first-import cycle is caught
- **WHEN** a fixed split module's back-import of its facade is moved above a definition the facade imports from it
- **THEN** the first-import guard fails and names that module

### Requirement: The journal cluster map SHALL be a checked-in artifact before any extraction
`tests/fixtures/file_journal_cluster_map.json` SHALL assign every top-level definition of `services/orchestrator/file_orchestration_journal.py` to exactly one of `facade`, `context`, `read_attribution`, `primitives`, `replay`, `candidates`, `paths`, `retry_helpers`, and SHALL list, for every name that any test monkeypatches on the facade (found by searching `tests/` for `file_orchestration_journal.<name>` patch targets and for `setattr(<alias>, "<name>"`), its cluster and the clusters of all its call sites. A test SHALL fail when a top-level definition is unassigned, assigned twice, or when a patched name is assigned to a cluster other than `facade` while one of its call sites is assigned elsewhere without the late-lookup rule below.

#### Scenario: Map covers the file
- **WHEN** the map test runs at the commit that introduces the map
- **THEN** every top-level `def`/`class`/assignment name in the facade is assigned exactly once and every patched name is listed with its call-site clusters

### Requirement: Journal responsibility clusters SHALL be extracted behind a stable facade
`services/orchestrator/file_orchestration_journal.py` SHALL be reduced by moving, in the order `context` (`file_journal_context.py`), `read_attribution` (`file_journal_read_attribution.py`), `primitives` (`file_journal_primitives.py`), `replay` (`file_journal_replay.py`), `candidates` (`file_journal_candidates.py`), `paths` (`file_journal_paths.py`), `retry_helpers` (`file_journal_retry_helpers.py`) the definitions the map assigns to that cluster. After each step the facade SHALL re-export (listed in `__all__`, `# noqa: F401` where needed) every moved name that any non-test module **imports** and every moved name a test **imports**; a name whose only test references are monkeypatches SHALL NOT be re-exported (base requirement "Relocated monkeypatch seams keep their negative oracles live"). Every existing test file that references the journal module SHALL pass with no edits other than patch-target repointing listed for that step.

Patched seams follow two rules:
- A patched name moves only together with **all** its call sites; the tests' patch targets move to the new owner in the same PR and the facade does not re-export it.
- A patched name with a call site that stays in the facade (the `FileOrchestrationJournalRepository` and `FileJournalRetryService` classes stay; today this covers all eighteen patched names recorded in the cluster map's `patched` table, including `_utcnow`, `read_bytes_limited_no_follow`, `list_directory_no_follow_limited`, `_stat_signature`, `_iter_regular_json_files`, `_iter_jsonl_files`, `_released_candidate_cycle_scope`, `_cycle_scope_from_job_id`; `_next_file_manual_retry_job_id_for_run` left the table when #3157 deleted the manual-retry pending-row allocator) stays in the facade; a moved function that calls it SHALL resolve it at call time as an attribute of the facade module (late import inside the function body), never by binding the name at import time, so a patch on the facade still reaches every call.

#### Scenario: Extraction step keeps importers and patches working
- **WHEN** a cluster is moved
- **THEN** every test under `tests/` that imports or patches `services.orchestrator.file_orchestration_journal` passes, with test edits limited to the patch-target strings of names the map marks as moving with all their call sites

#### Scenario: Facade-resident seam still intercepts a moved caller
- **WHEN** a test patches `file_orchestration_journal._stat_signature` with a counting stub and exercises a function that moved to `file_journal_paths.py`
- **THEN** the stub's call count is greater than zero

#### Scenario: Paths-cluster walkers reach facade-resident IO names
- **WHEN** a test patches `file_orchestration_journal.stat_no_follow` and `file_orchestration_journal.list_directory_no_follow_limited` with counting stubs and iterates the moved `_iter_discovered_files` or `_iter_retention_surface_files` in `file_journal_paths.py`
- **THEN** each stub's call count is greater than zero

#### Scenario: Candidates-cluster callers of facade-resident patched names
- **WHEN** a test patches `file_orchestration_journal.read_bytes_limited_no_follow` or `file_orchestration_journal.accepted_submit_candidate_immutable_evidence` with a counting stub and calls the moved `_run_manifest_model_package_identity` or `_candidate_row_self_bound_identity` in `file_journal_candidates.py`
- **THEN** the stub's call count is greater than zero

#### Scenario: Retry-helper callers of the facade clock
- **WHEN** a test patches `file_orchestration_journal._utcnow` with a counting stub and calls the moved `_journal_record_for_write` in `file_journal_retry_helpers.py` (`_private_runtime_root_recovery_record` was deleted with its write-only lane by #3183)
- **THEN** the stub's call count is greater than zero and the record's `created_at` is the stub's value

#### Scenario: Annotation-only back references
- **WHEN** a moved module needs a facade-resident name (e.g. `FileOrchestrationJournalRepository`, `FileOrchestrationJournalError`) only in annotations
- **THEN** it imports it under `TYPE_CHECKING` and no import cycle is introduced

### Requirement: Journal contract SHALL remove dead re-exports
After the last extraction, the facade's `__all__` SHALL contain only names with a remaining importer, where "importer" means an AST-resolved `from … import` or attribute access through any alias of the facade module, or a dotted-string patch target, found by scanning `apps/ packages/ services/ workers/ scripts/ tests/`. A test SHALL fail if `__all__` lists a name no module uses by that definition, or if a module other than the facade and its `file_journal_*.py` siblings imports or reads through an alias a facade name missing from `__all__` (monkeypatch-only targets are never listed), and the facade's remaining top-level definitions SHALL equal the map's `facade` cluster.

#### Scenario: Unused re-export detected
- **WHEN** `__all__` lists `_foo` and no file imports, accesses or patches `file_orchestration_journal._foo`
- **THEN** the re-export hygiene test fails naming `_foo`

#### Scenario: Unlisted external read detected
- **WHEN** a non-sibling module imports `file_orchestration_journal._bar` and `__all__` does not list `_bar`
- **THEN** the re-export hygiene test fails naming `_bar`

### Requirement: Retention tests SHALL remain complete under physical partitioning

The repository SHALL partition the retention pytest corpus into four collectible
modules and at most one non-collectible local helper whose individual line counts
are below 1,000, without adding a guard exclusion or retaining a collectible
compatibility shim. Every pre-partition case SHALL be collected exactly once after
partitioning: the module prefix may change, but all 120 unique
`::test_name[param-id]` suffixes, test bodies, decorators, fixtures, parameter
values/IDs, monkeypatch targets, and assertion oracles MUST remain equivalent.
`tests/test_retention.py` SHALL remain the same-name suite for the production
retention owner.

#### Scenario: collection identity and oracles are preserved one-to-one

- **WHEN** the original monolith and all four post-partition files are collected
- **THEN** each side yields exactly 120 unique suffixes after the first `::`, their
  sorted suffix files are byte-identical, and every original test body/decorator
  fingerprint maps to exactly one post-partition definition
- **AND** running the four files passes all 120 cases without changed assertions,
  skips, duplicates, production-source modifications, or helper collection.

#### Scenario: the final exception is removed without replacement

- **WHEN** line counts and `.large-file-guard.json` are evaluated after partitioning
- **THEN** every collectible/helper file is below 1,000 lines,
  `tests/test_retention.py` is absent from exclusions, no replacement exclusion is
  added, and every unrelated exclusion and the threshold remain unchanged.

### Requirement: Basins package publication tests SHALL remain complete under physical partitioning

The repository SHALL retain every case from the frozen Basins package publication baseline
while extending the current corpus to exactly seven collectible modules and one
non-collectible helper, each below 1,000 lines, without changing
`.large-file-guard.json` or retaining a collectible compatibility shim. The six original
partitions SHALL still contain all 88 unique baseline `::test_name[param-id]` suffixes and
all 80 normalized baseline test bodies, decorators, fixture arguments, parameter values/IDs,
assertions, skips and monkeypatch targets exactly once. The seventh owner,
`tests/test_basins_package_publication_rivseg.py`, SHALL contain only additive issue #1903
mapping tests and SHALL import `tests.basins_package_helpers` at module scope.

#### Scenario: frozen collection identity and additive tests are both preserved

- **WHEN** the baseline monolith, the six baseline partitions, and the seven current suites
  are collected and fingerprinted
- **THEN** the six baseline owners yield exactly the frozen 88 unique suffixes and 80
  one-to-one normalized definitions without dropped, duplicated, renamed or weakened cases
- **AND** the seventh owner collects the finite issue #1903 scenario matrix exactly once
- **AND** executing all seven files preserves the baseline pass/skip semantics and adds real
  mapping assertions
- **AND** `tests/basins_package_helpers.py` collects no tests.

#### Scenario: structural guard and current validation commands match the extended layout

- **WHEN** the eight Python outputs, root/child current validation matrices, guard
  configuration and documentation authority are evaluated
- **THEN** every changed/new text source is strictly below 1,000 lines, the guard threshold
  and exclusion list are byte-identical, and an ordinary commit passes the hook
- **AND** the live M9 closeout, #148 regression and opt-in Basins smoke commands execute all
  seven suites, the moved real-smoke node still runs, and historical M9 result bullets plus
  archived evidence remain unchanged
- **AND** the structural diff changes no frozen baseline test definition, production
  database filter, or unrelated #1912 owner contract.

### Requirement: Copyback replay and retention mutex corpora SHALL partition without drift

The repository SHALL partition the scheduler state-index copyback replay suite and
the retention copyback-mutex corpus into collectible modules below 1,000 lines, with
shared definitions confined to non-collectible helpers and no collectible
compatibility shim. The retention copyback-mutex lane SHALL move to its own owner
module while `services/orchestrator/retention.py` retains the planner. Copyback
mutex semantics, budget semantics, and deletion selection SHALL be unchanged.

#### Scenario: collection identity survives partitioning

- **WHEN** the replay and mutex corpora are collected after partitioning
- **THEN** the sorted `::test_name[param-id]` suffix sets equal the pre-split
  baselines of 32 and 25 exactly, with no additions, losses, duplicates or skips
- **AND** assertions, decorators, fixtures and parameter IDs are unchanged.

### Requirement: Refresh-timer and cutover-flip corpora SHALL partition without drift

The repository SHALL partition the node-22 refresh-timer health suite into collectible
modules below 1,000 lines with shared definitions in non-collectible helpers.
The direct-grid display cutover-flip corpus that this requirement
also partitioned was deleted together with the station-flag flip hook by #3164, so its
partitions, its helper, its routing and its tracked-tree guard are retired with it.

#### Scenario: collection identity survives partitioning

- **WHEN** the refresh-timer corpus is collected after partitioning
- **THEN** the sorted `::test_name[param-id]` suffix set equals the pre-split
  baseline exactly, with no additions, losses, duplicates or skips.
