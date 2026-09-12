"""Guard: tracked env templates satisfy their documented required-key blocks.

Issue #2075.  ``infra/env/compute.scheduler-dbfree.env.example`` is the only
tracked source for the node-22 DB-free scheduler environment, and it shipped
without ``NHMS_ORCHESTRATOR_TERMINAL_STAGE`` — a node rebuilt from it runs the
orchestrator chain into the ``publish`` stage and fails ``DATABASE_URL_MISSING``
every cycle on a database-free host.

The required set is driven by the machine-readable block in
``infra/env/README.md`` (design decision D6), never by a hardcoded pair of key
names here: a guard that re-derived the mandate from the same ambiguous prose
that caused the drift would pass trivially and prevent nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "infra" / "env" / "README.md"
TEMPLATE = REPO_ROOT / "infra" / "env" / "compute.scheduler-dbfree.env.example"

BLOCK_NAME = "compute.scheduler-dbfree"
BLOCK_PATTERN = re.compile(
    rf"<!--\s*nhms-required-keys:\s*{re.escape(BLOCK_NAME)}\s*-->(?P<body>.*?)<!--\s*/nhms-required-keys\s*-->",
    re.DOTALL,
)
FENCE_PATTERN = re.compile(r"```[a-zA-Z0-9_-]*\n(?P<fenced>.*?)```", re.DOTALL)
KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


def parse_required_keys(markdown: str) -> dict[str, str | None]:
    """Return ``{key: pinned value or None}`` from the README's block.

    ``KEY=value`` pins the value; a bare ``KEY`` requires presence only.
    """
    match = BLOCK_PATTERN.search(markdown)
    if match is None:
        raise AssertionError(f"no nhms-required-keys block named {BLOCK_NAME!r} in the README")
    fenced = FENCE_PATTERN.search(match.group("body"))
    if fenced is None:
        raise AssertionError("the nhms-required-keys block carries no fenced code block")
    required: dict[str, str | None] = {}
    for raw in fenced.group("fenced").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not KEY_PATTERN.match(key):
            raise AssertionError(f"malformed required-key entry: {raw!r}")
        if key in required:
            raise AssertionError(f"duplicate required-key entry: {key}")
        required[key] = value.strip() if separator else None
    return required


def parse_env_template(text: str) -> dict[str, str]:
    """Return the assignments an ``EnvironmentFile`` would actually export."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            continue
        # Trailing `# ...` commentary is how this template annotates values.
        value = value.split("#", 1)[0].strip()
        values[key.strip()] = value
    return values


def check_template(required: dict[str, str | None], values: dict[str, str]) -> list[str]:
    """Return one human-readable violation per unsatisfied required key."""
    violations: list[str] = []
    for key, pinned in sorted(required.items()):
        if key not in values:
            violations.append(f"{key}: missing from the template")
        elif pinned is not None and values[key] != pinned:
            violations.append(f"{key}: expected {pinned!r}, template has {values[key]!r}")
    return violations


@pytest.fixture(scope="module")
def required_keys() -> dict[str, str | None]:
    return parse_required_keys(README.read_text())


@pytest.fixture(scope="module")
def template_values() -> dict[str, str]:
    return parse_env_template(TEMPLATE.read_text())


def test_the_readme_block_is_parseable_and_non_empty(
    required_keys: dict[str, str | None],
) -> None:
    assert len(required_keys) >= 20, required_keys


def test_template_satisfies_every_required_key_including_pinned_values(
    required_keys: dict[str, str | None], template_values: dict[str, str]
) -> None:
    """R23: key present **and** value equal where the block pins one."""
    assert check_template(required_keys, template_values) == []


def test_the_readme_block_pins_the_two_issue_2075_keys(
    required_keys: dict[str, str | None],
) -> None:
    """Guards the README itself, not the template: weakening the block to a
    presence-only entry would silently re-open #2075.
    """
    assert required_keys.get("NHMS_ORCHESTRATOR_TERMINAL_STAGE") == "forecast_state_save_qc"
    assert required_keys.get("NHMS_REQUIRE_FORECAST_WARM_START") == "true"


@pytest.mark.parametrize(
    ("mutation", "expected_fragment"),
    [
        pytest.param(
            {"NHMS_ORCHESTRATOR_TERMINAL_STAGE": "forecast"},
            "NHMS_ORCHESTRATOR_TERMINAL_STAGE: expected 'forecast_state_save_qc'",
            id="wrong_terminal_stage",
        ),
        pytest.param(
            {"NHMS_SCHEDULER_REGISTRY_BACKEND": "db"},
            "NHMS_SCHEDULER_REGISTRY_BACKEND: expected 'file'",
            id="db_backed_selector",
        ),
    ],
)
def test_the_guard_bites_on_a_wrong_pinned_value(
    required_keys: dict[str, str | None],
    template_values: dict[str, str],
    mutation: dict[str, str],
    expected_fragment: str,
) -> None:
    """`NHMS_ORCHESTRATOR_TERMINAL_STAGE=forecast` must never pass."""
    violations = check_template(required_keys, {**template_values, **mutation})

    assert any(expected_fragment in violation for violation in violations), violations


def test_the_guard_bites_on_a_missing_key(
    required_keys: dict[str, str | None], template_values: dict[str, str]
) -> None:
    reduced = {
        key: value
        for key, value in template_values.items()
        if key != "NHMS_ORCHESTRATOR_TERMINAL_STAGE"
    }

    violations = check_template(required_keys, reduced)

    assert "NHMS_ORCHESTRATOR_TERMINAL_STAGE: missing from the template" in violations


def test_the_template_carries_no_database_selector(template_values: dict[str, str]) -> None:
    """The DB-free gate refuses to start if a libpq selector is inherited, so
    the template that rebuilds this env must not introduce one."""
    forbidden = {"DATABASE_URL", "PIPELINE_DATABASE_URL"}
    present = sorted(
        key
        for key in template_values
        if key in forbidden or (key.startswith("PG") and key.isupper())
    )

    assert present == []


def test_the_runbook_states_the_same_pinned_terminal_stage() -> None:
    """#2075 acceptance: README, runbook and template stay mutually consistent."""
    runbook = (REPO_ROOT / "docs" / "runbooks" / "current-production-ops.md").read_text()

    assert "NHMS_ORCHESTRATOR_TERMINAL_STAGE=forecast_state_save_qc" in runbook
    assert "NHMS_REQUIRE_FORECAST_WARM_START=true" in runbook
