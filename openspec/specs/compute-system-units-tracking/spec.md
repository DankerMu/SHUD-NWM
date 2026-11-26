# compute-system-units-tracking Specification

## Purpose
The system-level files that the NWM compute role installed by hand on its two hosts, `w3` and `w2` (the mount
self-heal unit and timer, the `slurmd` mount drop-ins, the SMT-off tmpfiles entry of `w2`), are tracked in the
repository as byte-exact copies with a checksum list per host, so that one read-only command on a host shows whether
they still match. The user-scope units of the compute role are the subject of `node22-unit-tracking`.

## Requirements

### Requirement: The compute hosts' system-level NWM files SHALL be tracked byte-exactly per host with a checksum list the host can verify
The repository SHALL track under `infra/systemd/compute-system/<host>/` a byte-exact copy of the system-level files named below, which the NWM compute role installed by hand on that host, at each file's absolute path below the host directory (`<host>` is `w3` or `w2`). For `w3` these are `/etc/systemd/system/nhms-mounts-ensure.service`, `/etc/systemd/system/nhms-mounts-ensure.timer` and `/etc/systemd/system/slurmd.service.d/10-nhms-mounts.conf`; for `w2` the same three paths and `/etc/tmpfiles.d/nwm-smt-off.conf`. Next to each host directory the repository SHALL carry `infra/systemd/compute-system/<host>.sha256` in the format `sha256sum -c --strict` reads: one line per tracked file, 64 lowercase hex digits, two spaces, the file's absolute path on the host, LF line ends, no other line and no path twice. Other hand-made host state of the compute role is recorded in the runbook and its receipts and is not tracked here: the mask of the production user's user manager on `w2`, `/etc/fstab`, `/etc/exports` and `slurm.conf`. The `slurmd` drop-in of `w3` SHALL require the mounts `/scratch/frd_muziyao`, `/scratch/frd_muziyao/nhms-prod/workspace/runs` and `/ghdc/data`. A test SHALL assert all of the following:

- the set of files under each host directory equals the set of paths in that host's checksum list, and equals the set named above;
- every line of a list has the shape above, and every checksum equals the sha256 of the tracked copy;
- the two hosts' copies of `nhms-mounts-ensure.service` and of `nhms-mounts-ensure.timer` are byte-identical;
- the `RequiresMountsFor=` value of each host's `slurmd` drop-in contains the runs path;
- no tracked file assigns a non-empty value to a variable whose name ends in `TOKEN`, `SECRET`, `PASS` or `PASSWORD`.

#### Scenario: Tracked copies equal the checksum lists
- **WHEN** the test reads `w3.sha256`, `w2.sha256` and the files under `infra/systemd/compute-system/`
- **THEN** each list names exactly the files tracked for its host and every sha256 matches the tracked copy

#### Scenario: A tracked copy edited without its checksum
- **WHEN** one byte of a tracked copy changes and the checksum list does not
- **THEN** the test fails and names that file

#### Scenario: A file added under a host directory without a checksum line
- **WHEN** a file exists under `infra/systemd/compute-system/w2/` that `w2.sha256` does not list
- **THEN** the test fails and names that file

#### Scenario: Both slurmd drop-ins require the runs mount
- **WHEN** the test reads the two `10-nhms-mounts.conf` copies
- **THEN** each `RequiresMountsFor=` value contains `/scratch/frd_muziyao/nhms-prod/workspace/runs`

#### Scenario: The host verifies its files against the list
- **WHEN** root on that host runs `sha256sum -c --strict` on the production checkout's `infra/systemd/compute-system/<host>.sha256` (the checkout is readable by root and the production user only, and the production user does not log in to `w2`)
- **THEN** every line reports `OK` and the exit code is 0

#### Scenario: A malformed list line
- **WHEN** a list holds a line that is not `<64 hex>  <absolute path>`, or names one path twice
- **THEN** the test fails and names the line
