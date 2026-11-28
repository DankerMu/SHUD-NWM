# Design: the deployed units describe themselves as units of the compute role (#3427)

## Context

`infra/systemd/node22/MANIFEST.json` lists the 24 user unit files deployed on the compute control
host. `scripts/node22_unit_drift_check.py` compares the installed copies with the tracked ones byte
for byte; it is run by hand before and after a unit change (no timer runs it). Seven of the 24 are
tracked under `infra/systemd/node22/` as copies of files that were deployed before they were
tracked, and `tests/test_node22_unit_tracking.py` pins each to the read-only receipt that recorded
it. The other 17 are tracked at `infra/systemd/<name>` and pinned by the manifest alone.

On 2026-11-28 the production checkout on the compute control host is at `f5c3b487e`; `infra/systemd`
has not changed between that commit and master.

## Decisions

### D1. The texts

`Description=`, 16 units:

| Unit | Before | After |
|---|---|---|
| the 14 units that say "NHMS node-22 …": the two health probes and their timers (4), the alert template (1), the file-provider refresh service (1; its timer has no codename and does not change), and the services and timers of evidence retention, journal retention, state-index capacity and state-index prune (8) | `… NHMS node-22 …` | `… NHMS compute …` |
| `node22/nhms-compute-scheduler.service` | `NHMS production scheduler one bounded continuous pass (node 22 user)` | `NHMS production scheduler one bounded continuous pass (compute role user)` |
| `node22/nhms-slurm-gateway.service` | `NHMS standalone Slurm gateway (node 22 user)` | `NHMS standalone Slurm gateway (compute role user)` |

Only the words change; every other character of the line stays. "NHMS compute" is the phrase of
the umbrella's `design.md` §5, the same the alert mail's subject uses since slice 9.1.

Comments (the term by what the sentence is about, umbrella `design.md` §1):

| File | Before | After |
|---|---|---|
| `nhms-node22-refresh-timer-health.service` | "the shipped defaults are the / node-22 production values" | "the shipped defaults are / the compute role's production values" (the article moves down with the term; the sentence is re-wrapped) |
| same, the comment above `UMask=` (five lines) | "Same node-22 namespace constraint the refresh unit documents: user-systemd makes the filesystem root inaccessible inside a PrivateTmp mount namespace, and the probe's no-follow receipt read opens an absolute path. So no PrivateTmp here either; …" | "For the reason the refresh unit records (on the retired node-22 host, user-systemd made the filesystem root inaccessible inside a PrivateTmp mount namespace), and because the probe's no-follow receipt read opens an absolute path, this unit does not use PrivateTmp either; …" |
| `nhms-node22-scheduler-stall-health.service` | "node-22 reaches no live database and this probe requires none" | "The compute role reaches no live database and this probe requires none" |
| same | "the shipped defaults ARE the node-22 / production values" | "the shipped defaults ARE the compute role's / production values" |
| same, the comment above `UMask=` (five lines) | "The same node-22 namespace constraint the refresh units document: user-systemd makes the filesystem root inaccessible … So no PrivateTmp here either; …" | "For the reason the refresh units record (on the retired node-22 host, user-systemd made the filesystem root inaccessible inside a PrivateTmp mount namespace), and because this probe's no-follow reads open absolute paths under /scratch, this unit does not use PrivateTmp either; …" |
| `nhms-node22-unit-failure-alert@.service` | "Generic node-22 unit-failure alert" | "Generic unit-failure alert of the compute control host" |
| same | "with node-22 absolute paths: the node-22 checkout lives under" | "with the compute hosts' absolute paths: the production checkout lives under" |
| `nhms-scheduler-file-provider-refresh.service`, the comment above `UMask=` (three lines) | "node-22 user-systemd makes the filesystem root inaccessible inside a PrivateTmp mount namespace. The provider's no-follow verifier must open the root and walk each absolute path component, so this unit cannot use PrivateTmp." | "On the retired node-22 host, user-systemd made the filesystem root inaccessible inside a PrivateTmp mount namespace (recorded no later than 2026-07-25; not re-tested on the compute control host). The provider's no-follow verifier must open the root and walk each absolute path component, so this unit does not use PrivateTmp." |
| `nhms-w3-journal-backup.service` | "like the other node-22 oneshots" | "like the other oneshots of the compute role" |

Nine comment lines carry the codename. In three of them it opens a comment about the namespace, and
there the whole comment is rewritten (three comments of three to five lines): the sentence is an
observation of the original host, in the repository since its first commit (2026-07-25). Whether the
user manager of the compute control host has the same limit was not tested: testing it means
running a unit with `PrivateTmp=` as the production user. With the host removed from its first line
only, the rest of the comment would state the limit of today's host in the present tense, so every
sentence of the three comments is in the past tense or says what the unit does ("does not use
PrivateTmp", where it said "cannot use"). The units keep the setting they were deployed with. No
comment contains the directive spelled with a value: `tests/test_scheduler_refresh_deployment_contract.py`
searches the unit's whole text for it. Rewritten comments may change the number of lines of a unit.

The example unit `infra/systemd/nhms-slurm-gateway.service` (a template, not installed and not in
the manifest): "the standalone NHMS Slurm gateway of the compute role", "NOT THE LIVE UNIT OF THE
COMPUTE ROLE", "The live gateway of the compute role runs as a USER-systemd unit", "NOT the live
values of the compute role", "the absolute repository checkout path on the compute hosts", "secret
env source of the compute role" (the line above it keeps "the live"), `Description=NHMS standalone Slurm gateway (compute role)`,
"on the compute control host the user-systemd drop-in RESETS this generic".

A unit's diff holds `Description=` lines and comment lines and nothing else. A term stays on one
comment line, so several sentences are re-wrapped and five files change their number of lines.

### D2. The manifest

17 `sha256` values change: the 16 units of D1 and `nhms-w3-journal-backup.service`. The seven other
entries, the entry order, the paths and the schema are untouched. The example unit is not an entry.

### D3. Receipts, and the test's rule

A receipt is a read-only record of what was on the host on a day. The receipt of 2026-10-16 records
`nhms-compute-scheduler.service` and `nhms-slurm-gateway.service` with the bytes of that day, and
it keeps its rows. The redeployed bytes are recorded in a new receipt,
`docs/runbooks/receipts/2026-11-28-w3-scheduler-and-gateway-units-snapshot.md`, taken on the host
after the install (sha256, size, mode, mtime of the two files), as #3359 did for the gateway
drop-in.

The test read "no path is recorded by two receipts". Two paths are now recorded twice, so the rule
becomes:

- the receipts are read in date order and a later row replaces an earlier one (the merge the test
  already performs);
- a tracked file under `node22/` equals the sha256 of the **latest** receipt that records it;
- the set of paths recorded by more than one receipt is **exactly** the two redeployed units: any
  other path recorded twice fails, and so does a redeploy receipt that records anything else;
- for each of the two, the new receipt's sha256 differs from the 2026-10-16 row (the new receipt is
  not a copy of the old rows);
- "latest" is checked, not assumed: the test asserts that the receipts are listed in the order of
  the dates their file names start with.

The test reads every line of a receipt that has the shape "backticked path, backticked sha256" as a
row. The new receipt therefore holds exactly two such lines. The old sha256 values, where it quotes
them, and anything else are written in another shape.

The rule lives in small module-level functions, as the test's own header asks, with permanent
synthetic cases next to `test_receipt_union_reports_a_path_recorded_twice`: the overlap is not the
expected set; the latest receipt records more or less than the redeployed paths; a redeployed row
equals the earlier row; the dates do not strictly increase.

Red runs on the real files, each restored afterwards: (R1) the old sha256 of one of the two units
restored in the manifest: the manifest test fails; (R2) the old sha256 restored in the new
receipt's row: the receipt test fails; (R3) the row of `nhms-compute-scheduler.timer`, which the
2026-10-16 receipt records, added to the new receipt: the overlap rule fails; (R4) the old
`Description=` restored in one of the two tracked files: the manifest test and the receipt test
fail; (R5) the receipt paths listed out of date order: the order assertion fails; (R6) one of the
two units reverted in the tracked file and the manifest, and the new receipt's row set to the
2026-10-16 sha256: only the "differs from the earlier row" assertion fails; (R7) one of the two rows
removed from the new receipt: the rule on what the latest receipt records fails.

### D4. Order of work, and the host step

1. The unit files, the manifest and the test are changed. The new receipt exists as a draft, marked
   as one, whose two rows hold the sha256 of the tracked files. **Everything that can find fault
   with the bytes of the 17 files runs before the host step**, because the go-ahead covers one
   install: this state is committed and pushed, and on that commit
   - the hard gate is compared with the one before (finding multiset; `infra/systemd` is gate input);
   - the tests of `tasks.md` are green locally and on node-27;
   - the reviewers read it;
   - the PR is opened, so that CI's full sharded suite runs on it. The PR waits for the second
     commit before it merges.
2. **Host step**, on the compute control host, as the production user, at a scheduler pass boundary:
   `nhms-compute-scheduler.service` is inactive and none of the services being replaced is
   activating (the refresh oneshot can wait for a pass for hours), re-checked right before the
   install. The gateway service stays running throughout.
   1. drift probe with the production checkout's manifest: every entry `same`, nothing untracked,
      exit 0. Anything else stops the step. (Read before the slice, 2026-11-28: the 24 installed
      files equal master's manifest, and `infra/systemd` does not differ between the production
      checkout's commit and master. The install so carries the text of this slice and nothing
      else.)
   2. the 17 installed files are copied to a dated backup directory inside the user unit directory
      (the probe ignores directories it does not know);
   3. the 17 files of the reviewed commit are installed from an export of that commit that lives
      outside the production checkout and is removed afterwards. The export is pinned first: its
      commit id is the reviewed one, and the sha256 of its `MANIFEST.json` and of each of the 17
      files equal the values computed in the repository at that commit. Every target is checked to
      be a regular file and not a symlink. **All 17 are staged before any is replaced:** each is
      written next to its target under a temporary name that is not a unit name (a leading dot and
      a `.tmp` ending, which neither `systemd` nor the probe reads), with the mode the target had,
      and compared with its manifest sha256. Only when all 17 are staged and equal are they renamed
      over their targets;
   4. `systemctl --user daemon-reload`. Nothing is started, stopped or restarted; the alert
      template is not instantiated;
   5. drift probe with the PR's manifest (run from the export): every entry `same`, nothing
      untracked, exit 0;
   6. the two units of `node22/` are read back (sha256, size, mode, mtime) for the receipt;
   7. the timers that were active before are active, the gateway answers its health request, no
      user unit is in the failed state, and `systemctl --user show -p Description` gives the new
      text for a sample of units.
3. The draft receipt is completed with the values read in 2.6. If a value differs from the tracked
   file, the slice stops: the installed copy is not what the repository tracks.
4. A second commit holds the completed receipt, the operator receipt, the archive of this change
   and the umbrella tick; the reviewers confirm it. It is the PR's last push. The PR so has two
   pushes, against the repository's rule of one final push: the receipts can only be written after
   the host step, and CI must have run on the bytes before it. The PR records this as a deviation.

The production checkout on the host is not moved by this slice.

What stops the step, and what happens then:

| Fails | State of the host | Action |
|---|---|---|
| 2.1 (the probe before), the pass-boundary check, the export's pins, a target that is not a regular file, staging | nothing replaced | the staged temporary files are removed; the step stops and is reported |
| a rename, `daemon-reload`, 2.5 (the probe after), 2.7 | some or all of the 17 replaced | **rollback**: the 17 files are copied back from the backup of 2.2, `daemon-reload`, and the probe with the production checkout's manifest must read all `same` again; the step stops and is reported |

A rollback returns the host to the state it had before the step. It is part of the one action the
go-ahead covers, and a second attempt is not: it needs the owner again.

### D5. The production checkout's manifest lags until its next update

The drift probe reads the manifest of the checkout it is run from. After the host step the
installed files equal master's manifest, and the production checkout (at `f5c3b487e`) still holds
the old one: a probe run from it reports 17 `differs` until the checkout is updated to a commit that
contains this slice. Nothing runs the probe on a timer, so no unit fails and no mail is sent; an
operator who runs it by hand in that window has to run it from a current export, as step 2.5 does.
The two unit installers (`scripts/install_node22_refresh_timer_health.sh`,
`scripts/install_node22_scheduler_file_provider_refresh.sh`) copy their units from the checkout
they are run from: run from the production checkout in that window, they would put the old
descriptions back.
The PR and the issue say so, and the owner is told that the next update of that checkout closes the
window (the same update makes slice 9.1's mail subject effective).

### D6. The spec

`node22-unit-tracking`, one delta:

- RENAMED: "Live node-22 units SHALL be tracked byte-exactly with a manifest and without secret
  values" becomes "Live units of the compute role SHALL be tracked …".
- MODIFIED, the tracking requirement: the two redeployed units and their new receipt; "the latest
  receipt that records it"; "the three receipts"; the manifest lists "every user unit file deployed
  on the compute control host"; "the compute role's authority table"; one scenario added ("A
  redeployed unit follows its latest receipt"). No scenario is dropped or retitled.
- MODIFIED, the probe requirement: one scenario sentence, "the real leftovers of the retired node-22
  host recorded in `docs/governance/entropy-audit-2026-10-16.md`" (a dated record of that host).
- The Purpose line, edited in the main spec at archive: "The live user-scope systemd units of the
  compute role are …".

The delta is generated from the main spec with asserted substitutions and compared with the
expectation after the archive, as in slice 10.1.

### D7. The counter, and the one other sentence about the receipts

`infra/env/README.md` names the two receipts in one sentence; the third is added to it.

No `name` token of the counter moves: the unit names, script names and the manifest path are
untouched; the test keeps its `test_node22_*` names and its new code uses no additional occurrence
of a name that carries the codename; the receipts lie under `docs/runbooks/receipts/`, which the
counter treats as history. In the plain table the `bare` column of `infra/` and of
`openspec/specs/` falls, and the `name` and `quoted` columns rise by the lines that keep a name
next to the rewritten words (the alert template's opening comment keeps
`node22-config-hygiene`; the tracking requirement keeps `infra/systemd/node22/`). The sentences
that now say "the retired node-22 host" stay bare: one in each of the three namespace comments and
one in the spec.

### D8. Two receipts

- `docs/runbooks/receipts/2026-11-28-w3-scheduler-and-gateway-units-snapshot.md`: the read-only
  snapshot of the two redeployed units of `infra/systemd/node22/`, which the test reads. It holds
  exactly two rows of the shape the test parses.
- `docs/runbooks/receipts/2026-11-28-issue-3427-unit-descriptions-redeployed.md`: the operator
  receipt of the host step (the pattern of `2026-11-26-issue-3394-compute-api-unit-retired.md`):
  the two probe summaries, each of the 17 installed files with its sha256, the backup directory,
  the state of the timers and the gateway before and after. No test reads it. The issue's PR
  boundary says "one receipt"; the PR records the second file as a deviation.

## Risks

- **A wrong byte in an installed unit.** Each installed file is compared with the manifest sha256,
  and the probe after the install compares all 24. The diff of every unit is limited to
  `Description=` and comment lines, which `systemd` does not act on.
- **`daemon-reload` during a pass.** The step runs only while the scheduler service is inactive, and
  the scheduler timer is not stopped: the step takes seconds.
- **The window of D5.**
