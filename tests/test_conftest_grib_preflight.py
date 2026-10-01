"""#2594: the grib opt-in preflight in ``tests/conftest.py``.

With ``NHMS_RUN_GRIB=1`` and grib items left after deselection, a session whose
ecCodes runtime cannot load must stop with ONE usage error that names the missing
runtime and the lane recipe -- not eight per-test RuntimeErrors, and never a
skip. Without the opt-in, or with no grib item left, the hook does nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from tests import conftest


@dataclass
class _Item:
    keywords: dict[str, object] = field(default_factory=dict)


@dataclass
class _Session:
    items: list[_Item]


def _grib_item() -> _Item:
    return _Item({"grib": object(), "test_decode": object()})


def _plain_item() -> _Item:
    return _Item({"test_plain": object()})


def _missing_runtime() -> str:
    raise RuntimeError("Cannot find the ecCodes library")


def _probe_must_not_run() -> str:
    raise AssertionError("the ecCodes probe ran although the preflight should have stood down")


def test_a_missing_ecCodes_runtime_is_one_usage_error_naming_it_and_the_runbook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NHMS_RUN_GRIB", "1")
    monkeypatch.setattr(conftest, "_probe_eccodes_runtime", _missing_runtime)

    with pytest.raises(pytest.UsageError) as error:
        conftest.pytest_collection_finish(_Session([_plain_item(), _grib_item()]))

    message = str(error.value)
    assert "ecCodes runtime cannot be loaded" in message
    assert "Cannot find the ecCodes library" in message
    assert "docs/runbooks/ci-test-routing.md" in message


def test_an_import_error_is_the_same_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_bindings() -> str:
        raise ModuleNotFoundError("No module named 'eccodes'")

    monkeypatch.setenv("NHMS_RUN_GRIB", "1")
    monkeypatch.setattr(conftest, "_probe_eccodes_runtime", _no_bindings)

    with pytest.raises(pytest.UsageError, match="ecCodes runtime cannot be loaded"):
        conftest.pytest_collection_finish(_Session([_grib_item()]))


@pytest.mark.parametrize("value", [None, "", "0", "false"])
def test_without_the_opt_in_the_preflight_never_probes(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv("NHMS_RUN_GRIB", raising=False)
    else:
        monkeypatch.setenv("NHMS_RUN_GRIB", value)
    monkeypatch.setattr(conftest, "_probe_eccodes_runtime", _probe_must_not_run)

    assert conftest.pytest_collection_finish(_Session([_grib_item()])) is None


@pytest.mark.parametrize("items", [[], [_plain_item(), _plain_item()]], ids=["empty", "no-grib-item"])
def test_with_no_grib_item_left_the_preflight_never_probes(monkeypatch: pytest.MonkeyPatch, items: list[_Item]) -> None:
    monkeypatch.setenv("NHMS_RUN_GRIB", "1")
    monkeypatch.setattr(conftest, "_probe_eccodes_runtime", _probe_must_not_run)

    assert conftest.pytest_collection_finish(_Session(items)) is None


def test_a_loadable_runtime_lets_the_session_through(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def _loaded() -> str:
        calls.append("probe")
        return "2.47.0"

    monkeypatch.setenv("NHMS_RUN_GRIB", "1")
    monkeypatch.setattr(conftest, "_probe_eccodes_runtime", _loaded)

    assert conftest.pytest_collection_finish(_Session([_grib_item()])) is None
    assert calls == ["probe"]
