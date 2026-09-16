# Proposed selective-cold retirement and closeout delta

This replaces the withdrawn, never-promoted cold-enabling ADDED delta. Code is
still present; these are proposed target requirements, not current implemented
state. No canonical cold capability exists to remove. Final disposition must
preserve surviving-capability updates without publishing enabled cold storage;
see tasks.md R4.4. No archive is authorized by this revision.

## ADDED Requirements

### Requirement: Selective-cold retirement SHALL transfer genuine surviving consumers before deletion

R1 SHALL transfer only the command/container/evidence closure required by PGDATA
host/migrate and generic filesystem/Postgres/working-set sampling required by
resource governance to their existing domain owners. The image identity SHALL
come from the existing container contract/snapshot owner. Retained manual
workload/evidence producers, validators, schemas and tests SHALL migrate with
their consumers before old issue1895 exits disappear. No compatibility re-export
or wholesale renamed cold subsystem SHALL substitute for extraction.

#### Scenario: PGDATA relocation survives removal of cold modules

- **WHEN** retirement removes the former cold command/container/evidence owners
- **THEN** PGDATA preserves bounded process cleanup, descriptor/RAID/SMART refusal, clean-stop/copy verification, exact container preservation, pre-write rollback and no stale post-write rollback without importing retired families
- **AND** protective rejection of incompatible legacy cold binds/services remains a negative guard, not an enabled legacy path

#### Scenario: Ordinary governance and manual readers remain usable

- **WHEN** cold inventory/history/receipt branches and old C1-C3/G8 exits are removed
- **THEN** ordinary governance still observes `/data/GHDC` and attributes current PGDATA to its actual device with unknown/conflict refusal
- **AND** PGDATA manual instructions resolve to retained workload/evidence owners without a dangling cold rollout reference; independent C4 and canonical readonly validation remain available

#### Scenario: C4's production acceptance outlives the retired caller

- **WHEN** R3 removes the old G0/C3 publisher/binder
- **THEN** R1 has already delivered the existing display owner's Bringup-C4 production acceptance entrypoint and matching C4 delta, preserving reviewed-SHA freeze and exact-byte SHA-256/file-identity re-verification against the original binding record
- **AND** missing or mismatched SHA/bindings and changed C4 bytes/file identity refuse; local C4 CLI PASS alone is insufficient, and no SHA/digest fields are added to its closed schema/CLI

### Requirement: Ordinary compression SHALL have no cold launch dependency

R2 SHALL remove paired cold env/budget assembly and the cold launcher leg while
preserving safe configuration, execution and ordinary maintenance behavior.

#### Scenario: Compression launches with only its own safe configuration

- **WHEN** ordinary compression is launched without a cold env file
- **THEN** its real wrapper/CLI can run with one consistent statement/cleanup/wrapper/systemd budget and no cold entrypoint
- **AND** descriptor-bound mode-0600/no-symlink inert parsing, import-origin validation, bounded argv execution, secret-safe failure and lifecycle mutex remain enforced

#### Scenario: Invalid compression input cannot bypass safety

- **WHEN** compression config is malformed, unsafe, or its lifecycle lock is held
- **THEN** launch refuses safely without needing cold configuration and without changing ordinary retention windows or discovery/lag behavior

### Requirement: Cold-only implementations and rollout surfaces SHALL be removed rather than retained dormant

Destructive changes SHALL publish with their actual dependency closure. The
single-lane budget/preflight/unit cutover SHALL merge with cold runner/wrapper
and affected direct/transitive test/config/schema/selector removal, after only
the retained-consumer transfers needed by those paths. Other R3 closures may
publish separately after their own transfers; no merge may leave paired-API callers.
R3 SHALL remove the package/CLI/config/schema/example/test/CI and SQL
grant-audit families listed in tasks.md, including old G0-G8/manual callers and
the G7-only explicit-cycle recorder capability, #2227 test and selector edges.
Tests/docs SHALL accompany source changes. Migrations 000058/000059, current data,
regular maintenance, independent C4 and generic readonly SHALL remain intact.

#### Scenario: A disabled installer remains in the tree

- **WHEN** deployable cold install/move/rollout code or a compatibility alias remains despite disabled deployment
- **THEN** retirement is incomplete and #1895/#1891 cannot close

#### Scenario: Cold provisioning is removed without weakening other roles

- **WHEN** the cold CREATE grant and positive grant audit are removed from provisioning source
- **THEN** unrelated role/ownership/membership/trigger/default/security audits remain and no live REVOKE, DROP or data deletion is authorized

#### Scenario: A paired-budget consumer remains after the interface cutover

- **WHEN** a proposed intermediate merge removes the cold preflight/budget API while a cold executable, applicable test or selector still depends on it
- **THEN** that merge is refused; complete its actual retained-consumer transfers and R2/R3 caller closure rather than adding an alias, a broken intermediate or unrelated whole-group prerequisites

#### Scenario: A retired recorder still has a canonical or test consumer

- **WHEN** R3 deletes the G7-only `node27_issue1895_query` recorder
- **THEN** its `explicit-cycle-query-binding` requirements, dedicated #2227 test and selector edges retire in the same cutover, without changing the shipping forecast owner
- **AND** governance example/current-operations guidance and mixed lifecycle/Docker gate tests receive their explicit R3.7 dispositions; generic mutex and collection safety survive

### Requirement: Retirement SHALL replace active authority without rewriting completed history

R4 SHALL preserve completed ledger/evidence and withdraw outstanding old G0-G8
rollout rather than mark it executed. Fresh retirement-specific high-risk fixture
review SHALL precede implementation; independent review, finding verification,
Gap Sweep, survivor regression and exact-head CI SHALL precede merge. Historical
cold reviews SHALL NOT authorize retirement. Canonical survivor specs SHALL be
updated only with their matching implementation cutover.

#### Scenario: Historical fixture approval is offered for new deletion work

- **WHEN** a retirement implementation cites only old cold fixture approvals
- **THEN** it lacks retirement approval and must obtain fresh risk/invariant/consumer review

#### Scenario: Final archive would promote withdrawn cold additions

- **WHEN** closeout prepares archive or another final disposition
- **THEN** review preserves and validates surviving-spec updates and the G7-only REMOVED delta, including removal of its empty canonical capability, while excluding withdrawn cold-enabling additions and any newly enabled cold capability
- **AND** `--skip-specs`, if used, follows explicit application and validation of those updates/removals; `--no-validate` is forbidden and this document revision executes no archive

### Requirement: Closure SHALL require deletion proof and separately authorized effective-deployment handoff

R5 SHALL prove survivor behavior and zero active retired imports/entrypoints/
options/CI targets, with only justified protective guards and immutable history
exceptions. Required runtime oracles SHALL run on isolated node27 resources,
not inside production, alongside affected local checks and required backend
regression. Effective unit/dropins/env/ExecStartPre/reference disposition SHALL
have separate authorization and coordinate owners/foreign holds. No cold sample,
I9/I8/#2162/#2017 blanket dependency or new storage/RPO/RTO gate SHALL be added;
existing independent upgrade/recovery duties remain with their owners.

#### Scenario: Source retirement is complete but deployed references are unknown

- **WHEN** repository deletion passes but effective deployment has not been observed and disposition authorized
- **THEN** closure remains incomplete; source templates do not prove production state and normal maintenance must not be disabled

#### Scenario: Unexpected deployed cold state is found

- **WHEN** handoff discovers an unexpected cold relation, tablespace or other deployed cold state
- **THEN** work stops for dedicated safe disposition without silent DROP, ignored state, or deletion of data, old PGDATA or private evidence

#### Scenario: A tracked defect remains in extracted code

- **WHEN** R1 transfers an affected path of #2293, #2298 or #1938 to a surviving owner
- **THEN** that defect follows the new owner rather than being declared retired
- **AND** capability-retired disposition and #1895/#1891 closure occur only after affected source and deployed references are gone and deletion/regression/deployment evidence is linked
