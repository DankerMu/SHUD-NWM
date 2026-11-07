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

from tests.production_ops_runbook import combined_text as production_ops_text

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
        # systemd ignores only whole-line `#` / `;` comments ...
        if not line or line.startswith(("#", ";")):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            continue
        # ... so a trailing `# ...` is part of the exported value (#2970).
        values[key.strip()] = value.strip()
    return values


# The issue #2970 acceptance grep: any `#` after an assignment's `=`.
INLINE_COMMENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=[^#]*#")
ENV_EXAMPLES = sorted((REPO_ROOT / "infra" / "env").glob("*.example"))


def inline_comment_lines(text: str) -> list[str]:
    """Return ``lineno: line`` for every assignment whose value carries a ``#``."""
    return [
        f"{lineno}: {raw}"
        for lineno, raw in enumerate(text.splitlines(), start=1)
        if INLINE_COMMENT_PATTERN.match(raw.strip())
    ]


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


def test_the_template_gateway_timeout_is_the_node22_value_and_loads(
    template_values: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#3094: node-22 runs a 300 s gateway client timeout; the documented value
    must be one the strict loader accepts, not one that fails the pass."""
    from services.orchestrator.chain import OrchestratorConfig

    assert template_values["SLURM_GATEWAY_CLIENT_TIMEOUT_SECONDS"] == "300"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "SLURM_GATEWAY_CLIENT_TIMEOUT_SECONDS", template_values["SLURM_GATEWAY_CLIENT_TIMEOUT_SECONDS"]
    )

    assert OrchestratorConfig.from_env().slurm_gateway_timeout_seconds == 300.0


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
    # #1103 split the runbook into an index page plus `production-ops/`
    # sub-runbooks; the pinned pair lives in a sub-runbook now, so the pin is
    # taken over the whole tree instead of the (command-free) index page.
    runbook = production_ops_text()

    assert "NHMS_ORCHESTRATOR_TERMINAL_STAGE=forecast_state_save_qc" in runbook
    assert "NHMS_REQUIRE_FORECAST_WARM_START=true" in runbook


def test_env_examples_are_discovered() -> None:
    assert TEMPLATE in ENV_EXAMPLES, ENV_EXAMPLES


@pytest.mark.parametrize("example", ENV_EXAMPLES, ids=lambda path: path.name)
def test_env_example_assignments_carry_no_inline_comment(example: Path) -> None:
    """#2970: systemd ``EnvironmentFile=`` exports a trailing ``# ...`` as part
    of the value, so every annotation must be a whole-line comment."""
    assert inline_comment_lines(example.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("NHMS_SCHEDULER_TIMING_LEVEL=stage    # pass|stage|candidate", id="spaced"),
        pytest.param("NHMS_SCHEDULER_EVIDENCE_MAX_MB=512#cap", id="unspaced"),
        pytest.param("  NHMS_SCHEDULER_EVIDENCE_RETENTION_DAYS=90 # days", id="indented"),
    ],
)
def test_the_inline_comment_guard_bites(line: str) -> None:
    text = f"# header\nNHMS_A=1\n{line}\n# NHMS_B=2  # a commented-out assignment is fine\n"

    assert inline_comment_lines(text) == [f"3: {line}"]


def test_parse_env_template_keeps_a_trailing_comment_like_systemd() -> None:
    text = "# whole-line comment\n; also a comment\nNHMS_SCHEDULER_TIMING_LEVEL=stage  # pass|stage\n"

    assert parse_env_template(text) == {"NHMS_SCHEDULER_TIMING_LEVEL": "stage  # pass|stage"}
