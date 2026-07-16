"""Single source of truth for the node-27 DB-container external contract.

Both the replay supervisor (producer) and the live-evidence verifier bind the
container ``pg_restore`` entrypoint realpath.  Pinning it here — imported by both
planes — stops them from drifting apart, which is exactly the defect class issue
#1069 exists to kill: an external-contract value hard-coded identically in two
planes, where a fix updates one plane and leaves the twin rotted.
"""

from __future__ import annotations

# MEASURED on the real node-27 ``nhms-db`` container (timescale/timescaledb-ha:
# pg15-latest): inside the container ``/usr/bin/pg_restore`` is a symlink whose
# ``readlink -f`` realpath is the pg_wrapper dispatcher below (the stable
# entrypoint the child actually invokes), NOT ``/usr/bin/pg_restore`` itself.
# Source: .workplans/1069/review/round-5/node27-external-contract-gate.md (§G2,
# re-measured post-fix).
CONTAINER_PG_RESTORE_REALPATH = "/usr/share/postgresql-common/pg_wrapper"
