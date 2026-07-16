"""Single source of truth for the node-27 host external contracts.

This module pins the measured external-contract values that both the replay
supervisor (producer) and the live-evidence verifier bind.  Pinning them here —
imported by both planes — stops them from drifting apart, which is exactly the
defect class issue #1069 exists to kill: an external-contract value hard-coded
identically in two planes, where a fix updates one plane and leaves the twin
rotted.

It covers two measured node-27 host contracts:

* the ``nhms-db`` DB-container ``pg_restore`` entrypoint realpath, and
* the ``systemctl show`` rendering of an *unset* timestamp property.
"""

from __future__ import annotations

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
