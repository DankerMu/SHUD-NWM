from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

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


# #3376: an opted-in session aimed at the production postmaster port ends before collection.

_SECRET = "tripwire-secret-pw"
_HOST = "tripwire-host.invalid"
_REPO = Path(__file__).resolve().parents[1]


def _clean_integration_env(monkeypatch) -> None:
    for name in list(os.environ):
        if name.startswith("NHMS_") or name in {"DATABASE_URL", "PGPORT"}:
            monkeypatch.delenv(name)


class _StubConfig:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def addinivalue_line(self, name: str, line: str) -> None:
        self.lines.append((name, line))


def _assert_refusal_message(error: pytest.ExceptionInfo) -> None:
    message = str(error.value)
    assert "scripts/node27_integration_pg.py" in message
    assert "#3376" in message
    assert _SECRET not in message
    assert _HOST not in message


@pytest.mark.parametrize(
    ("url", "pgport"),
    [
        (f"postgresql://postgres:{_SECRET}@{_HOST}:55432/postgres", None),
        (f"host={_HOST} port=55432 dbname=postgres user=postgres password={_SECRET}", None),
        (f"postgresql://postgres:{_SECRET}@{_HOST}:5432,other-{_HOST}:55432/postgres", None),
        (f"postgresql://postgres:{_SECRET}@{_HOST}/postgres", "55432"),
    ],
    ids=["uri", "keyword", "multi-host", "pgport"],
)
def test_production_port_is_refused(monkeypatch, url: str, pgport: str | None) -> None:
    _clean_integration_env(monkeypatch)
    if pgport is not None:
        monkeypatch.setenv("PGPORT", pgport)

    with pytest.raises(pytest.UsageError) as error:
        conftest._refuse_production_postmaster(url)

    _assert_refusal_message(error)


@pytest.mark.parametrize(
    ("url", "pgport"),
    [
        (f"postgresql://postgres:{_SECRET}@{_HOST}:5432/postgres", None),
        (f"postgresql://postgres:{_SECRET}@{_HOST}:55442/postgres", None),
        (f"postgresql://postgres:{_SECRET}@{_HOST}/postgres", None),
        (f"postgresql://postgres:{_SECRET}@{_HOST}:5432/postgres", "55432"),
        (f"postgresql://postgres:{_SECRET}@[::1:55432/postgres", None),
    ],
    ids=["5432", "55442", "no-port-no-pgport", "explicit-port-beats-pgport", "unparsable"],
)
def test_other_ports_and_unparsable_urls_pass(monkeypatch, url: str, pgport: str | None) -> None:
    _clean_integration_env(monkeypatch)
    if pgport is not None:
        monkeypatch.setenv("PGPORT", pgport)

    assert conftest._refuse_production_postmaster(url) is None


def test_configure_refuses_opted_in_dedicated_url_at_production_port(monkeypatch) -> None:
    _clean_integration_env(monkeypatch)
    monkeypatch.setenv("NHMS_RUN_INTEGRATION", "1")
    monkeypatch.setenv("NHMS_INTEGRATION_DATABASE_URL", f"postgresql://postgres:{_SECRET}@{_HOST}:55432/postgres")

    with pytest.raises(pytest.UsageError) as error:
        conftest.pytest_configure(_StubConfig())

    _assert_refusal_message(error)


def test_configure_ignores_production_port_without_opt_in(monkeypatch) -> None:
    _clean_integration_env(monkeypatch)
    monkeypatch.setenv("NHMS_INTEGRATION_DATABASE_URL", f"postgresql://postgres:{_SECRET}@{_HOST}:55432/postgres")
    config = _StubConfig()

    assert conftest.pytest_configure(config) is None
    assert any(line.startswith("integration:") for _, line in config.lines)


def test_configure_ignores_opt_in_without_integration_url(monkeypatch) -> None:
    _clean_integration_env(monkeypatch)
    monkeypatch.setenv("NHMS_RUN_INTEGRATION", "1")
    monkeypatch.setenv("DATABASE_URL", f"postgresql://postgres:{_SECRET}@{_HOST}:55432/postgres")

    assert conftest.pytest_configure(_StubConfig()) is None


def test_configure_follows_generic_url_only_under_compatibility_flag(monkeypatch) -> None:
    _clean_integration_env(monkeypatch)
    monkeypatch.setenv("NHMS_RUN_INTEGRATION", "1")
    monkeypatch.setenv("DATABASE_URL", f"postgresql://postgres:{_SECRET}@{_HOST}:55432/postgres")

    assert conftest.pytest_configure(_StubConfig()) is None

    monkeypatch.setenv("NHMS_ALLOW_DATABASE_URL_INTEGRATION", "1")
    with pytest.raises(pytest.UsageError) as error:
        conftest.pytest_configure(_StubConfig())

    _assert_refusal_message(error)


def _collect_only(extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("NHMS_") and name not in {"DATABASE_URL", "PGPORT"}
    }
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_integration_gate.py"]
        + ["--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def test_pytest_session_aimed_at_production_port_ends_before_collection() -> None:
    refused = _collect_only(
        {
            "NHMS_RUN_INTEGRATION": "1",
            "NHMS_INTEGRATION_DATABASE_URL": f"postgresql://postgres:{_SECRET}@{_HOST}:55432/postgres",
        }
    )
    output = refused.stdout + refused.stderr

    assert refused.returncode == 4, output
    assert "test_integration_gate.py::" not in output
    assert "scripts/node27_integration_pg.py" in output
    assert _SECRET not in output

    plain = _collect_only({})
    assert plain.returncode == 0, plain.stdout + plain.stderr
    assert "test_integration_gate.py::test_integration_gate_requires_explicit_opt_in" in plain.stdout
