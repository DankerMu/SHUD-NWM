# Design

## Layout on node-27

```text
/home/nwm/nhms-it-pg/            instance directory (default; --root overrides), mode 0700
  .nhms-it-pg.json               sentinel, written first: {"container": "nhms-it-db", "port": N, "image": "sha256:..."}
  postgres.env                   0600: POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB, PGDATA, TIMESCALEDB_TELEMETRY
  passwd, group                  the image's files with the postgres rows rewritten to the caller's uid and gid
  pgdata/                        bind mount for the cluster, mode 0700
/home/nwm/nhms-it-pg.lock        lock file beside the directory (it outlives it): shared by every run,
                                 exclusive for start and stop
```

The instance directory is on `/home` because the docker root is on the 26 GB
root volume. The cluster is owned by the calling user, so `stop` can remove
it without privilege; `initdb` needs a passwd entry for that uid, which the
image does not have, hence the two rewritten files (the recipe
`tests/test_node27_pgdata_migrate_oracle.py` already uses).

The sentinel is written before anything else, so a start that dies half-way
leaves a directory the next `start` or `stop` recognises as its own; in
particular the env file with the password never sits in an unmarked
directory.

## Constants

| Name | Value | Why |
|---|---|---|
| container | `nhms-it-db` | fixed; never taken from input |
| label | `nhms.integration-pg=<resolved instance directory>` | proves the container is the tool's, and whose |
| production container | `nhms-db` | only ever inspected, and only for its image id |
| production port | `55432` | refused as the instance port |
| default port | `55442` | free on node-27 on 2026-11-25; `--port` overrides |
| PGDATA in the container | `/home/postgres/pgdata/data` | the image's layout |
| expected collation, encoding | `C.UTF-8`, `UTF8` | measured on production |

## The two removal functions

- **Container.** One function removes the container. It takes no name: it
  inspects `nhms-it-db` with `--format` for its label, does nothing when the
  container is absent, refuses (non-zero, nothing removed) when the label is
  missing or names another directory than the resolved root, and otherwise
  runs `docker rm -f nhms-it-db`.
- **Directory.** One function removes the instance directory. It refuses a
  path that is a symbolic link, requires the sentinel as a regular file
  inside it, and removes the tree without following links.

No other statement of the tool removes, stops or kills a container or
removes a directory.

## Subcommands

### `start [--root DIR] [--port N]`

1. Refuse a port equal to 55432 or outside 1024..65535; refuse a root that
   is not absolute.
2. Image id from `docker inspect nhms-db --format {{.Image}}`: the only
   call the tool ever makes that names the production container. Failure:
   exit non-zero, nothing created.
3. Existing `nhms-it-db` (inspected with `--format` for state and label):
   - label missing or naming another directory: refuse, nothing changed;
   - running, and the sentinel and env file exist: print that it is running,
     exit 0 (idempotent);
   - running without them: refuse and say so;
   - not running: remove it through the container function, continue as a
     fresh start.
4. Root directory: absent or empty is fine; non-empty with the sentinel is a
   stale instance and is removed through the directory function; non-empty
   without the sentinel is refused and left untouched.
5. Take the lock exclusively and without blocking, and hold it until
   `start` returns; if it is held (a `run` is alive, or another `start` or
   `stop` is at work), refuse and change nothing. Steps 3 and 4 are
   evaluated again under the lock when they would remove something. From
   here on, any failure removes the container (if created) and the
   directory through the two functions before exiting non-zero.
   Create the directory (0700), the sentinel, `pgdata` (0700); read the image's `/etc/passwd` and `/etc/group` with a one-shot
   container (`run --rm --network none --entrypoint /bin/sh`), rewrite the
   `postgres` rows, write both files; generate the password
   (`secrets.token_hex(24)`), write `postgres.env` created exclusively with
   mode 0600.
6. `docker run -d --name nhms-it-db --label nhms.integration-pg=<root>`
   `--user <uid>:<gid> --env-file <root>/postgres.env`
   `-p 127.0.0.1:<port>:5432`
   `-v <root>/pgdata:/home/postgres/pgdata/data:rw`
   `-v <root>/passwd:/etc/passwd:ro -v <root>/group:/etc/group:ro`
   `--memory 8g --cpus 8 --shm-size 1g`
   `--log-opt max-size=20m --log-opt max-file=2`
   `<image id> postgres` followed by `-c` pairs for:
   `shared_preload_libraries=timescaledb`, `max_connections=100`,
   `max_locks_per_transaction=512`, `max_worker_processes=59`,
   `timescaledb.max_background_workers=16`, `timezone=Etc/UTC`,
   `log_statement=ddl`, `fsync=off`, `synchronous_commit=off`,
   `full_page_writes=off`.
   `log_statement=ddl` makes the instance's own log show every
   `CREATE DATABASE` / `DROP DATABASE`, which is the positive half of the
   evidence that integration ran there.
7. Wait for readiness, at most 120 s, **over TCP**: the entrypoint's
   temporary init server answers on the Unix socket but not on TCP, so only
   the final server can satisfy the probe. The probe is
   `docker exec nhms-it-db sh -c '<script>'`, where the fixed script runs
   `psql -X -At -h 127.0.0.1 -p 5432` as `$POSTGRES_USER` with
   `PGPASSWORD="$POSTGRES_PASSWORD"` taken from the container's own
   environment (the argument list carries no secret) and returns
   `datcollate` and the encoding of `template1`. A value other than the
   expected pair, or a timeout, is a failure (step 5 cleanup).
8. Print the container name, port, short image id and directory. Nothing
   else.

### `run [--root DIR] -- <command...>`

Requires the sentinel, the env file and a running container carrying the
label for this root. Opens the lock file, takes a shared `flock` without
blocking (an exclusive holder means a `start` or `stop` is at work: exit
non-zero, the command not executed), and marks the descriptor inheritable, so the lock lives as long as the command and its
children. Reads the password from the env file by parsing lines (the file is
never sourced by a shell), builds
`postgresql://postgres:<password>@127.0.0.1:<port>/postgres`, and replaces
itself with the command (`os.execvpe`) with `NHMS_RUN_INTEGRATION=1` and
`NHMS_INTEGRATION_DATABASE_URL` set and
`NHMS_ALLOW_DATABASE_URL_INTEGRATION` removed from the child's environment.
It prints nothing. The password is hexadecimal, so the URL needs no
escaping. The exit code is the command's, because the tool has become the
command.

### `status [--root DIR]`

Prints one line: absent / stopped / running, and when running the port and
the short image id from the sentinel. Exit 0 when running, 3 otherwise.
Never prints the password or a URL.

### `stop [--root DIR] [--force]`

1. Take the lock exclusively and without blocking. If it is held (a `run`
   is alive, or a `start` is at work): without `--force` refuse, exit 3,
   nothing removed; with `--force` go on without the lock.
2. Remove the container through the container function.
3. Remove the directory through the directory function; a directory without
   the sentinel is left and reported.
4. Nothing there at all: exit 0 and say so.

## The tripwire in `tests/conftest.py`

```text
_refuse_production_postmaster(url):
    try: parsed = psycopg2.extensions.parse_dsn(url)    # URI and keyword forms
    except Exception: return                            # unparsable: today's behaviour, and its text is never printed
    ports = parsed.get("port") or os.environ.get("PGPORT") or "5432"
    if "55432" in ports.split(","):                     # a multi-host list is a comma list
        raise pytest.UsageError(<message naming the tool and #3376; no part of the URL>)

pytest_configure(config):
    ... existing marker lines ...
    if _integration_skip_reason() is None:              # the session would run integration tests
        _refuse_production_postmaster(_integration_database_url())
```

"Opted in" is exactly the condition under which integration tests would not
be skipped, so the tripwire cannot fire for a session that would not have
connected. The many unit tests that mention `55432` as a literal are
unaffected: they do not opt in.

The usage error ends the session before collection. A skip would be wrong:
a skipped integration run reads as a green run.

## Race and failure table

| Situation | Behaviour |
|---|---|
| `start` twice in a row | second prints "running", exit 0 |
| `start` after a host reboot (container exited, directory present) | container removed, directory removed by sentinel, fresh start |
| `start` with a foreign non-empty directory | refused, untouched |
| `start` meets a container named `nhms-it-db` without the label for this root | refused, untouched |
| `start` when the port is taken | `docker run` fails; step 5 cleanup; docker's message reported |
| any failure after the directory was created | container and directory removed, exit non-zero |
| `stop` while another session's `run` is alive | refused (lock), unless `--force` |
| `stop --root B` while the container belongs to root A | container refused (label); directory B handled by its own sentinel |
| `stop` when nothing exists | exit 0, says nothing was there |
| `run` without an instance | exit non-zero before executing the command |
| two `start` at the same moment | the second cannot take the lock and refuses, changing nothing |
| `start` while a `run` of a crashed instance is still alive | refused (lock) until that command ends |
| production container absent | `start` fails closed; `stop` and `status` still work |

## Alternatives rejected

- **A second database user or schema on the production postmaster.** The
  race is between `DROP DATABASE` and the launcher of that postmaster; only
  another postmaster removes it.
- **A template database and a pool of reusable test databases.** It would
  cut the number of drops, not remove the race, and changes the isolation
  every integration test relies on.
- **Lowering `autovacuum_naptime` back to 60 or an automatic launcher
  restart** (options B and C of #3376): both keep the trigger.
- **Compose file or systemd unit.** The production container is a bare
  `docker run` by decision (see the container runbook); an on-demand tool
  with `start`/`stop` is the smaller thing, and nothing needs the instance
  to survive a reboot.
- **Blocking port 55432 in the tool only.** The tool cannot see a URL an
  operator exports by hand; the tripwire in `conftest.py` can.
- **Refusing `stop` on open connections.** The fixtures connect only around
  `CREATE DATABASE` and `DROP DATABASE`, so a session in mid-run usually
  shows none.
