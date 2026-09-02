## Risk Triage

```text
Issue type: security hardening (least privilege) with live production DDL
Project profile: NHMS (openspec/project-profile.md)
Blast radius: high (every write unit on node-27; hypertable ownership)
Fixture level: expanded
Repair intensity: high
Upstream suggested level: absent (hand-written issue; expanded forced by auth/permissions + production config + example templates)
Why:
- production role/grant/ownership DDL on the primary; env cutover of four units
- a wrong inventory silently breaks ingest (ANALYZE skip) or fails ticks (permission denied)
OpenSpec change: node27-write-path-roles
Evidence floor:
- uv run ruff check . ; uv run pytest -q tests/test_node27_write_roles.py
- local disposable TimescaleDB 2.10.2 container: provision SQL twice + per-role probes (transcript in PR)
- node-27 pre-merge: `--roles-only` provision (additive), negative probes, pg_roles output
- node-27 post-merge: ownership transfer audit + relacl diff, per-component runs under the new roles, env cutover receipts per unit
- openspec validate node27-write-path-roles --strict --no-interactive
```

## Risk Packs

| Pack | 选择 | 理由 |
|---|---|---|
| Public API / CLI / script entry | selected | new provision runner; every write unit's DSN |
| Config / project setup | selected | six env files + templates; drill and replay exceptions |
| File IO / path safety / overwrite | selected | env backups `*.env.pre-1774`; runner must not print passwords |
| Schema / columns / units / field names | not selected | no schema change; ownership/grants only |
| Auth / permissions / secrets | selected | the entire change; passwords only via env, never in repo/logs |
| Concurrency / shared state / ordering | selected | ownership transfer takes AccessExclusiveLock per relation/chunk while the display API (unstoppable, public) holds AccessShareLock: T7 stops the writer timers, runs the loop under `SET lock_timeout='5s'` with retry, captures `relacl` before/after; cutover order |
| Resource limits / large input / discovery | not selected | no volume change |
| Legacy compatibility / examples | selected | templates aligned; migration role unchanged |
| Error handling / rollback / partial outputs | selected | rollback path; partial grant = permission denied in journal |
| Release / packaging / dependency compatibility | not selected | none |
| Documentation / migration notes | selected | inventory + procedure + re-run-after-migration step |
| PostGIS / TimescaleDB 域行为 | selected | owner requirement for tiering functions and ANALYZE; chunk ownership cascade |
| 其余 domain packs | not selected | not touched |

## Tasks

- [x] T1 Inventory: static scan per unit entrypoint widened to DML + ANALYZE + DDL + `SET TABLESPACE` + `psql --file`/`pg_dump`/`pg_restore` call sites, plus local container runs of each component under the candidate role (both stats-guard legs must report `ok`); write the table into the tier runbook incl. the download lane's "no DB connection" finding and the replay/drill exceptions.
- [x] T2 `db/roles/node27_write_roles.sql`: roles (flags), schema USAGE, schema-scoped ownership loop to `nhms_ingest_rw` (relkind r/p first, then S, plus v/m; core/hydro/met/ops/map/flood; absent schema tolerated; generated as one autocommitted `ALTER … OWNER TO` statement per relation — never one `DO` transaction — under `SET lock_timeout='5s'` with up to 5 retry passes; exhaustion -> partial transfer, audit red, runner non-zero), DML + sequence USAGE + default privileges in the six schemas for `nhms_ingest_rw` and on `met.*` for `nhms_download_rw`, conditional `GRANT CREATE ON TABLESPACE nhms_cold`, trailing audit query incl. `nhms_display_ro` SELECT privilege set (`has_table_privilege` per relation — privilege, not readability: a view executing as its new owner is out of this gate's sight, hence the T7 relkind v/m enumeration), role membership (`pg_auth_members`, both directions — writer as member, writer granted to anyone — any row = security regression), the `nhms_cold` CREATE grant (warning / strict error / explicit skip line), the rule/trigger inventory (`pg_rewrite` minus `_RETURN`, `pg_trigger` minus internal and `ts_insert_blocker`, vs the four-trigger `met` allowlist from 000043, each allow-listed trigger enabled), and the function-privilege sweep (every column default / `CHECK` / rule action / trigger function in the six schemas must reference only functions the write role can `EXECUTE`); a superuser-owned `ddl_command_start` event trigger refuses the rule/trigger DDL family from either write role on ordinary tables and chunks (installed in the additive phase, drop-and-recreate; TimescaleDB routes `CREATE TRIGGER` on hypertables around it — detection only there); the strict audit inventories rules/triggers in the six schemas plus write-role-owned `_timescaledb_internal` relations against the 000043 allow-list (present `count(*) = 4` and enabled; `ts_insert_blocker` recognised by `tgfoid` identity, not name) and sweeps every stored expression for functions untrusted for a superuser writer (temp schema / non-superuser owner / not executable by the write role / deny-listed `pg_catalog` gadgets); the full-mode ownership phase revokes `TEMPORARY` on the database from PUBLIC and re-grants it to `nhms_display_ro`, the `COPY … FROM PROGRAM` probe creating its scratch table as the superuser before `SET LOCAL ROLE`; idempotent (run twice locally, transcript in PR; drift case after a new table: INSERT ok, ANALYZE warning, audit red, re-run green).
- [x] T3 `scripts/node27_provision_write_roles.sh`: `docker exec -i nhms-db psql` runner with `--roles-only` (additive phase) and full mode (adds the per-relation autocommit ownership loop with `lock_timeout` + retry passes back-to-back by default, `--pass-interval` / `NODE27_WRITE_ROLES_PASS_INTERVAL` to space them, `relacl` before/after capture and diff, trailing audit); any psql refusal in `--roles-only` maps to exit 3; password from env or skip (presence check via `${!var:+x}` so `bash -x` never expands the value); audit diff → non-zero; no secret in output; shell test with a fake `docker` covering both modes and the exhaustion path.
- [x] T4 Templates: compression/cold-residency/retention → `nhms_ingest_rw`; comments on ingest/download; drill and compression-replay keep `nhms` with the reason.
- [x] T5 `tests/test_node27_write_roles.py`: templates carry no `nhms` credential in any form (`nhms:` or `PGUSER=nhms`) except the drill and replay allow-list; SQL names exactly the template roles; ownership loop covers the six schemas and is not wrapped in a single `DO`/transaction; the ingest default-privilege block and the display-audit clause are present; download grants ⊇ scanned `met.*` targets; forbidden statements absent (`REASSIGN OWNED`, `GRANT nhms TO`); `OWNER TO` only inside the ownership section; `\set AUTOCOMMIT off` never; every generated schema list carries all six schemas; the membership (both directions), tablespace, rule/trigger, presence, sweep and event-trigger-health audit clauses present with their severities pinned **structurally** (`IF v_strict THEN` bound to `RAISE EXCEPTION`, `ELSE` to `RAISE WARNING`, per block — a substring pin let `IF false` pass in round 3), all six event-trigger tags pinned, the provenance legs and deny-list names pinned, the identity-keyed `ts_insert_blocker` exclusion pinned at all three sites, and the `REVOKE/GRANT TEMPORARY` pair pinned to the ownership phase and absent from `do_roles` (do_audit section read through `_sql_code()`); the event-trigger block present and superuser-owned; the `--pass-interval` sleep count and the `bash -x` password non-expansion pinned with a fake `sleep` / trace (54 tests; includes the provenance sweep, `tgenabled`, presence, structural-severity, six-tag, identity-keyed exclusion, TEMP-placement and probe-order guards; 14 mutants red-proved in fix pass 3).
- [x] T6 Docs: runbook provision/cutover/rollback procedure, re-run-after-migration step, `current-production-ops.md` role table, bringup checklist.
- [ ] T7 node-27 pre-merge (queued session, first in queue, additive only): `scripts/node27_provision_write_roles.sh --roles-only` (roles, grants, default privileges, tablespace grant, `nhms_guard` schema + event trigger, `pg_roles` flags, membership/tablespace/rule-trigger/function-privilege audits, negative `COPY … FROM PROGRAM` probes as both roles, and a negative `CREATE RULE` probe as `nhms_ingest_rw` on a relation it owns — pre-merge it owns none, so the probe runs post-transfer) from a detached worktree; no ownership transfer, no unit touched. Post-merge (same session, after the reviewed merge): before any transfer, enumerate relkind v/m in the six app schemas together with their `pg_depend` targets outside those schemas (expected empty; if non-empty, the runner's `SET ROLE nhms_display_ro; EXPLAIN SELECT` execution probe must be added before proceeding, because views execute as their new owner); stop compression + retention + autopipe timers, verify no in-flight tick/`compress_chunk`, run the full provision (per-relation autocommit ownership loop, `lock_timeout` + retry, `relacl` before/after diff, trailing audit), restart timers; per-component runs under the new roles (autopipe dry tick with both stats-guard legs `ok`, compression, retention, cold-residency dry-runs, download as no-DB control); env cutover download → ingest → compression + cold-residency → retention with restarts and receipts; redacted `grep` of env files (replay/drill still `nhms`, documented); live escalation-surface sweep on the PostGIS-bearing production catalog: `SELECT nspacl FROM pg_namespace WHERE nspname='public'` (no legacy `CREATE` to PUBLIC), `SECURITY DEFINER` functions executable by either write role (expect none), the rule/trigger inventory of the six schemas plus the write-role-owned `_timescaledb_internal` relations (expect exactly the four `met` triggers + `ts_insert_blocker`; no row from the internal schema), the function-privilege sweep summary line (`N expression(s)/trigger(s) scanned, M distinct function(s) referenced, 0 not executable by nhms_ingest_rw` — PostGIS functions enter the scan for the first time here; any non-zero count is investigated before the transfer), and the event trigger present and owned by `nhms`; the audit-only invocation creates and drops one session temp table (`pg_temp.nhms_audit_function_refs`) and writes nothing else; the audit-only invocation is re-run before every later superuser-write session (migration apply, replay `pg_restore`) per §9.6 — its two preconditions (full provision already run; migration `000043` applied) hold on node-27 and are recorded in §9.6; after the full provision the receipt records `has_database_privilege(<role>, current_database(), 'TEMP')` = false for both write roles and true for `nhms_display_ro` (the one non-additive statement, full mode only), `SELECT datacl FROM pg_database WHERE datname = current_database()` before/after, `SHOW shared_preload_libraries` (whether `pg_stat_statements` could retain the `ALTER ROLE … PASSWORD` text — record, do not remediate here), and the sweep summary `… 0 untrusted for a superuser writer` with PostGIS functions in scan for the first time.

## Recorded deviations (implementation)

1. **T1, "local container runs of each component under the candidate role"** —
   not closable locally. The `timescale/timescaledb:2.10.2-pg15` image ships no
   PostGIS, so `packages/common/migrate.py` cannot apply the schema and no lane
   can execute against a disposable container. What T2's container **does**
   prove is the privilege shape each lane needs, measured as primitives under a
   real `nhms_ingest_rw` / `nhms_download_rw` login: `compress_chunk`,
   `decompress_chunk`, `drop_chunks`, `chunks_detailed_size`, chunk `ANALYZE`,
   authority-table `ANALYZE`, `ALTER … SET TABLESPACE nhms_cold`, `met.*` DML,
   and the negative probes. The **per-component runs move to T7 post-merge**
   (design D5 already schedules them there). The static half of T1 (scan widened
   to DML / `ANALYZE` / DDL / `SET TABLESPACE` / `psql --file` / `pg_dump` /
   `pg_restore`) is complete and is re-derived by
   `tests/test_node27_write_roles.py`, not frozen into a list.
2. **`infra/env/node27-archive-rebuild-drill.example` does not exist.** The
   archive lane was permanently retired in #1370 (ADR 0002 Revision 2026-08-11):
   no unit, no wrapper, no template. The proposal's "comments in
   node27-archive-rebuild-drill.example" is therefore unimplementable. The name
   is kept in the guard test's allow-list (the live `node27-archive-rebuild-drill.env`
   still exists on node-27 per the issue evidence) and the exception is recorded
   in the runbook's inventory table; no template was invented for it.
3. **`relacl` before/after is a receipt, not a gate.** `ALTER … OWNER TO`
   rewrites the grantor references inside every ACL entry, so the diff is never
   empty after a real transfer (measured: `nhms_display_ro=r/nhms` →
   `nhms_display_ro=r/nhms_ingest_rw`). The runner prints the diff and gates on
   `nhms_display_ro`'s **effective** `SELECT` set instead, which is what the
   spec scenario actually asserts. Exit 4 is reserved for that regression.

## Additions beyond the fixture (recorded for review)

Two hardening items the fixture does not name, both kept inside its stated risk
posture ("passwords never in repo or logs"; "least privilege must not degrade a
safety check"):

A. **Password logging.** `ALTER ROLE … PASSWORD` is written verbatim to the
   server log under `log_statement=ddl|mod|all`, so the env-var-by-name design
   still leaked into the container log. `db/roles/node27_write_roles.sql` now
   wraps both ALTERs in `SET log_statement = 'none'` /
   `SET log_min_duration_statement = -1` and restores them. Proved with a
   canary against a container started `-c log_statement=ddl`: 1 hit for an
   unsuppressed control statement, 0 for the runner-set passwords (transcript
   appendix A). Residual recorded, not fixed: `log_min_error_statement` still
   logs a **failed** ALTER.

B. **Superuser-gated reads.** The T1 scan covered writes, which fail loudly.
   `pg_stat_activity` does not fail for a non-superuser — it returns every row
   but masks `query` / `state` / `wait_event*` for other users' backends
   (`pg_locks` is not row-filtered at all; it only degrades when joined back to
   that masked view), so a quiescence guard would go silently green. Re-scanned
   the converted lanes: no executed hit (all matches are `#` comments), every
   live caller sits in a lane that keeps the superuser, and
   `compressed_chunk_cold_residency.py` never names `pg_toast.*` in emitted SQL.
   No grant was added; the audit is pinned by
   `test_converted_lanes_do_not_read_superuser_gated_catalogs` and written up in
   runbook §9.2 + transcript appendix B.

C. **CI selector rules had to be MERGED, not appended.**
   `tests/test_select_ci_tests.py` enforces that a `PathTestRule` pattern
   appears at most once outside `INTENTIONAL_DUPLICATE_PATTERNS`
   (`test_path_rule_duplicate_patterns_are_allowlisted_decisions`,
   `test_duplicate_pattern_guard_flags_an_unmerged_sibling_collision`). Six of
   the converted lanes already had a rule, so `tests/test_node27_write_roles.py`
   was merged into those rules' test tuples; only the four patterns with no
   pre-existing rule got a new row. This also required one 4-line edit to the
   pinned expectation in
   `test_select_tests_maps_autopipeline_script_without_core_smoke_fallback`
   — the sole change to `tests/test_select_ci_tests.py`, which is a shared file.

## Non-goals (explicit)

`pg_hba` trust lines, SSL, migrations' role, drill and compression-replay env role change (documented exceptions), one-time `CREATE TABLESPACE` install, password rotation.

## Known limits (recorded, not scribed — batch directive)

- The sweep's deny-list of PUBLIC-executable `pg_catalog` functions with superuser-sensitive effects (`set_config`, backend signalling, `pg_sleep*`, advisory locks) is incomplete by construction — recorded reason: the structural legs (temp schema, non-superuser owner, not executable) close every attacker-authored body, and the remaining surface is built-in functions with side effects, which only an allow-list of the ~15 functions the migrations actually reference would close; that allow-list is the next tightening if a new gadget is found. Detection stays time-of-check: a plant between the §9.6 audit-only run and the superuser write is caught only by the next audit, which is why the superuser-write half (line below) is the real residual.
- Migrations (`packages.common.migrate`), seeds and the compression-replay supervisor still write the application relations as superuser `nhms`; that superuser-write half of the owner-planted rule/trigger gadget is mitigated here by the event trigger (prevention) and the allowlist audit (detection), not removed. Moving those lanes off `nhms` is out of this change's scope (design Non-Goals) and is deliberately not filed as an issue per the batch-23–28 directive; the reason is recorded in design D2 and PR #1964's 偏离记录.
