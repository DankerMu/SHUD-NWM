"""Shared refusal type for #1895 pre-node-27 readiness owners."""

from __future__ import annotations


class Issue1895ReadinessError(Exception):
    """Stable, non-secret readiness refusal. Never carries a DSN or raw OS error."""

    def __init__(self, message: str, *, code: str, stage: str = "readiness") -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage
