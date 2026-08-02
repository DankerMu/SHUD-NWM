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
(``RECOVERY_TARGET_SCHEMA``/``RECOVERY_TARGET_TABLE``/``RECOVERY_TARGET``), the
supervised-hypertable whitelist, and the fail-closed ``validated_probe_target``
every write-privilege probe must pass its target through.  It lives here for
the same reason as the measured values: it was hard-coded identically in two
planes with nothing forcing the copies to move together (issue #1087).
"""

from __future__ import annotations

import re

# MEASURED on the real node-27 ``nhms-db`` container (timescale/timescaledb-ha:
# pg15-latest): inside the container ``/usr/bin/pg_restore`` is a symlink whose
# ``readlink -f`` realpath is the pg_wrapper dispatcher below (the stable
# entrypoint the child actually invokes), NOT ``/usr/bin/pg_restore`` itself.
# Source: .workplans/1069/review/round-5/node27-external-contract-gate.md (§G2,
# re-measured post-fix).
CONTAINER_PG_RESTORE_REALPATH = "/usr/share/postgresql-common/pg_wrapper"

# MEASURED on the real node-27 host (systemd 249, Ubuntu 22.04): for a unit that
# has never started in the current boot, ``systemctl --user show`` renders the
# unset ``ExecMainStartTimestamp`` property as the literal string ``"n/a"``, NOT
# as an empty value.  The inactive recurring compression unit therefore reports
# ``ExecMainStartTimestamp=n/a`` while the replay unit that is actively starting
# reports a real timestamp.  Both planes pin this literal so an inactive-unit
# checkpoint accepts ``n/a`` while an "is-active" assertion rejects it.
# Source: tonight's live arming attempt (#1069, gap G6, measured post-fix).
SYSTEMD_UNSET_TIMESTAMP = "n/a"

# MEASURED on the real node-27 primary (PG 15, launch 7 postflight, 2026-07-17
# 00:17 CST, gap G9): ``pg_stat_activity.backend_type`` renders external client
# sessions as the literal ``'client backend'`` and PostgreSQL-owned workers as
# other literals (``'autovacuum worker'``, TimescaleDB background workers,
# parallel workers, ...).  The bound-1 recompress deterministically woke
# autovacuum on the compressed chunk it had just created within the same
# second postflight ran, so an "ANY non-idle session = conflict" predicate can
# essentially never pass a post-mutation checkpoint.  Only client backends can
# be the external writers the trust boundary targets (a parallel worker of an
# external query is always accompanied by its leader client backend), so both
# planes capture every session at full fidelity but judge conflicts on
# ``backend_type == CLIENT_BACKEND_TYPE`` only.
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
