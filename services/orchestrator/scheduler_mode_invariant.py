"""The scheduler has one mode: DB-free (#3255, redesign-orchestration-core R2b).

``ProductionSchedulerConfig.db_free_required`` is constant ``True``. R2c-1
(#3256) deleted the SQL-factory arms and the other non-DB-free mode branches.
The two that remain, the PostgreSQL scheduler lease
(``scheduler_core:_build_scheduler_lease``, R2c-3) and the SQL reconcile store
(``scheduler_runtime:_restart_reconcile_store``, R2c-2), raise this error if a
config ever reports ``False`` (a duck-typed stub, a patched property), so the
retired SQL path can never run silently. ``scheduler_core:_default_orchestrator_for``
also raises it when the scheduler has no journal repository.

It subclasses ``Exception`` directly, not ``ValueError`` / ``RuntimeError`` /
``OSError``, so the scheduler's narrower fail-safe handlers do not absorb it.
"""

from __future__ import annotations

SCHEDULER_NON_DB_FREE_ARM_REACHED = "scheduler_non_db_free_arm_reached"


class SchedulerModeInvariantError(Exception):
    """A non-DB-free scheduler arm was reached; ``site`` is ``<module>:<function>``."""

    def __init__(self, *, site: str) -> None:
        self.code = SCHEDULER_NON_DB_FREE_ARM_REACHED
        self.site = site
        super().__init__(
            f"{self.code}: {site} is a retired non-DB-free scheduler arm; "
            "the production scheduler only runs in DB-free mode"
        )
