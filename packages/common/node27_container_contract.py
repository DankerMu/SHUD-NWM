"""Single source of truth for the node-27 host external contracts.

This module pins the measured external-contract values that both the replay
supervisor (producer) and the live-evidence verifier bind.  Pinning them here —
imported by both planes — stops them from drifting apart, which is exactly the
defect class issue #1069 exists to kill: an external-contract value hard-coded
identically in two planes, where a fix updates one plane and leaves the twin
rotted.

It covers three measured node-27 host contracts:

* the ``nhms-db`` DB-container ``pg_restore`` entrypoint realpath,
* the ``systemctl show`` rendering of an *unset* timestamp property, and
* the ``pg_stat_activity.backend_type`` value naming an external client session.

It additionally pins one contract that is NOT a measured host value but a
repo-side decision: the hypertable this supervision plane recovers and guards
(``RECOVERY_TARGET_SCHEMA``/``RECOVERY_TARGET_TABLE``/``RECOVERY_TARGET``) plus
the exact chunk and time window it recovers (``RECOVERY_TARGET_FIELDS``), the
supervised-hypertable whitelist, and the fail-closed ``validated_probe_target``
every write-privilege probe must pass its target through.  It lives here for
the same reason as the measured values: it was hard-coded identically in two
planes with nothing forcing the copies to move together (issue #1087).

For the same reason it pins a second repo-side decision: the container-side path
prefix every forensic container dump path must live inside, and the containment
predicate over it
(``CONTAINER_DB_MOUNT_PREFIX``/``container_dump_path_within_mount``).  Four
admission gates across the two planes each carried their own inline copy of the
prefix literal, spelled as a bare string prefix that constrains the string's
opening and not containment at all; a fifth chain -- the supervisor's pre-spawn
capture argv -- judged the path not at all, and this issue is what gave it a
gate.  Of the four inline copies only ``resolve_container_pg_restore_identity``
CLAIMED containment in its refusal message; the other three refuse with "argv
differs" / "argv/output ownership differs" / "not verifiable" (issue #1269).
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

# MEASURED on the real node-27 ``nhms-db`` container (image ref
# ``timescale/timescaledb-ha:pg15-latest``, cited to
# ``host_context.nhms_db_image_ref``: ``docker inspect
# '--format={{.Config.Image}}|{{.Image}}' nhms-db``, 2026-08-02): inside the
# container ``/usr/bin/pg_restore`` is a symlink whose ``readlink -f`` realpath
# is the pg_wrapper dispatcher below, NOT ``/usr/bin/pg_restore`` itself --
# cited to ``contract.container_pg_restore_realpath`` (``docker exec nhms-db
# /usr/bin/readlink -f /usr/bin/pg_restore``, 2026-08-02), whose output
# DIFFERING from its input path is what determines both of those clauses.
# Nothing is claimed here about what any child process execs: a path-resolution
# command determines no exec behavior.
# Source: packages/common/node27_external_contract_snapshot.json --
# ``contract.container_pg_restore_realpath`` and
# ``host_context.nhms_db_image_ref``, each with its ``_provenance.command`` and
# date.
CONTAINER_PG_RESTORE_REALPATH = "/usr/share/postgresql-common/pg_wrapper"

# MEASURED on the real node-27 host (``systemd 249 (249.11-0ubuntu3.21)``,
# cited to ``host_context.systemd_version``: ``systemctl --version``,
# 2026-08-02): for a NONEXISTENT unit, ``systemctl --user show`` renders the
# unset ``ExecMainStartTimestamp`` property as the literal string ``"n/a"``, NOT
# as an empty value.  DUAL-CARRIER citation, because no single entry determines
# that whole sentence: value and command come from
# ``contract.systemd_unset_timestamp`` (``systemctl --user show
# nhms-external-contract-snapshot-witness-does-not-exist.service -p LoadState -p
# ExecMainStartTimestamp``, 2026-08-02), which records no LoadState and so
# cannot by itself establish "nonexistent"; the nonexistence comes from that
# probe's design in scripts/node27_external_contract_snapshot.py --
# ``RESERVED_WITNESS_UNIT`` is a name deliberately never installed (:88-107),
# and collection refuses fail-closed unless the probe reports
# ``LoadState == "not-found"`` (:307-315).
# The same committed snapshot measures the other direction too: an
# inactive/dead unit CAN report a real ``ExecMainStartTimestamp``.
# ``informational.recurring_unit`` records
# ``nhms-node27-timeseries-compression.service`` as ``ActiveState=inactive`` /
# ``SubState=dead`` with ``ExecMainStartTimestamp="Sun 2026-08-02 12:25:00
# CST"``.  Dual-carrier again, since ``informational.*`` entries carry no
# ``_provenance`` wrapper by design: date from ``informational.measured_at``
# (``2026-08-02T11:13:05.968511Z``), command from the frozen
# ``PROBE_RECURRING_UNIT`` argv (snapshot script :180-184).  ``informational.*``
# sits OUTSIDE the drift lock -- ``COMPARED_SECTIONS`` holds only
# ``contract``/``host_context``, so these entries are dump-recorded diagnostics,
# never compared, and carry no ``contract.*``-grade guarantee.  The runtime
# consequence for the inactive-unit checkpoints below is tracked as #1255; this
# record states the measured fact only.
# Both planes REQUIRE this literal (they do not merely accept it): it is one
# member of a whole-dict equality over the recurring unit's ``systemctl show``
# properties -- scripts/node27_timeseries_compression_supervisor.py:1382-1393
# and scripts/node27_timeseries_compression_live_evidence.py:953-964 -- while
# the "is-active" side rejects it explicitly (live_evidence.py:971-978).
# Source: packages/common/node27_external_contract_snapshot.json --
# ``contract.systemd_unset_timestamp``, ``host_context.systemd_version``,
# ``informational.recurring_unit`` + ``informational.measured_at``; witness
# design and fail-closed refusal in
# scripts/node27_external_contract_snapshot.py (:88-107, :180-184, :307-315).
SYSTEMD_UNSET_TIMESTAMP = "n/a"

# MEASURED on the real node-27 primary (PG 15 -- ``15.2 (Ubuntu
# 15.2-1.pgdg22.04+1)``, cited to ``host_context.pg_server_version``: ``psql
# --dbname nhms --no-psqlrc -At -c 'SHOW server_version'``, 2026-08-02):
# ``pg_stat_activity.backend_type`` renders an external client session as the
# literal ``'client backend'`` -- cited to ``contract.client_backend_type``
# (``psql --dbname nhms --no-psqlrc -At -c 'SELECT backend_type FROM
# pg_stat_activity WHERE pid = pg_backend_pid()'``, 2026-08-02, i.e. the
# probe's own external session).
# The other literals are not enumerated speculatively.  The measured 2026-08-02
# distribution over every session, ``informational.backend_type_distribution``,
# is: ``TimescaleDB Background Worker Launcher`` 1, ``TimescaleDB Background
# Worker Scheduler`` 2, ``autovacuum launcher`` 1, ``background writer`` 1,
# ``checkpointer`` 1, ``client backend`` 14, ``logical replication launcher`` 1,
# ``walwriter`` 1.  Note what that set does and does not contain: it carries
# ``autovacuum launcher`` and NOT ``autovacuum worker``, and it contains no
# ``parallel worker`` at all.  Dual-carrier citation, as ``informational.*``
# carries no ``_provenance`` wrapper by design: date from
# ``informational.measured_at`` (``2026-08-02T11:13:05.968511Z``), command from
# the frozen ``PROBE_BACKEND_TYPE_DISTRIBUTION`` argv (snapshot script
# :204-208); and ``informational.*`` sits OUTSIDE the drift lock
# (``COMPARED_SECTIONS`` holds only ``contract``/``host_context``), so this set
# is a dump-recorded diagnostic, never compared -- one host at one instant, not
# a guarantee.
# FIELD ANECDOTE (launch 7 postflight, 2026-07-17 00:17 CST, gap G9), cited as
# unverifiable field anecdote (no committed artifact; the #1069 arm-session
# bundles lived in the gitignored ``.workplans`` tree -- issue directory
# ``1069`` -- which resolves nowhere in this repository): the bound-1 recompress
# woke autovacuum on the compressed chunk it had just created within the same
# second postflight ran, so an "ANY non-idle session = conflict" predicate can
# essentially never pass a post-mutation checkpoint.  The same anecdote is
# retold from the same source at
# scripts/node27_timeseries_compression_supervisor.py:1226-1229.
# DESIGN RULING of this plane (a decision, not a measurement): the external
# writers the trust boundary targets are judged on ``backend_type ==
# CLIENT_BACKEND_TYPE`` only, so both planes capture every session at full
# fidelity but judge conflicts on client backends alone.  Nothing is asserted
# here about PostgreSQL's semantics for any other backend type.
# Source: packages/common/node27_external_contract_snapshot.json --
# ``contract.client_backend_type``, ``host_context.pg_server_version``,
# ``informational.backend_type_distribution`` + ``informational.measured_at``.
CLIENT_BACKEND_TYPE = "client backend"

# REPO-SIDE PINNED CONTRACT (not a measured host value, gap G14 / issue #1087):
# the single recovery target of this supervision plane.  The supervisor pins it
# twice -- once in the expected decompress argv
# (``--hypertable-schema``/``--hypertable-name``) and once inside the
# ``has_write_privilege_on_target`` probe that decides whether a concurrent
# session is a conflicting writer -- and the benchmark carries a third copy in
# its own activity SQL.  Sourcing all three here is what stops a future target
# switch from silently leaving the probe pointed at the old table, where a real
# writer on the new target would be judged ``has_write_privilege_on_target =
# false`` and the checkpoint would pass.
RECOVERY_TARGET_SCHEMA = "hydro"
RECOVERY_TARGET_TABLE = "river_timeseries"
RECOVERY_TARGET = f"{RECOVERY_TARGET_SCHEMA}.{RECOVERY_TARGET_TABLE}"

# The recovery target is not the hypertable pair alone: the exact-chunk recovery
# names one chunk of that hypertable and the time window it covers (issue
# #1242).  Those four fields used to live in mutually independent copies, so a
# retarget could move the hypertable half and strand the chunk/range half.
RECOVERY_TARGET_CHUNK_SCHEMA = "_timescaledb_internal"
RECOVERY_TARGET_CHUNK_NAME = "_hyper_3_7_chunk"
RECOVERY_TARGET_RANGE_START = "2026-05-28T00:00:00Z"
RECOVERY_TARGET_RANGE_END = "2026-06-04T00:00:00Z"

# THE single assembly point of the six-field recovery-target contract.  Bound to
# this mapping today, so mutating one of them alone fails a test:
#   * derived from it directly -- the supervisor's expected decompress argv and
#     the capture producer's ``RECOVERY_TARGET`` dict plus the
#     ``RECOVERY_PREFLIGHT_SQL`` and ``CATALOG_POST_SQL`` it interpolates (both
#     rendered strings byte-frozen in
#     tests/test_node27_timeseries_compression_capture.py, issues #1242/#1244);
#     and the replay producer's ``TARGET``/``TARGET_RELATION`` in
#     scripts/node27_timeseries_decompression_replay.py (#1245), whose key set
#     is also that script's CLI flag contract;
#   * asserted field-by-field equal to it by the drift guard in
#     tests/test_node27_timeseries_compression_supervisor.py -- the live-evidence
#     schema ``$defs.recovery_target`` consts, the schema's
#     ``decompress_return_relation`` const, and the verifier's own
#     ``RECOVERY_TARGET``/``RECOVERY_RETURN_RELATION`` module constants;
#   * bound transitively -- ``plan_author``'s ``_RECOVERY_TARGET_ARGS`` literals,
#     because the plan it emits must pass the real supervisor gate in
#     tests/test_node27_timeseries_compression_capture.py, whose expected argv is
#     derived from here; and the verifier's expected decompress argv tail
#     (``_validate_exact_command_argv``, kind ``decompress``), which interpolates
#     the verifier's own guard-bound ``RECOVERY_TARGET`` rather than importing
#     the six fields from this module (#1244).
# Closure as of #1245: no PRODUCTION PYTHON copy of the six-field target remains
# outside the bound set -- the replay producer above was the last one.  Scoped
# deliberately: runbook prose (docs/runbooks/tier-node27-timeseries-storage.md)
# and the intentional byte-freeze literals in the test suites are documentation
# and frozen oracles, not derivation sites.  Any FUTURE copy that cannot derive
# from here must be named in this ledger with a tracked follow-up.
# Note the naming: ``RECOVERY_TARGET`` above is the ``schema.table`` STRING the
# write-privilege probe interpolates, while this is the six-field mapping the
# evidence documents carry.
RECOVERY_TARGET_FIELDS = {
    "hypertable_schema": RECOVERY_TARGET_SCHEMA,
    "hypertable_name": RECOVERY_TARGET_TABLE,
    "chunk_schema": RECOVERY_TARGET_CHUNK_SCHEMA,
    "chunk_name": RECOVERY_TARGET_CHUNK_NAME,
    "range_start": RECOVERY_TARGET_RANGE_START,
    "range_end": RECOVERY_TARGET_RANGE_END,
}

# The complete set of hypertables this plane supervises; any probe target
# outside it is a bug, not a configuration.  Mirrors ``HYPERTABLE_KEYS`` in the
# live-evidence verifier and the capture helper (guarded by tests).
SUPERVISED_HYPERTABLES = ("hydro.river_timeseries", "met.forcing_station_timeseries")

# A probe target is interpolated into SQL as a bare literal, so it must be a
# strict lowercase ``schema.table`` identifier -- no quoting, whitespace,
# comment or statement terminator can survive this.
_PROBE_TARGET_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")


def validated_probe_target(target: str) -> str:
    """Return ``target`` unchanged, or raise before any probe SQL can exist.

    Fail-closed on both axes: the target must name a supervised hypertable AND
    match the strict ``schema.table`` identifier form.
    """

    if target not in SUPERVISED_HYPERTABLES:
        raise ValueError(f"probe target is not a supervised hypertable: {target!r}")
    if _PROBE_TARGET_PATTERN.fullmatch(target) is None:
        raise ValueError(f"probe target is not a strict schema.table identifier: {target!r}")
    return target


# REPO-SIDE PINNED CONTRACT (not a measured host value, issue #1269): the
# container-side path prefix every forensic container dump path must live
# inside.  Repo-side is the DECISION of where dumps must live; the mount layout
# it is drawn over is measured (``docker inspect nhms-db``, 2026-08-02), and
# that output determines exactly this much (its ``.Mounts`` for the binds, its
# ``Config.Env`` ``PGDATA`` for which directory is the DB's own data directory):
# the container carries two host bind mounts, of which the only one
# landing under this prefix is ``/home/nwm/nhms-evidence`` ->
# ``/var/lib/postgresql/evidence`` (RW), while the DB's own data directory is
# the OTHER mount (``/home/nwm/nhms-pgdata`` -> ``/home/postgres/pgdata/data``),
# outside this prefix.  So the host-writable region inside the prefix is that
# ``evidence`` SUBTREE.  Nothing is claimed here about what the prefix path
# itself is or is not -- that inspect output cannot determine it.
# FIVE gates judge that path today, but they did not start level.  FOUR carried
# their own inline ``startswith`` of this literal -- the verifier's pg_restore
# list argv gate and its captured schema-dump-list listing gate, the
# supervisor's mirror argv gate and ``resolve_container_pg_restore_identity`` --
# which constrains the string's OPENING and not containment at all:
# ``/var/lib/postgresql/../../../etc/shadow`` satisfies the prefix and
# normalizes to ``/etc/shadow``, while the supervisor really
# ``docker exec sha256sum``s that path into the forensic bundle.  The FIFTH --
# the supervisor's pre-spawn capture-argv gate -- carried no containment check
# at all until this issue added one, so the ``--schema-dump-container`` value
# the spawned capture producer really ``docker exec pg_restore --list``s went
# unjudged on that route.  Of those FOUR inline copies only
# ``resolve_container_pg_restore_identity`` ever claimed containment in its
# refusal ("pg_restore dump path is outside the DB container data mount" --
# deliberately kept byte-unchanged, this change edits none of the four
# pre-existing refusal texts; "the DB container data mount" there denotes the
# prefix as scoped above); the other three say only that an argv "differs" or
# that an identity is "not verifiable".  The fifth gate's refusal is text this
# change writes, and it names this prefix rather than that older noun.
CONTAINER_DB_MOUNT_PREFIX = "/var/lib/postgresql/"


def container_dump_path_within_mount(value: str) -> bool:
    """Whether ``value`` names a path inside the pinned container dump prefix.

    Judges, never rewrites: the caller keeps recording and comparing the plan's
    original string, so the lane's verbatim forensic posture survives intact (no
    ``normpath``, no ``Path()`` write-back anywhere on this route).  Two textual
    conjuncts -- the prefix scoped at ``CONTAINER_DB_MOUNT_PREFIX`` AND no ``..``
    component, the same ``..``-parts shape the plan author's own canonicality
    guard uses.  ``PurePosixPath`` drops empty components, so an interior double
    slash (``/var/lib/postgresql//evidence/...``) and a trailing slash stay
    admitted exactly as the bare prefix admitted them, leaving the #1268
    authoring adjudication untouched -- and both of those rows survive
    normalization too, so they are NOT what separates this implementation from a
    ``resolve()``/``normpath``-based one.  The single accept row that would flip
    is the bare prefix root ``/var/lib/postgresql/`` (test id
    ``bare_mount_root``): it normalizes to ``/var/lib/postgresql``, which no
    longer carries the trailing-slash prefix and would be refused.

    Two recorded residuals, both outside what a textual predicate can reach:

    * A symlink planted INSIDE the mount still escapes the container
      ``sha256sum``, which follows symlinks.  Planting one does NOT require
      access inside the DB container: the mount's HOST side is where the plan's
      own ``pg_dump`` writes ``--file <schema_dump_host>``, so a host-side writer
      can create the link and name it through its container path.  That is the
      same actor class the pinned-plan boundary already assumes, not a stronger
      one.  Measured node-27 container shape (``docker inspect nhms-db``,
      2026-08-02; its ``.Mounts`` for the binds, its ``Config.Env`` ``PGDATA``
      for which directory is the DB's data directory): the only bind mount
      landing under this prefix is host ``/home/nwm/nhms-evidence`` -> container
      ``/var/lib/postgresql/evidence`` (RW), i.e. the host-writable region is a
      SUBTREE of the prefix this predicate spans, and the DB data directory is a
      separate mount outside it (``/home/postgres/pgdata/data``).  What this
      predicate closes is the pure-string traversal, which needs no filesystem
      foothold at all; a planted symlink stays open.
    * ``/var/lib/postgresql/..\\x00/etc`` (Python escape form) is ADMITTED:
      ``PurePosixPath`` keeps ``..\\x00`` as one component, so the ``..``
      conjunct never matches it, while an ``execve``-truncated reading of the
      same bytes means ``/var/lib``.  Not an escape in practice -- CPython raises
      ``ValueError: embedded null byte`` before any spawn -- and the argv token
      model's lack of a NUL check is pre-existing and out of scope here.
    """

    return value.startswith(CONTAINER_DB_MOUNT_PREFIX) and ".." not in PurePosixPath(value).parts
