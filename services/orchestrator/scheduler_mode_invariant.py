"""The scheduler has one mode: DB-free (#3255, redesign-orchestration-core R2b).

``ProductionSchedulerConfig.db_free_required`` is constant ``True``. The
non-DB-free arms still present in ``scheduler_core`` and ``scheduler_runtime``
are dead and are deleted by R2c; until then each one raises this error if a
config ever reports ``False`` (a duck-typed stub, a patched property), so the
retired SQL path can never run silently.

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
