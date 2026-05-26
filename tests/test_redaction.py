from __future__ import annotations

import json

import pytest

from packages.common.redaction import REDACTION_MARKER, redact_payload, redact_text


@pytest.mark.parametrize(
    "payload",
    [
        {"Authorization": "Bearer live-token-123"},
        {"authorization": "Basic basic-secret-123"},
        {"headers": {"Proxy-Authorization": "Bearer proxy-secret-123"}},
        {"headers": {"auth": "Bearer short-auth-secret"}},
        {"headers": {"auth_header": "Basic header-secret"}},
    ],
)
def test_redact_payload_treats_authorization_header_keys_as_sensitive(payload: dict[str, object]) -> None:
    redacted = redact_payload(payload)
    body = json.dumps(redacted, sort_keys=True)

    for raw_secret in (
        "live-token-123",
        "basic-secret-123",
        "proxy-secret-123",
        "short-auth-secret",
        "header-secret",
    ):
        assert raw_secret not in body
    assert REDACTION_MARKER in body


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Authorization: Bearer live-token-123", "Authorization: Bearer [redacted]"),
        ("authorization=Bearer live-token-123", "authorization=Bearer [redacted]"),
        ("Authorization: Basic basic-secret-123", "Authorization: Basic [redacted]"),
        ("authorization=Basic basic-secret-123", "authorization=Basic [redacted]"),
        ("Proxy-Authorization: Bearer proxy-secret-123", "Proxy-Authorization: Bearer [redacted]"),
        ("auth_header=Basic header-secret", "auth_header=Basic [redacted]"),
        ('{"Authorization": "Bearer live-token-123"}', '{"Authorization": "Bearer [redacted]"}'),
        ("'authorization': 'Basic basic-secret-123'", "'authorization': 'Basic [redacted]'"),
        ("Authorization='Bearer live-token-123'", "Authorization='Bearer [redacted]'"),
        ('Proxy-Authorization: "Basic proxy-secret"', 'Proxy-Authorization: "Basic [redacted]"'),
    ],
)
def test_redact_text_redacts_authorization_assignment_forms(raw: str, expected: str) -> None:
    assert redact_text(raw) == expected
