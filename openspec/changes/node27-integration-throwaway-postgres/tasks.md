# Tasks

Fixture level: `expanded`. Risk packs: production host safety (a tool that
runs `docker rm` and removes a directory tree next to the production
database container); secrets (a superuser password is generated, stored and
handed to child processes); test-harness behaviour (a new session-level
abort in `tests/conftest.py` that every pytest run passes through);
operator documentation and root instructions (the integration convention on
node-27 changes).

Must preserve: `tests/conftest.py` keeps its opt-in rules, its skip reasons
and its per-test `nhms_it_<uuid>` create/drop on the server it is given; CI
(`localhost:5432`) and sessions that would skip integration behave exactly
as today; no existing test is edited; the production container `nhms-db`,
its data directory and its configuration are never written, stopped or
removed by the tool (its only call naming it is `docker inspect nhms-db
--format {{.Image}}`); no password or URL with a password is printed by the
tool or written into the repository, an issue or a PR.

## 1. Tool (`scripts/node27_integration_pg.py`)

- [ ] 1.1 Subcommands `start`, `stop`, `status`, `run` with the options,
  constants, order of steps, exit codes and messages of `design.md`.
  Standard library only; `docker` is found on `PATH`.
- [ ] 1.2 The two removal functions of `design.md` and no other removal: the
  container function takes no name, checks the label for the resolved root
  and refuses a mismatch; the directory function refuses a symbolic link,
  requires the sentinel and does not follow links.
- [ ] 1.3 The password is created with `secrets`, written only to
  `postgres.env` (created exclusively with mode 0600), never placed in an
  argument list, never printed; `run` passes it to the child through the
  environment and prints nothing. The sentinel is written before the env
  file.
- [ ] 1.4 `start` refuses port 55432, a relative root, a non-empty root
  without the sentinel, a container named `nhms-it-db` without the label
  for this root, and a held lock; fails closed when `nhms-db` cannot be
  inspected; after the directory exists, any failure (passwd dump, `docker
  run`, readiness, collation) removes what it created.
- [ ] 1.5 Locking as in `design.md`: `run` shared and inherited by the
  command, `start` and `stop` exclusive and non-blocking, `stop --force`
  goes on without it.
- [ ] 1.6 Readiness is probed over TCP inside the container, with the
  password taken from the container's own environment by a fixed script.
- [ ] 1.7 (review) An instance that is already running is checked before
  `start` reports it: sentinel image against the production image, an
  explicit `--port` against the sentinel's, one readiness probe. "The
  container is absent" is concluded only from docker's no-such-container
  answer, never from another inspect failure.

## 2. Tripwire (`tests/conftest.py`)

- [ ] 2.1 `_refuse_production_postmaster(url)` as in `design.md`, called at
  the end of `pytest_configure` only when `_integration_skip_reason()` is
  `None`. The message names `scripts/node27_integration_pg.py` and #3376
  and contains no part of the URL and no parser error text.

## 3. Spec and documents (orchestrator)

- [ ] 3.1 Spec delta `real-integration-test-matrix`, one requirement added.
- [ ] 3.2 `docs/runbooks/node-27-database-container-operations.md`: the
  bullet on test databases and the trigger paragraph of the stalled-launcher
  section describe the new convention; the "until #3376 lands" bullet goes;
  the three amendments (sweep bound from `pg_control_checkpoint()`, the
  identification query needs the superuser, mechanism confirmed by the
  2026-11-25 restart) are applied; section 8 documents the tool. The file
  had two sections numbered 6: the second one ("已知未处置") loses its number,
  so sections 7, 7.1 and 8, which other documents and issues cite, keep
  theirs.
- [ ] 3.3 `docs/runbooks/production-ops/oncall-sql.md` (the integration
  rule), `docs/runbooks/tier-node27-timeseries-storage.md` (the paragraph on
  throwaway-database fixtures), `docs/runbooks/ci-test-routing.md` (the
  node-27 detached-worktree lane gains the `run --` form for integration)
  and `openspec/project-profile.md` (the "node-27 isolated real-DB pytest"
  oracle line): the integration convention on node-27 names the tool and no
  longer describes throwaway databases on the production server.
- [ ] 3.4 Root instruction source `instructions/agents/shared.md` (the
  oracle routing row, the development loop line and the "iteration oracle"
  bullet keep their meaning; one bullet under the node-27 rules states the
  tool and the refusal) and the generated instruction files regenerated
  from the sources, not edited by hand; the tests that pin instruction or
  runbook text pass (`tests/test_python_environment_truth.py`,
  `tests/test_node22_entrypoint_invariant.py`,
  `tests/test_entropy_audit_scan_boundaries.py`).

## 4. Tests

New file `tests/test_node27_integration_pg.py`. The tool is run as a
subprocess under `umask 022` with a fake `docker` first on `PATH`: an
executable that appends its argument vector (one JSON line per call) to a
log file and answers per call from a table keyed on the leading arguments
(inspect of `nhms-db`, inspect of `nhms-it-db`, the passwd/group dump,
`run -d`, `exec`, `rm`), each entry giving stdout and exit code; a test sets
the table it needs. No test starts a real container. Roots are under
`tmp_path`.

A finaliser used by every test of the file asserts over the whole call log
that each call naming `nhms-db` is exactly `inspect nhms-db --format
{{.Image}}`.

- [ ] 4.1 `start`, happy path: the `docker run -d` call has `--name
  nhms-it-db`, the label with the resolved root, the image id returned by
  the inspect of `nhms-db`, `--user` with the caller's uid and gid, `-p
  127.0.0.1:55442:5432`, the three binds, the memory, CPU, shm and log
  limits, `--env-file`, and every `-c` setting of the design; no
  `--restart`. The sentinel names the container, port and image. The root
  and `pgdata` have mode 0700 and `postgres.env` 0600. The rewritten passwd
  and group rows carry the caller's uid and gid. The readiness call is an
  `exec` whose script contains `-h 127.0.0.1`.
- [ ] 4.2 Secrecy: across every recorded docker call of `start` and across
  the tool's stdout and stderr, the password read back from `postgres.env`
  does not occur. The same for `run`, `status` and `stop`.
- [ ] 4.3 Refusals of `start`, each with a non-zero exit, a message, no
  `docker run -d` recorded and nothing created or changed: `--port 55432`;
  a relative root; a non-empty root without the sentinel (its content is
  byte-for-byte unchanged afterwards); inspect of `nhms-db` failing; a
  running `nhms-it-db` whose label names another directory; a running one
  with the right label but no sentinel; the lock held by another process.
- [ ] 4.4 Idempotence and stale state: with the fake reporting a running
  `nhms-it-db` with the label and the sentinel present, `start` exits 0
  without a `docker run -d`; with an exited one, `start` records `rm -f
  nhms-it-db` and then a fresh run, and the old directory content is gone.
- [ ] 4.5 Failure cleanup, each with a non-zero exit, `rm -f nhms-it-db`
  recorded when the container had been created, and the directory gone: the
  passwd dump fails (the fake records the root's file listing at the moment
  it answers the dump call: the sentinel is present and `postgres.env` is
  absent, so the secret never sits in an unmarked directory); `docker run
  -d` fails; readiness never succeeds (the
  timeout is shortened through an option documented as existing for tests);
  the collation reply is not the expected pair.
- [ ] 4.6 `stop`: refuses with exit 3 and removes nothing while another
  process holds the shared lock; with `--force`, or with the lock free,
  records `rm -f nhms-it-db` and removes the directory; with a directory
  lacking the sentinel, leaves it; with a container whose label names
  another directory, records no `rm` and exits non-zero; with the root
  being a symbolic link to a directory that carries a sentinel, removes
  neither the link target nor its content; with a symbolic link inside the
  root pointing to a directory elsewhere under `tmp_path`, that directory
  and its content still exist afterwards.
- [ ] 4.7 `run`, executed with `PYTHONUNBUFFERED=1` so that anything the
  tool prints before replacing itself is seen: the command (a small script
  that dumps its environment to a file and then tries the exclusive lock)
  sees `NHMS_RUN_INTEGRATION=1` and an `NHMS_INTEGRATION_DATABASE_URL` with
  host `127.0.0.1`, the sentinel's port, user `postgres` and the password of
  `postgres.env`; `NHMS_ALLOW_DATABASE_URL_INTEGRATION` set in the parent is
  absent in the child; the exclusive lock is not available to the command
  while it runs (the shared lock was inherited); the tool's own stdout and
  stderr are empty; the command's exit code is the tool's. Without an
  instance, and with the lock held exclusively: non-zero, the command not
  executed.
- [ ] 4.8 `status`: running, stopped and absent give the three outputs and
  exit codes; no secret in the output.
- [ ] 4.9 Tripwire, added to `tests/test_integration_gate.py` (existing
  tests unedited), with the environment set through `monkeypatch`
  (`NHMS_*`, `DATABASE_URL` and `PGPORT` removed first):
  - the function raises `pytest.UsageError` for a URI with port 55432, a
    keyword DSN with `port=55432`, a comma list containing it, and a URI
    without a port while `PGPORT=55432`; it returns for port 5432, for
    55442, for a URI without a port and without `PGPORT`, and for an
    unparsable string; the message contains neither the URL's password nor
    its host;
  - `conftest.pytest_configure` called with a stub config (the way
    `tests/test_conftest_grib_preflight.py` does): raises with the opt-in
    and a dedicated URL at 55432; returns without the opt-in; returns with
    the opt-in and no URL; with the generic `DATABASE_URL` at 55432 it
    returns without the compatibility flag and raises with it;
  - one subprocess run with the interpreter running the suite
    (`[sys.executable, "-m", "pytest", "tests/test_integration_gate.py",
    "--collect-only", "-q", "-p", "no:cacheprovider"]`, `cwd` the repository
    root), its environment being `os.environ` without every `NHMS_*`,
    `DATABASE_URL` and `PGPORT`, plus the variables of the case: exit code 4
    and nothing collected with the opt-in and a 55432 URL; exit code 0
    without them.

- [ ] 4.10 (review) `start` with only a stale container, and with only a
  stale directory; refusal cases assert the reason in the message and the
  foreign-label case can be refused by the label check alone; `--root /`
  refused; `run` refused for a container labelled for another directory; a
  round on port 55443 (URL and status use the sentinel's port); the four
  cases of 1.7; an inspect failure that is not "no such container" removes
  nothing on `stop`. The fake's `run -d` fails on a name clash, and the
  finaliser also fails on any call the fake did not know.

## 5. Evidence

- [ ] 5.1 Red first, recorded in `.workplans/3376/red-runs.log`: 4.9's
  refusal cases fail against the unmodified `conftest.py`.
- [ ] 5.2 Mutations, each applied by an exact-replacement script kept under
  `.workplans/3376/`, run against the named test, the failing test and
  location recorded, restored and compared:
  - M1 the port refusal of `start` removed -> 4.3 red;
  - M2 the sentinel check before directory removal removed -> 4.3 / 4.6 red
    (foreign directory content changed or gone);
  - M3 the password passed with `-e POSTGRES_PASSWORD=...` instead of the
    env file -> 4.2 red;
  - M4 `run` prints the URL -> 4.7 red;
  - M5 `stop` does not take the lock -> 4.6 red;
  - M6 the collation check accepts anything -> 4.5 red;
  - M7 the env file created with the default mode -> 4.1 red;
  - M8 `run` keeps `NHMS_ALLOW_DATABASE_URL_INTEGRATION` -> 4.7 red;
  - M9 the tripwire reads only `NHMS_INTEGRATION_DATABASE_URL` -> 4.9 red
    (compatibility-flag case);
  - M10 the tripwire fires whenever `NHMS_RUN_INTEGRATION` is set, URL or
    not, and for a session that has not opted in -> 4.9 red;
  - M11 the label check before container removal removed -> 4.3 / 4.6 red;
  - M12 the inspect of `nhms-db` without `--format` -> the finaliser red;
  - M13 the env file written before the sentinel and before the passwd
    dump -> 4.5 red (the listing recorded at the dump call);
  - M14 the directory function resolves the root before checking and
    removing it -> 4.6 red (symbolic-link root);
  - M15 the tripwire ignores `PGPORT` -> 4.9 red;
  - M16-M24 (review): stale container or stale directory removed only when
    both are stale; the label check of `start` removed; `run` not comparing
    the label; a root of `/` accepted; `run` using the default port instead
    of the sentinel's; the image comparison and the readiness probe of 1.7
    removed; "absent" concluded from any inspect failure. Each is red at a
    test added for it (4.10).
- [ ] 5.3 Local: the two test files; `tests/test_integration_gate.py`
  whole; `tests/test_conftest_grib_preflight.py`; the governance tests the
  new script or the changed documents touch (named in the report);
  `uv run --no-sync ruff check --no-cache .`; `openspec validate
  node27-integration-throwaway-postgres --strict --no-interactive`; markdown
  lint of the edited documents; the repository entropy hard gate at its
  baseline; the production topology hard gate.
- [ ] 5.4 node-27, live, in a temporary detached worktree of the PR head
  (receipt on the PR; before posting, the receipt is searched for the
  instance password: zero hits):
  - before: `docker inspect nhms-db --format {{.State.StartedAt}}`,
    `df -h / /home`, production `max(last_autovacuum)`, production count of
    `nhms_it_%` databases, the UTC start time;
  - `start` (duration recorded), `status`;
  - `--collect-only -m integration` count on that commit;
  - through `run`: the whole integration selection, `pytest -m integration
    -rs`: passed plus xfailed plus the listed skips for other reasons (the
    node-27 docker oracle opt-in, the real-basins precondition, runtime
    skips of single tests) equals the collected count, and no skip names
    `NHMS_RUN_INTEGRATION` or `NHMS_INTEGRATION_DATABASE_URL`;
  - the instance's log shows `CREATE DATABASE` / `DROP DATABASE` for
    `nhms_it_` names, count above zero; `docker logs --since <start> nhms-db`
    contains no `nhms_it_`;
  - with the opt-in and a URL at port 55432 with a dummy password, pytest
    ends with the usage error (exit code 4);
  - `stop`; the container and the directory are gone;
  - after: `StartedAt` unchanged, `df` back within noise, production
    `max(last_autovacuum)` advanced, production `nhms_it_%` count unchanged.

Deviation from the issue's `Verification:`, stated in the PR: "the pass count
equals the count before the change" is replaced by "passed, xfailed and the
listed skips for other reasons add up to the collected count, with no skip
for the integration opt-in", because a baseline run on the production
postmaster would recreate the race this change removes.

Owed on #3376 after merge, not a merge gate: the next natural governance
tick on node-27 does not report `AUTOVACUUM_OUTPUT_STALLED`.
