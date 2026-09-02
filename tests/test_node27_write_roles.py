"""Guards for the node-27 write-path least-privilege roles (issue #1774).

Two families, matching the two halves of the change:

* **Repo guards** over ``db/roles/node27_write_roles.sql`` and
  ``infra/env/*.example`` -- the drift this issue is actually about was a
  template that named a role nobody had ever provisioned, so the guards tie the
  template, the SQL and the statically scanned call sites to each other.
* **Runner behaviour** for ``scripts/node27_provision_write_roles.sh``, driven
  by a fake ``docker`` on ``PATH``. That covers the three paths that cannot be
  reproduced in the disposable container: ``--roles-only`` really is additive,
  the ownership retry passes exhaust into a non-zero, audit-visible partial
  transfer, and a change in the display role's SELECT set blocks the cutover.

Live privilege behaviour (compress/decompress/drop_chunks/ANALYZE/SET
TABLESPACE under the role, COPY ... FROM PROGRAM refused) is measured against a
real TimescaleDB 2.10.2/PG15 container, not mocked here; the transcript is
``openspec/changes/node27-write-path-roles/evidence/local-container-transcript.md``.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SQL_PATH = _ROOT / "db" / "roles" / "node27_write_roles.sql"
_RUNNER_PATH = _ROOT / "scripts" / "node27_provision_write_roles.sh"
_ENV_DIR = _ROOT / "infra" / "env"

_INGEST_ROLE = "nhms_ingest_rw"
_DOWNLOAD_ROLE = "nhms_download_rw"
_SUPERUSER = "nhms"

_APP_SCHEMAS = ("core", "hydro", "met", "ops", "map", "flood")

# The only runtime env templates allowed to name the superuser, each for a
# reason recorded in the OpenSpec design (D5) and in the template itself:
# migration-class work `nhms_ingest_rw` structurally cannot carry.
_SUPERUSER_ALLOW_LIST = {
    # supervisor runs pg_dump / `psql --file <migration>` / pg_restore
    "node27-timeseries-compression-replay.example",
    # POSTGRES_ADMIN_URL needs CREATEDB against the `postgres` database
    "node27-archive-rebuild-drill.example",
}

# Recurring node-27 unit entrypoints whose privilege needs this change encodes.
_RECURRING_ENTRYPOINTS = (
    "scripts/node27_autopipeline.py",
    "scripts/node27_download_cycles.py",
    "scripts/node27_timeseries_compression.py",
    "scripts/node27_timeseries_retention.py",
    "scripts/node27_cold_residency.py",
)


@pytest.fixture(scope="module")
def sql_text() -> str:
    return _SQL_PATH.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _psql_section(text: str, variable: str) -> str:
    r"""Body of the ``\if :<variable>`` block, honouring nested ``\if``.

    The file gates its phases on psql variables; every assertion about "the
    ownership loop" has to be scoped to that block, or the ``DO $roles$`` block
    in the additive phase would satisfy a "no DO block" check by accident.
    """
    lines = text.splitlines()
    opener = rf"\if :{variable}"
    start = next((i for i, line in enumerate(lines) if line.strip() == opener), None)
    assert start is not None, f"{opener} not found in {_SQL_PATH}"
    depth = 0
    for index in range(start, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("\\if"):
            depth += 1
        elif stripped == "\\endif":
            depth -= 1
            if depth == 0:
                return "\n".join(lines[start + 1 : index])
    raise AssertionError(f"unterminated {opener} in {_SQL_PATH}")


def _sql_code(text: str) -> str:
    """Drop whole-line ``--`` comments.

    Structural assertions ("no DO block in the loop", "no literal schema list")
    must read the executable text; this file's own prose explains exactly the
    shapes those assertions forbid, and would otherwise trip them.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("--")
    )


def _env_templates() -> list[Path]:
    return sorted(_ENV_DIR.glob("*.example"))


def _superuser_credential_hits(text: str) -> list[str]:
    """Every way a template can name the superuser as a credential."""
    hits: list[str] = []
    hits += re.findall(rf"postgresql://{_SUPERUSER}[:@]\S*", text)
    hits += re.findall(rf"(?m)^\s*PGUSER={_SUPERUSER}\s*$", text)
    return hits


def _dsn_users(text: str) -> set[str]:
    return set(re.findall(r"(?m)^[A-Z0-9_]*DATABASE_URL=postgresql://([A-Za-z0-9_]+):", text))


def _grep_repo(pattern: str, roots: tuple[str, ...]) -> list[str]:
    """Static scan (design D3): the inventory is derived, never assumed."""
    matches: list[str] = []
    compiled = re.compile(pattern)
    for root in roots:
        base = _ROOT / root
        paths = [base] if base.is_file() else sorted(base.rglob("*.py"))
        for path in paths:
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                found = compiled.search(line)
                if found:
                    matches.append(found.group(0))
    return matches


# --------------------------------------------------------------------------- #
# Scenario: Runtime env files carry no superuser
# --------------------------------------------------------------------------- #
def test_env_templates_name_no_superuser_credential_outside_the_allow_list() -> None:
    offenders = {
        path.name: _superuser_credential_hits(path.read_text(encoding="utf-8"))
        for path in _env_templates()
        if path.name not in _SUPERUSER_ALLOW_LIST
        and _superuser_credential_hits(path.read_text(encoding="utf-8"))
    }
    assert offenders == {}, (
        "these runtime env templates name the superuser `nhms` as a credential; "
        f"only {sorted(_SUPERUSER_ALLOW_LIST)} may: {offenders}"
    )


def test_the_allow_listed_exception_actually_names_the_superuser() -> None:
    """A vacuous allow-list would silently stop guarding anything."""
    present = [
        name for name in _SUPERUSER_ALLOW_LIST if (_ENV_DIR / name).exists()
    ]
    assert present, "no allow-listed template exists; the allow-list has gone stale"
    for name in present:
        text = (_ENV_DIR / name).read_text(encoding="utf-8")
        assert _superuser_credential_hits(text), (
            f"{name} is allow-listed as a superuser lane but names no superuser "
            "credential -- drop it from the allow-list instead of carrying it dead"
        )
        assert "#1774" in text, f"{name} must carry the recorded reason for the exception"


def test_node27_writer_templates_name_exactly_the_provisioned_roles(sql_text: str) -> None:
    template_roles: set[str] = set()
    for path in _ENV_DIR.glob("node27-*.example"):
        if path.name in _SUPERUSER_ALLOW_LIST:
            continue
        template_roles |= {
            user for user in _dsn_users(path.read_text(encoding="utf-8"))
            if user != "nhms_display_ro"
        }
    assert template_roles == {_INGEST_ROLE, _DOWNLOAD_ROLE}, template_roles

    sql_roles = set(re.findall(r"nhms_(?:ingest|download|control|tiering)_rw", sql_text))
    assert sql_roles == template_roles, (
        "the provision SQL and the node-27 writer templates must name the same "
        f"roles; SQL={sorted(sql_roles)} templates={sorted(template_roles)}"
    )


def test_no_template_still_carries_the_unprovisioned_writer_placeholder() -> None:
    offenders = [
        path.name
        for path in _env_templates()
        if "REPLACE_ME_WRITER" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], (
        "REPLACE_ME_WRITER is what made the deployment fall back to the superuser; "
        f"name the provisioned role instead: {offenders}"
    )


# --------------------------------------------------------------------------- #
# Scenario: Provision is idempotent  /  role flag contract
# --------------------------------------------------------------------------- #
def test_role_creation_is_existence_guarded_and_negates_every_privilege_flag(sql_text: str) -> None:
    roles_section = _psql_section(sql_text, "do_roles")
    assert "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_role)" in roles_section

    flags = "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
    # once on the CREATE branch, once on the ALTER branch: a re-run has to
    # converge a hand-edited role back to the committed flags.
    assert roles_section.count(flags) == 2, roles_section

    assert re.search(
        r"CREATE ROLE (?!%I)", sql_text
    ) is None, "roles must be created through format(%I) inside the guarded DO block"


def test_passwords_are_read_from_the_environment_and_never_committed(sql_text: str) -> None:
    for var, role in (
        ("NODE27_INGEST_RW_PASSWORD", _INGEST_ROLE),
        ("NODE27_DOWNLOAD_RW_PASSWORD", _DOWNLOAD_ROLE),
    ):
        assert re.search(rf"\\getenv \w+ {var}\b", sql_text), (
            f"{var} must reach psql through \\getenv, i.e. through the process "
            "environment rather than an argument vector"
        )
        assert f"ALTER ROLE {role} PASSWORD :'" in sql_text, (
            "the password must arrive as a psql variable from \\getenv, never as a literal"
        )
    assert "PASSWORD '" not in sql_text, "no password literal may live in the repo"
    # psql echoes the offending query text on error unless verbosity is clamped.
    assert "\\set VERBOSITY terse" in sql_text
    assert "\\set SHOW_CONTEXT never" in sql_text


# --------------------------------------------------------------------------- #
# Scenario: Display grants survive the ownership transfer (SQL side)
# --------------------------------------------------------------------------- #
def test_ownership_loop_covers_the_six_schemas_for_every_relkind(sql_text: str) -> None:
    section = _sql_code(_psql_section(sql_text, "do_ownership"))
    blocks = [block for block in section.split("\\gexec") if "ALTER" in block]
    assert len(blocks) == 4, "expected one \\gexec block per relkind family (r/p, S, v, m)"

    expected_commands = (
        "ALTER TABLE %I.%I OWNER TO nhms_ingest_rw",
        "ALTER SEQUENCE %I.%I OWNER TO nhms_ingest_rw",
        "ALTER VIEW %I.%I OWNER TO nhms_ingest_rw",
        "ALTER MATERIALIZED VIEW %I.%I OWNER TO nhms_ingest_rw",
    )
    for block, command in zip(blocks, expected_commands, strict=True):
        assert command in block, command
        for schema in _APP_SCHEMAS:
            assert f"'{schema}'" in block, f"{schema} missing from the {command!r} block"


def test_ownership_loop_transfers_tables_before_sequences(sql_text: str) -> None:
    """An OWNED BY sequence follows its table; a standalone ALTER on it errors."""
    section = _sql_code(_psql_section(sql_text, "do_ownership"))
    tables_at = section.index("c.relkind IN ('r', 'p')")
    sequences_at = section.index("c.relkind = 'S'")
    assert tables_at < sequences_at


def test_ownership_loop_is_not_one_transaction(sql_text: str) -> None:
    """AccessExclusiveLock per relation vs. the unstoppable public display API."""
    section = _sql_code(_psql_section(sql_text, "do_ownership"))
    assert "DO $" not in section, "the loop must not be wrapped in a DO block"
    assert not re.search(r"(?mi)^\s*(BEGIN|START TRANSACTION|COMMIT)\s*;", section), (
        "the loop must not be wrapped in an explicit transaction"
    )
    assert section.count("\\gexec") == 4, "each statement must be autocommitted via \\gexec"
    assert "SET lock_timeout = '5s'" in section
    # A failed relation must not abandon the rest of the pass.
    assert "\\unset ON_ERROR_STOP" in section


def test_audit_asserts_the_display_role_select_set_and_the_hypertable_owners(sql_text: str) -> None:
    section = _psql_section(sql_text, "do_audit")
    assert "has_table_privilege(r.oid, c.oid, 'SELECT')" in section
    assert "r.rolname = 'nhms_display_ro'" in section
    assert "timescaledb_information.hypertables" in section
    # drift must be a hard failure in full mode only
    strict = _psql_section(sql_text, "strict_audit")
    assert "RAISE EXCEPTION 'owner drift" in strict


def test_runner_captures_the_display_select_set_before_and_after() -> None:
    runner = _RUNNER_PATH.read_text(encoding="utf-8")
    assert "display_before" in runner and "display_after" in runner
    assert "relacl_before" in runner and "relacl_after" in runner
    assert "has_table_privilege(r.oid, c.oid, 'SELECT')" in runner
    assert "exit 4" in runner, "a display-grant regression must have its own exit code"


# --------------------------------------------------------------------------- #
# Scenario: Migration-added tables stay usable before re-provision
# --------------------------------------------------------------------------- #
def test_default_privileges_cover_every_application_schema_for_ingest(sql_text: str) -> None:
    section = _psql_section(sql_text, "do_roles")
    tables_clause = (
        "ALTER DEFAULT PRIVILEGES FOR ROLE nhms IN SCHEMA %I "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO nhms_ingest_rw"
    )
    sequences_clause = (
        "ALTER DEFAULT PRIVILEGES FOR ROLE nhms IN SCHEMA %I "
        "GRANT USAGE ON SEQUENCES TO nhms_ingest_rw"
    )
    assert tables_clause in section
    assert sequences_clause in section
    for clause in (tables_clause, sequences_clause):
        block = section[section.index(clause) : section.index(clause) + 600]
        for schema in _APP_SCHEMAS:
            assert f"'{schema}'" in block, f"{schema} missing from the default-privilege block"


def test_schema_scoped_grants_are_generated_not_listed(sql_text: str) -> None:
    """`flood` is provisioned outside db/; a literal `IN SCHEMA a, b, flood` aborts."""
    section = _sql_code(_psql_section(sql_text, "do_roles"))
    assert not re.search(r"IN SCHEMA\s+core\s*,", section), (
        "a comma list over the six schemas fails outright when `flood` is absent; "
        "generate the statements from pg_namespace instead"
    )
    assert section.count("FROM pg_namespace n") >= 5


# --------------------------------------------------------------------------- #
# Scenario: Program execution is closed (SQL side)
# --------------------------------------------------------------------------- #
def test_copy_from_program_probe_asserts_both_roles_and_the_right_refusal(sql_text: str) -> None:
    section = _psql_section(sql_text, "do_roles")
    probe = section[section.index("$copy_probe$") :]
    assert f"ARRAY['{_INGEST_ROLE}', '{_DOWNLOAD_ROLE}']" in probe
    assert "COPY node27_copy_program_probe FROM PROGRAM" in probe
    assert "SECURITY REGRESSION" in probe, "a successful COPY must fail the run loudly"
    assert "'%external program%'" in probe, (
        "CREATE TEMP TABLE can also be refused with SQLSTATE 42501; the probe must "
        "match the COPY refusal specifically"
    )
    # the temp table is created outside the guarded block for exactly that reason
    create_at = probe.index("CREATE TEMP TABLE node27_copy_program_probe")
    guard_at = probe.index("EXCEPTION")
    assert create_at < probe.index("COPY node27_copy_program_probe") < guard_at


def test_forbidden_statements_are_absent(sql_text: str) -> None:
    for forbidden, why in (
        ("REASSIGN OWNED", "moves the database, schemas and extension-adjacent objects"),
        ("GRANT nhms TO", "role membership would hand the writer the superuser back"),
        ("SUPERUSER", "no write role may be created or altered as a superuser"),
    ):
        if forbidden == "SUPERUSER":
            assert not re.search(r"(?<!NO)SUPERUSER", sql_text), why
        else:
            assert forbidden not in sql_text, f"{forbidden}: {why}"


# --------------------------------------------------------------------------- #
# Scenario: Tiering functions run as the hypertable owner (static inventory)
# --------------------------------------------------------------------------- #
def test_download_grants_cover_every_scanned_met_dml_target(sql_text: str) -> None:
    targets = {
        match.split(".")[-1]
        for match in _grep_repo(
            r"(?:INSERT INTO|UPDATE|DELETE FROM)\s+met\.[a-z_]+",
            ("packages", "workers", "scripts", "services"),
        )
    }
    assert len(targets) >= 5, f"the met.* scan collapsed to {targets}; the guard is not biting"
    section = _psql_section(sql_text, "do_roles")
    assert (
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA met "
        f"TO {_DOWNLOAD_ROLE};" in section
    ), (
        f"the download role must cover the whole met schema (scanned targets: "
        f"{sorted(targets)})"
    )
    assert f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA met TO {_DOWNLOAD_ROLE};" in section


def test_scanned_set_tablespace_sites_have_a_matching_tablespace_grant(sql_text: str) -> None:
    sites = _grep_repo(r"SET TABLESPACE", ("packages/common/compressed_chunk_cold_residency.py",))
    assert sites, "the cold-residency lane no longer issues SET TABLESPACE; re-derive the grant"
    section = _psql_section(sql_text, "do_roles")
    assert f"GRANT CREATE ON TABLESPACE %I TO {_INGEST_ROLE}" in section
    assert "t.spcname = 'nhms_cold'" in section, "the grant must be conditional on the tablespace"


def test_migration_class_tooling_stays_inside_the_allow_listed_lane() -> None:
    """pg_dump / pg_restore / `psql --file` is what keeps the replay lane on `nhms`."""
    offenders = [
        entrypoint
        for entrypoint in _RECURRING_ENTRYPOINTS
        if _grep_repo(r"pg_dump|pg_restore|psql --file|--file <migration>", (entrypoint,))
    ]
    assert offenders == [], (
        "a recurring lane grew migration-class tooling; it cannot run as "
        f"{_INGEST_ROLE} any more: {offenders}"
    )
    supervisor = _grep_repo(
        r"pg_dump|pg_restore", ("scripts/node27_timeseries_compression_supervisor.py",)
    )
    assert supervisor, (
        "the replay supervisor no longer runs pg_dump/pg_restore; its superuser "
        "exception must be re-justified or removed"
    )


def test_stats_guard_analyze_legs_still_exist_and_justify_ownership() -> None:
    autopipe = (_ROOT / "scripts" / "node27_autopipeline.py").read_text(encoding="utf-8")
    assert "_STATS_GUARD_AUTHORITY_CANDIDATES_SQL" in autopipe, "the #1468 leg vanished"
    assert re.search(r"cur\.execute\(f'ANALYZE \"\{schema\}\"\.\"\{name\}\"'\)", autopipe), (
        "the ANALYZE call site moved; ownership is granted because of it"
    )
    assert 'entry["status"] = "ok" if refreshed else "warning"' in autopipe, (
        "a skipped non-owner ANALYZE is graded `warning`, i.e. tick green / leg dead -- "
        "that grading is the reason ownership is not optional"
    )


# --------------------------------------------------------------------------- #
# Runner behaviour, against a fake `docker` on PATH
# --------------------------------------------------------------------------- #
_FAKE_DOCKER = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
phase=""
for arg in "$@"; do
  case "$arg" in
    phase=*) phase="${arg#phase=}" ;;
  esac
done
cat >> "$FAKE_DOCKER_STDIN_LOG" 2>/dev/null || true
case "$phase" in
  remaining)
    printf '%s\n' "${FAKE_REMAINING:-0}"
    ;;
  display_before)
    printf 'core.basin\nhydro.river_timeseries\n'
    ;;
  display_after)
    if [ -n "${FAKE_DISPLAY_DRIFT:-}" ]; then
      printf 'core.basin\n'
    else
      printf 'core.basin\nhydro.river_timeseries\n'
    fi
    ;;
  relacl_before)
    printf 'core.basin | r | nhms | nhms=arwdDxt/nhms nhms_display_ro=r/nhms\n'
    ;;
  relacl_after)
    printf 'core.basin | r | nhms_ingest_rw | nhms_ingest_rw=arwdDxt/nhms_ingest_rw nhms_display_ro=r/nhms_ingest_rw\n'
    ;;
  audit)
    printf '## audit: owner drift -- relations NOT owned by nhms_ingest_rw\n'
    if [ "${FAKE_REMAINING:-0}" != "0" ]; then
      printf 'hydro.river_timeseries | r | nhms\n'
      printf 'ERROR:  owner drift: %s application relation(s) not owned by nhms_ingest_rw\n' \
        "${FAKE_REMAINING}" >&2
      exit 3
    fi
    ;;
esac
exit 0
"""


@pytest.fixture()
def runner(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "docker"
    fake.write_text(_FAKE_DOCKER, encoding="utf-8")
    fake.chmod(0o755)
    log = tmp_path / "docker-argv.log"
    stdin_log = tmp_path / "docker-stdin.log"
    log.touch()
    stdin_log.touch()

    def run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        child = dict(os.environ)
        child.pop("NODE27_INGEST_RW_PASSWORD", None)
        child.pop("NODE27_DOWNLOAD_RW_PASSWORD", None)
        child["PATH"] = f"{bin_dir}{os.pathsep}{child['PATH']}"
        child["FAKE_DOCKER_LOG"] = str(log)
        child["FAKE_DOCKER_STDIN_LOG"] = str(stdin_log)
        child.update(env or {})
        return subprocess.run(
            ["bash", str(_RUNNER_PATH), *args],
            capture_output=True,
            text=True,
            env=child,
            cwd=str(_ROOT),
        )

    run.argv_log = log  # type: ignore[attr-defined]
    run.stdin_log = stdin_log  # type: ignore[attr-defined]
    return run


def _phases(log: Path) -> list[str]:
    return re.findall(r"phase=(\w+)", log.read_text(encoding="utf-8"))


def test_roles_only_mode_is_additive_and_takes_no_ownership_action(runner) -> None:
    result = runner("--roles-only")
    assert result.returncode == 0, result.stderr
    phases = _phases(runner.argv_log)
    assert phases == ["roles"], phases
    argv = runner.argv_log.read_text(encoding="utf-8")
    assert "do_ownership=off" in argv
    assert "strict_audit=off" in argv
    assert "ownership transfer deferred to the post-merge run" in result.stdout


def test_full_mode_captures_transfers_and_audits(runner) -> None:
    result = runner()
    assert result.returncode == 0, result.stderr + result.stdout
    assert _phases(runner.argv_log) == [
        "display_before",
        "relacl_before",
        "ownership",
        "remaining",
        "display_after",
        "relacl_after",
        "audit",
    ]
    argv = runner.argv_log.read_text(encoding="utf-8")
    assert "do_roles=on -v do_ownership=on" in argv, "pass 1 must also run the additive phase"
    assert "do_ownership=off -v do_audit=on -v strict_audit=on" in argv
    assert "read-side boundary preserved" in result.stdout
    assert "full provision complete; audit clean" in result.stdout


def test_retry_passes_exhaust_into_a_non_zero_audit_visible_partial_transfer(runner) -> None:
    result = runner(env={"FAKE_REMAINING": "2"})
    assert result.returncode == 3, (result.returncode, result.stdout, result.stderr)
    assert _phases(runner.argv_log).count("ownership") == 5, "expected 5 retry passes"
    # the audit ran and its drift listing reached the operator ...
    assert "owner drift" in result.stdout
    # ... and the refusal explains why this is safe and blocks the cutover
    assert "PARTIAL, audit-visible transfer, not a rollback" in result.stderr
    assert "do not cut the env files over" in result.stderr.lower()


def test_retry_passes_stop_early_when_the_transfer_completes(runner) -> None:
    result = runner()
    assert result.returncode == 0
    assert _phases(runner.argv_log).count("ownership") == 1


def test_display_select_set_regression_blocks_the_cutover(runner) -> None:
    result = runner(env={"FAKE_DISPLAY_DRIFT": "1"})
    assert result.returncode == 4, (result.returncode, result.stdout, result.stderr)
    assert "DISPLAY GRANT REGRESSION" in result.stderr
    assert "do not cut the env files over" in result.stderr


def test_passwords_are_forwarded_by_name_and_never_printed(runner) -> None:
    sentinel_ingest = "s3cr3t-ingest-sentinel"
    sentinel_download = "s3cr3t-download-sentinel"
    result = runner(
        "--roles-only",
        env={
            "NODE27_INGEST_RW_PASSWORD": sentinel_ingest,
            "NODE27_DOWNLOAD_RW_PASSWORD": sentinel_download,
        },
    )
    assert result.returncode == 0, result.stderr
    argv = runner.argv_log.read_text(encoding="utf-8")
    for sentinel in (sentinel_ingest, sentinel_download):
        assert sentinel not in result.stdout
        assert sentinel not in result.stderr
        assert sentinel not in argv, "the password must never appear in an argument vector"
    assert "-e NODE27_INGEST_RW_PASSWORD" in argv
    assert "-e NODE27_DOWNLOAD_RW_PASSWORD" in argv


def test_unset_password_is_skipped_rather_than_blanked(runner) -> None:
    result = runner("--roles-only", env={"NODE27_INGEST_RW_PASSWORD": ""})
    assert result.returncode == 0, result.stderr
    argv = runner.argv_log.read_text(encoding="utf-8")
    assert "-e NODE27_INGEST_RW_PASSWORD" not in argv, (
        "`docker exec -e VAR` on an empty value sets an EMPTY password"
    )
    assert "password will be left unchanged" in result.stderr


def test_runner_rejects_bad_arguments(runner) -> None:
    assert runner("--nope").returncode == 2
    assert runner("--max-passes", "0").returncode == 2
    assert runner("--container").returncode == 2


def test_runner_targets_the_production_container_by_default(runner) -> None:
    runner("--roles-only")
    argv = runner.argv_log.read_text(encoding="utf-8")
    assert "exec -i nhms-db psql -U nhms -d nhms" in argv


def test_runner_sends_the_committed_sql_file_on_stdin(runner) -> None:
    runner("--roles-only")
    sent = runner.stdin_log.read_text(encoding="utf-8")
    assert "ALTER TABLE %I.%I OWNER TO nhms_ingest_rw" in sent, (
        "the runner must execute the committed SQL, not an inlined copy"
    )


def test_password_alter_is_not_written_to_the_server_log() -> None:
    """`ALTER ROLE ... PASSWORD` is logged verbatim under log_statement=ddl/all."""
    sql = _sql_code(_SQL_PATH.read_text(encoding="utf-8"))
    suppress = sql.find("SET log_statement = 'none'")
    first_alter = sql.find("ALTER ROLE nhms_ingest_rw PASSWORD")
    restore = sql.find("RESET log_statement")
    last_alter = sql.find("ALTER ROLE nhms_download_rw PASSWORD")
    assert suppress != -1, "logging must be suppressed before any password is sent"
    assert "SET log_min_duration_statement = -1" in sql
    assert suppress < first_alter, "log_statement is still 'ddl'/'all' when the password is sent"
    assert last_alter < restore, "logging is restored before the second password is sent"
    assert "RESET log_min_duration_statement" in sql


# The lanes whose env templates this change switches from the superuser `nhms`
# to `nhms_ingest_rw` / `nhms_download_rw`.  Not the disposable-cluster probe
# (`compressed_chunk_cold_probe/*`), which builds and owns its own container and
# connects as that cluster's own superuser.
_CONVERTED_LANE_SOURCES = _RECURRING_ENTRYPOINTS + (
    "scripts/node27_ingest_run.py",
    "packages/common/compressed_chunk_cold_residency.py",
    "packages/common/compressed_chunk_cold_runtime.py",
    "packages/common/compressed_chunk_cold_tick.py",
    "packages/common/node27_cold_tablespace_integration.py",
)

# Surfaces a superuser reads freely and a plain table owner does not:
# pg_stat_activity/pg_locks show only the caller's own sessions without
# pg_read_all_stats, and schema `pg_toast` has no USAGE granted to PUBLIC.
_SUPERUSER_GATED_READS = re.compile(
    r"pg_stat_activity|pg_locks|pg_toast\.|pg_stat_file|pg_ls_dir|pg_read_file"
    r"|pg_read_binary_file|pg_terminate_backend|pg_cancel_backend|pg_reload_conf"
)


def test_converted_lanes_do_not_read_superuser_gated_catalogs() -> None:
    """A superuser-gated READ degrades silently, unlike a write which errors.

    pg_stat_activity/pg_locks do not raise for a non-superuser -- they return a
    filtered result, so a quiescence or lock-conflict guard would go permanently
    green.  Any new hit here must either be paired with a grant in
    db/roles/node27_write_roles.sql or move to a lane that stays superuser.
    """
    offenders: list[str] = []
    for rel in _CONVERTED_LANE_SOURCES:
        path = _ROOT / rel
        assert path.is_file(), f"lane source moved or was renamed: {rel}"
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue  # prose (e.g. the #1714 application_name attribution notes)
            if _SUPERUSER_GATED_READS.search(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "these lanes now run as a non-superuser and would read a silently filtered "
        f"catalog: {offenders}"
    )
