"""#3155: every reconcile partition module logs under ``services.orchestrator.reconcile``.

Tests filter reconcile warnings on that exact logger name, so a leaf that falls
back to ``logging.getLogger(__name__)`` would make their "no warning" checks pass
vacuously. The module set is derived from the tree, so a new leaf is covered
without editing this file.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

_ORCHESTRATOR = Path(__file__).resolve().parents[1] / "services" / "orchestrator"
_LOGGER_NAME = "services.orchestrator.reconcile"


def _modules_defining_logger() -> list[str]:
    modules = []
    for path in sorted(_ORCHESTRATOR.glob("reconcile*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "LOGGER" for target in node.targets)
            for node in tree.body
        ):
            modules.append(f"services.orchestrator.{path.stem}")
    return modules


_LOGGING_MODULES = _modules_defining_logger()


def test_logger_discovery_covers_the_known_logging_modules() -> None:
    assert set(_LOGGING_MODULES) >= {
        "services.orchestrator.reconcile",
        "services.orchestrator.reconcile_comment_sacct",
        "services.orchestrator.reconcile_inflight",
        "services.orchestrator.reconcile_reserved",
        "services.orchestrator.reconcile_sacct_probe",
    }


@pytest.mark.parametrize("module_name", _LOGGING_MODULES)
def test_reconcile_partition_module_logs_under_the_reconcile_logger(module_name: str) -> None:
    module = importlib.import_module(module_name)

    assert module.LOGGER.name == _LOGGER_NAME
