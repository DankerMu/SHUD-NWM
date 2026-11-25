# Retire the docker/compose deployment lane

## Why

Production does not deploy with docker or compose (#3231). Both nodes run
systemd user units; no installed `nhms*` unit references docker, compose,
the validators or `infra/docker` (read on both hosts on 2026-11-25). The
only Dockerfile of the tree is the lane's; nothing else builds an image.
The lane is about 7,000 lines of scripts and infra files and about 6,500
lines of tests that guard files nobody deploys.

The previous change (`display-c1-receipt-on-systemd`) removed the three
outside dependencies: the C1 receipt is read from the running unit, the
tests of surviving files left the lane's test file, and no test or
procedure outside the lane imports or runs the validators.

## What changes

**Deleted**

- `scripts/validate_two_node_docker_runtime.py`,
  `scripts/validate_two_node_docker_source_trust.py`
- `infra/compose.compute.yml`, `infra/compose.display.yml`,
  `infra/docker/Dockerfile.app`, `infra/docker/entrypoint.sh`,
  `.dockerignore`
- `infra/systemd/nhms-compute-compose.service`,
  `infra/systemd/nhms-display-compose.service`
- `infra/README.two-node-docker.md`
- `infra/env/compute.example` (its header says: template of the compose
  lane only, loaded by no unit)
- `tests/test_two_node_docker_runtime.py`,
  `tests/test_two_node_docker_runbook_environment_invariant.py`,
  `tests/test_two_node_docker_source_trust.py`
- `tests/test_two_node_22_e2e.py`: a live check that runs `docker exec`
  in the compose container of the compute API on the retired node-22 host
  (found during implementation; skipped unless an env variable that
  nothing sets is given)
- the capability `two-node-docker-runtime`

**Kept, and why**

- `infra/env/display.example`: the template of the live `display.env`.
  Its header and the keys that only compose read go; a requirement now
  says what it must hold.
- `infra/docker-compose.dev.yml`: the local development stack, not part of
  the lane.
- The `node27_docker` pytest marker and the database container on node-27:
  not part of the lane.

**Followers**

- Governance checks: the topology scan no longer lists the deleted README
  and reads `infra/systemd` instead; the artifact ignore and ownership
  checks stop asking for `.dockerignore`.
- Tests that pinned lane files or lane text in docs follow the docs.
- Docs that describe the lane as a way to deploy are corrected; history
  (plans, receipts, archived changes) is not rewritten.
- Eight other capabilities name the lane in a requirement; each is
  corrected (the deltas of this change). Three more name it outside a
  requirement (two Purpose paragraphs and one "Future Requirement" of
  `entropy-automation`); those are edited in the main specs after the
  archive, because a delta cannot reach them.

## Decisions

- **D1 — `.dockerignore` goes with the Dockerfile.** Without a Dockerfile
  there is no build context to trim. The two governance checks that
  required it lose that half; their `.gitignore` half stays.
- **D2 — `infra/env/compute.example` is deleted, not kept as
  documentation.** Its values were never the scheduler's; a reader who
  wants a compute variable is sent to the unit-to-file table of
  `infra/env/README.md`. Tests that read it move to the live template
  (`compute.scheduler-dbfree.env.example`) where the asserted key exists
  there, and drop the path otherwise.
- **D3 — a key leaves `display.example` only if nothing but the lane read
  it** (no reader in `apps/`, `packages/`, `services/`, `workers/`,
  `scripts/`, `infra/systemd/` once the lane's files are gone). A value
  that is a path inside the container becomes the node-27 host path that
  the template's own comment already names
  (`NHMS_PUBLISHED_ARTIFACT_ROOT`: `/var/lib/nhms/published` becomes
  `/home/ghdc/nwm/published`, the value the live file holds); otherwise a
  `display.env` copied from the template would fail the C1 claim
  `storage_not_writable`. The live `display.env` on node-27 is not
  edited: an unread key in it is harmless.
- **D4 — the topology scan reads `infra/systemd`** in place of the README.
  The audit reads only files whose name it accepts as text, and no unit
  suffix is among them, so `.service`, `.timer` and `.conf` become
  scannable names. Prototyped on a copy of the tree: 247 findings / 0
  failing before and after, no finding appears or disappears. If the
  implementation surfaces one after all, it is reported and decided, not
  suppressed.
- **D5 — the docs list of forbidden display keys is pinned to the runtime
  guard**, which is the only owner left once the validator and the
  entrypoint are gone. The guard has no single constant (two keys are
  written out in its code); the oracle is the set
  `DISPLAY_RUNTIME_FORBIDDEN_ENV_KEYS` that `tests/test_role_boundary_static.py`
  already builds from it.
- **D6 — history is left alone**: `docs/plans/**`, `docs/runbooks/receipts/**`,
  archived OpenSpec changes, `docs/review-loop-log.jsonl`.

## Not in this change

- The untracked `infra/env/compute.env` on the compute host: a leftover
  that no unit reads. Removing it is a host action for the owner.
- The other blocks of the tier runbook's G7 section, which call
  `scripts/node27_issue1895_*` scripts that are no longer in the tree.
- The receipt with `C1=PASS` on the merged head: it needs a restart of the
  display API and waits for the owner.
