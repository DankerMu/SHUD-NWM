## Context

The source baseline is the four f24 I8 execution artifacts, approved after five comprehensive rounds and an independent
final review. They are versioned under the parent change's `receipts/issue-1987-execution-tools/` on commit
`f24c3fb37be392300052c0d0ebf0ee35366a4599`.

The f24 branch and target are diverged (GitHub compare: merge base `2e3e8241d4a8f43567b8ac4c0c8b1ea229c1f143`, ahead 98,
behind 12). Absence on target is not evidence of retirement. No deletion was found in available history, and no matching
successor execution contract was found in target scripts/services/OpenSpec.

Adopt only the required four artifacts as this change's
`tools/{window_execute,window_smoke,governance_stage,governance_smoke}.py`. Preserve the original f24 receipts and
record each original SHA-256 plus the semantic patch separately. Do not import the already-applied PGDATA environment
patch as a new change.

Live OLD remains `a8db554d6402bec642e9a05627eae64b2b79aec3`. The healthy governance pin uses
`/home/nwm/NWM-governance-reviewed-1a32ebb7`; its owned state is
`/home/nwm/.local/state/issue1987-governance-f24c3fb37`. Neither is replaced during this change's qualification.

The final target is `415cbd1e9d0eee39ba0dfb623a586b02cbb340f2`, captured after the initial `4df9bae14` comparison. The
intervening changes leave window worker, API, dependency and installed node-27 unit surfaces unchanged; ordinary
governance transfers to `node27_resource_governance_collection`. Later master movements are separate decisions.

## Goals / Non-Goals

Goals: preserve legitimate migration history; admit exactly one pending expand; qualify the new immutable runtime; hand
off the existing owned governance pin without changing its persisted ownership identity.

Non-goals: new migration SQL, a general deployment framework, new production CLI flags, manual ledger/state repair, node-22
operations, shared permissions, retention execution, cold activation, PGDATA relocation, unit installation, foreign
HOLD/YD changes, or completion claims for parent task 5.2.

## Authority and lineage

The user explicitly selected a new deployment-admission change after the f24 review sequence. This authorizes local
implementation and isolated/read-only node-27 qualification, conditional on preserving history, only executing 000059,
proving governance handoff and rollback before opening a window.

The original five-round ledger is retained, not reset. Two new, explicitly bounded child PRs precede #1987's remaining
production window and evidence work. The normal merge gate still applies. No sixth ordinary review is disguised as a
child of identical scope.

Old preparation directories and their admission/driver hashes are superseded, never upgraded in place. Inputs and
execution state are separate directories; `prepare` receives a nonexistent state directory. Governance state is
different: its existing ownership record and argument identity must be reused unchanged for post-cutover unstage.

## Decisions

### D1: Exact pending set, complete immutable history

`pending = sorted(current_sql_filenames - applied_versions)` must equal exactly
`[000059_river_timeseries_narrow_expand.sql]` in both prepare and the migration worker. Do not require applied versions
to be a subset of current filenames.

The migration owner iterates current files and skips already-applied filenames (`packages/common/migrate.py`). Seven
historical SQL files were intentionally removed by `b97c16e28b1b4ddd7a55c93d7b9682f764c5d8a3`. GitHub compare confirms
this commit is an ancestor; local ancestry negatives are unreliable in the shallow checkout.

Let the original ledger be L. Successful expand produces L plus 000059; D12 reverses catalog names but MUST retain that
complete post-expand ledger, including 000059. Failure before migration commits leaves L. Recovery preserves whichever
admitted ledger it observes; no historical row or 000059 row is deleted or re-applied.

The disposable oracle seeds these observed retired versions into `public.schema_migrations`, without creating SQL files:

- `000007_flood.sql`
- `000015_flood_return_period_identity_indexes.sql`
- `000017_return_period_max_over_window_identity.sql`
- `000020_valid_time_discovery_indexes.sql`
- `000031_search_discovery_return_period_performance.sql`
- `000034_return_period_run_quality_materialization.sql`
- `000036_run_product_quality_explicit_source.sql`

Both actual prepare admission and the actual migration-worker action must read this real catalog. The old worker-only
happy path is insufficient. For each site, historical-only differences plus pending 000059 accept; zero/extra pending
refuse before DDL. Compare all original ledger rows through expand/D12. Do not repeat the production query to reconfirm
the captured defect.

### D2: Two real merge boundaries

Child 1 changes only migration admission and its existing window oracle. It retains the old runtime pins and is
independently mergeable; it does not approve activation of that old target.

Child 2 depends on Child 1. Window target binding and governance post-cutover target binding ship together: releasing
only one side would admit a target for which owned pin removal is forbidden. Runtime qualification is part of this same
handoff, not a third feature.

### D3: Retain the healthy pin through cutover

Do not attempt to unstage before cutover: the old tool requires active HEAD to equal its old NEW, and active HEAD is
still OLD. Do not restage, manually remove the pin, or rewrite `state.identity`/`pin_digest`.

The governance constructor identity is exactly its existing argument dictionary, excluding operation; target constants
are not part of that dictionary. A new target-bound tool must preserve every argument name, type, default, value and the
pin template, while checking the new active target at unstage.

After #1987 validates cutover, unstage is the parent's first handoff action, before performance evidence. It reuses old
state/arguments, verifies ownership, stops the timer, removes only the owned pin, reloads, audits the ordinary new
runtime, then restores original timer activity. An audit failure follows existing durable recovery; a possible old-pin
audit tick before handoff is read-only.

The handoff oracle must load the original hash-verified f24 tool to produce staged state, then load the new tool with
identical ownership arguments. It must distinguish OLD active source, retained 1a32 runtime and new 415 active source by
queried repository path. Git-object and import-probe fakes cannot return one NEW/OK value for every query.

Exercise pre-cutover refusal, active runner, foreign pin/environment/unit drift, interruption after pin removal, and
post-removal audit failure/recovery. The original same-tool stage/unstage smoke does not prove this transition. Actual
production unstage stays a parent post-window action.

### D4: Exhaustive pin and interface qualification

Check all target literals, including the governance embedded import-origin probe; OLD, container identity and PGDATA
identity stay unchanged. Recreate the target checkout, fixed restore branch/ref, admission hashes and fresh preparation
with the final target. Never substitute moving `master` at execution time.

The following consumed surfaces are individually accounted for:

| Surface                   | Delta and required proof                                                                                                                       |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| worker imports            | Final-target imported modules must match frozen git bytes; dependency manifests and Python pin are unchanged.                                  |
| preparse                  | Parser/config/repository bodies unchanged; re-run the bounded real-sample read-only path with final-target code.                               |
| parse                     | Parser write contract unchanged; preserve actual rows, route and provenance in the isolated matrix.                                            |
| narrow reader             | Store constructor/forecast-series body unchanged; re-prove the exact opaque small-run selector with 168 points on retained isolated real data. |
| legacy reader             | OLD stays a8db; complete legacy response digest must survive expand and rollback.                                                              |
| migrate worker            | `apply_migration`/splitter/path contract unchanged; the changed migrator `main()` is not invoked. Keep worker lock/statement/outage bounds.    |
| cycles and national tiles | Hydro display routes unchanged; retain real uncached four-route proof as a parent live gate.                                                   |
| forecast selector guards  | Hindcast-role handling changed, but configured real mismatch 404 and unsupported-variable 422 guards remain applicable.                        |
| unit/process drain        | Installed node-27 units/wrappers and drain rules unchanged; do not install new node-22 unit files from the repo.                               |
| governance audit          | New maintenance-freshness recommendations and the ordinary collection owner are relevant; run final-target read-only audit before admission.   |
| governance unstage        | Prove old staged-state identity plus new active target, old pin ownership, fresh audit, interruption recovery and foreign-drift refusal.       |
| downstream C4 owner       | Changed production C4 acceptance code belongs to parent post-window evidence, not a child implementation change.                               |

A new `AUTOVACUUM_OUTPUT_STALLED` or any other critical recommendation is a genuine blocker, never filtered to obtain
PASS. Removed cold-only options are not reintroduced, and actual environment files are not edited to suppress findings.

### D5: Tool delivery, launch and archive lifetime

Deployment SHA, tool-delivery SHA and input/driver hashes are distinct. After the reviewed tool commit is pushed, fetch
it on node-27 without switching the active tree. Set `TOOL_SHA` to that exact commit and `BUNDLE` to a new private
directory under `/home/nwm/.local/state/issue1987-tools/`.

```bash
umask 077
mkdir "$BUNDLE"
git archive "$TOOL_SHA" openspec/changes/refresh-node27-window-admission/tools |
  tar -x --strip-components=4 -C "$BUNDLE"
sha256sum "$BUNDLE"/*.py
```

Compare all hashes with the reviewed receipt before execution. The guarded disposable launcher supplies private DSN,
oracle, repo/container and fresh state inputs to the published `window_smoke.py`; it never targets production. Child 2
launches published `governance_smoke.py`, including the new original-state handoff cases, before actual read-only
qualification/prepare.

Backend CI may start for these Python files but the test selector can be collect-only; pytest does not collect the
operational smoke files under OpenSpec. Required evidence is targeted local Ruff plus node-27 oracles, not CI green. Do
not add mirrored tests merely to trigger CI.

Keep this shared change active through both children and parent window/handoff. Launch only the copied, hash-bound
private bundle, never a moving checkout path. Archive only after parent use ends; immutable Git provenance and copied
bundles remain available without rewriting historical receipts.

## Invariant Matrix

Governing invariant: admit only the explicitly frozen, recoverable operation; preserve historical data and ownership
before any operational mutation.

Source-of-truth identity: current SQL filenames, complete applied ledger L, catalog OIDs, immutable runtime/tool hashes
and staged governance argument/pin identity.

- Producers: migration owner applies 000059; original f24 governance tool produces staged state.
- Validators/entrypoints: actual prepare, migration worker, window/recover and governance unstage/recover.
- Storage/read/write: real disposable `schema_migrations`, catalog and facts; private state/input/pin files; production
  reads only during child qualification.
- Downstream: legacy/narrow readers, ordinary governance runtime and parent #1987/#2280 evidence.
- Failure/stale state: exact pending refusals, phase-dependent ledger retention, D12 rename-only reversal, owned
  interruption recovery and foreign-drift refusal.
- Evidence: separate source/input hashes, original-state handoff oracle, node-27 receipts and explicit CI/production
  limits.

| Boundary                              | Regression / evidence                                                                                                                         |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| historical ledger -> prepare          | Real catalog contains the seven named versions; only pending 000059 accepts. Zero/extra pending refuse before DDL.                            |
| historical ledger -> migration worker | Same real catalog and three pending-set cases; f24 red and changed worker green.                                                              |
| expand -> D12                         | Expand changes L to L plus 000059; D12 retains that ledger and narrow provenance. Pre-commit failure retains L. No ledger DELETE.             |
| prepare persistence -> recovery       | Existing actual late-prepare disk-reload refusal and admitted/nested emergency cases remain valid.                                            |
| target -> runtime/imports             | Stale SHA/ref/hash refuses; every worker runs the declared frozen code.                                                                       |
| old staged pin -> new active source   | Original f24-created state, retained 1a32 root and independently changed active root; identical arguments/pin bytes, no identity rewrite.     |
| interrupted unstage -> recovery       | Pre-cutover, active-runner and foreign-drift refusals; recover owned post-removal interruption/audit failure, audit before timer restoration. |
| new governance audit -> admission     | Read-only live audit, real device binding and no criticals; critical output cannot be downgraded.                                             |
| child readiness -> production         | Child evidence does not claim T0, production migration, successful live pin removal or #2280 closure.                                         |

## Risk triage and packs

Fixture level: expanded for both children. Repair intensity: high. Profile: `openspec/project-profile.md`, unchanged
because migration and deployment boundaries are already covered.

Selected core packs: public CLI, config/setup, file IO/ownership, schema/field identity, permissions/secrets,
shared-state ordering, legacy compatibility, error/rollback, release/dependency compatibility, documentation. D1-D4 and
the invariant matrix give their observable oracles.

Resource limits/discovery: not selected for a new algorithm; existing bounded discovery, worker timeouts and
private-output guards are preserved and exercised by the retained matrix.

Selected domain packs: PostgreSQL/Timescale behavior, manifest/provenance and published artifact identity.
Geospatial/CRS, hydro-met temporal semantics, SHUD numerics/conservation/threading, Slurm lifecycle and
external-provider behavior are unchanged non-goals; no new scientific or node-22 behavior is introduced.

## Migration and verification sequence

1. Deliver Child 1 with f24-red/changed-green at both actual admission sites using the captured live historical ledger
   input in the isolated oracle. No repeated production query is needed.
2. Deliver Child 2 with target-bound oracles, the old-pin/new-active handoff scenario, final-target read-only governance
   audit and exact small-selector proof. Then run actual fresh prepare without T0.
3. After the child merge gate, parent #1987 refreshes admission evidence and opens its separately controlled window. No
   validation code runs against production as a substitute for an isolated mutation oracle.
4. Parent completes owned governance handoff immediately after validated cutover, then #2280 and task 5.2 evidence. Any
   later failure or delay requires inspecting durable state, not blindly repeating window/recover.

## Risks / Trade-offs

- Live inputs may drift while code is reviewed -> fresh state, current hashes and fail-closed guards; do not overwrite
  the earlier failed preparations.
- Fake external boundaries can miss live shapes -> retain real PostgreSQL/parser/reader evidence and require actual
  read-only audit plus fresh prepare.
- New master changes again -> retain this explicit snapshot; no automatic rebase of the deployment target.
- Node-22's undeployed producer can create old-mode gap cycles -> no node-22 action here; if they age into retention
  before the window, follow the separate runbook authority and requalify admission.

## Sketch seams under test

Existing prepare and migration-worker entrypoints, complete ledger transitions, real late-prepare persistence cut,
runtime import provenance, governance constructor identity, owned pin removal, service audit receipt and interruption
recovery. No new generic abstraction or production CLI surface is required; oracle-only inputs may identify original tool bytes.

## Not yet specified

None for either child implementation. Actual maintenance-window timing, post-cutover runtime receipts and parent
performance/C4 evidence remain parent execution work, not unimplemented child features.

## D6. Stable systemd configuration (#2370)

Expanded fixture; selected risks: persisted state compatibility, immutable configuration, systemd integration and
evidence. Not selected: migration SQL, application behavior, permissions, governance handoff changes; those bytes stay
unchanged. Minimal slice: window snapshot/comparison and its focused oracle together.

Use the established GetUnit object-path resolution with typed busctl JSON. Service ExecStart has signature
`a(sasbttttuii)`: compare each ordered command's executable path, complete argv and ignore_errors; discard only the
runtime timestamp/PID/result tail. Timer TimersCalendar has signature `a(sst)`: compare each base/expression pair;
discard only next-elapse. Validate signature, row shapes and types; malformed/unsupported data refuses. Multiple
commands/calendar entries are valid when represented unambiguously and completely; never compare only the first.

Snapshot stable semantics from typed properties, not regex stripping the human-readable show output. Keep all other
CONFIG_PROPS and protected-file hashes enforced. Service-only properties are never fetched on timers or vice versa.
Prefer retaining the current state field shape with canonical stable strings, plus an explicit snapshot-format marker
if needed. Existing failed/PREPARED state is not migrated or rewritten: refuse old-format state before any window
mutation and require a new prepare. Existing driver/config/source/ledger/ownership guards remain independent.

Invariant matrix: volatile rerun/deadline changes -> same configuration and admitted; path/argv/ignore_errors/calendar
changes -> UNIT_CONFIG_CHANGED; env/file/path/timeout changes -> original refusal; typed malformed -> fail closed;
old snapshot -> explicit new-state-required refusal and no writes to it; new fresh prepare -> pre-T0 checks intact.

Evidence uses actual node27 typed payloads and recorded before/current drift. Focused smoke must fail on original
raw-string comparison and pass after repair, exercise real comparison/window admission in isolation, and preserve
governance sibling source hashes. Main performs a read-only live semantic comparison and fresh production prepare;
child never performs T0. Parent uses the new immutable published executor only after child merge.
