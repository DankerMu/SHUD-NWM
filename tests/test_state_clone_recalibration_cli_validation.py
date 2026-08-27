"""Recalibration state carry-over: validation-boundary tests.

Split from :file:`tests/test_state_clone_recalibration_cli.py` at the existing
``# --- §6.8 --pairs resolution`` marker when that suite outgrew the 1000-line
source guard. This module owns every test AT and AFTER that marker -- the
``--pairs`` resolution branches (row 10, §6.8), the second-registry-payload
merge/refusal contract, and the parser's per-mode required flags (§4.1),
including the missing-``--receipt`` refusal for apply and dry-run (acceptance
#1715).

All CLI environment helpers come from
:file:`tests/state_clone_recalibration_cli_fixtures.py` (shared with the
original end-to-end suite) so dispatch/apply coverage is real -- never
parser-only stubs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.node22_clone_direct_grid_cutover_states import (
    CutoverCloneError,
    build_parser,
    dispatch,
    enforce_mode_flags,
)
from tests.state_clone_recalibration_cli_fixtures import (
    _build_cli_environment,
    _cli_args,
    _registry_models_by_id,
    _write_registry,
)
from tests.state_clone_recalibration_fixtures import (
    M1_MODEL_ID,
    M1_PACKAGE_CHECKSUM,
    M1_PACKAGE_URI,
    M1P_MODEL_ID,
    M1P_PACKAGE_CHECKSUM,
    M1P_PACKAGE_URI,
    ORIGINAL_BASELINE_MODEL_ID,
)

# --- §6.8 --pairs resolution (row 10) --------------------------------------


def test_pairs_resolves_by_model_id_not_through_the_baseline_keyed_map(
    tmp_path: Path,
) -> None:
    """Both variants declare the ORIGINAL baseline and still resolve correctly."""

    env = _build_cli_environment(tmp_path)
    registry = json.loads(Path(env["registry_path"]).read_text(encoding="utf-8"))
    baselines = {
        model["model_id"]: model["resource_profile"]["baseline_model_id"]
        for model in registry["models"]
    }
    # The precondition that makes the baseline-keyed variant map unusable.
    assert baselines == {
        M1_MODEL_ID: ORIGINAL_BASELINE_MODEL_ID,
        M1P_MODEL_ID: ORIGINAL_BASELINE_MODEL_ID,
    }

    receipt = dispatch(_cli_args(env, "--apply"))

    assert receipt["pairs"][0]["source_model_id"] == M1_MODEL_ID
    assert receipt["pairs"][0]["target_model_id"] == M1P_MODEL_ID


def test_pairs_spanning_two_sources_refuses_before_any_write(tmp_path: Path) -> None:
    env = _build_cli_environment(tmp_path, target_source_id="IFS")

    with pytest.raises(CutoverCloneError, match="spans two sources"):
        dispatch(_cli_args(env, "--apply"))

    payload = json.loads(Path(env["canonical_index"]).read_text(encoding="utf-8"))
    assert not any(item["model_id"] == M1P_MODEL_ID for item in payload["entries"])


def test_pairs_with_a_legacy_classifying_source_refuses_before_any_write(
    tmp_path: Path,
) -> None:
    env = _build_cli_environment(tmp_path, source_legacy_manifest=True)

    with pytest.raises(CutoverCloneError, match="does not classify as direct-grid"):
        dispatch(_cli_args(env, "--apply"))

    payload = json.loads(Path(env["canonical_index"]).read_text(encoding="utf-8"))
    assert not any(item["model_id"] == M1P_MODEL_ID for item in payload["entries"])


def test_pairs_with_identical_sides_refuses(tmp_path: Path) -> None:
    env = _build_cli_environment(tmp_path)
    parser = build_parser()
    args = parser.parse_args(
        [
            "--object-store-root",
            str(env["object_root"]),
            "--state-index",
            str(env["canonical_index"]),
            "--mirror-state-index",
            str(env["mirror_index"]),
            "--transfer-mode",
            "recalibration",
            "--variant-registry",
            str(env["registry_path"]),
            "--cutover-time",
            "2026081512",
            "--pairs",
            f"{M1_MODEL_ID}:{M1_MODEL_ID}",
            "--receipt",
            str(tmp_path / "identical-sides-receipt.json"),
            "--apply",
        ]
    )
    enforce_mode_flags(parser, args)
    args.dry_run = False

    with pytest.raises(CutoverCloneError, match="same model on both sides"):
        dispatch(args)


def test_pairs_naming_an_unknown_model_refuses(tmp_path: Path) -> None:
    env = _build_cli_environment(tmp_path)
    parser = build_parser()
    args = parser.parse_args(
        [
            "--object-store-root",
            str(env["object_root"]),
            "--state-index",
            str(env["canonical_index"]),
            "--mirror-state-index",
            str(env["mirror_index"]),
            "--transfer-mode",
            "recalibration",
            "--variant-registry",
            str(env["registry_path"]),
            "--cutover-time",
            "2026081512",
            "--pairs",
            f"{M1_MODEL_ID}:huai_dg_gfs_v9",
            "--receipt",
            str(tmp_path / "unknown-model-receipt.json"),
            "--apply",
        ]
    )
    enforce_mode_flags(parser, args)
    args.dry_run = False

    with pytest.raises(CutoverCloneError, match="registry entry missing"):
        dispatch(args)


@pytest.mark.parametrize(
    ("pairs_value", "expected_message"),
    [
        (f"{M1_MODEL_ID}:{M1P_MODEL_ID}:extra", "malformed --pairs entry"),
        (f":{M1P_MODEL_ID}", "malformed --pairs entry"),
        (f"{M1_MODEL_ID}:", "malformed --pairs entry"),
        (
            f"{M1_MODEL_ID}:{M1P_MODEL_ID},{M1_MODEL_ID}:{M1P_MODEL_ID}",
            "duplicate --pairs entry",
        ),
        (",", "--pairs declared no <M1>:<M1prime> pair"),
    ],
)
def test_malformed_pairs_refuse_before_any_registry_read(
    tmp_path: Path, pairs_value: str, expected_message: str
) -> None:
    """§6.8: every ``_parse_pairs`` refusal branch, and it runs FIRST.

    ``--variant-registry`` deliberately points at a path that does not exist.
    ``_parse_pairs`` runs before the registry payload is read, so a pairs-shaped
    ``CutoverCloneError`` -- rather than the ``FileNotFoundError`` the read would
    raise -- is itself the proof that no registry or gate work happened. Both
    indexes and the receipt path are checked untouched on top of that.
    """

    env = _build_cli_environment(tmp_path)
    before = {
        label: Path(path).read_text(encoding="utf-8")
        for label, path in (
            ("canonical", env["canonical_index"]),
            ("mirror", env["mirror_index"]),
        )
    }
    receipt_path = tmp_path / "never-written-receipt.json"

    with pytest.raises(CutoverCloneError) as excinfo:
        dispatch(
            _cli_args(
                env,
                "--apply",
                "--receipt",
                str(receipt_path),
                "--pairs",
                pairs_value,
                "--variant-registry",
                str(tmp_path / "no-such-registry.json"),
            )
        )

    assert expected_message in str(excinfo.value)
    assert not receipt_path.exists()
    for label, path in (
        ("canonical", env["canonical_index"]),
        ("mirror", env["mirror_index"]),
    ):
        assert Path(path).read_text(encoding="utf-8") == before[label], label


# --- --baseline-registry as a second payload under recalibration -----------


def test_a_pair_spanning_two_registry_payloads_resolves_and_clones(
    tmp_path: Path,
) -> None:
    """PR deviation record 3: the two payloads MERGE, they do not compete.

    The realistic node-22 shape: ``M1`` lives only in the pre-update canonical
    registry and ``M1'`` only in the freshly provisioned one. The refusal
    without ``--baseline-registry`` is asserted first, in the same environment,
    so the merge is proven load-bearing rather than incidentally harmless.
    """

    env = _build_cli_environment(tmp_path)
    models = _registry_models_by_id(env)
    _write_registry(Path(env["registry_path"]), [models[M1P_MODEL_ID]])
    baseline_path = _write_registry(
        tmp_path / "baseline-registry.json", [models[M1_MODEL_ID]]
    )

    # One payload alone cannot resolve the pair at all.
    with pytest.raises(CutoverCloneError, match="registry entry missing"):
        dispatch(_cli_args(env, "--apply"))

    receipt = dispatch(
        _cli_args(env, "--apply", "--baseline-registry", str(baseline_path))
    )

    assert receipt["invocation_outcome"] == "complete"
    assert receipt["cloned_pair_count"] == 1
    pair_record = receipt["pairs"][0]
    # The source resolved out of the SECOND payload, with ITS package root.
    assert pair_record["source_model_id"] == M1_MODEL_ID
    assert pair_record["source_model_package_version"] == M1_PACKAGE_URI
    assert pair_record["source_model_package_checksum"] == M1_PACKAGE_CHECKSUM
    assert pair_record["target_model_package_version"] == M1P_PACKAGE_URI
    assert pair_record["target_model_package_checksum"] == M1P_PACKAGE_CHECKSUM
    assert pair_record["clone_gate_kind"] == "state_compatibility"
    for label, index_path in (
        ("canonical", env["canonical_index"]),
        ("mirror", env["mirror_index"]),
    ):
        entries = json.loads(Path(index_path).read_text(encoding="utf-8"))["entries"]
        assert any(item["model_id"] == M1P_MODEL_ID for item in entries), label


def test_a_model_disagreeing_between_the_two_payloads_refuses_before_any_write(
    tmp_path: Path,
) -> None:
    """The fail-closed half of the same contract: disagreement is not merged.

    Same ``model_id`` in both payloads with a differing ``package_checksum``.
    Which of the two is authoritative is unknowable from the payloads alone, so
    the tool refuses by name instead of silently preferring one.
    """

    env = _build_cli_environment(tmp_path)
    before = {
        label: Path(path).read_text(encoding="utf-8")
        for label, path in (
            ("canonical", env["canonical_index"]),
            ("mirror", env["mirror_index"]),
        )
    }
    models = _registry_models_by_id(env)
    disagreeing_source = dict(models[M1_MODEL_ID])
    disagreeing_source["package_checksum"] = "sha256:pkg-m1-recorded-differently"
    baseline_path = _write_registry(
        tmp_path / "baseline-registry.json",
        [disagreeing_source, models[M1P_MODEL_ID]],
    )

    with pytest.raises(CutoverCloneError) as excinfo:
        dispatch(
            _cli_args(env, "--apply", "--baseline-registry", str(baseline_path))
        )

    assert (
        f"model {M1_MODEL_ID} differs between --variant-registry and "
        "--baseline-registry"
    ) in str(excinfo.value)
    for label, path in (
        ("canonical", env["canonical_index"]),
        ("mirror", env["mirror_index"]),
    ):
        assert Path(path).read_text(encoding="utf-8") == before[label], label


# --- Parser: per-mode required flags (§4.1) --------------------------------


def test_baseline_cutover_still_refuses_its_missing_flags() -> None:
    """The existing mode keeps refusing a missing partition/registry flag."""

    parser = build_parser()
    args = parser.parse_args(
        [
            "--object-store-root",
            "/tmp/object-store",
            "--state-index",
            "/tmp/index.json",
            "--cutover-time",
            "2026081512",
        ]
    )
    assert args.transfer_mode == "baseline_cutover"
    with pytest.raises(SystemExit):
        enforce_mode_flags(parser, args)


def test_baseline_cutover_invocation_parses_verbatim() -> None:
    """The July invocation keeps working with no new flag supplied."""

    parser = build_parser()
    args = parser.parse_args(
        [
            "--object-store-root",
            "/tmp/object-store",
            "--state-index",
            "/tmp/index.json",
            "--baseline-registry",
            "/tmp/baseline.json",
            "--variant-registry",
            "/tmp/variant.json",
            "--cutover-time",
            "2026070112",
            "--warm-basins",
            "a,b",
            "--cold-basins",
            "c",
        ]
    )
    enforce_mode_flags(parser, args)
    assert args.transfer_mode == "baseline_cutover"
    assert args.pairs is None
    assert args.mirror_state_index is None


@pytest.mark.parametrize(
    "dropped",
    ["--pairs", "--mirror-state-index", "--variant-registry", "--receipt"],
)
def test_recalibration_refuses_its_missing_flags(dropped: str) -> None:
    supplied = {
        "--pairs": f"{M1_MODEL_ID}:{M1P_MODEL_ID}",
        "--mirror-state-index": "/tmp/mirror.json",
        "--variant-registry": "/tmp/variant.json",
        "--receipt": "/tmp/receipt.json",
    }
    argv = [
        "--object-store-root",
        "/tmp/object-store",
        "--state-index",
        "/tmp/index.json",
        "--cutover-time",
        "2026081512",
        "--transfer-mode",
        "recalibration",
    ]
    for flag, value in supplied.items():
        if flag == dropped:
            continue
        argv.extend([flag, value])

    parser = build_parser()
    args = parser.parse_args(argv)
    with pytest.raises(SystemExit):
        enforce_mode_flags(parser, args)


@pytest.mark.parametrize("apply", [False, True])
def test_recalibration_refuses_missing_receipt_for_apply_and_dry_run(
    apply: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance #1715: ``--receipt`` is required for apply AND dry-run.

    The same per-mode flag enforcement must reject an omitted receipt in both
    invocation shapes with the same parser-style error text naming ``--receipt``.
    ``--apply`` toggles the mode; without it the invocation is a dry-run.
    ``parser.error`` writes the message to stderr and exits with status 2.
    """

    argv = [
        "--object-store-root",
        "/tmp/object-store",
        "--state-index",
        "/tmp/index.json",
        "--transfer-mode",
        "recalibration",
        "--variant-registry",
        "/tmp/variant.json",
        "--cutover-time",
        "2026081512",
        "--pairs",
        f"{M1_MODEL_ID}:{M1P_MODEL_ID}",
        "--mirror-state-index",
        "/tmp/mirror.json",
    ]
    if apply:
        argv.append("--apply")

    parser = build_parser()
    args = parser.parse_args(argv)
    assert args.receipt is None
    assert args.apply is apply
    with pytest.raises(SystemExit):
        enforce_mode_flags(parser, args)
    assert "--transfer-mode recalibration requires: --receipt" in capsys.readouterr().err
