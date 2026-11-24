# Tasks

Fixture level: `standard`. Risk packs: docs/governance drift, topology
contract.

Must preserve: every `node22` / `node-22` identifier; the pinned section 5.2 of
`two-node-deployment-overview.md`; the three instruction sentences pinned by
`tests/test_python_environment_truth.py`; `gate_eligible_count == 0` in the
topology hard gate; `docs/runbooks/receipts/**` already in the repository;
`docs/governance/ROLE_BOUNDARY.md` and `openspec/project-profile.md` sentences
pinned by `tests/test_role_boundary_static.py` (append, never rewrite); the
non-current classification of `two-node-deployment-overview.md` (its banner
must not contain "当前生产值守" or "current production operations").

Never written into the repository (it is public): operator account names and
sudo policy, proxy endpoints and ports, database or munge credentials, anything
copied from `.workplans/3345/migration/` other than the two lock receipts.

## 1. Agent instructions

- [x] 1.1 `instructions/agents/shared.md`: host table row for the compute role
  (`w3` control + `w2`, reached through node-27, production user, repo path,
  no live DB); retired-host bullet for `210.77.77.22` that keeps the existing
  archived/stopped do-not-connect wording for its local PostgreSQL; a bullet
  stating the role codename; oracle-routing row and loop step point at `w3`;
  link to the new runbook.
- [x] 1.2 Replace the node-22 3.12.7 exception block with the compute-role
  rule from the proposal (routine prohibition, observation commands, the
  three-condition `uv.lock` deploy exception, fail-closed on a missing
  interpreter, one-line rule for the retired host).
- [x] 1.3 Regenerate `CLAUDE.md` and `AGENTS.md` by the concatenation recipe
  that `test_generated_roots_byte_exact` checks.
- [x] 1.4 Rewrite the oracle of `test_instruction_roots_contain_command_contract`
  to pin the new rule: one line naming the compute role with `w3`, `w2`,
  `uv sync`, `uv run --no-sync`, `--active`, `系统 Python`, `禁止`; one line
  carrying the three deploy conditions (`uv.lock`, timer stopped, no pass in
  flight, no NWM job in the queue); and one line naming the retired `node-22`
  host with `3.12.7` and a prohibition on running `uv` there.

## 2. Runbook, receipts, units

- [x] 2.1 `docs/runbooks/w2-w3-compute-topology.md` covering: hosts and roles;
  storage layout and the loopback-NFS rule with its reason; the
  `daemon-reload`-before-mount gotcha, `no_root_squash` on the scratch export,
  nfsd `threads=64`; Slurm layout; `RequiresMountsFor` drop-ins and
  `nhms-mounts-ensure.timer`; the w3/w2 cross-dependency; the journal-backup
  units; retention as deployed and why the extra-roots window stays at 14;
  deploy steps at a pass boundary; cold-boot order and manual starts;
  bring-up gaps (`loginctl enable-linger`, slurmdbd, `compute.host.env` has no
  template); rollback to the retired host; the rule that the production
  user's systemd manager runs on `w3` only; the `uv.lock` step of a deploy;
  the gap that `w3`'s slurmd drop-in does not require the runs mount. The
  cold-boot section states that
  no cold boot has been exercised in the final layout. The two
  `nhms-mounts-ensure` unit files are reproduced in the runbook.
- [x] 2.2 Receipts under `docs/runbooks/receipts/`:
  `2026-11-23-lock-interop-node27-w3.md`,
  `2026-11-23-lock-interop-w2-w3.md` (both verbatim from the migration
  records, ACQUIRED rows included; a provenance line above the verbatim body
  says where the referenced 2026-09-14 receipt lives),
  `2026-11-23-shud-output-compare-w3-w2.md` (measured counts, differing files
  listed as not examined; provenance stated as recovered from the session
  log).
- [x] 2.3 `infra/systemd/nhms-w3-journal-backup.service` and `.timer`,
  byte-identical to the deployed files (sha256 `4e3ce187…ad4807`,
  `64725ceb…d045f3`); two entries added to
  `infra/systemd/node22/MANIFEST.json`; `EXPECTED_ENTRIES` in
  `tests/test_node22_unit_tracking.py` follows.

## 3. Existing docs

- [x] 3.1 Banner (compute role on `w3` / `w2` since 2026-11-23; retired host;
  literal ssh commands to `210.77.77.22` are run on `w3` and never on the
  retired host; link to the new runbook) on
  `docs/runbooks/current-production-ops.md`,
  `docs/runbooks/two-node-deployment-overview.md` (outside pinned section
  5.2), `docs/runbooks/node22-control-plane-manual-recovery.md`.
- [x] 3.1a Rewrite the Python execution note at the top of
  `current-production-ops.md` and of
  `node22-control-plane-manual-recovery.md` to the compute-role rule (shared
  `.venv` on `w3` / `w2`, exact interpreter, the one synchronization window);
  the 3.12.7 statement survives only as a description of the retired host.
- [x] 3.2 `docs/governance/ROLE_BOUNDARY.md` "Current physical deployment":
  add a bullet at the head of the list that names `w3` / `w2` as the hosts of
  the compute role, states that `210.77.77.22` is retired and a rollback
  archive only, excepts the local-PostgreSQL sentence, and links the new
  runbook; the Slurm verification bullet gains "run it on `w3`". Pinned
  sentences are left untouched.
- [x] 3.3 `docs/runbooks/node22-control-plane-manual-recovery.md`: the retired
  host's journal copy is stale from 2026-11-23T02:34Z; rollback copies the
  journal back first.
- [x] 3.4 `docs/runbooks/production-ops/service-bringup.md`: pointer to the
  bring-up gaps section of the new runbook.
- [x] 3.5 One-line pointer to the index banner at the top of every
  `docs/runbooks/production-ops/*.md` page that contains a literal ssh
  command to `210.77.77.22`.
- [x] 3.6 `instructions/agents/shared.md` (and regenerated roots): do not log
  in to `w2` as the production user.

## 4. Evidence Floor

The issue's `pytest -q tests/governance` names a directory that does not
exist; the guards below replace it (deviation recorded in the PR body).

- [x] 4.1 `uv run pytest -q tests/test_node22_entrypoint_invariant.py tests/test_node22_entrypoint_invariant_python_scan.py tests/test_python_environment_truth.py tests/test_node22_runbook_section_markers.py tests/test_node22_unit_tracking.py tests/test_node22_unit_drift_check.py tests/test_node22_unit_failure_alert.py tests/test_two_node_docker_runbook_environment_invariant.py tests/test_role_boundary_static.py`, every `tests/test_entropy_audit_*.py`, and every test file that reads one of the edited docs (`git grep -l` over `tests/` for each edited path), locally and on node-27.
- [x] 4.2 Red runs: the old oracle fails on the new instructions; the new
  oracle fails when the compute-role line loses `禁止` or `w2`, and when the
  retired-host line is removed; `test_generated_roots_byte_exact` fails on a
  hand edit of `CLAUDE.md`; the unit-tracking test fails when a manifest entry
  is dropped or a sha256 is changed (`EXPECTED_ENTRIES` is extended first, so
  the exact-set assertion sees the two new entries).
- [x] 4.3 `uv run python scripts/governance/audit_repo_entropy.py --mode hard-gate --format json`:
  `gate_eligible_count` and `hard_gate_failing_count` stay 0 (baseline at
  `abbf9b303`: 247 findings, 0 gate-eligible) on the working tree with the
  change unarchived, and again on a scratch copy with the change archived.
- [x] 4.4 `uv run ruff check .`; `openspec validate record-compute-role-on-w3-w2 --strict --no-interactive`.
- [x] 4.5 On w3, read-only: `sha256sum` of the two deployed journal-backup
  unit files equals the checked-in copies; `scripts/node22_unit_drift_check.py`
  run with the new manifest reports neither unit as untracked.
- [x] 4.6 Archive in the final push: `openspec archive record-compute-role-on-w3-w2 --yes`.

## Evidence recorded

- 4.1: local, 40 files, 4142 passed (first commit `319aebb46`); after fix pass
  1, 27 files, 1416 passed. node-27 at `319aebb46`: 40 files, 4142 passed; the
  guard subset is rerun on node-27 at the final commit and posted on the PR.
- 4.2: ten red runs, each restored and checksum-verified (old oracle on new
  instructions; compute-role line without `禁止` / without `w2`; retired-host
  line removed / without `#1831`; a deploy condition removed; `长期保持` added
  to the compute-role line; hand edit of `CLAUDE.md`; manifest entry dropped;
  sha256 changed).
- 4.3: 247 findings / 0 gate-eligible / 0 failing on the working tree, on
  node-27, and on a scratch copy with the change archived (baseline 247/0/0).
- 4.5: on w3, both deployed journal-backup unit files hash equal to the
  tracked copies; the drift probe with the new manifest reports 24 entries
  `same`. It still exits 1 because of one deployed drop-in that predates this
  change (`nhms-slurm-gateway.service.d/20-runtime-path.conf`); tracked in the
  host-hardening follow-up.

## Deviations

- Issue verification names `tests/governance`, which does not exist; section 4
  lists the guards that were run instead.
- Issue scope item 6 (umbrella bookkeeping) rides in PR #3352's final push.
- The instruction-guard hook caps line length, so the host-table row and the
  first line of the Python rule carry less than the prepared text; the moved
  clauses sit in the bullets directly below them.
- Review round 1 added scope: the `w2` login rule, the `uv.lock` deploy step,
  pointers on seven `production-ops/` pages, and the `node22-unit-tracking`
  delta.
- Review round 2: the `uv.lock` branch of the deploy steps now drains the
  `nhms_*` jobs before the merge instead of after it, and the runs-mount gap
  is described as detectable only after the fact.
- Markdown lint: adjacent blockquotes were joined, and blank lines were added
  around the headings and tables of the w2/w3 lock receipt (whitespace only;
  its rows are unchanged).
