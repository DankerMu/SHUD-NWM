from __future__ import annotations

import os
import re
import uuid

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
    assert str(os.getpid()) not in first_name
    uuid.UUID(hex=first_name.removeprefix("nhms_it_"))
