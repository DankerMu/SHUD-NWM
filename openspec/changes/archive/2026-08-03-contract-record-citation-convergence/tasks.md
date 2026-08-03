# Tasks — contract-record-citation-convergence (#1273)

Anchors verified at master 9da59d71: the three records at
`packages/common/node27_container_contract.py:43-73`; snapshot fixture
`packages/common/node27_external_contract_snapshot.json` carries FIVE
citation targets with `_provenance.command` + date 2026-08-02
(`contract.container_pg_restore_realpath`,
`contract.systemd_unset_timestamp`, `contract.client_backend_type`,
`host_context.nhms_db_image_ref`, `host_context.systemd_version`);
the two `informational.*` targets
(`informational.backend_type_distribution`,
`informational.recurring_unit`) carry NO `_provenance` wrapper BY
DESIGN — dump-recorded diagnostics, never compared
(`scripts/node27_external_contract_snapshot.py:386-388`;
`COMPARED_SECTIONS` at `:127` holds only `contract`/`host_context`) —
so a record citing them must use the DUAL-CARRIER form: date from
`informational.measured_at` (`2026-08-02T11:13:05.968511Z`) and command from
the frozen `PROBES` table (`PROBE_RECURRING_UNIT` at script
`:180-184`, `PROBE_BACKEND_TYPE_DISTRIBUTION` at `:204-208`), and
must state that these entries are OUTSIDE the drift lock (the runbook
`docs/runbooks/tier-node27-timeseries-storage.md:1710-1712` and the
snapshot change's archived tasks both record informational as never
compared) so readers do not mistake them for `contract.*`-grade
guarantees. In the production path domain the only `.workplans/`
reference is `packages/common/node27_container_contract.py:47`
(`git grep -n "\.workplans/" -- packages scripts apps services
workers` → 1 hit; the unrestricted `'*.py'` form returns 6 — the
other 5 live in `tests/test_scheduler_generation.py`, outside this
issue's production domain).

Risk triage: fixture level **compact** (records-only, zero runtime;
S-size). Risk packs selected: **record-accuracy** (the entire issue is
the #1271 invariant applied to three records; every rewritten clause
must be covered by its citation, every uncoverable clause deleted —
the repair vocabulary is deletion or copying a proven carrier, never
authored replacement claims) and **forensic-verbatim** (constant
values and all refusal/gate texts byte-unchanged; module AST-identical
to master). Not selected: oracle-discrimination (no runtime oracle
changes — AST identity is the guard), performance / UI / migration
(n/a). Node-27 is NOT touched: every citation target already lives in
the committed snapshot fixture; no live command runs anywhere in this
change.

Must-preserve behavior:

- `packages/common/node27_container_contract.py` compiles to an AST
  identical to master's. Precision (measured): `#` comments never
  enter the AST, but DOCSTRINGS DO — AST identity therefore also
  forbids editing the module docstring (`:10-14`, which summarizes
  these very records); it stays byte-unchanged, stated here so the
  implementer does not "helpfully" sync it. The check is
  `ast.dump(ast.parse(old)) == ast.dump(ast.parse(new))`.
- All three constant VALUES byte-unchanged
  (`/usr/share/postgresql-common/pg_wrapper`, `n/a`,
  `client backend`).
- The `:163-239` #1271-written records byte-unchanged (line
  NUMBERS will shift as comment lengths change; no file OUTSIDE
  this change anchors the module by line number — `git grep
  "node27_container_contract.py:" --
  ':!openspec/changes/contract-record-citation-convergence'` → 0
  hits (the unscoped grep hits this fixture's own master-scoped
  anchors, which is expected) — so byte-identity of the record text
  is the invariant, not line positions).
- Both planes' test suites stay green untouched. Machine lock
  discovered at implementation:
  `tests/test_node27_external_contract_snapshot.py::test_measured_constant_count_matches_the_fixture_contract_section`
  requires EXACTLY three lines opening with `# MEASURED` in the
  module — the three records keep exactly one such opener each;
  continuation paragraphs must not start with `# MEASURED`.

Seams under test (upstream-declared, consumed not renegotiated): the
snapshot fixture's `_provenance` schema (issue #1089 baseline; each
entry in the compared sections — `contract`/`host_context` — pins
command+date; `informational.*` carries none by design); the `CONTRACT_CONSTANTS` per-constant value
lock in `scripts/node27_external_contract_snapshot.py` (value changes
are its drift process, not this change); the #1271 invariant wording
in `openspec/specs/hypertable-compression/spec.md` (this change adds a
sibling requirement, it does not edit #1271's).

Non-goals: changing any constant value; #1255's runtime checkpoint fix
(the falsified narrative's runtime consequence); #1240; adding new
snapshot probes (the "Ubuntu 22.04" clause is deleted, not re-proved);
touching `.workplans/` content or gitignore. NAMED RESIDUE (recorded,
not fixed here): the two frozen importer planes each carry a verbatim
sibling of the never-started-unit over-claim —
`scripts/node27_timeseries_compression_supervisor.py:1388-1389` and
`scripts/node27_timeseries_compression_live_evidence.py:959-960`
(`# MEASURED: systemd renders the never-started unit's unset start
timestamp ...`) — after this change the single-source module says the
converged truth while these two copies keep the old wording; they sit
inside #1255's edit region and route there. E1's "narrowed" rows mean
the contract module only, not a repo-wide sweep.

Minimal mergeable slice: all three records plus the spec delta — fixing
one record while siblings keep dangling/falsified citations would leave
the module half-compliant with the very invariant the change cites.

## 1. Record rewrites (comments only)

- [x] 1.1 `CONTAINER_PG_RESTORE_REALPATH` (:43-49): rewrite the
  comment so every remaining clause is determined by a named snapshot
  entry — image tag by `host_context.nhms_db_image_ref`
  (`docker inspect '--format={{.Config.Image}}|{{.Image}}' nhms-db`,
  2026-08-02), realpath by `contract.container_pg_restore_realpath`
  (`docker exec nhms-db /usr/bin/readlink -f /usr/bin/pg_restore`,
  2026-08-02); KEEP "NOT `/usr/bin/pg_restore` itself" (determined:
  the command's output differs from its input path) and KEEP the
  "is a symlink" clause (same determination: only a final-component
  symlink makes the resolved output differ from the input path);
  DELETE "the
  stable entrypoint the child actually invokes" (readlink determines
  no exec behavior); `Source:` becomes the snapshot fixture path +
  entry names (repo-resolvable), the `.workplans/1069` line deleted.
- [x] 1.2 `SYSTEMD_UNSET_TIMESTAMP` (:51-59): narrow the general
  claim to the witness command's coverage with a DUAL-CARRIER
  citation — value/command from `contract.systemd_unset_timestamp`
  (`systemctl --user show
  nhms-external-contract-snapshot-witness-does-not-exist.service -p
  LoadState -p ExecMainStartTimestamp` → `n/a`), and the
  "nonexistent" qualifier from the snapshot script's witness design
  (`RESERVED_WITNESS_UNIT`, deliberately never installed, script
  `:88-107`; fail-closed `LoadState != "not-found"` refusal at
  `:307-315`) — the fixture entry alone records no LoadState and
  cannot determine "nonexistent". REPLACE the falsified
  inactive-recurring-unit sentence with the snapshot's measured fact
  from `informational.recurring_unit` (an inactive/dead unit CAN
  report a real `ExecMainStartTimestamp` —
  `Sun 2026-08-02 12:25:00 CST` — dual-carrier cited per the anchor
  paragraph; cross-reference #1255 for the runtime checkpoint
  consequence); systemd version cited to
  `host_context.systemd_version`; DELETE "Ubuntu 22.04" (no citation
  exists — the only "22.04" substrings in the fixture,
  `pg_server_version`/`container_pg_restore_version`'s
  `pgdg22.04+1`, describe in-container package builds, and
  `systemd_version`'s `-0ubuntu3.21` determines only Ubuntu-packaged
  systemd, not the distro release; record this rationale in the E1
  deleted row so nobody "re-proves" the clause from pgdg22.04). The
  both-planes-pin-this-literal sentence is REWRITTEN to match the
  code it describes: both planes REQUIRE `n/a` inside a whole-dict
  equality (not "accept") —
  `scripts/node27_timeseries_compression_supervisor.py:1382-1393`,
  `scripts/node27_timeseries_compression_live_evidence.py:953-964`,
  is-active-side explicit rejection at live_evidence `:971-978`; use
  these CURRENT line anchors, not the stale `:1282-1293`/`:834-845`
  pair recorded inside the snapshot script's own comment
  (pre-existing drift, that script is frozen here, do not fix it).
- [x] 1.3 `CLIENT_BACKEND_TYPE` (:61-73): value cited to
  `contract.client_backend_type` (own-session `pg_stat_activity`
  probe, 2026-08-02); the `PG 15` clause KEPT with its determining
  citation `host_context.pg_server_version`
  (`15.2 (Ubuntu 15.2-1.pgdg22.04+1)`); converge the worker
  enumeration to the measured
  `informational.backend_type_distribution` set (dual-carrier cited
  per the anchor paragraph; the measured set contains
  `autovacuum launcher`, NOT `autovacuum worker`, and NO
  `parallel worker` — say so instead of listing unobserved types;
  `'autovacuum worker'` may appear only inside the anecdote row,
  never presented as part of the 2026-08-02 measured set). The
  launch-7 autovacuum anecdote (2026-07-17 00:17 CST) has NO
  repo-resolvable artifact — keeping it with a bare date would
  violate this change's own scenario 2 ("an event cited in place of
  a command"); KEEP it only by marking it with the lane's proven
  carrier wording, copied not authored — ADJUDICATED FORM (the copied
  wording's literal `.workplans/1069/` path would itself become the
  one production-domain grep hit E3 exists to remove, measured
  during implementation; the path-with-slash spelling is therefore
  avoided while keeping the carrier's full semantics, and a
  slashless mention of the directory NAME inside the
  explicitly-unverifiable parenthesis is accepted): "cited as
  unverifiable field anecdote (no committed artifact; the #1069
  arm-session bundles lived in the gitignored ``.workplans`` tree --
  issue directory ``1069`` -- which resolves nowhere in this
  repository)" (wording source:
  `openspec/changes/archive/2026-08-02-prearm-reset-compression-replay/proposal.md:14-18`),
  cross-referencing the same-source retelling at
  `scripts/node27_timeseries_compression_supervisor.py:1226-1229`,
  and dropping "deterministically". DOWNGRADE the "always accompanied
  by its leader client backend" universal to this plane's design
  ruling, stated as the plane's decision with no
  PostgreSQL-universality assertion — and state the DELIVERED
  predicate, which is two-conjunct (cross-review F1, verified P2:
  the old "judged on client backends only" wording is contradicted
  by the committed code): client-backend-ness is the necessary
  eligibility conjunct AND `has_write_privilege_on_target` is the
  G14 narrowing, per
  `scripts/node27_timeseries_compression_supervisor.py:1230-1241`
  (G14 measurement: the read-only display pool also renders as
  'client backend', so client-backend-only aborts every
  post-decompress checkpoint) and the predicates at
  supervisor `:1257-1261` / live_evidence `:943-947` (the
  one-line-earlier ranges land on the preceding statement's last
  line — corrected during the fix pass).

## 2. Spec + validation

- [x] 2.1 Spec delta: ADDED requirement in `hypertable-compression` —
  measured records in the cross-plane contract module SHALL cite
  repo-resolvable artifacts whose commands determine each stated
  claim, with 3 scenarios (dangling source, claim beyond command,
  falsified-by-snapshot narrative).
- [x] 2.2 `openspec validate contract-record-citation-convergence
  --strict --no-interactive` green.

## Evidence Floor

- [x] E1 Per-clause mapping table in the PR body: every clause of the
  three OLD records → kept-with-citation / narrowed-to-citation /
  deleted / replaced-by-snapshot-fact, one row each, with the
  covering snapshot entry named. (The table is the review oracle for
  a records-only change; there is no runtime red-proof and that is
  recorded here explicitly.)
- [x] E2 AST identity: `uv run python -c` comparing
  `ast.dump(ast.parse(git show master:...))` against the working
  file → `True`, pasted output. Constant values re-asserted by the
  machine lock: `uv run pytest -q
  tests/test_node27_external_contract_snapshot.py` (its
  `alignment_mismatches(committed) == []` and
  `len(CONTRACT_CONSTANTS) == 3` assertions are the per-constant
  value lock; runs locally, measured 19 passed). Additionally
  (cheap; proves citation-to-fixture correspondence by output rather
  than by claim): `jq` out every snapshot entry the new records cite
  and paste the outputs in the PR body next to the record text.
- [x] E3 `git grep -n "\.workplans/" -- packages scripts apps
  services workers` → zero hits (acceptance criterion #2 of the
  issue), pasted output.
- [x] E4 `uv run pytest -q
  tests/test_node27_timeseries_compression_live_evidence.py
  tests/test_node27_timeseries_compression_supervisor.py
  tests/test_node27_timeseries_compression_capture.py
  tests/test_node27_external_contract_snapshot.py` green with suite
  counts unchanged per the issue's acceptance bar (live_evidence 407
  / supervisor 164 / capture 14 = 585, plus the snapshot suite);
  `uv run ruff check .` green.
- [x] E5 Surface check: `git diff master...HEAD --name-only` = the
  one module file plus this openspec change, nothing else; snapshot
  fixture and script zero diff via the same branch-scoped form.
- [x] E6 openspec strict green (2.2).
