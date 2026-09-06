"""Focused public-seam tests for issue #1903 river/segment mapping validation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

import workers.model_registry.basins_package as basins_package
import workers.model_registry.basins_package_source_io as basins_package_source_io
from packages.common.object_store import LocalObjectStore
from tests.basins_package_helpers import _object_store_env, _write_valid_inventory

_MAPPING_BYTE_LIMIT = 16 * 1024 * 1024
_VALID_MULTI_RIV = """# source metadata

2 6
Index Down Type Slope Length BC
10 30 0 0.01 100 0
# reach-table comment

30 0 0 0.01 100 0
2 1
Index NodeX NodeY
1 2 3
"""
_VALID_MULTI_RIVSEG = """// segment metadata
2 4
Index iRiv iEle Length
1 10 1 100
% segment-table comment

2 30 2 100
"""
_VALID_SINGLE_RIV = "1 6\nIndex Down Type Slope Length BC\n1 0 0 0.01 100 0\n"
_VALID_SINGLE_RIVSEG = "1 4\nIndex iRiv iEle Length\n1 1 1 100\n"
_SINGLE_MAPPING_LINES = {
    "riv": ("1 6", "Index Down Type Slope Length BC", "1 0 0 0.01 100 0"),
    "rivseg": ("1 4", "Index iRiv iEle Length", "1 1 1 100"),
}
_SPLITLINES_SEPARATOR_CASES = (
    ("lf", "\n"),
    ("cr", "\r"),
    ("crlf", "\r\n"),
    ("vertical-tab", "\v"),
    ("form-feed", "\f"),
    ("file-separator", "\x1c"),
    ("group-separator", "\x1d"),
    ("record-separator", "\x1e"),
    ("next-line", "\x85"),
    ("line-separator", " "),
    ("paragraph-separator", " "),
)


def _mapping_paths(tmp_path: Path) -> tuple[Path, Path]:
    return (
        tmp_path / "basins" / "basin-a" / "input" / "alias-a" / "alias-a.sp.riv",
        tmp_path / "basins" / "basin-a" / "input" / "alias-a" / "alias-a.sp.rivseg",
    )


def _inventory_with_mapping(tmp_path: Path, riv: bytes | str, rivseg: bytes | str) -> tuple[Path, str]:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    riv_path, rivseg_path = _mapping_paths(tmp_path)
    riv_path.write_bytes(riv if isinstance(riv, bytes) else riv.encode("utf-8"))
    rivseg_path.write_bytes(rivseg if isinstance(rivseg, bytes) else rivseg.encode("utf-8"))
    return inventory_path, model_id


def _assert_invalid_error(error: pytest.ExceptionInfo[basins_package.BasinsPackageError], cause: str) -> None:
    assert error.value.error_code == "BASINS_RIVSEG_MAPPING_INVALID"
    assert error.value.details["cause"] == cause
    assert "UnicodeDecodeError" not in str(error.value)
    assert "Traceback" not in str(error.value)
    assert len(str(error.value)) < 1024
    assert len(json.dumps(error.value.details, sort_keys=True)) < 1024


def _assert_invalid_both_seams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    riv: bytes | str,
    rivseg: bytes | str,
    *,
    cause: str,
    details: dict[str, object] | None = None,
) -> None:
    inventory_path, model_id = _inventory_with_mapping(tmp_path, riv, rivseg)
    with pytest.raises(basins_package.BasinsPackageError) as identity_error:
        basins_package.basins_package_source_identity(inventory_path=inventory_path, model_id=model_id)
    _assert_invalid_error(identity_error, cause)
    if details is not None:
        assert identity_error.value.details == {"cause": cause, **details}

    root = _object_store_env(tmp_path, monkeypatch)
    output = tmp_path / "output" / "manifest.json"
    with pytest.raises(basins_package.BasinsPackageError) as publication_error:
        basins_package.publish_basins_package(
            inventory_path=inventory_path,
            model_id=model_id,
            version="v-rivseg-invalid",
            output_path=output,
        )
    _assert_invalid_error(publication_error, cause)
    assert publication_error.value.details == identity_error.value.details
    assert not root.exists()
    assert not output.exists()


def _assert_valid_both_seams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    riv: bytes | str,
    rivseg: bytes | str,
    *,
    version: str,
) -> None:
    inventory_path, model_id = _inventory_with_mapping(tmp_path, riv, rivseg)
    assert basins_package.basins_package_source_identity(inventory_path=inventory_path, model_id=model_id)[
        "content_sha256"
    ]
    _object_store_env(tmp_path, monkeypatch)
    assert (
        basins_package.publish_basins_package(
            inventory_path=inventory_path,
            model_id=model_id,
            version=version,
            output_path=tmp_path / "manifest.json",
        )["status"]
        == "published"
    )


def test_valid_mapping_with_actual_noncontiguous_reaches_comments_headers_and_trailing_blocks_reaches_both_seams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_path, model_id = _inventory_with_mapping(tmp_path, _VALID_MULTI_RIV, _VALID_MULTI_RIVSEG)
    identity = basins_package.basins_package_source_identity(inventory_path=inventory_path, model_id=model_id)
    _object_store_env(tmp_path, monkeypatch)
    output = tmp_path / "manifest.json"

    result = basins_package.publish_basins_package(
        inventory_path=inventory_path,
        model_id=model_id,
        version="v-rivseg-valid",
        output_path=output,
        expected_source_identity=identity,
    )

    assert result["status"] == "published"
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert {
        entry["relative_path"]
        for entry in manifest["included_files"]
        if entry["relative_path"].endswith((".sp.riv", ".sp.rivseg"))
    } == {"alias-a.sp.riv", "alias-a.sp.rivseg"}


@pytest.mark.parametrize("calibration_name", ("notes.sp.riv", "notes.sp.rivseg"))
def test_calibration_mapping_suffix_collision_preserves_canonical_mapping_and_calibration_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    calibration_name: str,
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    calibration_bytes = b"ordinary calibration payload\n"
    calibration_path = tmp_path / "basins" / "basin-a" / "CALIB" / calibration_name
    calibration_path.parent.mkdir()
    calibration_path.write_bytes(calibration_bytes)

    identity = basins_package.basins_package_source_identity(inventory_path=inventory_path, model_id=model_id)
    _object_store_env(tmp_path, monkeypatch)
    output = tmp_path / "manifest.json"
    result = basins_package.publish_basins_package(
        inventory_path=inventory_path,
        model_id=model_id,
        version=f"v-rivseg-calibration-{calibration_name.removeprefix('notes.')}",
        output_path=output,
        expected_source_identity=identity,
    )

    manifest = json.loads(output.read_text(encoding="utf-8"))
    entry = next(entry for entry in manifest["included_files"] if entry["relative_path"] == f"CALIB/{calibration_name}")
    assert result["status"] == "published"
    assert entry["role"] == "calibration"
    assert entry["size_bytes"] == len(calibration_bytes)
    assert entry["sha256"] == hashlib.sha256(calibration_bytes).hexdigest()
    published = (
        tmp_path
        / "object-store"
        / "models"
        / model_id
        / f"v-rivseg-calibration-{calibration_name.removeprefix('notes.')}"
        / "package"
        / "CALIB"
        / calibration_name
    )
    assert published.read_bytes() == calibration_bytes


def test_mapping_with_exactly_32_leading_comment_lines_reaches_both_public_seams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_valid_both_seams(
        tmp_path,
        monkeypatch,
        _VALID_SINGLE_RIV,
        "# leading comment\n" * 32 + _VALID_SINGLE_RIVSEG,
        version="v-rivseg-leading-comments",
    )


@pytest.mark.parametrize(
    ("separator_id", "separator"),
    _SPLITLINES_SEPARATOR_CASES,
    ids=tuple(separator_id for separator_id, _ in _SPLITLINES_SEPARATOR_CASES),
)
@pytest.mark.parametrize("target", ("riv", "rivseg"), ids=("sp-riv", "sp-rivseg"))
def test_mapping_splitlines_separators_with_32_comments_reach_both_public_seams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    separator_id: str,
    separator: str,
    target: str,
) -> None:
    target_mapping = separator.join(("# leading comment",) * 32 + _SINGLE_MAPPING_LINES[target])
    riv = target_mapping if target == "riv" else _VALID_SINGLE_RIV
    rivseg = target_mapping if target == "rivseg" else _VALID_SINGLE_RIVSEG

    _assert_valid_both_seams(
        tmp_path,
        monkeypatch,
        riv,
        rivseg,
        version=f"v-rivseg-splitlines-{target}-{separator_id}",
    )


def test_genuine_single_reach_mapping_remains_compatible_at_both_seams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_valid_both_seams(
        tmp_path,
        monkeypatch,
        _VALID_SINGLE_RIV,
        _VALID_SINGLE_RIVSEG,
        version="v-rivseg-single",
    )


@pytest.mark.parametrize(
    ("riv", "rivseg", "version"),
    (
        (
            "1 6\nIndex Down Type Slope Length BC\n10 0 0 0.01 100 0\n",
            "2 4\nIndex iRiv iEle Length\n1 10 1 100\n2 10 2 100\n",
            "v-rivseg-one-reach-two-segments",
        ),
        (
            "2 6\nIndex Down Type Slope Length BC\n10 0 0 0.01 100 0\n30 0 0 0.01 100 0\n",
            "1 4\nIndex iRiv iEle Length\n1 10 1 100\n",
            "v-rivseg-two-reaches-one-segment",
        ),
    ),
    ids=("one-reach-two-segments", "two-reaches-one-segment"),
)
def test_single_axis_mapping_does_not_trigger_multi_axis_collapse_guard_at_both_public_seams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    riv: str,
    rivseg: str,
    version: str,
) -> None:
    _assert_valid_both_seams(tmp_path, monkeypatch, riv, rivseg, version=version)


@pytest.mark.parametrize(
    ("reach_token", "version"),
    (
        ("123456789012345678", "v-rivseg-exact-18-unsigned"),
        ("+123456789012345678", "v-rivseg-exact-18-signed"),
    ),
    ids=("unsigned-18-digit", "explicitly-signed-18-digit"),
)
def test_exact_18_digit_reach_identifier_is_accepted_at_both_public_seams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reach_token: str,
    version: str,
) -> None:
    riv = f"1 6\nIndex Down Type Slope Length BC\n{reach_token} 0 0 0.01 100 0\n"
    rivseg = f"1 4\nIndex iRiv iEle Length\n1 {reach_token} 1 100\n"

    _assert_valid_both_seams(tmp_path, monkeypatch, riv, rivseg, version=version)


@pytest.mark.parametrize(
    ("riv", "rivseg", "version"),
    (
        (
            _VALID_MULTI_RIV,
            "2 4\nIndex iRiv iEle Length\n1 10 1 100\n1 30 2 100\n",
            "v-rivseg-duplicate-segment-index",
        ),
        (
            "3 6\nIndex Down Type Slope Length BC\n10 30 0 0.01 100 0\n30 0 0 0.01 100 0\n50 0 0 0.01 100 0\n",
            "2 4\nIndex iRiv iEle Length\n1 10 1 100\n2 30 2 100\n",
            "v-rivseg-unreferenced-reach",
        ),
    ),
    ids=("duplicate-segment-index", "unreferenced-reach"),
)
def test_mapping_compatibility_cases_reach_both_public_seams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    riv: str,
    rivseg: str,
    version: str,
) -> None:
    _assert_valid_both_seams(tmp_path, monkeypatch, riv, rivseg, version=version)


@pytest.mark.parametrize(
    ("riv", "rivseg", "cause"),
    (
        (_VALID_MULTI_RIV, "2 4\nIndex iRiv iEle Length\n1 10 1 100\n2 99 2 100\n", "missing_reference"),
        ("2 6\n1 0 0\n1 0 0\n", _VALID_MULTI_RIVSEG, "duplicate_reach_index"),
        (
            "2 6\nIndex Down Type Slope Length BC\n1234567890123456789 0\n2 0\n",
            _VALID_MULTI_RIVSEG,
            "invalid_integer",
        ),
        (
            "2 6\nIndex Down Type Slope Length BC\n+1234567890123456789 0\n2 0\n",
            _VALID_MULTI_RIVSEG,
            "invalid_integer",
        ),
        ("2 6\nIndex Down Type Slope Length BC\n1_0 0\n2 0\n", _VALID_MULTI_RIVSEG, "invalid_integer"),
        ("2 6\nIndex Down Type Slope Length BC\n١ 0\n2 0\n", _VALID_MULTI_RIVSEG, "invalid_integer"),
        ("0 6\n", _VALID_MULTI_RIVSEG, "invalid_count"),
        ("-1 6\n", _VALID_MULTI_RIVSEG, "invalid_count"),
        ("1_0 6\n", _VALID_MULTI_RIVSEG, "invalid_count"),
        ("1.0 6\n", _VALID_MULTI_RIVSEG, "invalid_count"),
        ("# only a comment\n", _VALID_MULTI_RIVSEG, "missing_count"),
        ("# leading comment\n" * 33 + _VALID_SINGLE_RIV, _VALID_SINGLE_RIVSEG, "leading_skip_exceeded"),
        ("2 6\nIndex NOT_A_STANDARD_HEADER\n10 0\n30 0\n", _VALID_MULTI_RIVSEG, "invalid_header"),
        (_VALID_MULTI_RIV, "2 4\nIndex iRiv NOT_A_STANDARD_HEADER\n1 10\n2 30\n", "invalid_header"),
        (_VALID_MULTI_RIV, "2 4\nindex iriv\n1 10\n2 30\n", "invalid_header"),
        (_VALID_MULTI_RIV, "1 4\nIndex iRiv iEle Length\n1\n", "missing_column"),
        ("2 6\nIndex Down Type Slope Length BC\n10\n", _VALID_MULTI_RIVSEG, "truncated_block"),
        (
            _VALID_MULTI_RIV,
            "2 4\nIndex iRiv iEle Length\n1 10\n2 30\n3 10\n",
            "extra_segment_row",
        ),
        (b"1 6\n\xff\n", _VALID_SINGLE_RIVSEG, "invalid_utf8"),
    ),
    ids=(
        "missing-reference",
        "duplicate-reach-index",
        "overlong-unsigned-integer",
        "overlong-signed-integer",
        "underscore-integer",
        "non-ascii-integer",
        "zero-count",
        "negative-count",
        "underscore-count",
        "decimal-count",
        "missing-count",
        "leading-comment-limit",
        "riv-header-prefix",
        "rivseg-header-prefix",
        "lowercase-header",
        "missing-column",
        "truncated-block",
        "extra-segment-row",
        "invalid-utf8",
    ),
)
def test_malformed_mapping_is_bounded_and_refused_before_output_at_both_public_seams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    riv: bytes | str,
    rivseg: bytes | str,
    cause: str,
) -> None:
    _assert_invalid_both_seams(tmp_path, monkeypatch, riv, rivseg, cause=cause)


def test_missing_reference_list_is_sorted_and_bounded_at_both_public_seams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_ids = list(range(20, 0, -1))
    rivseg = "20 4\nIndex iRiv iEle Length\n" + "".join(
        f"{index} {reach_id} {index} 100\n" for index, reach_id in enumerate(missing_ids, start=1)
    )
    _assert_invalid_both_seams(
        tmp_path,
        monkeypatch,
        _VALID_SINGLE_RIV,
        rivseg,
        cause="missing_reference",
        details={"missing_iriv": list(range(2, 18))},
    )


def test_mapping_file_at_exact_cap_is_accepted_at_both_public_seams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical_mapping = _VALID_SINGLE_RIVSEG.encode("utf-8")
    exact_cap_mapping = canonical_mapping.ljust(_MAPPING_BYTE_LIMIT, b"#")
    assert len(exact_cap_mapping) == _MAPPING_BYTE_LIMIT

    _assert_valid_both_seams(
        tmp_path,
        monkeypatch,
        _VALID_SINGLE_RIV,
        exact_cap_mapping,
        version="v-rivseg-exact-16-mib",
    )


def test_mapping_file_at_cap_plus_one_is_refused_at_both_public_seams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_invalid_both_seams(
        tmp_path,
        monkeypatch,
        _VALID_SINGLE_RIV,
        b"#" * (_MAPPING_BYTE_LIMIT + 1),
        cause="over_limit",
    )


def test_high_density_leading_comments_stop_at_limit_before_environment_store_or_output_at_both_public_seams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_path, model_id = _inventory_with_mapping(
        tmp_path,
        b"#\n" * 500_000,
        _VALID_SINGLE_RIVSEG,
    )
    original_lines = basins_package_source_io._iter_mapping_snapshot_lines
    consumed_lines: list[int] = []

    def count_and_limit_lines(
        source_file: basins_package_source_io._MappingSourceFile,
    ) -> object:
        iterator = original_lines(source_file)
        consumed = 0
        while True:
            try:
                line = next(iterator)
            except StopIteration:
                return
            consumed += 1
            if consumed == 33:
                consumed_lines.append(consumed)
            if consumed > 33:
                pytest.fail(f"mapping parser consumed more than 33 leading physical lines ({consumed=})")
            yield line

    monkeypatch.setattr(basins_package_source_io, "_iter_mapping_snapshot_lines", count_and_limit_lines)
    with pytest.raises(basins_package.BasinsPackageError) as identity_error:
        basins_package.basins_package_source_identity(inventory_path=inventory_path, model_id=model_id)
    _assert_invalid_error(identity_error, "leading_skip_exceeded")
    assert consumed_lines == [33]

    root = _object_store_env(tmp_path, monkeypatch)
    output = tmp_path / "high-density-output" / "manifest.json"
    with pytest.raises(basins_package.BasinsPackageError) as publication_error:
        basins_package.publish_basins_package(
            inventory_path=inventory_path,
            model_id=model_id,
            version="v-rivseg-high-density-leading-comments",
            output_path=output,
        )
    _assert_invalid_error(publication_error, "leading_skip_exceeded")
    assert consumed_lines == [33, 33]
    assert not root.exists()
    assert not output.exists()


def test_normalized_qhh_tab_separated_mapping_is_accepted_at_both_public_seams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_root = Path(__file__).parent / "fixtures" / "basins" / "qhh-sample"
    riv_source = (fixture_root / "qhh.sp.riv").read_bytes()
    rivseg_source = (fixture_root / "qhh.sp.rivseg").read_bytes()
    riv = b"5" + riv_source[riv_source.index(b"\t") :]
    rivseg = b"18" + rivseg_source[rivseg_source.index(b"\t") :]

    assert riv_source.startswith(b"1633\t6\n")
    assert rivseg_source.startswith(b"3738\t4\n")
    assert riv.split(b"\t", 1)[1] == riv_source.split(b"\t", 1)[1]
    assert rivseg.split(b"\t", 1)[1] == rivseg_source.split(b"\t", 1)[1]
    assert riv.endswith(b"\n")
    assert rivseg.endswith(b"\n")
    assert riv.splitlines()[0] == b"5\t6"
    assert rivseg.splitlines()[0] == b"18\t4"
    assert riv.splitlines()[1] == b"Index\tDown\tType\tSlope\tLength\tBC"
    assert rivseg.splitlines()[1] == b"Index\tiRiv\tiEle\tLength"
    assert [line.split(b"\t", 1)[0] for line in riv.splitlines()[2:]] == [b"1", b"2", b"3", b"9", b"180"]
    assert [line.split(b"\t", 1)[0] for line in rivseg.splitlines()[2:]] == [
        b"1",
        b"2",
        b"3",
        b"4",
        b"5",
        b"6",
        b"7",
        b"8",
        b"9",
        b"10",
        b"11",
        b"12",
        b"22",
        b"23",
        b"24",
        b"25",
        b"404",
        b"405",
    ]
    assert {line.split(b"\t")[1] for line in rivseg.splitlines()[2:]} == {b"1", b"2", b"3", b"9", b"180"}
    assert all(b"\t" in line for line in riv.splitlines())
    assert all(b"\t" in line for line in rivseg.splitlines())

    _assert_valid_both_seams(
        tmp_path,
        monkeypatch,
        riv,
        rivseg,
        version="v-rivseg-qhh-tab-normalized-5-18",
    )


def test_mapping_file_that_grows_after_fstat_is_refused_at_both_public_seams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_path, model_id = _inventory_with_mapping(tmp_path, _VALID_SINGLE_RIV, _VALID_SINGLE_RIVSEG)
    _, rivseg_path = _mapping_paths(tmp_path)
    original_fstat = os.fstat
    state: dict[str, Any] = {}

    def arm_post_fstat_growth() -> None:
        target = rivseg_path.stat()
        state.update(
            device=target.st_dev,
            inode=target.st_ino,
            matching_fstats=0,
            grew=False,
        )

    def fstat_then_grow(fd: int) -> os.stat_result:
        result = original_fstat(fd)
        if (result.st_dev, result.st_ino) == (state["device"], state["inode"]):
            state["matching_fstats"] += 1
            if state["matching_fstats"] == 2:
                rivseg_path.write_bytes(b"#" * (_MAPPING_BYTE_LIMIT + 1))
                state["grew"] = True
        return result

    monkeypatch.setattr(os, "fstat", fstat_then_grow)

    arm_post_fstat_growth()
    with pytest.raises(basins_package.BasinsPackageError) as identity_error:
        basins_package.basins_package_source_identity(inventory_path=inventory_path, model_id=model_id)
    _assert_invalid_error(identity_error, "over_limit")
    assert state["grew"] is True

    rivseg_path.write_text(_VALID_SINGLE_RIVSEG, encoding="utf-8")
    arm_post_fstat_growth()
    root = _object_store_env(tmp_path, monkeypatch)
    output = tmp_path / "growth-output" / "manifest.json"
    with pytest.raises(basins_package.BasinsPackageError) as publication_error:
        basins_package.publish_basins_package(
            inventory_path=inventory_path,
            model_id=model_id,
            version="v-rivseg-growth",
            output_path=output,
        )
    _assert_invalid_error(publication_error, "over_limit")
    assert state["grew"] is True
    assert not root.exists()
    assert not output.exists()


def test_multi_reach_multi_segment_mapping_collapsed_to_one_reach_is_rejected_with_exact_details_at_both_seams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collapsed = "2 4\nIndex iRiv iEle Length\n1 10 1 100\n2 10 2 100\n"
    inventory_path, model_id = _inventory_with_mapping(tmp_path, _VALID_MULTI_RIV, collapsed)
    with pytest.raises(basins_package.BasinsPackageError) as identity_error:
        basins_package.basins_package_source_identity(inventory_path=inventory_path, model_id=model_id)
    assert identity_error.value.error_code == "BASINS_RIVSEG_MAPPING_DEGENERATE"
    assert identity_error.value.details == {
        "reach_count": 2,
        "segment_count": 2,
        "mapped_reach_count": 1,
        "mapped_reach_id": 10,
    }

    root = _object_store_env(tmp_path, monkeypatch)
    output = tmp_path / "degenerate-output" / "manifest.json"
    with pytest.raises(basins_package.BasinsPackageError) as publication_error:
        basins_package.publish_basins_package(
            inventory_path=inventory_path,
            model_id=model_id,
            version="v-rivseg-degenerate",
            output_path=output,
        )
    assert publication_error.value.error_code == "BASINS_RIVSEG_MAPPING_DEGENERATE"
    assert publication_error.value.details == identity_error.value.details
    assert not root.exists()
    assert not output.exists()


def test_environment_store_is_not_constructed_and_injected_store_gains_no_child_on_mapping_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_path, model_id = _inventory_with_mapping(
        tmp_path,
        _VALID_MULTI_RIV,
        "2 4\n1 10 1 100\n2 99 2 100\n",
    )
    environment_root = tmp_path / "environment-store"
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(environment_root))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms")
    environment_output = tmp_path / "environment-output" / "manifest.json"
    with pytest.raises(basins_package.BasinsPackageError):
        basins_package.publish_basins_package(
            inventory_path=inventory_path,
            model_id=model_id,
            version="v-rivseg-refusal",
            output_path=environment_output,
        )
    assert not environment_root.exists()
    assert not environment_output.exists()

    injected_root = tmp_path / "injected-store"
    injected = LocalObjectStore(injected_root, "s3://nhms")
    injected_output = tmp_path / "injected-output" / "manifest.json"
    with pytest.raises(basins_package.BasinsPackageError):
        basins_package.publish_basins_package(
            inventory_path=inventory_path,
            model_id=model_id,
            version="v-rivseg-injected-refusal",
            output_path=injected_output,
            object_store=injected,
        )
    assert list(injected_root.iterdir()) == []
    assert not injected_output.exists()


def test_mapping_replacement_after_validation_preserves_source_identity_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_path, model_id = _inventory_with_mapping(tmp_path, _VALID_MULTI_RIV, _VALID_MULTI_RIVSEG)
    _, rivseg_path = _mapping_paths(tmp_path)
    expected = basins_package.basins_package_source_identity(inventory_path=inventory_path, model_id=model_id)
    original_validator = basins_package._validate_rivseg_reach_mapping

    def validate_then_replace(*args: object, **kwargs: object) -> list[basins_package.SourceFile]:
        result = original_validator(*args, **kwargs)
        rivseg_path.write_text("2 4\n1 10\n2 99\n", encoding="utf-8")
        return result

    monkeypatch.setattr(basins_package, "_validate_rivseg_reach_mapping", validate_then_replace)

    assert basins_package.basins_package_source_identity(inventory_path=inventory_path, model_id=model_id) == expected
    assert rivseg_path.read_text(encoding="utf-8") == "2 4\n1 10\n2 99\n"


def test_mapping_replacement_after_validation_publishes_validated_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_path, model_id = _inventory_with_mapping(tmp_path, _VALID_MULTI_RIV, _VALID_MULTI_RIVSEG)
    _, rivseg_path = _mapping_paths(tmp_path)
    validated_bytes = rivseg_path.read_bytes()
    expected_source_identity = basins_package.basins_package_source_identity(
        inventory_path=inventory_path,
        model_id=model_id,
    )
    _object_store_env(tmp_path, monkeypatch)
    original_validator = basins_package._validate_rivseg_reach_mapping
    replaced = False

    def validate_then_replace(*args: object, **kwargs: object) -> list[basins_package.SourceFile]:
        nonlocal replaced
        result = original_validator(*args, **kwargs)
        rivseg_path.write_text("2 4\n1 10\n2 99\n", encoding="utf-8")
        replaced = True
        return result

    monkeypatch.setattr(basins_package, "_validate_rivseg_reach_mapping", validate_then_replace)
    output = tmp_path / "manifest.json"
    result = basins_package.publish_basins_package(
        inventory_path=inventory_path,
        model_id=model_id,
        version="v-rivseg-snapshot",
        output_path=output,
        expected_source_identity=expected_source_identity,
    )

    assert replaced is True
    manifest = json.loads(output.read_text(encoding="utf-8"))
    entry = next(entry for entry in manifest["included_files"] if entry["relative_path"].endswith(".sp.rivseg"))
    assert entry["sha256"] == hashlib.sha256(validated_bytes).hexdigest()
    published = (
        tmp_path / "object-store" / "models" / model_id / "v-rivseg-snapshot" / "package" / entry["relative_path"]
    )
    assert published.read_bytes() == validated_bytes
    assert result["status"] == "published"
