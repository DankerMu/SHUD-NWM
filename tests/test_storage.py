import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from packages.common.storage import (
    DEFAULT_RETENTION_WINDOW_DAYS,
    RETENTION_ENV_PATH_VARIABLE,
    VALID_PREFIX_PATTERNS,
    ArchiveConfigurationError,
    read_retention_window_days,
    validate_object_path,
)
from scripts import node27_raw_retention, node27_resource_governance, node27_timeseries_retention


@pytest.mark.parametrize(
    ("path", "category", "expected_components"),
    [
        (
            "raw/gfs/2026050100/gfs_t2m.grib2",
            "raw",
            {"source": "gfs", "cycle_time": "2026050100"},
        ),
        (
            "canonical/gfs/2026050100/t2m/data.nc",
            "canonical",
            {"source": "gfs", "cycle_time": "2026050100", "variable": "t2m"},
        ),
        (
            "forcing/gfs/2026050100/yangtze_v2026_01/yangtze_shud_v12/forcing.tar.gz",
            "forcing",
            {
                "source": "gfs",
                "cycle_time": "2026050100",
                "basin_version_id": "yangtze_v2026_01",
                "model_id": "yangtze_shud_v12",
            },
        ),
        (
            "models/yangtze_shud_v12/model_package.tar.gz",
            "models",
            {"model_id": "yangtze_shud_v12"},
        ),
        (
            "states/yangtze_shud_v12/2026050100/state.ic",
            "states",
            {"model_id": "yangtze_shud_v12", "valid_time": "2026050100"},
        ),
        (
            "runs/fcst_gfs_2026050100_yangtze_shud_v12/input/manifest.json",
            "runs",
            {"run_id": "fcst_gfs_2026050100_yangtze_shud_v12", "sub_prefix": "input"},
        ),
        (
            "runs/fcst_gfs_2026050100_yangtze_shud_v12/output/rivqdown.csv",
            "runs",
            {"run_id": "fcst_gfs_2026050100_yangtze_shud_v12", "sub_prefix": "output"},
        ),
        (
            "runs/fcst_gfs_2026050100_yangtze_shud_v12/logs/run.log",
            "runs",
            {"run_id": "fcst_gfs_2026050100_yangtze_shud_v12", "sub_prefix": "logs"},
        ),
        (
            "tiles/hydro/run123/tile.pbf",
            "tiles",
            {"tile_type": "hydro", "run_id": "run123"},
        ),
    ],
)
def test_validate_object_path_happy_paths(
    path: str,
    category: str,
    expected_components: dict[str, str],
) -> None:
    result = validate_object_path(path)

    assert result.valid is True
    assert result.category == category
    assert result.error is None
    assert result.components == expected_components


@pytest.mark.parametrize(
    "path",
    [
        "s3://nhms/raw/gfs/2026050100/file.grib2",
        "s3://other-bucket/raw/gfs/2026050100/file.grib2",
    ],
)
def test_validate_object_path_accepts_s3_uris(path: str) -> None:
    result = validate_object_path(path)

    assert result.valid is True
    assert result.category == "raw"
    assert result.components == {"source": "gfs", "cycle_time": "2026050100"}


@pytest.mark.parametrize(
    "path",
    [
        "data/gfs/something.grib2",
        "invalid/path",
        "forcing/gfs/file.tar.gz",
        "",
        "/",
    ],
)
def test_validate_object_path_errors(path: str) -> None:
    result = validate_object_path(path)

    assert result.valid is False
    assert result.category is None
    assert result.components == {}
    assert result.error is not None
    assert "Valid prefixes:" in result.error
    for pattern in VALID_PREFIX_PATTERNS:
        assert pattern.display in result.error


def test_validate_object_path_unknown_prefix_error_is_descriptive() -> None:
    result = validate_object_path("data/gfs/something.grib2")

    assert result.error is not None
    assert "Unrecognized object path prefix" in result.error


# ---------------------------------------------------------------------------
# #1227 — the min-age guard compares against the LIVE DB retention window.
# Helper seam: `read_retention_window_days` extracts one variable from the
# deployed retention env file with shell-source lexical semantics.
# ---------------------------------------------------------------------------

_WINDOW_VAR = "NODE27_TIMESERIES_RETENTION_WINDOW_DAYS"
_RETENTION_ENV_VAR = RETENTION_ENV_PATH_VARIABLE
_REPO_ROOT = Path(__file__).resolve().parents[1]

# The runner-equivalent default applies ONLY to a file that is recognizably the
# deployed retention env (#1227 design D1 round-1 amendment): at least one
# NODE27_TIMESERIES_RETENTION_* assignment accepted. This sibling is a real
# variable from infra/env/node27-timeseries-retention.example.
_SIBLING = "NODE27_TIMESERIES_RETENTION_PER_TICK_BOUND=5"


def _retention_env(tmp_path: Path, body: str, *, name: str = "retention.env") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


_LEXICAL_ROWS = [
    pytest.param(f"{_WINDOW_VAR}=21\n", 21, id="plain"),
    pytest.param(
        f"DATABASE_URL=postgresql://x\n{_SIBLING}\n",
        14,
        id="missing-assignment-uses-runner-default",
    ),
    pytest.param(f"{_WINDOW_VAR}=\n", 14, id="empty-value-uses-runner-default"),
    pytest.param(f'{_WINDOW_VAR}=""\n', 14, id="quoted-empty-uses-runner-default"),
    pytest.param(f"{_SIBLING}\n{_WINDOW_VAR}=\n", 14, id="sibling-plus-empty-value-defaults"),
    pytest.param(f"export {_WINDOW_VAR}=21\n", 21, id="export-prefix"),
    pytest.param(f'{_WINDOW_VAR}="21"\n', 21, id="double-quoted"),
    pytest.param(f"{_WINDOW_VAR}='21'\n", 21, id="single-quoted"),
    pytest.param(f"{_WINDOW_VAR}=21   # trailing comment\n", 21, id="trailing-comment"),
    pytest.param(f"   {_WINDOW_VAR}=21   \n", 21, id="surrounding-whitespace"),
    pytest.param(f"# {_WINDOW_VAR}=99\n{_WINDOW_VAR}=21\n", 21, id="full-line-comment-ignored"),
    pytest.param(
        f"# {_WINDOW_VAR}=99\n{_SIBLING}\n",
        14,
        id="only-commented-assignment-is-unassigned",
    ),
    pytest.param(f"{_WINDOW_VAR}_OLD=99\n{_WINDOW_VAR}=21\n", 21, id="near-name-decoy-ignored"),
    pytest.param(f"{_WINDOW_VAR}_OLD=99\n", 14, id="decoy-alone-is-unassigned"),
    pytest.param(f"{_WINDOW_VAR}=14\n{_WINDOW_VAR}=21\n", 21, id="last-assignment-wins"),
    pytest.param(f"{_WINDOW_VAR}=21\n{_WINDOW_VAR}=30\n", 30, id="last-assignment-wins-again"),
]


@pytest.mark.parametrize(("body", "expected"), _LEXICAL_ROWS)
def test_read_retention_window_days_lexical_forms_and_runner_defaults(
    tmp_path: Path, body: str, expected: int
) -> None:
    """Runner-equivalent resolution + the exact lexical forms pinned by design D1."""
    assert read_retention_window_days(_retention_env(tmp_path, body)) == expected


def test_missing_or_empty_assignment_resolves_to_the_shared_runner_default() -> None:
    """The default is the runner's live-effective value, not a comparison fallback."""
    assert DEFAULT_RETENTION_WINDOW_DAYS == 14


_PRESENT_INVALID_ROWS = [
    pytest.param(f"{_WINDOW_VAR}=not-an-int\n", "must be an integer", id="non-integer"),
    pytest.param(f"{_WINDOW_VAR}=0\n", "must be positive", id="zero"),
    pytest.param(f"{_WINDOW_VAR}=-1\n", "must be positive", id="negative"),
    pytest.param(f"{_WINDOW_VAR}=21.5\n", "must be an integer", id="float"),
    pytest.param(f'{_WINDOW_VAR}=" 21 "\n', "whitespace", id="quoted-whitespace-padding"),
    pytest.param(f"{_WINDOW_VAR}=$OTHER\n", "must be an integer", id="interpolation-refused"),
]


@pytest.mark.parametrize(("body", "match"), _PRESENT_INVALID_ROWS)
def test_read_retention_window_days_refuses_present_invalid_values(
    tmp_path: Path, body: str, match: str
) -> None:
    with pytest.raises(ArchiveConfigurationError, match=match):
        read_retention_window_days(_retention_env(tmp_path, body))


# #1230 closed-world grammar: the single refusal fragment every non-conforming
# LINE now produces, regardless of which shell form produced it.
_GRAMMAR_REFUSAL = "not a supported assignment"
# The second layer, reachable only from CONFORMING lines after #1230 (design D2).
_MENTION_REFUSAL = "cannot accept as an"

_UNSUPPORTED_SHAPE_ROWS = [
    # Round-1 narrowing (#1229 review A1): bash reads `VAR= 21` as an
    # assignment prefix plus the command `21`, so the runner sees the
    # variable UNSET. Accepting 21 would validate against a window the
    # runner never uses.
    pytest.param(f"{_WINDOW_VAR}= 21\n", "assignment is malformed", id="unquoted-leading-whitespace"),
    # Same narrowing: previously read as an empty value defaulting to 14;
    # bash also leaves the variable unset here, so refusing is fail-closed.
    pytest.param(
        f"{_WINDOW_VAR}= # comment\n",
        "assignment is malformed",
        id="empty-value-then-comment-is-malformed",
    ),
    # `#` opens a comment only AFTER whitespace: bash exports `#21`.
    pytest.param(f"{_WINDOW_VAR}=#21\n", "must be an integer", id="hash-first-character-is-a-value"),
    pytest.param(f"{_SIBLING}\r\n{_WINDOW_VAR}=21\r\n", "non-newline line breaks", id="crlf-content"),
    pytest.param(f"{_SIBLING}\n{_WINDOW_VAR}=21\v", "non-newline line breaks", id="vertical-tab-content"),
    # Unsupported assignment shapes: refused because the LINE is outside the
    # closed-world grammar (#1230) — before that these were caught one layer
    # later, by the `NAME=` mention detector. The inputs and their fail-closed
    # direction are unchanged; only the refusing layer moved.
    pytest.param(f"{_SIBLING}\nreadonly {_WINDOW_VAR}=21\n", _GRAMMAR_REFUSAL, id="readonly-prefix"),
    pytest.param(f"{_SIBLING}\ndeclare -i {_WINDOW_VAR}=21\n", _GRAMMAR_REFUSAL, id="declare-prefix"),
    pytest.param(f'{_SIBLING}\n"{_WINDOW_VAR}=21"\n', _GRAMMAR_REFUSAL, id="truncated-quoted-edit"),
    # Round-2 fail-open closure (#1229 review C2): the refusal is PER LINE.
    # `VAR=14` + `readonly VAR=30` is exported as 30 by `set -a; . file`,
    # so the round-1 "refuse only when nothing was assigned" gate returned the
    # stale 14 — a fail-open against a LARGER live window.
    pytest.param(
        f"{_WINDOW_VAR}=14\nreadonly {_WINDOW_VAR}=30\n",
        _GRAMMAR_REFUSAL,
        id="mixed-plain-then-readonly",
    ),
    # Reverse order: bash fails to re-assign the readonly variable and `. file`
    # exits non-zero, so the runner never starts — refusing is right either way.
    pytest.param(
        f"readonly {_WINDOW_VAR}=30\n{_WINDOW_VAR}=14\n",
        _GRAMMAR_REFUSAL,
        id="mixed-readonly-then-plain",
    ),
    pytest.param(
        f"{_WINDOW_VAR}=14\ndeclare -i {_WINDOW_VAR}=30\n",
        _GRAMMAR_REFUSAL,
        id="mixed-plain-then-declare",
    ),
    # #1230: eight shell forms that export the window WITHOUT the literal
    # `NAME=` substring, so the open-world mention detector let them through and
    # the helper answered with the runner-equivalent default 14 while
    # `set -a; . file` exported a LARGER window (issue table, 8/8 differentially
    # reproduced). The closed-world grammar refuses each at the offending line —
    # including the nested source lines, which no variable-name detector can see.
    pytest.param(f"{_SIBLING}\n{_WINDOW_VAR}+=21\n", _GRAMMAR_REFUSAL, id="append-assignment"),
    pytest.param(f"{_WINDOW_VAR}=14\n{_WINDOW_VAR}+=7\n", _GRAMMAR_REFUSAL, id="plain-then-append"),
    pytest.param(f"{_SIBLING}\n: ${{{_WINDOW_VAR}:=21}}\n", _GRAMMAR_REFUSAL, id="default-expansion"),
    pytest.param(f"{_SIBLING}\n. other.env\n", _GRAMMAR_REFUSAL, id="nested-dot-source"),
    pytest.param(f"{_SIBLING}\nsource other.env\n", _GRAMMAR_REFUSAL, id="nested-source-keyword"),
    pytest.param(f"{_SIBLING}\nprintf -v {_WINDOW_VAR} 21\n", _GRAMMAR_REFUSAL, id="printf-v-assignment"),
    pytest.param(f"{_SIBLING}\nread {_WINDOW_VAR} <<< 21\n", _GRAMMAR_REFUSAL, id="read-here-string"),
    pytest.param(f"{_SIBLING}\neval '{_WINDOW_VAR}'=21\n", _GRAMMAR_REFUSAL, id="eval-quoted-name"),
    # #1230 design D5(a1): a quoted value spanning lines closes on a bare `"`
    # line, which the grammar refuses. bash keeps the window line INSIDE the
    # other variable's string (runner runs its default 14) while the
    # line-oriented extractor read it as an assignment — refusing is the
    # over-strict, fail-closed side of that class (was a strict-xfail
    # differential row before the grammar landed).
    pytest.param(
        f'{_SIBLING}\nOTHER="\n{_WINDOW_VAR}=21\n"\n',
        _GRAMMAR_REFUSAL,
        id="multi-line-quoted-closing-quote-refused",
    ),
    # #1230 design D2: after the grammar, the `NAME=` mention layer is reachable
    # from two CONFORMING shapes — a value embedding the name, and a key that
    # merely ends with it. Both refuse (over-strict for the decoy, fail-closed);
    # without these rows the mention branch loses its last direct coverage.
    pytest.param(f"{_SIBLING}\nX={_WINDOW_VAR}=21\n", _MENTION_REFUSAL, id="mention-embedded-in-value"),
    pytest.param(f"{_SIBLING}\nOLD_{_WINDOW_VAR}=99\n", _MENTION_REFUSAL, id="mention-key-suffix-decoy"),
    # Wrong file entirely: no retention-family assignment at all.
    pytest.param(
        "DATABASE_URL=postgresql://x\nNHMS_ARCHIVE_MIN_AGE_DAYS=14\n",
        "does not look like the deployed retention env",
        id="wrong-file-has-no-retention-family",
    ),
    pytest.param("", "does not look like the deployed retention env", id="empty-file-mirrors-dev-null"),
    # Round-2 C1: the archive-side POINTER variable shares the retention prefix
    # but is never consumed by the runner, so it must not grant recognition —
    # otherwise pointing the guard at an archive env defaults it to 14.
    pytest.param(
        f"NHMS_ARCHIVE_MIN_AGE_DAYS=14\n{_RETENTION_ENV_VAR}=/home/nwm/x.env\n",
        "does not look like the deployed retention env",
        id="pointer-variable-alone-is-not-the-retention-env",
    ),
]


@pytest.mark.parametrize(("body", "match"), _UNSUPPORTED_SHAPE_ROWS)
def test_read_retention_window_days_refuses_unsupported_shapes(
    tmp_path: Path, body: str, match: str
) -> None:
    """Fail-direction hardening: every divergence from `set -a; . file` refuses."""
    path = _retention_env(tmp_path, body)

    with pytest.raises(ArchiveConfigurationError, match=match) as error:
        read_retention_window_days(path)

    assert str(path) in str(error.value)


@pytest.mark.parametrize(
    ("body", "offending_line"),
    [
        pytest.param(f"{_SIBLING}\n{_WINDOW_VAR}+=21\n", f"{_WINDOW_VAR}+=21", id="append-assignment"),
        pytest.param(f"{_SIBLING}\n. other.env\n", ". other.env", id="nested-dot-source"),
        pytest.param(
            f"{_SIBLING}\nprintf -v {_WINDOW_VAR} 21\n",
            f"printf -v {_WINDOW_VAR} 21",
            id="printf-v-assignment",
        ),
        # Two offending lines: the FIRST in file order must be the one named,
        # so the message is deterministic for an operator diffing the file.
        pytest.param(
            f"{_SIBLING}\n. first.env\nsource second.env\n",
            ". first.env",
            id="first-offending-line-in-file-order",
        ),
    ],
)
def test_grammar_refusal_names_the_offending_line(
    tmp_path: Path, body: str, offending_line: str
) -> None:
    """#1230 acceptance item 1: the operator gets the path AND the exact line."""
    path = _retention_env(tmp_path, body)

    with pytest.raises(ArchiveConfigurationError, match=_GRAMMAR_REFUSAL) as error:
        read_retention_window_days(path)

    message = str(error.value)
    assert str(path) in message
    assert repr(offending_line) in message


@pytest.mark.parametrize(
    ("body", "offending_line"),
    [
        pytest.param(f"{_SIBLING}\nX={_WINDOW_VAR}=21\n", f"X={_WINDOW_VAR}=21", id="embedded-in-value"),
        pytest.param(
            f"{_SIBLING}\nOLD_{_WINDOW_VAR}=99\n",
            f"OLD_{_WINDOW_VAR}=99",
            id="key-suffix-decoy",
        ),
    ],
)
def test_mention_refusal_names_the_offending_line(
    tmp_path: Path, body: str, offending_line: str
) -> None:
    """#1230 acceptance item 3: the mention layer localizes its refusal too."""
    path = _retention_env(tmp_path, body)

    with pytest.raises(ArchiveConfigurationError, match=_MENTION_REFUSAL) as error:
        read_retention_window_days(path)

    message = str(error.value)
    assert str(path) in message
    assert repr(offending_line) in message


def test_shipped_env_templates_never_hit_the_grammar_refusal(tmp_path: Path) -> None:
    """#1230 design D3: zero grammar-class false refusals on the shipped templates.

    Bound to BEHAVIOR, not to a re-implementation of the grammar: every
    `infra/env/*.example` goes through the public helper, and the only allowed
    outcomes are a positive window (the retention template) or a refusal that
    is NOT the closed-world grammar refusal (unrelated templates refuse as
    "does not look like the deployed retention env"). A future template line
    that stops conforming turns this red.
    """
    templates = sorted((_REPO_ROOT / "infra/env").glob("*.example"))
    assert len(templates) >= 12

    for template in templates:
        path = tmp_path / f"{template.name}.env"
        path.write_bytes(template.read_bytes())
        try:
            window = read_retention_window_days(path)
        except ArchiveConfigurationError as error:
            assert _GRAMMAR_REFUSAL not in str(error), f"{template.name}: {error}"
            continue
        assert isinstance(window, int) and window > 0, f"{template.name} resolved to {window!r}"


def test_read_retention_window_days_accepts_the_shipped_retention_env(tmp_path: Path) -> None:
    """The counterpart lock: the real retention template still parses to 14."""
    path = tmp_path / "retention.env"
    path.write_bytes((_REPO_ROOT / "infra/env/node27-timeseries-retention.example").read_bytes())

    assert read_retention_window_days(path) == 14


# ---------------------------------------------------------------------------
# 6.2 invariant audit (#1229 round-2, design D5(d)): the parser-fail-direction
# class repeated across two review rounds, so the invariant is made executable
# instead of re-audited by eye. For every corpus body the helper must EITHER
# refuse OR return exactly the window the retention RUNNER would actually run
# with — a different number, or any number where the runner refuses / never
# starts, fails. Fail-closed narrowings (the helper refusing where the runner
# runs) are legitimate by design and deliberately NOT asserted against.
# ---------------------------------------------------------------------------

_BASH = shutil.which("bash")
_UNSET_SENTINEL = "__UNSET__"

# Residual class tripwire (#1230 design D5(a2)): a quoted value spanning lines
# whose EVERY line happens to fullmatch the grammar. `OTHER="` takes the bare
# quote as its value, the inner window line is read as the last assignment
# (helper 7) while bash keeps it inside OTHER's string and exports the earlier
# 30 — still FAIL-OPEN, not closable by a line grammar (it needs unbalanced
# quote tracking). Pinned strict-xfail so the day that lands shows up as XPASS.
# The (a1) sibling — a closing bare `"` line — is now a plain grammar refusal
# row in `_UNSUPPORTED_SHAPE_ROWS`.
_MULTILINE_QUOTED_ALL_CONFORMING_BODY = f'{_WINDOW_VAR}=30\nOTHER="\n{_WINDOW_VAR}=7\nX=y"\n'

# Second residual class tripwire (#1230 design D5(b)): the grammar is LINE-level,
# so a fully CONFORMING `KEY=VALUE` line can still assign the WINDOW variable
# from inside its VALUE via a shell expansion. `X=${WINDOW:=21}` carries no
# literal `WINDOW=` substring, so it slips the grammar AND the mention layer:
# the helper sees only the sibling and answers the runner-equivalent 14 while
# `set -a; . file` exports 21 — FAIL-OPEN. `X=$((WINDOW+=7))` is the arithmetic
# sibling of the same family. Closing this needs expansion-aware value scanning,
# out of scope here; pinned strict-xfail so the day that lands shows up as XPASS.
_VALUE_LEVEL_EXPANSION_BODY = f"{_SIBLING}\nX=${{{_WINDOW_VAR}:=21}}\n"


def _differential_corpus() -> list[Any]:
    rows: list[Any] = [
        pytest.param(param.values[0], id=param.id)
        for param in (*_LEXICAL_ROWS, *_PRESENT_INVALID_ROWS, *_UNSUPPORTED_SHAPE_ROWS)
    ]
    rows.append(
        pytest.param(
            _MULTILINE_QUOTED_ALL_CONFORMING_BODY,
            id="multi-line-quoted-all-conforming-still-diverges",
            marks=pytest.mark.xfail(
                strict=True,
                reason=(
                    "#1230 design D5(a2) recorded residual: every line of a multi-line quoted "
                    "value conforms to the grammar, so the helper reads the inner assignment "
                    "while bash keeps it inside the outer string"
                ),
            ),
        )
    )
    rows.append(
        pytest.param(
            _VALUE_LEVEL_EXPANSION_BODY,
            id="value-level-expansion-still-diverges",
            marks=pytest.mark.xfail(
                strict=True,
                reason=(
                    "#1230 design D5(b) recorded residual: value-level shell expansion on a "
                    "CONFORMING line assigns the window variable itself, invisible to a "
                    "line-level grammar and to the `NAME=` mention layer"
                ),
            ),
        )
    )
    return rows


def _runner_effective_window(env_file: Path) -> int | None:
    """Return the window the retention runner would run with, or None if it cannot.

    None means the runner never gets a window: either the wrapper's
    `set -a; . <env>` fails (ENV_FILE_SOURCE_FAILED) or the runner's strict
    parse refuses the exported value.
    """
    script = (
        'set -a; . "$1" 2>/dev/null; rc=$?; '
        f'printf %s "${{{_WINDOW_VAR}-{_UNSET_SENTINEL}}}"; exit "$rc"'
    )
    # Bytes, not text mode: universal-newline translation would hide the `\r`
    # of a CRLF value, which is exactly what the runner's strict parse rejects.
    completed = subprocess.run(
        [str(_BASH), "-c", script, "bash", str(env_file)],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        return None
    exported = completed.stdout.decode("utf-8", errors="surrogateescape")
    raw = None if exported == _UNSET_SENTINEL else exported
    try:
        return node27_timeseries_retention._optional_positive_int(
            raw,
            name=_WINDOW_VAR,
            default=node27_timeseries_retention._DEFAULT_WINDOW_DAYS,
        )
    except node27_timeseries_retention.RetentionConfigError:
        return None


@pytest.mark.skipif(_BASH is None, reason="differential oracle needs a real bash to source env files")
@pytest.mark.parametrize("body", _differential_corpus())
def test_helper_never_returns_a_window_the_runner_would_not_use(tmp_path: Path, body: str) -> None:
    """Differential oracle against `bash -c 'set -a; . file'` + the runner's parse."""
    path = _retention_env(tmp_path, body)
    runner_window = _runner_effective_window(path)

    try:
        helper_window = read_retention_window_days(path)
    except ArchiveConfigurationError:
        return  # Fail-closed narrowing: allowed, and not asserted against.

    assert runner_window is not None, (
        f"helper returned {helper_window} but the runner would refuse or never start on {body!r}"
    )
    assert helper_window == runner_window, (
        f"helper returned {helper_window} but the runner would run with {runner_window} on {body!r}"
    )


def test_read_retention_window_days_refuses_unset_path() -> None:
    with pytest.raises(ArchiveConfigurationError, match="NODE27_TIMESERIES_RETENTION_ENV must be set"):
        read_retention_window_days(None)


def test_read_retention_window_days_refuses_empty_path() -> None:
    with pytest.raises(ArchiveConfigurationError, match="NODE27_TIMESERIES_RETENTION_ENV must be set"):
        read_retention_window_days("   ")


def test_read_retention_window_days_refuses_relative_path_by_absoluteness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative path that EXISTS and parses must still refuse, naming absoluteness."""
    _retention_env(tmp_path, f"{_WINDOW_VAR}=21\n")
    monkeypatch.chdir(tmp_path)
    assert Path("retention.env").is_file()

    with pytest.raises(ArchiveConfigurationError, match="must be an absolute path: retention.env"):
        read_retention_window_days("retention.env")


def test_read_retention_window_days_refuses_missing_file_without_fallback(tmp_path: Path) -> None:
    """Missing FILE is NOT the missing-ASSIGNMENT case: no constant fallback."""
    with pytest.raises(ArchiveConfigurationError, match="retention env file is unreadable"):
        read_retention_window_days(tmp_path / "absent.env")


def test_read_retention_window_days_refuses_directory_source(tmp_path: Path) -> None:
    directory = tmp_path / "retention.env"
    directory.mkdir()
    with pytest.raises(ArchiveConfigurationError, match="retention env file is unreadable"):
        read_retention_window_days(directory)


def test_raw_retention_object_store_override_precedence_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = tmp_path / "shared"
    override = tmp_path / "raw-override"
    shared.mkdir()
    override.mkdir()
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(shared))
    monkeypatch.setenv("NODE27_RAW_RETENTION_OBJECT_STORE_ROOT", str(override))

    config, blockers = node27_raw_retention.config_from_env(node27_raw_retention.build_parser().parse_args([]))

    assert blockers == []
    assert config is not None
    assert config.object_store_root == override.resolve()


def test_governance_object_store_override_precedence_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = tmp_path / "shared"
    override = tmp_path / "governance-override"
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(shared))
    monkeypatch.setenv("NODE27_GOVERNANCE_OBJECT_STORE_ROOT", str(override))

    args = node27_resource_governance.build_parser().parse_args([])

    assert args.object_store_root == str(override)
