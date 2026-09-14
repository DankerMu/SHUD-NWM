"""Independent PGDATA workload refusal type.

Never interpolates a DSN, password, raw OS error, or caller path into
``str(error)``. Codes stay QUERY_*/PLAN_*/SQL_*/API_* plus a closed input/IO
set translated at the public CLI.
"""

from __future__ import annotations


class PgdataWorkloadError(Exception):
    """Stable, secret-safe PGDATA workload refusal."""

    def __init__(self, message: str, *, code: str, stage: str) -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage


def refuse(message: str, *, code: str, stage: str) -> None:
    raise PgdataWorkloadError(message, code=code, stage=stage)
