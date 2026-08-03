# Converge the three pre-existing MEASURED records in node27_container_contract to claim-determining citations (#1273)

## Why

`packages/common/node27_container_contract.py` is the lane's
cross-plane contract single-source; both planes import it by name and
its comments are the only record a future reader has for "why is this
literal this value". PR #1271 registered the invariant (now at
`openspec/specs/hypertable-compression/spec.md`, the container-dump
prefix requirement): a record about the live environment may state
only what its cited command actually determines. #1271 applied it to
its own new `CONTAINER_DB_MOUNT_PREFIX` record and explicitly
report-not-fixed the three pre-existing `MEASURED` records at
`:43-73`, each of which oversteps its citation (read-only re-review
2026-08-02, recorded in issue #1273):

1. `CONTAINER_PG_RESTORE_REALPATH` (:43-49): `readlink -f` resolves a
   path and cannot determine what a child process execs, yet the
   comment claims "the stable entrypoint the child actually invokes";
   the image-tag assertion carries no citation at all; and the
   `Source:` line points at
   `.workplans/1069/review/round-5/node27-external-contract-gate.md`,
   which resolves nowhere — `.workplans/` is gitignored
   (`.gitignore:48`), `git ls-tree master .workplans/` is empty,
   `git log --all -- '.workplans/1069/**'` is empty. It is the only
   `.workplans/` reference in tracked production code.
2. `SYSTEMD_UNSET_TIMESTAMP` (:51-59): cites "tonight's live arming
   attempt" (an event, not a command, unlocatable after 2026-07-16)
   yet asserts the general "for a unit that has never started in the
   current boot"; the "systemd 249, Ubuntu 22.04" clause has no
   citation; and the narrative "the inactive recurring compression
   unit therefore reports ExecMainStartTimestamp=n/a" is FALSIFIED by
   the repo's own live snapshot
   (`packages/common/node27_external_contract_snapshot.json`
   `informational.recurring_unit`: `ActiveState=inactive` with
   `ExecMainStartTimestamp="Sun 2026-08-02 12:25:00 CST"`). The
   runtime consequence is separately tracked as #1255; this change
   touches only the record.
3. `CLIENT_BACKEND_TYPE` (:61-73): a single snapshot (launch 7
   postflight, 2026-07-17) is cited for an enumeration the snapshot
   need not cover and for the universal PostgreSQL-semantics claim
   "a parallel worker ... is always accompanied by its leader client
   backend" — one snapshot cannot determine "always"; the repo's
   2026-08-02 measured distribution does not even contain
   `parallel worker`.

The risk is record-integrity, not runtime: the client-backend
conflict-eligibility ruling (delivered as the two-conjunct predicate
with `has_write_privilege_on_target`) is a trust-boundary core
ruling, and if its recorded rationale cannot be re-derived, a
future PG/TimescaleDB upgrade leaves no way to decide whether to
tighten or relax it except trusting an uncited "always".

## What Changes

The issue's recommended route, adopted in full — converge every
assertion to what an in-repo citation determines, delete what nothing
determines (this run's standing repair vocabulary: deletion or copying
a proven carrier, never authoring replacement claims), zero runtime:

1. **`CONTAINER_PG_RESTORE_REALPATH`**: image tag cited to the
   snapshot fixture's `host_context.nhms_db_image_ref`
   (`docker inspect '--format={{.Config.Image}}|{{.Image}}' nhms-db`,
   2026-08-02); realpath cited to `contract.container_pg_restore_realpath`
   (`docker exec nhms-db /usr/bin/readlink -f /usr/bin/pg_restore`);
   the "stable entrypoint the child actually invokes" exec claim is
   DELETED (readlink cannot determine it; a determining command would
   cost more than the claim is worth); "NOT `/usr/bin/pg_restore`
   itself" stays (the cited command's output differing from its input
   path determines exactly that). The dangling `.workplans/1069`
   `Source:` is replaced by the snapshot-fixture reference.
2. **`SYSTEMD_UNSET_TIMESTAMP`**: the general never-started-in-boot
   claim is narrowed to what the snapshot's witness command determines
   (`systemctl --user show
   nhms-external-contract-snapshot-witness-does-not-exist.service
   -p LoadState -p ExecMainStartTimestamp` renders the unset property
   of a nonexistent unit as the literal `n/a`); the falsified
   recurring-unit narrative is REPLACED by the snapshot's measured
   fact (inactive unit CAN carry a real `ExecMainStartTimestamp`,
   cross-referencing #1255 for the runtime consequence); the systemd
   version is cited to `host_context.systemd_version`
   (`systemctl --version` → `systemd 249 (249.11-0ubuntu3.21)`); the
   uncited "Ubuntu 22.04" clause is DELETED (the frozen snapshot
   `PROBES` table has no os-release probe; adding one is out of
   scope).
3. **`CLIENT_BACKEND_TYPE`**: the value's citation becomes
   `contract.client_backend_type` (`psql ... SELECT backend_type FROM
   pg_stat_activity WHERE pid = pg_backend_pid()` → `client backend`);
   the speculative enumeration is converged to the measured
   2026-08-02 `informational.backend_type_distribution` set (which
   contains no `parallel worker`); the universal "always accompanied
   by its leader" semantics claim is DOWNGRADED to this plane's design
   ruling stated with the DELIVERED two-conjunct predicate
   (client-backend eligibility AND `has_write_privilege_on_target`,
   the G14 narrowing — the bare "client backends only" wording is
   contradicted by the committed predicates; cross-review F1) with
   no universality assertion; the autovacuum anecdote (launch 7
   postflight, 2026-07-17) has no repo-resolvable artifact and is kept
   only as an explicitly marked unverifiable field anecdote (the
   lane's proven carrier wording, copied from the prearm-reset
   archive), its "deterministically" dropped.

Constant VALUES are untouched (they are locked per-constant by
`scripts/node27_external_contract_snapshot.py` `CONTRACT_CONSTANTS`
against the snapshot fixture; changing a value is a drift-process
matter, out of scope). The module's code is untouched — the delivered
module must be AST-identical to master's (`#` comments never enter
the AST; docstrings DO, so the module docstring stays byte-unchanged
too; the equality is machine-checked at evidence time).

Explicitly not adopted: the issue's fallback (delete all rationale,
keep bare values) — it closes fastest but discards the "why only
client backends" design reasoning the next reader needs.

## Impact

- Affected code (comments only):
  `packages/common/node27_container_contract.py` `:43-73`. The final
  file set is checked against `git diff master...HEAD --name-only` at
  evidence time.
- Frozen surfaces (zero diff): everything else — in particular
  `packages/common/node27_external_contract_snapshot.json`,
  `scripts/node27_external_contract_snapshot.py`, both planes'
  supervisor/verifier modules and tests, and the `:163-239`
  `CONTAINER_DB_MOUNT_PREFIX` / `container_dump_path_within_mount`
  records (#1271's compliant exemplars, the writing template) —
  byte-unchanged text; their line numbers may shift, which no repo
  file depends on.
- Affected specs: `hypertable-compression` (1 ADDED requirement:
  contract-module measured records cite repo-resolvable,
  claim-determining sources).
