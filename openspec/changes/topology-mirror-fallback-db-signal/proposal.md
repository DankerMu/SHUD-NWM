# Require a database signal before the topology fallback reports mirror drift

## Why

The `production-topology-node22-local-postgres` check exists to catch one thing:
an active surface that still treats the archived `:55433` rollback database as
current. Its bare fallback rule (`scripts/governance/audit_repo_entropy.py:1936`
on `origin/master`; every line number in this **Why** section is a master
coordinate, because this section describes the state being changed) is a
two-term conjunction, and the second term
(`_topology_line_mentions_mirror`, `:1971-1973`) is a plain substring test for
`"mirror"` / `"镜像"`. So the fallback fires on any line that names the compute
node and happens to contain that word — with no database signal required at all.

This repository has a second, older, entirely legitimate mirror concept:
`NHMS_SLURM_SCHEDULER_STATE_INDEX`, a local `/scratch` **file** mirror of the NFS
canonical state index (`services/orchestrator/retry.py:148`,
`services/orchestrator/chain_manifests.py:224`,
`services/slurm_gateway/real_backend.py:996`,
`services/orchestrator/chain_runtime_utils.py:1036`; introduced in `985b42c6`,
#874). It is not a database, is not archived, and is not a rollback surface, so
none of the fallback's archived/stopped-flavored exemptions
(`_topology_local_postgres_context_is_allowed`, master `:1989-1998`) can
truthfully apply to it.

Measured on master `ba783bd1`, with cwd inside the worktree under test — the
script roots at `Path.cwd()` (`audit_repo_entropy.py:955-960`), so the working
directory is part of the command:

```text
# check_id: production-topology-node22-local-postgres -> 4 findings
docs/runbooks/current-production-ops.md:1560
openspec/specs/production-scheduler-orchestration/spec.md:103
scripts/node22_clone_direct_grid_cutover_states.py:25
scripts/node22_clone_direct_grid_cutover_states.py:28
```

All four are false positives of the same substring collision, and two of them
say on the flagged line itself that the compute node holds no database at all
("DB-free"). The spec finding is flagged only because it uses "mirrored" as an
ordinary verb — "a fresh receipt's explicit null bound SHALL be mirrored
verbatim" — on a long line that separately names node-22 private storage.

`tests/test_entropy_audit_script.py:214::test_entropy_audit_current_repo_hard_gate_has_zero_production_topology_findings`
is permanently red because of this, so the gate's signal for this check is
already dead: a genuine future violation lands in a bucket that is expected to
be red anyway.

## What Changes

- Narrow **only** the bare fallback at `audit_repo_entropy.py:1936`
  (master coordinate; the rewritten `return` block is `:1936-1949` at HEAD). The
  two-term conjunction stops being sufficient; the line must additionally look
  like a rollback surface or carry a real database signal.
- Add two small line-scoped helpers used only by that fallback: one for rollback
  wording, and one that removes explicit "there is no database here" assertions
  before the database signal is measured.
- `_topology_line_mentions_mirror` is **not** touched — six call sites depend on
  its current meaning. At HEAD they are `:1934`, `:1939`, `:2020`, `:2311`,
  `:2609`, `:2613` (this change's insertion shifted five of the six; the count is
  what the constraint is about, and `grep -n _topology_line_mentions_mirror` is
  the durable way to recheck it).
- `_topology_mentions_database` is **not** touched either — three other callers
  (`:1725`, `:1758`, `:1844`; these sit above the insertion point and are
  unshifted) depend on it. The DB-absence handling is applied at
  the fallback's own call site.
- No exemption-token list grows; see design.md D2 for why the alternative was
  rejected.
- Tests pin both directions: the four real flagged lines as must-not-flag, and
  real drift wording as must-still-flag.

## Impact

- Affected specs: `entropy-automation` (one ADDED requirement)
- Affected code: `scripts/governance/audit_repo_entropy.py`,
  `tests/test_entropy_audit_script.py`
- **Explicitly not changed**: `docs/runbooks/current-production-ops.md`,
  `openspec/changes/archive/2026-08-22-recalibration-state-carryover/**`,
  `scripts/node22_clone_direct_grid_cutover_states.py`,
  `openspec/specs/production-scheduler-orchestration/spec.md`. Rewriting the
  evidence to please the checker is the failure mode this change exists to
  prevent.
