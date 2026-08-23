from __future__ import annotations

import re
import uuid
from types import SimpleNamespace

from tests import conftest


def test_integration_gate_requires_explicit_opt_in(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://nhms:secret@localhost:5432/nhms")
    monkeypatch.delenv("NHMS_RUN_INTEGRATION", raising=False)
    monkeypatch.delenv("NHMS_INTEGRATION_DATABASE_URL", raising=False)
    monkeypatch.delenv("NHMS_ALLOW_DATABASE_URL_INTEGRATION", raising=False)

    assert conftest._integration_database_url() == ""
    assert "NHMS_RUN_INTEGRATION=1" in (conftest._integration_skip_reason() or "")


def test_integration_gate_prefers_dedicated_database_url(monkeypatch) -> None:
    monkeypatch.setenv("NHMS_RUN_INTEGRATION", "1")
    monkeypatch.setenv("DATABASE_URL", "postgresql://nhms:secret@localhost:5432/app")
    monkeypatch.setenv("NHMS_INTEGRATION_DATABASE_URL", "postgresql://nhms:secret@localhost:5432/integration")
    monkeypatch.delenv("NHMS_ALLOW_DATABASE_URL_INTEGRATION", raising=False)

    assert conftest._integration_database_url() == "postgresql://nhms:secret@localhost:5432/integration"
    assert conftest._integration_skip_reason() is None


def test_integration_gate_allows_database_url_only_with_compat_flag(monkeypatch) -> None:
    monkeypatch.setenv("NHMS_RUN_INTEGRATION", "1")
    monkeypatch.setenv("DATABASE_URL", "postgresql://nhms:secret@localhost:5432/compat")
    monkeypatch.delenv("NHMS_INTEGRATION_DATABASE_URL", raising=False)

    monkeypatch.delenv("NHMS_ALLOW_DATABASE_URL_INTEGRATION", raising=False)
    assert conftest._integration_database_url() == ""
    assert "NHMS_INTEGRATION_DATABASE_URL" in (conftest._integration_skip_reason() or "")

    monkeypatch.setenv("NHMS_ALLOW_DATABASE_URL_INTEGRATION", "1")
    assert conftest._integration_database_url() == "postgresql://nhms:secret@localhost:5432/compat"
    assert conftest._integration_skip_reason() is None


def test_integration_database_name_uses_high_entropy_uuid() -> None:
    first_name = conftest._integration_database_name()
    second_name = conftest._integration_database_name()

    assert first_name != second_name
    assert re.fullmatch(r"nhms_it_[0-9a-f]{32}", first_name)
    uuid.UUID(hex=first_name.removeprefix("nhms_it_"))


def test_integration_database_name_is_a_pure_function_of_uuid4(monkeypatch) -> None:
    """The name must be derived from uuid4 alone -- not from the pid or any other ambient input.

    Pinning the generator's declared randomness source and asserting exact equality proves that
    every unpinned input contributes nothing. The sentinel hex is deliberately not a valid uuid4
    (its version nibble is ``b``, so ``uuid.UUID(hex=...)`` yields ``version is None`` and a
    non-RFC-4122 variant), so no real uuid4 draw can coincidentally produce it -- a contaminated
    implementation fails loudly instead of matching by chance.
    """
    sentinel_hex = "deadbeef" * 4
    monkeypatch.setattr(
        conftest,
        "uuid",
        SimpleNamespace(uuid4=lambda: SimpleNamespace(hex=sentinel_hex)),
    )

    assert conftest._integration_database_name() == f"nhms_it_{sentinel_hex}"
