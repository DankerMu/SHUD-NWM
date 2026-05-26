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


def test_redact_payload_preserves_structured_auth_namespace() -> None:
    redacted = redact_payload(
        {
            "receipts": {
                "auth": {
                    "provider": {
                        "issuer_url": "https://user:pass@idp.example.invalid/auth?token=secret",
                        "client_secret": "super-secret",
                    },
                    "Authorization": "Bearer nested-token",
                    "message": "authorization=Basic text-token",
                }
            }
        }
    )

    auth_receipt = redacted["receipts"]["auth"]
    assert auth_receipt["provider"]["issuer_url"] == "https://idp.example.invalid/auth"
    assert auth_receipt["provider"]["client_secret"] == REDACTION_MARKER
    assert auth_receipt["Authorization"] == REDACTION_MARKER
    assert auth_receipt["message"] == "authorization=Basic [redacted]"

    body = json.dumps(redacted, sort_keys=True)
    for raw_secret in ("user:pass@", "token=secret", "super-secret", "nested-token", "text-token"):
        assert raw_secret not in body


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


@pytest.mark.parametrize(
    ("raw", "expected", "raw_secrets"),
    [
        ("Bearer live-token-123", "Bearer [redacted]", ("live-token-123",)),
        ("Basic basic-secret-123", "Basic [redacted]", ("basic-secret-123",)),
        (
            'gateway stderr="Bearer quoted-token-123"',
            'gateway stderr="Bearer [redacted]"',
            ("quoted-token-123",),
        ),
        (
            "retry failed: Basic quoted-basic-secret-123; queued next",
            "retry failed: Basic [redacted]; queued next",
            ("quoted-basic-secret-123",),
        ),
        (
            "log line Bearer punct-token-123, status follows",
            "log line Bearer [redacted], status follows",
            ("punct-token-123",),
        ),
        (
            "tail Basic terminal-basic-secret-123. retry pending",
            "tail Basic [redacted]. retry pending",
            ("terminal-basic-secret-123",),
        ),
    ],
)
def test_redact_text_redacts_free_form_authorization_scheme_credentials(
    raw: str,
    expected: str,
    raw_secrets: tuple[str, ...],
) -> None:
    redacted = redact_text(raw)

    assert redacted == expected
    for raw_secret in raw_secrets:
        assert raw_secret not in redacted
