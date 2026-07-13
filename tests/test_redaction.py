from __future__ import annotations

import json

import pytest
from psycopg2.extensions import parse_dsn

from packages.common import redaction
from packages.common.redaction import (
    REDACTION_MARKER,
    redact_database_dsn,
    redact_payload,
    redact_text,
)


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


def test_redact_payload_only_preserves_allowlisted_configured_booleans() -> None:
    redacted = redact_payload(
        {
            "database_url_configured": True,
            "api_key_configured": True,
            "password_configured": False,
        }
    )

    assert redacted["database_url_configured"] is True
    assert redacted["api_key_configured"] == REDACTION_MARKER
    assert redacted["password_configured"] == REDACTION_MARKER


def test_redact_payload_preserves_structured_auth_namespace() -> None:
    redacted = redact_payload(
        {
            "receipts": {
                "auth": {
                    "provider": {
                        "issuer_url": "https://user:pass@idp.example.invalid/auth?token=secret",
                        "client_secret": "super-secret",
                    },
                    "value": "opaque-live-token",
                    "Authorization": "Bearer nested-token",
                    "message": "authorization=Basic text-token",
                }
            }
        }
    )

    auth_receipt = redacted["receipts"]["auth"]
    assert auth_receipt["provider"]["issuer_url"] == "https://idp.example.invalid/auth"
    assert auth_receipt["provider"]["client_secret"] == REDACTION_MARKER
    assert auth_receipt["value"] == REDACTION_MARKER
    assert auth_receipt["Authorization"] == REDACTION_MARKER
    assert auth_receipt["message"] == REDACTION_MARKER

    body = json.dumps(redacted, sort_keys=True)
    for raw_secret in ("user:pass@", "token=secret", "super-secret", "opaque-live-token", "nested-token", "text-token"):
        assert raw_secret not in body


def test_redact_payload_redacts_opaque_auth_namespace_scalar_values() -> None:
    redacted = redact_payload(
        {
            "gateway_response": {
                "auth": {
                    "issuer_url": "https://user:pass@idp.example.invalid/auth?token=secret",
                    "value": "opaque-live-token",
                }
            }
        }
    )

    auth = redacted["gateway_response"]["auth"]
    assert auth["issuer_url"] == "https://idp.example.invalid/auth"
    assert auth["value"] == REDACTION_MARKER

    body = json.dumps(redacted, sort_keys=True)
    assert "opaque-live-token" not in body
    assert "user:pass@" not in body
    assert "token=secret" not in body


def test_redact_payload_redacts_broad_auth_namespace_descendant_tokens() -> None:
    redacted = redact_payload(
        {
            "gateway_response": {
                "auth": {
                    "errors": [{"status": "opaque-error-token"}],
                    "scope": {
                        "provider": "opaque-provider-token",
                        "status": "opaque-status-token",
                        "message": "opaque-scope-token",
                    },
                }
            }
        }
    )

    auth = redacted["gateway_response"]["auth"]
    assert auth["errors"] == [{"status": REDACTION_MARKER}]
    assert auth["scope"]["provider"] == REDACTION_MARKER
    assert auth["scope"]["status"] == REDACTION_MARKER
    assert auth["scope"]["message"] == REDACTION_MARKER

    body = json.dumps(redacted, sort_keys=True)
    for raw_secret in (
        "opaque-error-token",
        "opaque-provider-token",
        "opaque-status-token",
        "opaque-scope-token",
    ):
        assert raw_secret not in body


def test_redact_payload_role_mapping_preserves_only_safe_action_list_shapes() -> None:
    redacted = redact_payload(
        {
            "receipts": {
                "auth": {
                    "permissions": ["jobs.cancel", {"provider": "opaque-permission-token"}],
                    "role_mapping": {
                        "operator": ["pipeline.retry_run"],
                        "viewer": "opaque-direct-role-token",
                        "model_admin": {
                            "actions": ["models.activate"],
                            "roles": "opaque-role-string-token",
                            "metadata": {"message": "opaque-role-mapping-token"},
                            "token": "role-token-secret",
                        },
                    },
                    "role_mappings": {
                        "auditor": {
                            "permissions": [
                                "dashboards.read",
                                {"message": "opaque-role-permission-token"},
                            ],
                        }
                    },
                }
            }
        }
    )

    auth = redacted["receipts"]["auth"]
    assert auth["permissions"] == ["jobs.cancel", {"provider": REDACTION_MARKER}]
    assert auth["role_mapping"]["operator"] == ["pipeline.retry_run"]
    assert auth["role_mapping"]["viewer"] == REDACTION_MARKER
    assert auth["role_mapping"]["model_admin"]["actions"] == ["models.activate"]
    assert auth["role_mapping"]["model_admin"]["roles"] == REDACTION_MARKER
    assert auth["role_mapping"]["model_admin"]["metadata"]["message"] == REDACTION_MARKER
    assert auth["role_mapping"]["model_admin"]["token"] == REDACTION_MARKER
    assert auth["role_mappings"]["auditor"]["permissions"] == [
        "dashboards.read",
        {"message": REDACTION_MARKER},
    ]

    body = json.dumps(redacted, sort_keys=True)
    for raw_secret in (
        "opaque-permission-token",
        "opaque-direct-role-token",
        "opaque-role-string-token",
        "opaque-role-mapping-token",
        "role-token-secret",
        "opaque-role-permission-token",
    ):
        assert raw_secret not in body


def test_redact_payload_recognizes_auth_live_proof_receipt_payload_context() -> None:
    redacted = redact_payload(
        {
            "items": [
                {
                    "surface": "live_backend_auth",
                    "details": {
                        "surface": "auth",
                        "status": "parsed",
                        "payload": {
                            "proof_type": "auth",
                            "surface": "live_backend_auth",
                            "schema": "nhms.production_readiness.live_proof.v1",
                            "status": "passed",
                            "value": "opaque-live-token",
                            "provider": {
                                "issuer_url": "https://user:pass@idp.example.invalid/auth?token=secret",
                            },
                            "permissions": ["jobs.cancel", {"provider": "opaque-permission-token"}],
                            "errors": [{"status": "opaque-error-token"}],
                            "scope": {
                                "provider": "opaque-provider-token",
                                "status": "opaque-status-token",
                                "message": "opaque-scope-token",
                            },
                        },
                    },
                }
            ]
        }
    )

    payload = redacted["items"][0]["details"]["payload"]
    assert payload["schema"] == "nhms.production_readiness.live_proof.v1"
    assert payload["status"] == "passed"
    assert payload["provider"]["issuer_url"] == "https://idp.example.invalid/auth"
    assert payload["permissions"] == ["jobs.cancel", {"provider": REDACTION_MARKER}]
    assert payload["value"] == REDACTION_MARKER
    assert payload["errors"] == [{"status": REDACTION_MARKER}]
    assert payload["scope"]["provider"] == REDACTION_MARKER
    assert payload["scope"]["status"] == REDACTION_MARKER
    assert payload["scope"]["message"] == REDACTION_MARKER

    body = json.dumps(redacted, sort_keys=True)
    for raw_secret in (
        "opaque-live-token",
        "opaque-permission-token",
        "opaque-error-token",
        "opaque-provider-token",
        "opaque-status-token",
        "opaque-scope-token",
        "user:pass@",
        "token=secret",
    ):
        assert raw_secret not in body


def test_redact_payload_treats_malformed_auth_receipt_details_payload_as_auth_context() -> None:
    redacted = redact_payload(
        {
            "surface": "auth",
            "status": "parsed",
            "payload": {
                "schema": "nhms.production_readiness.live_proof.v1",
                "status": "passed",
                "accepted": True,
                "value": "opaque-live-token",
                "provider": {
                    "issuer_url": "https://user:pass@idp.example.invalid/auth?token=secret",
                    "client_secret": "super-secret",
                },
                "allowed_actions": ["jobs.cancel", {"provider": "opaque-permission-token"}],
                "errors": [{"status": "opaque-error-token"}],
                "scope": {
                    "provider": "opaque-provider-token",
                    "status": "opaque-status-token",
                    "message": "opaque-scope-token",
                },
            },
        }
    )

    payload = redacted["payload"]
    assert payload["schema"] == "nhms.production_readiness.live_proof.v1"
    assert payload["status"] == "passed"
    assert payload["accepted"] is True
    assert payload["provider"]["issuer_url"] == "https://idp.example.invalid/auth"
    assert payload["provider"]["client_secret"] == REDACTION_MARKER
    assert payload["allowed_actions"] == ["jobs.cancel", {"provider": REDACTION_MARKER}]
    assert payload["value"] == REDACTION_MARKER
    assert payload["errors"] == [{"status": REDACTION_MARKER}]
    assert payload["scope"]["provider"] == REDACTION_MARKER
    assert payload["scope"]["status"] == REDACTION_MARKER
    assert payload["scope"]["message"] == REDACTION_MARKER

    body = json.dumps(redacted, sort_keys=True)
    for raw_secret in (
        "opaque-live-token",
        "opaque-permission-token",
        "opaque-error-token",
        "opaque-provider-token",
        "opaque-status-token",
        "opaque-scope-token",
        "user:pass@",
        "token=secret",
        "super-secret",
    ):
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


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ('payload="prefix {"password":"inner-json-secret"} suffix"', "inner-json-secret"),
        (
            r'payload="prefix {\\\"p\\u0061ssword\\\":\\\"layer-two-secret\\\"} suffix"',
            "layer-two-secret",
        ),
        (
            r'payload="prefix {\\\\\\\"password\\\\\\\":\\\\\\\"layer-three-secret\\\\\\\"} suffix"',
            "layer-three-secret",
        ),
    ],
)
def test_redact_text_fails_closed_for_nested_json_assignment_layers(
    raw: str, secret: str
) -> None:
    redacted = redact_text(raw)
    assert secret not in redacted
    assert REDACTION_MARKER in redacted


@pytest.mark.parametrize("key", ["password", "token", "api_key", "Authorization"])
def test_standalone_escaped_json_sensitive_assignment_is_redacted(key: str) -> None:
    raw = rf'{{\"{key}\":\"standalone-escaped-leak-value\"}}'
    redacted = redact_text(raw)
    assert "standalone-escaped-leak-value" not in redacted
    assert REDACTION_MARKER in redacted


def test_standalone_escaped_json_ordinary_assignment_remains_visible() -> None:
    raw = r'{\"ordinary.key\":\"visible\"}'
    assert redact_text(raw) == raw


@pytest.mark.parametrize(
    "key", ["token", "password", "api_key", "access.key", "session-key", "secret", "credential"]
)
@pytest.mark.parametrize(
    "value",
    [
        "Bearer bare-leak-value",
        "'Basic single quoted leak value'",
        '"Bearer double quoted leak value"',
        r'Bearer \"escaped quoted leak value\"',
        'Basic\u2003"unicode whitespace leak value"',
        "Bearer punctuation-leak-value, safe=visible",
    ],
)
def test_sensitive_assignment_redacts_complete_authorization_scheme_value(
    key: str, value: str
) -> None:
    raw = f"{key}={value}"
    redacted = redact_text(raw)
    assert "leak value" not in redacted
    assert "leak-value" not in redacted
    assert redacted.startswith(f"{key}={REDACTION_MARKER}")
    assert redact_text(redacted) == redacted
    if "safe=visible" in raw:
        assert redacted.endswith(" safe=visible")


@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("key", ["password", "api.key", "auth.header"])
@pytest.mark.parametrize("slash_count", range(8))
def test_nested_assignment_slash_run_matrix_fails_closed(
    quote: str, key: str, slash_count: int
) -> None:
    quoted = "\\" * slash_count + quote
    secret = f"matrix {key} {slash_count} secret tail"
    raw = f"payload={quote}prefix {{{quoted}{key}{quoted}:{quoted}{secret}{quoted}}} suffix{quote}"
    redacted = redact_text(raw)
    assert secret not in redacted
    assert REDACTION_MARKER in redacted


@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("slash_count", range(8))
def test_nested_assignment_slash_run_matrix_preserves_ordinary_dotted_keys(
    quote: str, slash_count: int
) -> None:
    quoted = "\\" * slash_count + quote
    raw = (
        f"payload={quote}prefix "
        f"{{{quoted}ordinary.key{quoted}:{quoted}visible-{slash_count}{quoted}}} "
        f"suffix{quote}"
    )
    assert redact_text(raw) == raw


def test_even_slash_run_assignment_scan_is_monotonic_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = redaction._skip_assignment_key_closer
    calls = 0

    def count_closer(value: str, start: int) -> int:
        nonlocal calls
        calls += 1
        return original(value, start)

    monkeypatch.setattr(redaction, "_skip_assignment_key_closer", count_closer)
    slash_run = "\\" * 200_000
    raw = f'payload="prefix {{{slash_run}"password{slash_run}":"bounded-secret"}} suffix"'
    redacted = redaction._redact_sensitive_assignments(raw)
    assert "bounded-secret" not in redacted
    assert calls <= 6


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ('Authorization=Bearer "quoted bearer secret" tail', "quoted bearer secret"),
        ("Proxy-Authorization=Basic 'quoted basic secret' tail", "quoted basic secret"),
        ('auth_header=Bearer\u2003"unicode bearer secret" tail', "unicode bearer secret"),
        (r'auth_header=Basic \"escaped basic secret\" tail', "escaped basic secret"),
        (r'Authorization=Bearer \\\"layered escaped bearer secret\\\" tail', "layered escaped bearer secret"),
        (
            r'{\"Authorization\":\"Bearer escaped-json-authorization-secret\"}',
            "escaped-json-authorization-secret",
        ),
    ],
)
def test_redact_text_consumes_quoted_authorization_credentials(
    raw: str, secret: str
) -> None:
    redacted = redact_text(raw)
    assert secret not in redacted
    assert REDACTION_MARKER in redacted


@pytest.mark.parametrize(
    "raw",
    [
        "Authorization=Bearer [redacted]",
        "Proxy-Authorization: Basic [redacted]",
        "Bearer [redacted]",
    ],
)
def test_authorization_redaction_is_idempotent(raw: str) -> None:
    assert redact_text(redact_text(raw)) == redact_text(raw)


def test_authorization_escaped_quote_staircase_has_bounded_monotonic_scan() -> None:
    staircase = "\\" * 200_000 + '"'
    raw = f"Authorization=Bearer {staircase}staircase-auth-secret{staircase} tail"
    redacted = redact_text(raw)
    assert "staircase-auth-secret" not in redacted
    assert REDACTION_MARKER in redacted


@pytest.mark.parametrize(
    ("raw", "secrets"),
    [
        ("AWS_SECRET_ACCESS_KEY=aws-secret", ("aws-secret",)),
        ("AWS_ACCESS_KEY_ID='key with spaces'", ("key with spaces",)),
        ("token=opaque-token api_key=opaque-key", ("opaque-token", "opaque-key")),
        (
            "request https://user:pass@example.test/path?X-Amz-Signature=signed#token=tail",
            ("user", "pass", "X-Amz-Signature", "signed", "token=tail"),
        ),
        (r"password=escaped\ value host=db", (r"escaped\ value",)),
    ],
)
def test_redact_text_covers_terminal_diagnostic_credential_shapes(
    raw: str, secrets: tuple[str, ...]
) -> None:
    redacted = redact_text(raw)
    for secret in secrets:
        assert secret not in redacted


def test_redact_database_dsn_covers_verbatim_decoded_and_libpq_passwords() -> None:
    dsn = "postgresql://reader:p%40ss%20word@db/nhms"
    raw = (
        f"verbatim={dsn}; decoded=p@ss word; "
        "password='quoted with spaces'; password=escaped\\ value host=db"
    )
    redacted = redact_database_dsn(raw, dsn)
    for secret in (dsn, "p%40ss%20word", "p@ss word", "quoted with spaces", "escaped\\ value"):
        assert secret not in redacted


def test_redact_text_replaces_lone_surrogates_before_utf8_output() -> None:
    redacted = redact_text("bad-\udcff-token=secret")
    assert redacted.encode("utf-8")
    assert "\udcff" not in redacted
    assert "secret" not in redacted


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('password="closed value" tail', "password=[redacted] tail"),
        ("password='closed value' tail", "password=[redacted] tail"),
        ('password="unterminated value', "password=[redacted]"),
        ("password='unterminated value", "password=[redacted]"),
        (r'password="escaped\"quote" tail', "password=[redacted] tail"),
        (r'password="even\\" tail', "password=[redacted] tail"),
        (r"password=escaped\ value host=db", "password=[redacted] host=db"),
        ("password=", "password=[redacted]"),
    ],
)
def test_sensitive_assignment_scanner_handles_quoted_and_escaped_boundaries(
    raw: str, expected: str
) -> None:
    assert redact_text(raw) == expected


def test_sensitive_assignment_scanner_handles_quoted_keys_and_consecutive_fields() -> None:
    raw = (
        '{"password" : "sec\\\"ret", \'api_key\'=\'key\\\'tail\','
        '"token":"", "safe": "visible", "ordinary": "password"}'
    )

    assert redact_text(raw) == (
        '{"password" : [redacted], \'api_key\'=[redacted],'
        '"token":[redacted], "safe": "visible", "ordinary": "password"}'
    )


@pytest.mark.parametrize(
    "raw",
    [
        'prefix "password"="one"\'api_key\':\'two\' suffix',
        "prefix 'password' = \"one\" \"api_key\" : 'two' suffix",
    ],
)
def test_sensitive_assignment_scanner_accepts_paired_single_or_double_quoted_keys(
    raw: str,
) -> None:
    redacted = redact_text(raw)
    assert "one" not in redacted and "two" not in redacted
    assert redacted.count(REDACTION_MARKER) == 2


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        (r'{"p\u0061ssword": "unicode-secret"}', "unicode-secret"),
        (r"{'\u0061pi_key': 'single-secret'}", "single-secret"),
        (r'{"passw\u006frd": "mixed-secret"}', "mixed-secret"),
        (r'{"\uD83D\uDE00password": "surrogate-pair-secret"}', "surrogate-pair-secret"),
        ('{"密password": "literal-unicode-secret"}', "literal-unicode-secret"),
    ],
)
def test_quoted_assignment_key_scanner_decodes_unicode_before_sensitive_matching(
    raw: str, secret: str
) -> None:
    redacted = redact_text(raw)
    assert secret not in redacted
    assert REDACTION_MARKER in redacted


def test_assignment_scanner_uses_authoritative_sensitive_key_catalog_only() -> None:
    assert not hasattr(redaction, "_SENSITIVE_KEY_FRAGMENTS")
    assert not hasattr(redaction, "_contains_sensitive_key_fragment")


@pytest.mark.parametrize(
    "key",
    [
        "api.key",
        "access.key",
        "session.key",
        "auth.header",
        "auth-header",
        "auth_header",
        "proxy.auth.header",
        "proxy.auth-header",
        "proxy_auth.header",
        "proxy-auth_header",
        "proxy-authorization",
        "proxy.authorization",
        "proxy_authorization",
        "ProxyAuthorization",
        "requestAuthorization",
    ],
)
def test_authoritative_catalog_covers_auth_header_and_proxy_spellings(key: str) -> None:
    assert redact_payload({key: "catalog-secret"}) == {key: REDACTION_MARKER}
    assert redact_text(f"{key}=catalog-secret") == f"{key}={REDACTION_MARKER}"


@pytest.mark.parametrize(
    "key",
    [
        "api.status",
        "access.mode",
        "session.state",
        "author.profile",
        "oauth.header",
        "authentication.header",
        "proxy.status",
    ],
)
def test_authoritative_catalog_does_not_overmatch_ordinary_dotted_keys(key: str) -> None:
    payload = {key: "visible"}
    assert redact_payload(payload) == payload
    assert redact_text(f"{key}=visible") == f"{key}=visible"


def test_authoritative_catalog_is_shared_by_database_dsn_redaction() -> None:
    raw = "driver api.key=api-secret access.key=access-secret session.key=session-secret"
    redacted = redact_database_dsn(raw, None)

    for secret in ("api-secret", "access-secret", "session-secret"):
        assert secret not in redacted
    assert redacted.count(REDACTION_MARKER) == 3


@pytest.mark.parametrize(
    "raw",
    [
        "authorization=Bearer plain-auth-secret",
        '"proxy_authorization": "Bearer quoted-auth-secret"',
        r'{"auth_\u0068eader": "Basic escaped-auth-secret"}',
        r'{"\u0061uth": "Bearer exact-auth-secret"}',
        '{"前缀authorization": "Bearer unicode-auth-secret"}',
    ],
)
def test_authoritative_catalog_covers_bare_quoted_escaped_and_unicode_auth_keys(
    raw: str,
) -> None:
    redacted = redact_text(raw)
    assert "auth-secret" not in redacted
    assert REDACTION_MARKER in redacted


@pytest.mark.parametrize(
    "raw",
    [
        '"password=double-secret"',
        "'token=single-secret'",
        'payload="api_key=payload-secret"',
        'source_error="driver password=source-secret failed"',
        r'payload="prefix \" token=escaped-secret \" suffix"',
        r'payload="prefix {\"password\":\"escaped-json-secret\"} suffix"',
        r'payload="prefix \'api_key\'=\'inner-single-secret\' suffix"',
        'payload="unterminated password=unterminated-secret',
    ],
)
def test_sensitive_inner_assignments_are_redacted_without_quote_backtracking(raw: str) -> None:
    redacted = redact_text(raw)
    assert "secret" not in redacted
    assert REDACTION_MARKER in redacted


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('"password=inner-secret": "outer-visible"', "[redacted]: [redacted]"),
        (
            'payload="token=inner-secret" = "outer-visible"',
            "payload=[redacted] = [redacted]",
        ),
    ],
)
def test_sensitive_inner_assignment_and_following_outer_value_are_both_redacted(
    raw: str, expected: str
) -> None:
    assert redact_text(raw) == expected


@pytest.mark.parametrize("whitespace", ["\v", "\f", "\u0085", "\u00a0", "\u2003"])
def test_assignment_scanner_uses_unicode_whitespace_for_separators_and_values(
    whitespace: str,
) -> None:
    raw = f"password{whitespace}={whitespace}unicode-secret{whitespace}safe=visible"
    redacted = redact_text(raw)

    assert "unicode-secret" not in redacted
    assert redacted.endswith(f"{whitespace}safe=visible")


def test_database_dsn_scanner_uses_shared_unicode_whitespace_semantics() -> None:
    dsn = "host=db password\u00a0=\u00a0dsn-secret dbname=nhms"
    redacted = redact_database_dsn("driver echoed password\u0085=\u0085dsn-secret", dsn)

    assert "dsn-secret" not in redacted
    assert REDACTION_MARKER in redacted


@pytest.mark.parametrize(
    "raw",
    [
        '"ordinary quoted fragment"',
        "payload='ordinary value'",
        'source_error="status=failed"',
    ],
)
def test_non_sensitive_quoted_fragments_remain_unchanged(raw: str) -> None:
    assert redact_text(raw) == raw


@pytest.mark.parametrize(
    "raw",
    [
        r'{"ordinary\q": "illegal-escape-secret"}',
        r'{"ordinary\u12": "truncated-unicode-secret"}',
        r'{"ordinary\uD800": "lone-high-secret"}',
        r'{"ordinary\uDC00": "lone-low-secret"}',
        r'{"ordinary\uD800\u0041": "non-low-pair-secret"}',
    ],
)
def test_malformed_quoted_assignment_keys_fail_closed_when_followed_by_assignment(
    raw: str,
) -> None:
    redacted = redact_text(raw)
    assert "secret" not in redacted
    assert REDACTION_MARKER in redacted


@pytest.mark.parametrize(
    "raw",
    [
        r'{"\u006frdinary": "visible"}',
        '{"普通键": "visible"}',
        r'{"emoji\uD83D\uDE00": "visible"}',
        r'{"quote\"key": "visible"}',
        r"{'single\'quote': 'visible'}",
    ],
)
def test_valid_escaped_or_unicode_non_sensitive_quoted_keys_remain_unchanged(raw: str) -> None:
    assert redaction._redact_sensitive_assignments(raw) == raw


def test_sensitive_assignment_scanner_handles_long_unterminated_hostile_input() -> None:
    hostile = 'password="' + "\\" * 200_000
    assert redact_text(hostile) == "password=[redacted]"


def test_sensitive_assignment_key_scanner_has_bounded_character_visits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = redaction._is_assignment_key_char
    visits = 0

    def count_visit(character: str) -> bool:
        nonlocal visits
        visits += 1
        return original(character)

    monkeypatch.setattr(redaction, "_is_assignment_key_char", count_visit)
    hostile = "ordinary-token." * 50_000 + " password=secret"
    assert redaction._redact_sensitive_assignments(hostile).endswith(" password=[redacted]")
    assert visits <= 3 * len(hostile)


def test_quoted_assignment_key_scanner_has_bounded_character_visits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = redaction._is_assignment_key_char
    visits = 0

    def count_visit(character: str) -> bool:
        nonlocal visits
        visits += 1
        return original(character)

    monkeypatch.setattr(redaction, "_is_assignment_key_char", count_visit)
    hostile = ('"ordinary_key":"value",' * 50_000) + '"password":"secret"'
    assert redaction._redact_sensitive_assignments(hostile).endswith(
        '"password":[redacted]'
    )
    assert visits <= 2 * len(hostile)


def test_unicode_quoted_assignment_key_scanner_has_bounded_character_visits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_hex = redaction._is_hex_digit
    original_key = redaction._is_assignment_key_char
    visits = 0

    def count_hex(character: str) -> bool:
        nonlocal visits
        visits += 1
        return original_hex(character)

    def count_key(character: str) -> bool:
        nonlocal visits
        visits += 1
        return original_key(character)

    monkeypatch.setattr(redaction, "_is_hex_digit", count_hex)
    monkeypatch.setattr(redaction, "_is_assignment_key_char", count_key)
    hostile = '"' + r"\u0061" * 33_333 + r'\u12": "hostile-secret"'
    redacted = redaction._redact_sensitive_assignments(hostile)
    assert "hostile-secret" not in redacted
    assert visits <= 2 * len(hostile)


def test_escaped_quote_staircase_fragment_has_deterministic_character_visit_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = redaction._is_assignment_key_char
    visits = 0

    def count_visit(character: str) -> bool:
        nonlocal visits
        visits += 1
        return original(character)

    monkeypatch.setattr(redaction, "_is_assignment_key_char", count_visit)
    hostile = '"' + r'\"' * 100_000 + ' password=staircase-secret"'
    assert redaction._redact_sensitive_assignments(hostile) == REDACTION_MARKER
    assert visits <= 2 * len(hostile)


def test_escaped_quote_staircase_with_outer_value_has_bounded_character_visits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = redaction._is_assignment_key_char
    visits = 0

    def count_visit(character: str) -> bool:
        nonlocal visits
        visits += 1
        return original(character)

    monkeypatch.setattr(redaction, "_is_assignment_key_char", count_visit)
    hostile = '"' + r'\"' * 100_000 + ' password=staircase-secret": "outer-secret"'
    assert redaction._redact_sensitive_assignments(hostile) == (
        f"{REDACTION_MARKER}: {REDACTION_MARKER}"
    )
    assert visits <= 2 * len(hostile)


def test_quoted_inner_key_fragment_has_bounded_character_visits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = redaction._is_assignment_key_char
    visits = 0

    def count_visit(character: str) -> bool:
        nonlocal visits
        visits += 1
        return original(character)

    monkeypatch.setattr(redaction, "_is_assignment_key_char", count_visit)
    hostile = 'payload="' + r'\"ordinary.key\":\"visible\",' * 20_000
    hostile += r'\"password\":\"hostile-secret\""'
    redacted = redaction._redact_sensitive_assignments(hostile)

    assert "hostile-secret" not in redacted
    assert visits <= 2 * len(hostile)


@pytest.mark.parametrize(
    "malformed_url",
    [
        "https://user:secret@example.test:not-a-port/path?token=query",
        "https://user:secret@example.test:99999/path?token=query",
        "https://user:secret@[::1/path?token=query",
        "https://user:secret@[not-ipv6]/path?token=query",
    ],
)
def test_redact_text_is_total_and_fail_closed_for_malformed_urls(malformed_url: str) -> None:
    redacted = redact_text(f"request failed: {malformed_url}")
    assert "secret" not in redacted
    assert "token=query" not in redacted
    assert REDACTION_MARKER in redacted


def test_redact_database_dsn_is_total_when_error_contains_malformed_url() -> None:
    dsn = "postgresql://reader:db-secret@db.example.test:55432/nhms"
    malformed = "https://user:url-secret@example.test:not-a-port/path?token=query"
    redacted = redact_database_dsn(f"dsn={dsn}; remote={malformed}", dsn)
    for secret in ("db-secret", "url-secret", "token=query"):
        assert secret not in redacted


@pytest.mark.parametrize(
    ("dsn", "bare_password"),
    [
        ("host=db user=reader password=plain-secret dbname=nhms", "plain-secret"),
        ("dbname=nhms password='quoted secret' user=reader host=db", "quoted secret"),
        (r"user=reader password=escaped\ secret host=db dbname=nhms", "escaped secret"),
        ("opaque-dsn-secret", None),
        ("postgresql:reader:missing-slashes-secret@db/nhms", "missing-slashes-secret"),
    ],
)
def test_redact_database_dsn_replaces_exact_dsn_with_fixed_marker_and_bare_password(
    dsn: str, bare_password: str | None
) -> None:
    raw = f"configured={dsn}"
    if bare_password is not None:
        raw += f"; driver password echo={bare_password}"
    redacted = redact_database_dsn(raw, dsn)
    assert dsn not in redacted
    assert REDACTION_MARKER in redacted
    if bare_password is not None:
        assert bare_password not in redacted


def test_redact_database_dsn_masks_reordered_keyword_echo() -> None:
    configured = "host=db user=reader password='keyword secret' dbname=nhms"
    reordered = "dbname=nhms host=db password='keyword secret' user=reader"
    redacted = redact_database_dsn(f"driver echoed {reordered}", configured)
    assert reordered not in redacted
    assert "keyword secret" not in redacted


@pytest.mark.parametrize("quoted", [False, True])
@pytest.mark.parametrize("backslash_count", [1, 2, 3])
def test_redact_database_dsn_masks_raw_and_decoded_libpq_password_bodies(
    quoted: bool, backslash_count: int
) -> None:
    raw_body = "raw-secret" + "\\" * backslash_count + "tail"
    lexical = f"'{raw_body}'" if quoted else raw_body
    dsn = f"host=db user=reader password={lexical} dbname=nhms"
    decoded_body = "raw-secret" + "\\" * (backslash_count // 2) + "tail"
    redacted = redact_database_dsn(
        f"configured={dsn}; raw={raw_body}; decoded={decoded_body}",
        dsn,
    )
    assert dsn not in redacted
    assert raw_body not in redacted
    assert decoded_body not in redacted


def test_redact_database_dsn_masks_plain_quoted_password_body() -> None:
    dsn = "host=db user=reader password='plain quoted secret' dbname=nhms"
    redacted = redact_database_dsn("driver raw=plain quoted secret", dsn)
    assert "plain quoted secret" not in redacted


def test_overlapping_dsn_candidates_are_replaced_longest_first_in_one_pass() -> None:
    dsn = r"host=db password=abc password=abc\def password=abcdef dbname=nhms"
    redacted = redact_database_dsn(
        "short=abc raw=abc\\def decoded=abcdef longest=abcdef configured=" + dsn,
        dsn,
    )
    assert "abc" not in redacted
    assert "\\def" not in redacted
    assert redacted.count(REDACTION_MARKER) == 5


def test_dsn_candidate_matching_does_not_rewrite_inserted_marker() -> None:
    dsn = "host=db password=redacted password=redacted-long dbname=nhms"
    assert redact_database_dsn("a=redacted-long b=redacted", dsn) == (
        "a=[redacted] b=[redacted]"
    )


@pytest.mark.parametrize(
    ("dsn", "echoed_secrets"),
    [
        (
            "postgresql://reader:userinfo@db/nhms?password=query%20secret",
            ("query%20secret", "query secret"),
        ),
        (
            "postgresql://reader:userinfo@db/nhms?password=first&password=effective%2Bsecond",
            ("first", "effective%2Bsecond", "effective+second"),
        ),
        (
            "postgresql://reader:userinfo@db/nhms?sslpassword=ssl%20key%2Bsecret",
            ("ssl%20key%2Bsecret", "ssl key+secret"),
        ),
        (
            r"host=db user=reader sslpassword='ssl\ key secret' dbname=nhms",
            (r"ssl\ key secret", "ssl key secret"),
        ),
    ],
)
def test_redact_database_dsn_uses_libpq_password_and_sslpassword_contract(
    dsn: str, echoed_secrets: tuple[str, ...]
) -> None:
    parsed = parse_dsn(dsn)
    assert "password" in parsed or "sslpassword" in parsed
    redacted = redact_database_dsn(" | ".join(echoed_secrets), dsn)
    for secret in echoed_secrets:
        assert secret not in redacted


def test_libpq_repeated_uri_password_uses_last_value_and_redacts_all_echoes() -> None:
    dsn = "postgresql://reader:userinfo@db/nhms?password=first&password=effective%2Bsecond"
    assert parse_dsn(dsn)["password"] == "effective+second"
    redacted = redact_database_dsn("first | effective%2Bsecond | effective+second", dsn)
    for secret in ("first", "effective%2Bsecond", "effective+second"):
        assert secret not in redacted


def test_nested_fragment_decode_work_is_fixed_and_does_not_recurse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = redaction._decode_escaped_fragment_once
    calls = 0

    def count_decode(value: str) -> str:
        nonlocal calls
        calls += 1
        return original(value)

    monkeypatch.setattr(redaction, "_decode_escaped_fragment_once", count_decode)
    hostile = 'payload="' + r"\\\\\"ordinary.key\\\\\":\\\\\"visible\\\\\"," * 20_000
    hostile += r'\\\\\"password\\\\\":\\\\\"bounded-secret\\\\\""'
    redacted = redaction._redact_sensitive_assignments(hostile)
    assert "bounded-secret" not in redacted
    assert calls <= redaction.MAX_FRAGMENT_DECODE_LAYERS - 1
