# Tasks: compute-host-units-hardening (#3359 items 3–5)

## Must preserve

- The journal-backup unit's `ExecCondition=`, `ExecStart=`, `ExecStartPost=`, `Nice=`, `IOSchedulingClass=` and its
  timer are byte-identical; so is every other file under `infra/systemd/` that this change does not name.
- `docs/runbooks/receipts/2026-10-16-node22-live-units-snapshot.md` is not edited.
- `infra/systemd/system/`, `scripts/node22_unit_drift_check.py` and their tests are not edited
  (`tests/test_node22_unit_drift_check.py` pins no count; it reads the manifest).
- Entropy hard gate: 247 findings / 0 failing.

## 1. Fixture

- [x] 1.1 Fixture review (`pass`) and `openspec validate compute-host-units-hardening --strict --no-interactive`.

## 2. Journal backup alert (item 3)

- [ ] 2.1 `infra/systemd/nhms-w3-journal-backup.service`: the `OnFailure=` line in `[Unit]`, `TimeoutStartSec=900` in
      `[Service]`, a comment for each saying why; nothing else changes.
- [ ] 2.2 `tests/test_node22_unit_failure_alert.py`: `WIRED_UNITS` has eight entries; names and comments that say
      "seven" follow. New tests: the timeout bound (`0 < 3 × value < 3600`, exactly one line, in `[Service]`); the
      non-comment lines of `[Unit]` and of `[Service]`, in order, equal the lines of the unit at the merge base (written
      out in the test) plus the two new lines. Red runs: the timeout set to 1200; an `Exec*` line changed by one byte; a
      third non-comment line added.
- [ ] 2.3 `infra/systemd/node22/MANIFEST.json`: the sha256 of the journal-backup service follows its new content.

## 3. Gateway drop-in in the manifest (item 4)

- [ ] 3.1 `infra/systemd/node22/nhms-slurm-gateway.service.d/20-runtime-path.conf`: byte copy with sha256
      `ae59e26c2440c3a9e886c1f2914d6f4907c67f881feb5ca49d7d1555b1d66a97`; a manifest entry at its sorted position.
- [ ] 3.2 `tests/test_node22_unit_tracking.py`: eight files under `node22/`; the receipt table is the union of the two
      receipts, which must not overlap and must together equal the eight; module docstring and comments follow.
      Any other reader of the manifest's entry count follows (the fixture review found none).
- [ ] 3.3 Red runs, each restored: the new file removed from the manifest; one byte of the new file changed; the new
      receipt's row removed.

## 4. System-level files (item 5)

- [ ] 4.1 `infra/systemd/compute-system/`: the seven copies from `.workplans/3359/host-files/` at the paths of the
      proposal's tree, byte-exact, and the two checksum lists in `sha256sum -c --strict` format.
- [ ] 4.2 New test file `tests/test_compute_system_units_tracking.py`: one test per assertion of the requirement; rule
      functions exercised on synthetic input (a list line with a wrong checksum, an unlisted file, a listed file that
      does not exist, a malformed line, a path listed twice, a secret-named assignment).
- [ ] 4.3 Red runs, each restored: a byte changed in a tracked copy; a file added without a list line; a list line
      removed; the runs path removed from the `w3` drop-in copy; one of the two `nhms-mounts-ensure.service` copies
      changed.
- [ ] 4.4 The checksum lists verify locally against a scratch root that mirrors the host paths (the test may do this
      with `hashlib`; no `sha256sum` binary is required in CI).

## 5. Docs (orchestrator)

- [ ] 5.1 `docs/runbooks/w2-w3-compute-topology.md`: the mask on `w2` and its rollback (§1); the `w3` drop-in, the
      directory under the runs mount and what remains unproven until a reboot (§3); the node definition of `w2`
      (`ThreadsPerCore=1`, SMT off, the tmpfiles drop-in); the journal backup alerts, with the table of silent cases
      (§4); the system-level files are tracked, with the compare command (§4).
- [ ] 5.2 `docs/runbooks/receipts/2026-11-26-issue-3359-compute-host-hardening.md`: items 1 and 2 as done on the hosts,
      the checksums of the seven system-level files as read on the hosts.
- [ ] 5.3 `infra/env/README.md` or other docs that count the wired units or the manifest entries follow (implementer
      reports the hits; orchestrator edits).

## 6. Evidence Floor

- [ ] 6.1 Local: the touched test files and every reader of an edited file pass; `ruff check --no-cache .`; the
      entropy hard gate 247 / 0; markdownlint on the touched docs.
- [ ] 6.2 node-27 (detached worktree, no database): `tests/test_node22_unit_failure_alert.py`,
      `tests/test_node22_unit_tracking.py`, `tests/test_node22_unit_drift_check.py`,
      `tests/test_node27_raw_retention_canonical_deployment.py`, `tests/test_compute_system_units_tracking.py` and every
      `tests/test_entropy_audit_*.py`.
- [ ] 6.3 Read-only on the hosts before the merge, from the PR head's files: `sha256sum -c --strict` of each list
      against the host's files (the lists shipped to the host's temporary directory); on `w3` the drift probe run from
      an export of the PR head's `infra/systemd` and probe script in a temporary directory, with the exact interpreter:
      expected `untracked` empty and exactly one entry `differs`, `nhms-w3-journal-backup.service` (not deployed yet).
- [ ] 6.4 Code review (one seat, two lenses: config-contract correctness; test evidence); CI green.

After the merge, not a task: the six deployment steps of the proposal, on the issue.
