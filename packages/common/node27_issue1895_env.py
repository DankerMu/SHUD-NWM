"""Canonical positive-decimal env validation retained for watermark/post-target consumers."""

from __future__ import annotations

import re

from packages.common.node27_issue1895_types import Issue1895ReadinessError

POSITIVE_DECIMAL_RE = re.compile(r"^[1-9][0-9]*$")


def validate_canonical_positive_decimal(value: object, *, label: str) -> str:
    """Accept only a canonical positive decimal: no sign, whitespace, or leading zeros."""

    if not isinstance(value, str) or POSITIVE_DECIMAL_RE.fullmatch(value) is None:
        raise Issue1895ReadinessError(
            f"{label} is not a canonical positive decimal",
            code="ENV_VALUE_INVALID",
            stage="env",
        )
    return value
