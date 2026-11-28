# The deployed units describe themselves as units of the compute role (#3427)

Slice 9.2 of `openspec/changes/sweep-compute-role-narration` (#3345, epic #3217). The umbrella is the
design of the sweep (`design.md` §1 vocabulary, §3 rules, §5 the rows on the units); this change
carries the delta of one capability and the decisions of one slice.

## Why

16 of the user units that run on the compute control host carry the codename "node-22" in
`Description=`: 14 say "NHMS node-22 …", and the scheduler and gateway units end with
"(node 22 user)". `systemctl --user status` and the journal show that text on a machine that is not
node-22. Nine comment lines of five units and the example gateway unit use the codename the same
way.

Two of the 16 are tracked under `infra/systemd/node22/` as byte-exact copies of the deployed files,
pinned to the read-only receipt of 2026-10-16. Changing their description changes the deployed
bytes, and a receipt is history: it cannot be edited.

## What Changes

- **`Description=` of 16 units**: "NHMS node-22 …" becomes "NHMS compute …"; "(node 22 user)" becomes
  "(compute role user)". **Nine comment lines** in five units (three of them open a comment about a
  mount-namespace limit, which is rewritten whole), and the example unit
  `infra/systemd/nhms-slurm-gateway.service` (seven comment lines and its `Description=`), use the
  glossary terms. `design.md` D1 lists every line.
- **`infra/systemd/node22/MANIFEST.json`**: the sha256 of the 17 manifest files that change (the 16,
  and `nhms-w3-journal-backup.service` by one comment).
- **A new read-only receipt**, `docs/runbooks/receipts/2026-11-28-w3-scheduler-and-gateway-units-snapshot.md`,
  records the two redeployed units of `infra/systemd/node22/`. The operator receipt of the host step
  is a second file, `docs/runbooks/receipts/2026-11-28-issue-3427-unit-descriptions-redeployed.md`.
- **The receipt rule of `tests/test_node22_unit_tracking.py`**: "no path is recorded by two receipts"
  becomes "the latest receipt that records a path holds its sha256, and the only paths recorded
  twice are the two redeployed units".
- **`node22-unit-tracking`**: the requirement says which receipt records which file and states the
  new rule (one scenario added); its narration uses the glossary terms; its title is renamed
  (`appendix-titles.md` of the umbrella): "Live node-22 units SHALL be tracked …" becomes "Live
  units of the compute role SHALL be tracked …". The Purpose line is edited in the main spec at
  archive.
- **Host step** (owner's go-ahead of 2026-11-28, on #3427): the 17 files are installed on the
  compute control host at a scheduler pass boundary, followed by `daemon-reload`. Nothing is
  restarted.

## What does not change

- Unit names, `ExecStart=`, `OnFailure=`, timer schedules, every directive other than
  `Description=`, the content of every drop-in.
- The manifest's schema, its 24 entries and their paths; the drift probe.
- The receipts of 2026-10-16 and 2026-11-26.
- The alert script (slice 9.1) and the mail it sends. No test mail is sent, and the alert template
  is not started.

## Impact

- `infra/systemd/**` (18 files), `tests/test_node22_unit_tracking.py`, two new receipts, one sentence
  of `infra/env/README.md`, `openspec/specs/node22-unit-tracking/spec.md`.
- The installed copies of 17 unit files on the compute control host. A description is metadata:
  no unit's behaviour changes, and no process is restarted.
- Until the production checkout on the compute control host is next updated, its own copy of the
  manifest holds the old sha256 values: a drift probe run from that checkout reports the 17 files as
  `differs`. The probe is run by hand, not by a timer (`design.md` D5).
