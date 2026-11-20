"""The ``nhms-pipeline`` subcommand surface, pinned on both parsers.

The click parser (``_click_main``) is the production path because click is a
hard dependency; ``_argparse_main`` is the fallback.  Both must expose exactly
the same subcommands, and the retired ``trigger-analysis`` subcommand must be a
usage error (exit 2) on both.
"""

from __future__ import annotations

import argparse

import click
import pytest

from services.orchestrator import cli as cli_module

EXPECTED_SUBCOMMANDS = frozenset(
    {
        "publish-tiles",
        "publish-qdown",
        "cleanup",
        "migrate-scheduler-state",
        "prepare-file-journal-rollback",
        "launch-file-journal-rollback-writer",
        "complete-file-journal-rollforward",
        "plan-production",
        "recover-released-identity-blocked-reservation",
        "demote-reserved-job",
        "bind-reserved-job",
        "census-job-id-scope",
        "list-operator-actions",
        "confirm-operator-reentry",
    }
)

RETIRED_ARGV = ["trigger-analysis", "--model-id", "m", "--date-range", "r"]


class _ParserCaptured(Exception):
    pass


def _argparse_subcommands(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    captured: list[argparse.ArgumentParser] = []

    def capture(self: argparse.ArgumentParser, *_args: object, **_kwargs: object) -> None:
        captured.append(self)
        raise _ParserCaptured

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", capture)
    with pytest.raises(_ParserCaptured):
        cli_module._argparse_main([])
    monkeypatch.undo()

    (parser,) = captured
    actions = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
    assert len(actions) == 1
    return set(actions[0].choices)


def _click_subcommands(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    captured: list[click.Group] = []

    def capture(self: click.Group, *_args: object, **_kwargs: object) -> None:
        captured.append(self)

    monkeypatch.setattr(click.Group, "main", capture)
    cli_module._click_main([])
    monkeypatch.undo()

    (group,) = captured
    return set(group.commands)


def test_argparse_subcommand_set_is_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _argparse_subcommands(monkeypatch) == EXPECTED_SUBCOMMANDS


def test_click_subcommand_set_is_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _click_subcommands(monkeypatch) == EXPECTED_SUBCOMMANDS


def test_both_parsers_expose_the_same_subcommands(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _argparse_subcommands(monkeypatch) == _click_subcommands(monkeypatch)


@pytest.mark.parametrize("subcommand", sorted(EXPECTED_SUBCOMMANDS))
def test_argparse_subcommand_help_parses(subcommand: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        cli_module._argparse_main([subcommand, "--help"])
    assert error.value.code == 0
    assert subcommand in capsys.readouterr().out


@pytest.mark.parametrize("subcommand", sorted(EXPECTED_SUBCOMMANDS))
def test_click_subcommand_help_parses(subcommand: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_module._click_main([subcommand, "--help"]) == 0
    assert subcommand in capsys.readouterr().out


def test_argparse_retired_trigger_subcommand_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        cli_module._argparse_main(RETIRED_ARGV)
    assert error.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_click_retired_trigger_subcommand_is_usage_error() -> None:
    with pytest.raises(click.UsageError) as error:
        cli_module._click_main(RETIRED_ARGV)
    assert error.value.exit_code == 2
    assert "No such command" in error.value.format_message()
