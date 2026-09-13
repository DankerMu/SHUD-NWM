## Context and status

Selective-cold storage is withdrawn in favor of mandatory code retirement under
issues #1891/#1895. This is proposed design, not implemented state. The repository still
contains the runtime and old rollout machinery. [tasks.md](tasks.md) is the sole
executable R1-R5 contract and concrete path/evidence map; this document explains
its ownership and safety decisions, not a second plan.

The original cold enablement work and pinned PostgreSQL 15.2 / TimescaleDB 2.10.2
probe were delivered historically. Completed task entries, `evidence/**`,
`fixtures/**` and `probe-1892-throwaway.md` remain historical records. Original
G0-G8 rollout and pending tasks 4.1-4.8 are withdrawn, not fulfilled. The first
G1 NO-GO does not imply later rollout occurred. Old fixture reviews and probe
PASS records do not authorize retirement or production operations.

## Goals / Non-Goals

Retire obsolete code end-to-end while preserving actual PGDATA relocation,
ordinary compression/retention, resource governance, independent display C4 and
generic readonly contracts. Retained behavior goes to its existing domain owner;
no dormant cold modules, compatibility re-exports, future-use stubs or whole-PR
reverts substitute for a clean cutover.

Do not install a cold tablespace, move cold groups, restore an archive lane,
change migrations 000058/000059 or current data, or create a new general storage
construction/RPO/RTO acceptance epic. Existing upgrade/recovery/capacity duties
stay with their owners. Cold samples, I9, I8, #2162 and #2017 are not blanket
retirement prerequisites, while effective deployment must respect actual foreign
holds and owner authorization.

## R1 — Transfer minimal surviving consumers to their actual owners

PGDATA host/migrate imports are the extraction boundary. The retained closure is
bounded command execution (including child termination, pipes and output limits),
container snapshot normalization/serialization, and descriptor/root RAID/SMART
evidence policy. Use PGDATA-owned command/container/evidence modules and proper
PGDATA errors, not an alias to `ColdRuntimeError`. Resolve the existing image pin
through `node27_container_contract.py` / `node27_external_contract_snapshot.json`;
never copy the cold pin. Do not inflate the existing host/migrate modules or carry
cold bind/recreate/rollback/installer/backup-inventory machinery without consumers.

The migration state machine, owned persistent fences, clean stop, complete copy,
exact deployment rebind, pre-write rollback and monotonic no-stale-post-write
boundary remain unchanged. Protective rejection of incompatible legacy cold
binds/services survives as a negative safety check, not an enabled legacy path.

Ordinary resource governance owns generic filesystem/Postgres/working-set
sampling and formatting/process helpers. Retain `/data/GHDC`, current PGDATA's
actual device/available-space attribution and unknown/conflict refusal; remove
cold SQL inventory, optional receipt/topology/history branches and cold output/
CLI options. Preserve #2273 capacity and #1985 discovery/lag behavior via real
consumer tests, not whole cold modules.

Runbook/manual CLI invocations count as consumers. Before deleting old C1/C2/C3/
performance exits, repoint PGDATA workload/evidence instructions to their actual
PGDATA/display/readonly owners, transferring any genuinely retained producer,
validator, schema and tests together. Do not retain all issue1895 wrappers merely
to preserve names or leave a dangling “see C1-C4 below”. Independent C4's local
producer/binder does not import C3, but its canonical production-acceptance
contract still requires the outer reviewed-SHA/exact-byte check currently owned
by G0/C3. R1.6 transfers that guarantee to the existing display deployment owner's
**Bringup-C4 production acceptance** seam, with a real entrypoint and original
binding record before the old owner can be deleted. Approved delivery/C1 evidence
freezes reviewed SHA before execution; final acceptance rechecks SHA and exact
C4 bytes/file identity against the initial record, never a recomputed expectation.
Missing/mismatched/swapped bindings refuse. Keep the five local C4 inputs,
bracket, private-file rules and closed schema/CLI; do not add SHA/digest fields
there or revive the broader retired C3 checks. The C4 MODIFIED delta and callers
move with the source transfer. The canonical readonly facade and
`validate_readonly_db_boundary.py` survive the old C2 acceptor.

## R2 — Detach normal compression while preserving safety

The canonical compression unit currently couples two env files and two sequential
ExecStart legs; budget preflight reads both lanes before ordinary compression.
Replace that dependency with one compression-owned budget and launcher, migrate
all receipt/config callers, and delete cold arguments, fields, mirrors and
compatibility aliases. Statement plus cleanup must fit wrapper, which must fit
systemd; derive the full relationship rather than restoring obsolete constants.

Preserve held-descriptor mode-0600/no-symlink inert env parsing, import-origin
checks, argv execution without shell interpretation, finite timeouts/cleanup,
secret-safe refusal and the fixed lifecycle mutex before lane-local/DB locks.
Ordinary compression selection, retention windows, discovery and maintenance
scheduling safety do not change. Real wrapper/CLI success without cold env and
unsafe-config/lock/timeout refusal are public verification seams. Repository
unit templates prove source intent only, never effective production state.

## R3 — Delete cold-only runtime and old G0-G8 delivery surfaces

R1/R2 preparation can proceed concurrently. Publish destructive changes with
their actual dependency closure: the single-lane budget/preflight/unit/compression
wrapper merges with cold runner/wrapper removal and all affected direct/transitive
test/config/schema/selector edges. Complete only the R1 transfers needed by those
removed paths first. Other R3 closures may merge separately once their own retained
consumers are migrated; G0/C3 removal specifically waits for outer C4 acceptance.
Do not introduce compatibility aliases, broken intermediates or unrelated
whole-group prerequisites. Shared selector/contract edits have one integration
owner; concrete publication slices and the entry review gate are
in tasks.md. The candidate-family census is not wildcard deletion permission.

Remove cold-only CLIs/wrappers/env, installer/probe/governance and old rollout
acceptors, schemas/synthetic examples/tests/fakes/mutants, selector ownership and
SQL `nhms_cold` CREATE-grant/positive-audit branch. Preserve unrelated role flags,
ownership/membership/default/trigger audits. This changes provisioning source;
it authorizes no live REVOKE, DROP, tablespace/data/old-PGDATA deletion.

Tests/docs accompany every source slice. Move tests defending surviving behavior
to their owners and retire incidental source-text/default-count pins. Close
shared fixtures/conftest/import/generated/non-Python consumers, not filenames
alone. C4 and generic readonly retain independent tests and authority.

The `explicit-cycle-query-binding` capability belongs to the retired #1895/G7
recorder, including synthetic positional captures and four-lane evidence. Its
REMOVED delta, `test_issue2227_explicit_cycle_named_binding.py` and selector edges
leave with that recorder; a test dependency is not a reason to promote dead code.
R1.4 first transfers the minimum capture, canonical parameter validation, native
EXPLAIN binding and deterministic query identity needed by the retained PGDATA
manual workload, with real entrypoint and behavioral proof. The matching PGDATA
ADDED requirement preserves that guarantee; shipping SQL alone is not a recorder.
Only the synthetic positional/four-lane G7 protocol and obsolete owner retire.
The shipping forecast owner and native named query behavior are unchanged.
R3's mixed-surface matrix additionally owns the governance env example, current
production role guidance, cold branch of the shared lifecycle-lock test, and
relocation of generic Docker collection-gate behavior before deleting cold AST
tests. The fixed runtime mutex and display-cache cold-waterfall diagnostic stay;
“cold” in a name is not a storage ownership criterion.

## R4 — Correct active authority and preserve history

The proposed MODIFIED deltas copy full affected canonical requirement blocks and
preserve unrelated scenarios. They remove only retired installer/role/CI demands;
compression's new safe single-lane contract is an ADDED requirement in its real
surviving capability. Canonical specs are not edited as though source retirement
already shipped. Matching source/spec/test/doc cutover is required at implementation.

The former cold-only ADDED delta was never promoted to a canonical cold
capability. Its replacement contains retirement/closeout requirements only; do
not invent REMOVED entries for nonexistent cold requirements. In contrast,
the G7-only `explicit-cycle-query-binding` capability does exist and its three
requirements are explicitly removed with R3; no stale canonical G7 Purpose stays.
Completed
ledger remains non-normative history. Outstanding old rollout is explicitly
withdrawn and active runbooks/issues must point to surviving owners and R1-R5.

Fresh high-risk retirement fixture review must precede implementation. Select
CLI/config, file/process/permissions/secrets, state/recovery, SQL privileges,
TimescaleDB lifecycle, CI/consumer closure, manual documentation and deployment
risk packs. Record an invariant-to-public-seam matrix for each task group, obtain
independent review/finding verification/Gap Sweep and exact-head CI. Historical
cold fixture approvals cannot satisfy these gates.

At final archive, first review how surviving MODIFIED/ADDED updates and the
G7-only REMOVED delta are applied and validated; then disposition this change
without promoting withdrawn cold
additions or creating enabled cold capability. `openspec archive --skip-specs`
is the supported skip-promotion mechanism after survivor updates are explicitly
preserved. It is not permission to omit those updates; `--no-validate` is never
acceptable. No archive or canonical promotion occurs in this document revision.

## R5 — Verify survivors, authorize deployment handoff, then close

Deletion proof requires zero active imports/entrypoints/options/CI targets for
retired families, with only justified negative safety guards and immutable
history exceptions. Exercise actual retained PGDATA command/container/evidence,
compression/retention/locking, governance binding, provisioning and touched
display contracts. Run affected local Ruff/OpenSpec/frontend gates and node27
targeted plus required full/backend and isolated PG15.2/Timescale2.10.2 regression.
Use owned TMPDIR/resources; never substitute a database inside production for an
isolated oracle. This revision runs none of these implementation gates and makes
no PASS claim.

Effective-deployment handoff is a separate approval boundary. At the release,
observe actual unit/dropins/env/ExecStartPre and referenced paths, coordinate
owners/foreign holds, and retire cold activation/config without disabling normal
maintenance. Preserve data/private evidence and stop for a dedicated safe
disposition if unexpected deployed cold relations/state exist. Source absence
alone cannot prove deployment retirement.

Issues #2293/#2298/#1938 are retired, not fixed, only once their affected code and
deployed references are gone. If extraction carries a defect, responsibility
follows its new owner. Close #1895/#1891 only with completed R1-R5 deletion,
regression, reviewed authority and authorized deployment evidence. No optional
dormant backlog or unrelated storage project replaces that closure.
