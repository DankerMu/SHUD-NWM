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

# The only non-internal triggers a clean node-27 catalog carries in the six
# application schemas, all created by db/migrations/000043_canonical_grid_snapshot.sql
# (:127, :187, :209, :234). Keyed on (schema, table, name) because the same
# trigger name on another table is not the migration's trigger.
# The audit follows OWNERSHIP into TimescaleDB's internal schema: the transfer
# hands `_compressed_hypertable_N` and every chunk to the write role, and
# TimescaleDB's process-utility hook runs `CREATE TRIGGER` on a hypertable
# without ever firing a `ddl_command_start` event trigger (measured, transcript
# §15), so the do_roles guard cannot refuse it there. Owner-scoped, so
# TimescaleDB's own superuser-owned catalog tables are never scanned.
_INTERNAL_SCOPE_RE = (
    r"OR \(n\.nspname = '_timescaledb_internal'\s+"
    r"AND c\.relowner IN \('nhms_ingest_rw'::regrole, 'nhms_download_rw'::regrole\)\)"
)

_ALLOWLISTED_TRIGGERS = (
    ("met", "canonical_met_product", "canonical_met_product_grid_definition_uri_match_trg"),
    ("met", "canonical_grid_snapshot", "canonical_grid_snapshot_identity_immutable_trg"),
    ("met", "canonical_grid_cell", "canonical_grid_cell_immutable_trg"),
    ("met", "canonical_grid_cell", "canonical_grid_cell_direct_delete_blocked_trg"),
)

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
#
# `scripts/node27_timeseries_compression_supervisor.py` is deliberately NOT in
# this tuple: its unit
# (`infra/systemd/nhms-node27-timeseries-compression-replay.service:8`) is
# `Type=oneshot` with no `.timer`, i.e. an operator-triggered migration-class
# replay, not a recurring lane. Its full sequence
# (`EXPECTED_COMMAND_SEQUENCE`, :117-128) is pg_dump / pg_restore_version /
# pg_restore_list / two `migration_apply` / `decompress` /
# `compression_dry_run` / `compression_enforce` plus the two benchmarks -- the
# exception is scoped by "one-shot migration-class unit", not by which of those
# calls it makes. It keeps the superuser by documented exception and is guarded
# separately by `test_migration_class_tooling_stays_inside_the_allow_listed_lane`.
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


def test_owner_to_appears_only_inside_the_ownership_section(sql_text: str) -> None:
    """`--roles-only` is additive, and that has to hold for the WHOLE file.

    Scoping the guard to the ``do_ownership`` block would miss an
    ``ALTER … OWNER TO`` in the un-gated preamble (which runs in every mode,
    including the audit-only call) or in ``do_roles`` (which runs under
    ``--roles-only``, the phase whose contract is "takes no relation lock").
    """
    ownership = _psql_section(sql_text, "do_ownership")
    assert ownership in sql_text
    outside = _sql_code(sql_text.replace(ownership, "\n"))
    assert "OWNER TO" not in outside, (
        "an ownership transfer outside the do_ownership block would execute under "
        f"--roles-only and in the audit-only call:\n{outside}"
    )
    assert _sql_code(ownership).count("OWNER TO") == 4, (
        "expected exactly one ALTER … OWNER TO per relkind family inside the loop"
    )


def test_autocommit_is_never_turned_off(sql_text: str) -> None:
    r"""``\set AUTOCOMMIT off`` is session-scoped, so the guard must be file-wide.

    Setting it anywhere -- even inside ``do_roles`` -- would wrap the later
    ``\gexec`` ownership statements into one implicit transaction and reinstate
    exactly the whole-file AccessExclusiveLock this change exists to avoid.
    """
    assert not re.search(r"(?i)\\set\s+AUTOCOMMIT", sql_text), (
        "AUTOCOMMIT is session-scoped; toggling it anywhere in the file batches "
        "the per-relation ALTER … OWNER TO statements into one transaction"
    )
    assert not re.search(r"(?i)\bAUTOCOMMIT\s+off\b", sql_text)


def test_audit_rejects_role_membership_in_both_directions(sql_text: str) -> None:
    """Flag columns alone do not bound the roles, and one direction does not either.

    ``GRANT pg_write_server_files TO nhms_ingest_rw`` leaves every ``pg_roles``
    flag false while restoring ``COPY … FROM`` on server files, and a membership
    in the migration role restores everything. The mirror image is just as bad:
    ``GRANT nhms_ingest_rw TO nhms_display_ro`` leaves the writer holding no
    membership at all while letting the read-only display credential
    ``SET ROLE nhms_ingest_rw`` into the whole write and ownership set.

    Anchored on the executable ``FROM pg_auth_members`` (the prose above it names
    the catalog too, and the flags query a few lines earlier already carries a
    ``WHERE rolname IN`` the role names would satisfy).
    """
    section = _psql_section(_sql_code(sql_text), "do_audit")
    assert "FROM pg_auth_members" in section, (
        "the audit reads only the pg_roles flag columns; a role membership is "
        "invisible to it"
    )
    block = section[section.index("FROM pg_auth_members") :]
    roles_list = f"('{_INGEST_ROLE}', '{_DOWNLOAD_ROLE}')"
    # Both literals, so swapping `m.` for `g.` (one direction, spelled the other
    # way round) turns this red instead of green.
    assert f"WHERE m.rolname IN {roles_list}" in block, (
        "the member direction (GRANT <role> TO a write role) is not audited"
    )
    assert f"OR g.rolname IN {roles_list}" in block, (
        "the grantee direction (GRANT a write role TO <role>) is not audited; "
        "`GRANT nhms_ingest_rw TO nhms_display_ro` would pass the audit"
    )
    assert "RAISE EXCEPTION 'SECURITY REGRESSION: role % is a member of %" in block, (
        "the member direction must RAISE EXCEPTION, not warn"
    )
    assert "RAISE EXCEPTION 'SECURITY REGRESSION: role % has been granted to %" in block, (
        "the grantee direction must RAISE EXCEPTION with its own message, not "
        "reuse the member wording"
    )
    # the check must be outside `\if :strict_audit`, i.e. it also runs under
    # --roles-only and in the audit-only call
    strict = _psql_section(_sql_code(sql_text), "strict_audit")
    assert "pg_auth_members" not in strict, (
        "a membership regression must fail in every mode, not only in the strict audit"
    )


def test_audit_asserts_the_cold_tablespace_create_grant(sql_text: str) -> None:
    r"""The cold-tablespace CREATE grant must be audited, not just emitted.

    The grant in ``do_roles`` is a ``\gexec`` that emits NOTHING when the
    tablespace is absent, so a revoked or never-issued
    ``GRANT CREATE ON TABLESPACE nhms_cold`` was invisible -- the cold-residency
    lane would discover it at its first ``SET TABLESPACE`` instead.
    """
    section = _psql_section(_sql_code(sql_text), "do_audit")
    assert "has_tablespace_privilege" in section, (
        "the audit never checks the cold-tablespace CREATE grant"
    )
    assert re.search(
        rf"has_tablespace_privilege\(\s*'{_INGEST_ROLE}'\s*,\s*'nhms_cold'\s*,\s*'CREATE'\s*\)",
        section,
    ), section
    assert "tablespace nhms_cold absent" in section, (
        "an absent tablespace must be reported loudly, not silently skipped"
    )
    strict = _psql_section(_sql_code(sql_text), "strict_audit")
    assert "has_tablespace_privilege" in strict, (
        "a missing CREATE grant must be a hard failure in full mode"
    )
    assert re.search(
        r"RAISE EXCEPTION 'cold-residency regression: nhms_ingest_rw lacks CREATE", strict
    ), (
        "the strict leg must RAISE EXCEPTION; a WARNING here would let the "
        "cutover proceed with the cold-residency lane already broken"
    )
    non_strict = section.replace(strict, "\n")
    assert re.search(
        rf"has_tablespace_privilege\(\s*'{_INGEST_ROLE}'\s*,\s*'nhms_cold'\s*,\s*'CREATE'\s*\)",
        non_strict,
    ), (
        "--roles-only must still warn about a missing CREATE grant, and it must "
        "test the grant of nhms_ingest_rw -- the role that runs the "
        f"cold-residency lane -- not of {_DOWNLOAD_ROLE}"
    )
    assert re.search(
        r"RAISE WARNING 'cold-residency regression: nhms_ingest_rw lacks CREATE", non_strict
    ), (
        "--roles-only is the additive pre-merge phase: the missing grant must "
        "surface as a WARNING naming nhms_ingest_rw, not be silent and not abort"
    )


def test_additive_phase_installs_the_rule_and_trigger_event_trigger(sql_text: str) -> None:
    """Ownership carries CREATE RULE / CREATE TRIGGER; their bodies run as the writer.

    A rule action or trigger body planted by ``nhms_ingest_rw`` executes as
    whichever role next writes the relation -- including the superuser `nhms`
    that runs migrations, seeds and the replay supervisor -- which reaches
    ``pg_read_file`` / ``lo_export`` / ``COPY … TO PROGRAM``. The event trigger
    is the prevention half and has to live in the ADDITIVE phase, so
    ``--roles-only`` installs it BEFORE the post-merge transfer grants the
    ability it refuses.
    """
    section = _sql_code(_psql_section(sql_text, "do_roles"))
    assert "CREATE EVENT TRIGGER nhms_guard_no_write_role_rules_triggers" in section, (
        "the event trigger must be created in the additive phase, not post-merge"
    )
    assert "DROP EVENT TRIGGER IF EXISTS nhms_guard_no_write_role_rules_triggers" in section, (
        "CREATE EVENT TRIGGER has no OR REPLACE; the install must be idempotent"
    )
    assert "ON ddl_command_start" in section
    for tag in ("'CREATE RULE'", "'CREATE TRIGGER'"):
        assert tag in section, f"{tag} is not refused"
    guard = section[section.index("CREATE OR REPLACE FUNCTION nhms_guard.") :]
    assert "RETURNS event_trigger" in guard
    assert f"session_user IN ('{_INGEST_ROLE}', '{_DOWNLOAD_ROLE}')" in guard, (
        "the guard must name BOTH write roles, and match on session_user: the "
        "write roles hold no membership, so SET ROLE cannot dodge it"
    )
    assert "RAISE EXCEPTION" in guard, "the guard must refuse, not warn"
    assert "SECURITY DEFINER" not in guard, (
        "a SECURITY DEFINER guard function would itself be an escalation surface"
    )
    # a schema the write roles cannot CREATE in: they get USAGE on the six
    # application schemas only, never on this one.
    assert "CREATE SCHEMA IF NOT EXISTS nhms_guard" in section
    assert not re.search(r"GRANT[^;]*nhms_guard", _sql_code(sql_text)), (
        "the write roles must hold nothing on the guard schema; a CREATE there "
        "would let them replace the guard function"
    )


def test_audit_enumerates_rules_and_triggers_against_the_migration_allow_list(
    sql_text: str,
) -> None:
    """Detection half: the event trigger cannot see what a superuser plants.

    A rule or trigger created as `nhms` (a compromised migration, or the object
    planted before this guard existed) fires on the next superuser write just the
    same, so the audit enumerates every rule and every non-internal trigger in
    the six schemas and refuses anything outside migration 000043's four `met`
    triggers.
    """
    section = _psql_section(_sql_code(sql_text), "do_audit")
    assert "FROM pg_rewrite" in section, "planted rules are not audited"
    assert "FROM pg_trigger" in section, "planted triggers are not audited"
    assert "r.rulename <> '_RETURN'" in section, (
        "every view carries a _RETURN rule; without the exclusion the audit is "
        "red on any database with a view and gets muted"
    )
    assert "NOT t.tgisinternal" in section, (
        "foreign-key/constraint triggers are internal and would drown the signal"
    )
    assert "t.tgname <> 'ts_insert_blocker'" in section, (
        "TimescaleDB recreates ts_insert_blocker on every hypertable and chunk"
    )
    for schema, table, trigger in _ALLOWLISTED_TRIGGERS:
        assert f"('{schema}', '{table}', '{trigger}')" in section, (
            f"{schema}.{table}.{trigger} (migration 000043) is not on the audit "
            "allow-list, so a clean production catalog would fail the audit"
        )
    # the allow-list is keyed on (schema, table, name): the same trigger name on
    # another table is NOT the migration's trigger.
    assert "(n.nspname, c.relname, t.tgname) NOT IN (" in section
    inventory = section[
        section.index("rules and non-internal triggers") : section.index(
            "function-privilege sweep"
        )
    ]
    assert len(re.findall(_INTERNAL_SCOPE_RE, inventory)) == 2, (
        "the every-mode inventory must list rules and triggers on the "
        "_timescaledb_internal relations the write roles own -- a trigger "
        "planted on `_compressed_hypertable_N` is invisible otherwise"
    )
    planted = section[section.index("DO $planted$") :]
    assert len(re.findall(_INTERNAL_SCOPE_RE, planted)) == 2, (
        "listing a planted trigger without failing on it is not detection"
    )
    assert "RAISE EXCEPTION 'SECURITY REGRESSION: rule/trigger outside the migration" in planted, (
        "the strict audit must refuse a planted rule/trigger"
    )
    assert "RAISE WARNING 'SECURITY REGRESSION: rule/trigger outside the migration" in planted, (
        "--roles-only and the audit-only invocation must still warn"
    )
    # severity comes from the phase, and the phase reaches the block through a
    # GUC because psql does not interpolate :variables inside a dollar-quoted body
    assert "SET nhms_provision.strict_audit TO :'strict_audit';" in section
    assert "current_setting('nhms_provision.strict_audit', true)" in planted
    assert "IN ('on', 'true', '1', 'yes')" in planted, (
        r"psql's own \if accepts on/true/1/yes; a stricter parse here splits the "
        "severity of one audit between its legs"
    )


def test_the_trigger_allow_list_is_exactly_what_the_migrations_create() -> None:
    """A derived allow-list, not an assumed one.

    If a later migration adds a trigger in one of the six schemas, the audit
    would refuse a *clean* catalog and the operator would learn it during the
    post-merge cutover. This test fails on the migration's PR instead, and the
    fix is one row in ``_ALLOWLISTED_TRIGGERS`` plus one in the provision SQL.
    """
    created: set[tuple[str, str, str]] = set()
    for path in sorted((_ROOT / "db").rglob("*.sql")):
        if path == _SQL_PATH:
            continue  # the provision file's own prose names these triggers
        text = _sql_code(path.read_text(encoding="utf-8"))
        for name, schema, table in re.findall(
            r"CREATE TRIGGER\s+(\w+)[^;]*?\sON\s+([a-z_]+)\.([a-z_]+)", text, re.S
        ):
            if schema in _APP_SCHEMAS:
                created.add((schema, table, name))
    assert created == set(_ALLOWLISTED_TRIGGERS), (
        "db/ creates a different set of triggers in the application schemas than "
        f"the provision audit allow-lists: {sorted(created)}"
    )
    sql = _SQL_PATH.read_text(encoding="utf-8")
    for schema, table, name in created:
        assert f"('{schema}', '{table}', '{name}')" in sql


def test_audit_sweeps_stored_expressions_for_functions_the_role_cannot_execute(
    sql_text: str,
) -> None:
    """The `ALTER TABLE` form of the planted-body escalation.

    No event trigger can refuse `ALTER TABLE` (the cold-residency lane needs it
    for `SET TABLESPACE`), so a column `DEFAULT` or `CHECK` calling
    ``pg_read_file`` is evaluated by whichever role writes the row -- the
    migration superuser. The discriminator is EXECUTE on the referenced
    function: everything the migrations use is PUBLIC-executable.
    """
    section = _psql_section(_sql_code(sql_text), "do_audit")
    assert "FROM pg_attrdef d" in section, "column defaults are not swept"
    assert "k.contype = 'c'" in section, "CHECK constraints are not swept"
    assert "t.tgfoid" in section, (
        "a trigger body is opaque, so the trigger FUNCTION is the unit of "
        "authority and must be swept by oid"
    )
    assert "d.adbin::text" in section and "k.conbin::text" in section, (
        "pg_depend records NOTHING for pinned catalog functions (measured: "
        "pg_attrdef has rows to pg_class only), so the stored parse trees "
        "themselves have to be scanned"
    )
    assert r":(?:op)?funcid (\d+)" in section, (
        "the scan must pick up both FUNCEXPR :funcid and OpExpr :opfuncid"
    )
    assert "has_function_privilege('nhms_ingest_rw', p.oid, 'EXECUTE')" in section, (
        "the sweep must test EXECUTE for the write role, not the function's "
        "schema or its owner"
    )
    sweep = section[section.index("function-privilege sweep") : section.index("DO $planted$")]
    assert len(re.findall(_INTERNAL_SCOPE_RE, sweep)) == 4, (
        "all four sweep legs (defaults, CHECKs, rule actions, trigger "
        "functions) must also cover the _timescaledb_internal relations the "
        "transfer hands to the write roles -- owner-scoped, so TimescaleDB's "
        "own catalog tables stay out"
    )
    planted = section[section.index("DO $planted$") :]
    assert (
        "RAISE EXCEPTION 'SECURITY REGRESSION: % the write role cannot execute" in planted
    ), "the strict audit must refuse a smuggled function"
    assert (
        "RAISE WARNING 'SECURITY REGRESSION: % the write role cannot execute" in planted
    ), "--roles-only and the audit-only invocation must still warn"
    # the every-mode inventory has to print a line even when the sweep is clean,
    # or the T7 receipt cannot show that it ran
    inventory = section[section.index("function-privilege sweep") : section.index("DO $planted$")]
    assert "not executable by nhms_ingest_rw" in inventory
    assert "expression(s)/trigger(s) scanned" in inventory


def test_audit_requires_the_allow_listed_triggers_to_stay_enabled(sql_text: str) -> None:
    """`ALTER TABLE ... DISABLE TRIGGER` carries no rule/trigger DDL tag.

    The event trigger never sees it, so an allow-listed guard can be switched
    off in place while the inventory still lists it. Migration 000043 creates
    all four with the default origin firing mode.
    """
    planted = _psql_section(_sql_code(sql_text), "do_audit")
    planted = planted[planted.index("DO $planted$") :]
    assert "t.tgenabled <> 'O'" in planted, (
        "an allow-listed trigger that is not enabled must be drift; 000043 uses "
        "no ENABLE ALWAYS/REPLICA, so 'O' is the only clean value"
    )
    for schema, table, trigger in _ALLOWLISTED_TRIGGERS:
        assert planted.count(f"('{schema}', '{table}', '{trigger}')") == 2, (
            f"{schema}.{table}.{trigger} must be checked both against the "
            "allow-list and for still being enabled"
        )
    assert (
        "RAISE EXCEPTION 'SECURITY REGRESSION: allow-listed trigger not enabled" in planted
    )
    assert (
        "RAISE WARNING 'SECURITY REGRESSION: allow-listed trigger not enabled" in planted
    )


def test_audit_asserts_the_event_trigger_is_present_enabled_and_superuser_owned(
    sql_text: str,
) -> None:
    """A dropped or disabled guard must be loud; it is the prevention half."""
    section = _psql_section(_sql_code(sql_text), "do_audit")
    planted = section[section.index("DO $planted$") :]
    assert "FROM pg_event_trigger" in planted or "pg_event_trigger e" in planted
    assert "e.evtname = 'nhms_guard_no_write_role_rules_triggers'" in planted
    assert "e.evtenabled = 'D'" in planted, "a disabled event trigger refuses nothing"
    assert "NOT o.rolsuper" in planted, (
        "an event trigger owned by a non-superuser could be dropped by that role"
    )
    assert (
        "RAISE EXCEPTION 'SECURITY REGRESSION: event trigger "
        "nhms_guard_no_write_role_rules_triggers is %" in planted
    )
    assert (
        "RAISE WARNING 'SECURITY REGRESSION: event trigger "
        "nhms_guard_no_write_role_rules_triggers is %" in planted
    )


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


def test_every_generated_schema_list_covers_all_six_schemas(sql_text: str) -> None:
    """Every `ANY (ARRAY[...])` in `do_roles` must name all six app schemas.

    A fixed-width window around one clause silently stops covering the list it
    was meant to check as soon as the SQL is reformatted, and it says nothing
    about the other four sites. Enumerate them instead: schema USAGE, DML on all
    tables, USAGE on all sequences, and the two ALTER DEFAULT PRIVILEGES blocks.
    """
    section = _sql_code(_psql_section(sql_text, "do_roles"))
    starts = [match.end() for match in re.finditer(r"ANY \(ARRAY\[", section)]
    assert len(starts) == 5, (
        "expected five generated schema lists in do_roles (schema USAGE, ALL "
        f"TABLES, ALL SEQUENCES, default-priv TABLES, default-priv SEQUENCES); "
        f"found {len(starts)}"
    )
    for start in starts:
        end = section.index("]", start)
        listed = tuple(re.findall(r"'([a-z_]+)'", section[start:end]))
        context = section[max(0, start - 400) : start].strip().splitlines()[-1:]
        assert set(listed) == set(_APP_SCHEMAS), (
            f"generated schema list {listed} after {context} must name all six "
            f"application schemas {_APP_SCHEMAS}"
        )


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
    supervisor_path = "scripts/node27_timeseries_compression_supervisor.py"
    supervisor = _grep_repo(r"pg_dump|pg_restore", (supervisor_path,))
    assert supervisor, (
        "the replay supervisor no longer runs pg_dump/pg_restore; its superuser "
        "exception must be re-justified or removed"
    )
    # pg_dump/pg_restore are READ_ONLY_KINDS in the supervisor's own taxonomy
    # (:139-142): they justify nothing on their own. `migration_apply` is a
    # MUTATION_KIND (:143) and is the DDL `nhms_ingest_rw` structurally cannot
    # carry, so anchor the exception on it as well.
    mutating = _grep_repo(r"migration_apply", (supervisor_path,))
    assert mutating, (
        "the replay supervisor no longer applies migrations; without a "
        "MUTATION_KIND step its superuser exception is only justified by "
        "read-only tooling and must be re-derived"
    )
    assert "MUTATION_KINDS" in (_ROOT / supervisor_path).read_text(encoding="utf-8")


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
  roles)
    if [ -n "${FAKE_ROLES_RC:-}" ]; then
      printf 'ERROR:  SECURITY REGRESSION: role nhms_ingest_rw is a member of pg_write_server_files\n' >&2
      exit "${FAKE_ROLES_RC}"
    fi
    ;;
  remaining)
    if [ -n "${FAKE_QUERY_RC:-}" ]; then
      printf 'psql: error: connection to server failed\n' >&2
      exit "${FAKE_QUERY_RC}"
    fi
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


# `sleep` is not a bash builtin, so a stub earlier on PATH makes --pass-interval
# observable without spending the wall clock on it.
_FAKE_SLEEP = r"""#!/usr/bin/env bash
printf '%s\n' "$1" >> "$FAKE_SLEEP_LOG"
"""


@pytest.fixture()
def runner(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "docker"
    fake.write_text(_FAKE_DOCKER, encoding="utf-8")
    fake.chmod(0o755)
    fake_sleep = bin_dir / "sleep"
    fake_sleep.write_text(_FAKE_SLEEP, encoding="utf-8")
    fake_sleep.chmod(0o755)
    log = tmp_path / "docker-argv.log"
    stdin_log = tmp_path / "docker-stdin.log"
    sleep_log = tmp_path / "sleep.log"
    log.touch()
    stdin_log.touch()
    sleep_log.touch()

    def run(
        *args: str,
        env: dict[str, str] | None = None,
        bash_flags: tuple[str, ...] = (),
    ) -> subprocess.CompletedProcess[str]:
        child = dict(os.environ)
        child.pop("NODE27_INGEST_RW_PASSWORD", None)
        child.pop("NODE27_DOWNLOAD_RW_PASSWORD", None)
        child["PATH"] = f"{bin_dir}{os.pathsep}{child['PATH']}"
        child["FAKE_DOCKER_LOG"] = str(log)
        child["FAKE_DOCKER_STDIN_LOG"] = str(stdin_log)
        child["FAKE_SLEEP_LOG"] = str(sleep_log)
        child.update(env or {})
        return subprocess.run(
            ["bash", *bash_flags, str(_RUNNER_PATH), *args],
            capture_output=True,
            text=True,
            env=child,
            cwd=str(_ROOT),
        )

    run.argv_log = log  # type: ignore[attr-defined]
    run.stdin_log = stdin_log  # type: ignore[attr-defined]
    run.sleep_log = sleep_log  # type: ignore[attr-defined]
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
    assert "SELECT privilege set unchanged" in result.stdout
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


def test_pass_interval_defaults_to_no_delay_between_passes(runner) -> None:
    result = runner(env={"FAKE_REMAINING": "2"})
    assert result.returncode == 3
    assert runner.sleep_log.read_text(encoding="utf-8") == "", (
        "the default must stay back-to-back; an implicit delay changes the "
        "documented retry window"
    )


def test_pass_interval_spaces_the_ownership_passes(runner) -> None:
    """A lock holder that outlives 5 back-to-back passes needs a wider window."""
    result = runner("--pass-interval", "7", env={"FAKE_REMAINING": "2"})
    assert result.returncode == 3, (result.returncode, result.stdout, result.stderr)
    slept = runner.sleep_log.read_text(encoding="utf-8").split()
    assert slept == ["7", "7", "7", "7"], (
        "expected one interval between each of the 5 passes and none after the "
        f"last one; got {slept}"
    )


def test_pass_interval_is_not_slept_after_a_completed_transfer(runner) -> None:
    result = runner("--pass-interval", "7")
    assert result.returncode == 0, result.stderr
    assert runner.sleep_log.read_text(encoding="utf-8") == "", (
        "the transfer completed on pass 1; there is nothing to wait for"
    )


def test_roles_only_maps_a_refused_run_to_the_documented_exit_code(runner) -> None:
    """psql's own exit status must not leak as an undocumented runner code."""
    result = runner("--roles-only", env={"FAKE_ROLES_RC": "1"})
    assert result.returncode == 3, (result.returncode, result.stdout, result.stderr)
    assert "docker exec/psql exit 1" in result.stderr, (
        "the message must name what produced the status; `psql exit N` is wrong "
        "for a docker/container failure, which is the likelier one on node-27"
    )
    assert "do not cut the env files over" in result.stderr.lower()


def test_full_mode_maps_a_failed_docker_query_to_the_documented_exit_code(runner) -> None:
    """Only `--roles-only` was wrapped; full mode propagated the raw status.

    A missing container exited 1 (undocumented) and a wrong database exited 2
    (which reads as this runner's "usage / environment error"), both of them
    after the ownership pass had already run.
    """
    result = runner(env={"FAKE_QUERY_RC": "2"})
    assert result.returncode == 3, (result.returncode, result.stdout, result.stderr)
    assert "docker exec/psql exit 2" in result.stderr
    assert "remaining-relation count after pass 1" in result.stderr
    assert "do not cut the env files over" in result.stderr.lower()


def test_password_presence_check_never_expands_the_value_under_bash_x(runner) -> None:
    """`[[ -n "${!var:-}" ]]` prints the password into the `set -x` trace."""
    sentinel = "s3cr3t-trace-sentinel"
    result = runner(
        "--roles-only",
        env={"NODE27_INGEST_RW_PASSWORD": sentinel},
        bash_flags=("-x",),
    )
    assert result.returncode == 0, result.stderr
    assert sentinel not in result.stderr, (
        "the password value reached the execution trace; an operator running the "
        "runner under `bash -x` would paste it into a ticket"
    )
    assert sentinel not in result.stdout
    assert "NODE27_INGEST_RW_PASSWORD" in result.stderr, (
        "the trace must still be produced, or this test proves nothing"
    )


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
    assert "cannot log in over TCP until one is set" in result.stderr


def test_runner_rejects_bad_arguments(runner) -> None:
    assert runner("--nope").returncode == 2
    assert runner("--max-passes", "0").returncode == 2
    assert runner("--container").returncode == 2
    assert runner("--pass-interval").returncode == 2
    assert runner("--pass-interval", "-1").returncode == 2
    assert runner("--pass-interval", "abc").returncode == 2


def test_runner_rejects_out_of_range_retry_settings(runner) -> None:
    """Operator-typo guard: the cutover window is timer-stopped and finite."""
    over_passes = runner("--max-passes", "101")
    assert over_passes.returncode == 2, over_passes.stderr
    assert "--max-passes must be <= 100" in over_passes.stderr
    over_interval = runner("--pass-interval", "3601")
    assert over_interval.returncode == 2, over_interval.stderr
    assert "--pass-interval must be <= 3600" in over_interval.stderr
    # the bounds themselves stay usable
    assert runner("--roles-only", "--max-passes", "100", "--pass-interval", "3600").returncode == 0


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

# Surfaces a superuser reads freely and a plain table owner does not.
# `pg_locks` itself is NOT row-filtered -- every role sees every lock row. What
# degrades is the attribution a quiescence guard needs: `pg_stat_activity` keeps
# one row per backend but masks `query`/`state`/`wait_event*` for other users'
# sessions unless the role holds pg_read_all_stats, so a "no concurrent writer"
# check goes permanently green instead of failing. `pg_locks` stays in the
# pattern because it is only ever useful joined to that masked view. Schema
# `pg_toast` has no USAGE granted to PUBLIC.
_SUPERUSER_GATED_READS = re.compile(
    r"pg_stat_activity|pg_locks|pg_toast\.|pg_stat_file|pg_ls_dir|pg_read_file"
    r"|pg_read_binary_file|pg_terminate_backend|pg_cancel_backend|pg_reload_conf"
)


def test_converted_lanes_do_not_read_superuser_gated_catalogs() -> None:
    """A superuser-gated READ degrades silently, unlike a write which errors.

    `pg_stat_activity` does not raise for a non-superuser -- it masks the
    `query`/`state`/`wait_event*` columns of other users' backends, so a
    quiescence or lock-conflict guard reading it (directly, or joined from
    `pg_locks`, which is itself unfiltered) would go permanently green.  Any new
    hit here must either be paired with a grant in
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
