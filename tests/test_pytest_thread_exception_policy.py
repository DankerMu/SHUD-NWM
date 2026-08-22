"""#1646: semantic proof that the shipping pytest config escalates thread exceptions.

Repository pytest config sets ``filterwarnings = ["error::pytest.PytestUnhandledThreadExceptionWarning"]``
and nothing else, so an unhandled worker-thread exception fails the owning test
with its original cause instead of passing as a warning-only false-green. This
suite proves that policy by running throwaway tests under the shipping config in
bounded, env-scrubbed subprocesses (``sys.executable -m pytest`` with an
explicit ``-c`` config, an explicit cwd, and no inherited ``PYTEST_*`` or
``PYTHONWARNINGS``), and by pinning the parsed config/lock state.

The repository DEFAULT is what is proved here. Ordinary Python/pytest code can
still intentionally override it: an explicit ``@pytest.mark.filterwarnings``
marker or an executed ``warnings.filterwarnings``/``simplefilter`` call on the
exact category outranks the ini error rule. Such overrides are ordinary,
reviewable test-policy choices; this suite does not and cannot statically
forbid them.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_PYPROJECT = REPO_ROOT / "pyproject.toml"
REPO_LOCK = REPO_ROOT / "uv.lock"

EXACT_FILTER = "error::pytest.PytestUnhandledThreadExceptionWarning"
EXACT_CATEGORY = "pytest.PytestUnhandledThreadExceptionWarning"
UNIQUE_CAUSE = "issue1646-policy-worker-cause"
_SUBPROCESS_TIMEOUT_SECONDS = 120
_CLEAN_ENV_KEYS = ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_TIMEOUT", "PYTHONWARNINGS")


def _clean_env() -> dict[str, str]:
    """Outer pytest/Python warning config must not leak into the probe."""
    env = dict(os.environ)
    for key in _CLEAN_ENV_KEYS:
        env.pop(key, None)
    return env


def _run_pytest(config_path: Path, test_path: Path) -> subprocess.CompletedProcess[str]:
    """Run one throwaway test under an explicit config; that config is the sole policy source."""
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:zarr",
            "-c",
            str(config_path),
            str(test_path),
        ],
        capture_output=True,
        text=True,
        cwd=str(test_path.parent),
        env=_clean_env(),
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )


def _worker_test(directory: Path) -> Path:
    """Throwaway test: a joined worker raises a unique RuntimeError."""
    test_path = directory / "test_worker_raises.py"
    test_path.write_text(
        "import threading\n"
        "\n"
        "def test_worker_raises():\n"
        "    def worker():\n"
        f"        raise RuntimeError('{UNIQUE_CAUSE}')\n"
        "    thread = threading.Thread(target=worker)\n"
        "    thread.start()\n"
        "    thread.join()\n",
        encoding="utf-8",
    )
    return test_path


def test_shipping_config_fails_worker_exception_with_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Hostile outer env: the nested probe must scrub it and stay deterministic.
    monkeypatch.setenv("PYTHONWARNINGS", "ignore")
    test_path = _worker_test(tmp_path)
    result = _run_pytest(REPO_PYPROJECT, test_path)
    combined = result.stdout + result.stderr
    assert result.returncode != 0, f"shipping config must fail, got {combined!r}"
    assert UNIQUE_CAUSE in combined
    assert EXACT_CATEGORY in combined
    leftovers = [p for p in tmp_path.iterdir() if p.name != "__pycache__"]
    assert leftovers == [test_path], f"unexpected residue: {[p.name for p in leftovers]}"


def test_removed_filter_mutant_passes_with_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONWARNINGS", "ignore")  # hostile outer env, scrubbed by the probe
    shipping_source = REPO_PYPROJECT.read_text(encoding="utf-8")
    mutant_source = "".join(
        line for line in shipping_source.splitlines(keepends=True) if EXACT_FILTER not in line
    )
    assert mutant_source != shipping_source
    for field in ("testpaths", "asyncio_mode", "markers", "filterwarnings"):
        assert field in mutant_source, f"mutant dropped {field}"
    mutant_config = tmp_path / "pyproject-mutant.toml"
    mutant_config.write_text(mutant_source, encoding="utf-8")
    result = _run_pytest(mutant_config, _worker_test(tmp_path))
    combined = result.stdout + result.stderr
    assert result.returncode == 0, f"mutant must pass, got rc={result.returncode}: {combined!r}"
    assert "1 passed" in result.stdout
    assert EXACT_CATEGORY in combined


def test_unrelated_user_warning_still_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONWARNINGS", "error")  # hostile outer env, scrubbed by the probe
    test_path = tmp_path / "test_unrelated_warning.py"
    test_path.write_text(
        "import warnings\n"
        "\n"
        "def test_emits_unrelated_warning():\n"
        "    warnings.warn('issue1646-unrelated-user-warning', UserWarning)\n",
        encoding="utf-8",
    )
    result = _run_pytest(REPO_PYPROJECT, test_path)
    combined = result.stdout + result.stderr
    assert result.returncode == 0, f"UserWarning must pass, got rc={result.returncode}: {combined!r}"
    assert "1 passed" in result.stdout
    assert "issue1646-unrelated-user-warning" in combined
    assert "UserWarning" in combined


def _repo_config() -> dict:
    with REPO_PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)


def test_repository_pins_exact_filter_and_no_timeout_config() -> None:
    ini = _repo_config()["tool"]["pytest"]["ini_options"]
    assert ini.get("filterwarnings") == [EXACT_FILTER]
    for key in ("addopts", "timeout", "timeout_method", "timeout_func_only"):
        assert key not in ini, f"repository pytest config must not set {key!r}: {ini!r}"


def test_no_pytest_timeout_dependency_or_lock_package() -> None:
    config = _repo_config()
    dev_deps = config["project"]["optional-dependencies"]["dev"]
    assert not any("pytest-timeout" in dep for dep in dev_deps)
    with REPO_LOCK.open("rb") as handle:
        lock_packages = [entry["name"] for entry in tomllib.load(handle).get("package", [])]
    assert "pytest-timeout" not in lock_packages
    markers = config["tool"]["pytest"]["ini_options"].get("markers", [])
    assert not any("timeout" in marker for marker in markers)
