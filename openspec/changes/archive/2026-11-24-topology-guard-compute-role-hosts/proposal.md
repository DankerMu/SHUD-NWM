# Topology guard: recognise the compute role by its hosts

## Why

The compute role moved from the host node-22 to two hosts, w3 (control) and w2, on 2026-11-23 (#3345). The spec now says that the two hosts inherit every compute-role boundary: no current doc presents them as an NHMS database host.

The Production Topology Hard Gate does not enforce that. Its database-writer and local-PostgreSQL checks recognise the compute role only by the codename literal. Issue #3358 measured four sentences on master (a writer claim, a primary-database claim, an instruction to use a local PostgreSQL, and a Chinese primary-database claim): written into a current runbook with the codename they are findings, with a host name instead they give none.

The planned rewrite of the runbooks that still use the codename (#3345) will replace it with the host names sentence by sentence. Every rewritten sentence leaves the guard unless the guard knows the hosts.

This file and `tasks.md` are inside the guard's scan set. They therefore write the example sentences with the placeholder `<h>` in place of a host name.

## What Changes

- The database-writer check (`production-topology-node22-db-writer`) treats the two host names as names of the compute role, on both sides: a writer or primary-database claim about `<h>` is a finding, and the negative wordings that are allowed for the codename are allowed for `<h>`.
- A host name is matched on the original text as a lower-case whole token. An upper-case spelling (a reviewer warning number, a table row label), a domain name and a longer word that contains the letters are not host names.
- The local-PostgreSQL check (`production-topology-node22-local-postgres`) gets a new leg: a clause that names `<h>` together with a local PostgreSQL is a finding unless that same clause denies or forbids it. The archived / stopped / rollback wording that excuses a reference to the retired host's own PostgreSQL does not apply: the two hosts never had one.
- Slurm's own accounting database on the control host is not an NHMS database. A clause about it is not a writer claim, unless the same clause also names PostgreSQL, a primary, NHMS, or current/active/production state.
- Finding texts name the role with all three names. Check ids do not change.

## Not in this change

- The mirror legs and the port leg of the local-PostgreSQL check, and the `DATABASE_URL` scan leg: they stay as they are. The mirror and the port belong to the retired host.
- Guards for "ingest writer" or "display host" claims about a compute host. There is none for the codename either.
- Host addresses as names of the role.
- Renaming any `node22` identifier, check id or function.
- Rewriting any document.

## Impact

- Code: `scripts/governance/entropy_audit/topology_predicates.py`, `topology_context_rules.py`, `check_topology.py`.
- Tests: `tests/test_entropy_audit_topology_db_boundary.py`.
- Spec: `production-topology-contract`, requirement "Static guardrails block topology drift".
- No production runtime code. The guard runs in CI and locally.
