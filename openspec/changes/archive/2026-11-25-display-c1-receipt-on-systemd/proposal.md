# Define the display deployment receipt (C1) on the running systemd unit

## Why

The bring-up checklist's C1 receipt is defined on a deployment mode that
production does not use (#3231). It runs
`scripts/validate_two_node_docker_runtime.py static`, which checks compose
files, env templates and image assets, and its "production deployment" step
is `docker compose up -d`. Both nodes run systemd user units; no installed
unit references docker or compose (read on both hosts, 2026-11-25).

What the static check proves is about files nobody deploys. What C1 is
for — node-27 serves the display role and has no reach into the compute
control plane — has to be read from the running service. Measured read-only
on node-27 on 2026-11-25:

| Claim of today's C1 | On the running unit | In the new C1 |
|---|---|---|
| `GET /api/v1/runtime/config` reports `display_readonly` | yes; `slurm_routes_enabled` and `control_mutations_enabled` are false | kept |
| `/api/v1/slurm/*` answers 404 | yes | kept |
| no Slurm CLI, config or Munge socket | the host has none | kept, as a host fact |
| no forbidden env | the API refuses to start in the display role with any of those keys set (`apps/api/runtime_mode.py`, `DISPLAY_BOUNDARY_CONFIG_UNSAFE`), so a running display process is the proof | kept, by construction |
| published storage read-only | there is no read-only mount; the object-store root and the published-artifact root are directories the service user cannot write | kept, as a permission fact about the two root directories (not about every file beneath them) |
| no Docker socket | false on the host: the service user manages the database container | dropped |
| no forbidden mount, HostConfig isolation, read-only root filesystem | exist only for a container | dropped |

Three facts the old text never carried:

- The receipt speaks for a commit only if the process loaded it. The
  checkout on node-27 is pulled without a restart all the time (the timers
  run from it). A receipt taken from a process that is older than the
  checkout's last move, or older than its env file, must not pass; neither
  may one taken from a checkout with modified tracked files.
- The answers must come from the unit. Whatever listens on the port
  answers the requests; the receipt has to show that the listener is the
  unit's main process and its workers, not another server on the same
  port.
- The record that the C4 acceptance tool consumes is written by the
  operator; its `head_sha` is the receipt's `checkout_head`.

Three things outside the lane still depend on it and block its deletion:

- `tests/test_role_boundary_static.py` imports the validator for four
  constants;
- `tests/test_start_display_api_restart_anchor.py` imports its harness from
  `tests/test_two_node_docker_runtime.py`, and that file holds tests of
  files that stay (`scripts/ops/start-display-api.sh`, the db-free scheduler
  env template, `.gitignore`);
- `docs/runbooks/tier-node27-timeseries-storage.md` runs the validator in a
  go/no-go block.

## What changes

1. **C1 is an observation of the running unit.** A new read-only script,
   `scripts/ops/display-c1-receipt.sh`, prints one line per claim and
   `C1=PASS` only if every claim holds (the spec delta lists them). It
   starts, stops and restarts nothing, does not source the env file, and
   prints no value from it except the two storage roots. The checklist's C1
   names the wrapper as the deployment step (a production action) and this
   script as the receipt, lists what the receipt proves and what it no
   longer claims.
2. **Live tests leave the lane's test file.** Every test function of
   `tests/test_two_node_docker_runtime.py` is classified by the file it
   tests; those whose subject stays move, bodies unchanged, to a file named
   after the subject. The restart-anchor test file imports the harness from
   the new home.
3. **The role-boundary test stops importing the validator.** Its
   alignment test compared the runtime guard's forbidden keys with the
   validator's list. The validator goes; what stays worth pinning is that
   the display env template is a configuration the runtime guard accepts.
4. **The go/no-go block of the tier runbook no longer runs the validator.**

Nothing is deleted from the lane in this change; that is the next one.

## Decisions

- **D1 — a script, not a runbook block.** The receipt is a gate; a gate
  needs a test that shows it red. A block inside Markdown would have to be
  extracted to be run.
- **D2 — every claim is evaluated and printed**, also after the first
  failure, so that one run shows the whole state.
- **D3 — no environment dump.** "No forbidden env" is proven by the
  startup guard, which is unit-tested; the script does not read the
  process environment.
- **D4 — the port comes from the main process's command line**, not from
  the env file: it is the port the observed process listens on.
- **D5 — the repository root is the script's own checkout unless given as
  the first argument.** The argument exists so that the script can be run
  from another checkout against the production one before a merge; the
  receipt prints the root it used.
- **D6 — staleness is decided on integers**: the process start (now minus
  `ps -o etimes=`) must be later than the time of the
  checkout's last HEAD move (`git log -g -1 --date=unix --format=%gd HEAD`
  prints `HEAD@{<seconds>}`) and than the env file's modification time
  (`date -r <file> +%s`, which GNU and BSD `date` both have). By more
  than one second: the observation time and the elapsed time are both
  floored, so their difference can be one second later than the real
  start (found in code review). The systemd on
  node-27 (249) has no unix timestamp output.
- **D7 — the listener is tied to the unit through the process tree**:
  `ss -Hltnp` names the processes that hold the port; each must be the
  main process or have it as parent (measured: the main process and its
  two workers). Reading the unit's cgroup would need a path under `/sys`
  that a test cannot fake without a seam in the script.
- **D8 — tracked changes are read without touching the index**:
  `GIT_OPTIONAL_LOCKS=0 git status --porcelain --untracked-files=no`.
  Untracked files do not fail the claim (the checkout holds evidence and
  data directories).
- **D9 — the Slurm client scan uses the invoking shell's `PATH`.** The
  checklist says to run the script from a login shell of the service
  user, which is what the unit's `bash -lc` gets.

## Not in this change

- Deleting the lane (validators, compose files, Dockerfile, units, README,
  `infra/env/compute.example`, the capability `two-node-docker-runtime`)
  and everything that names it in governance checks, docs and specs.
- The other blocks of the tier runbook's G7 section. They call
  `scripts/node27_issue1895_*` scripts that are no longer in the tree;
  reported on #3231, not touched here.
- Restarting the display API on node-27. The receipt of the merged head
  needs it and it waits for the owner.
