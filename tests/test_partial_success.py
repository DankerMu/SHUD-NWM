from __future__ import annotations

from services.orchestrator.chain import (
    _output_river_contract,
    build_reindexed_manifest,
    parse_sacct_array_results,
)


def test_array_aggregation_all_succeed() -> None:
    result = parse_sacct_array_results(
        "\n".join(
            [
                "12345|COMPLETED|0:0",
                "12345.batch|COMPLETED|0:0",
                "12345_0|COMPLETED|0:0",
                "12345_1|COMPLETED|0:0",
                "12345.extern|COMPLETED|0:0",
            ]
        ),
        "12345",
    )

    assert result.status == "succeeded"
    assert result.succeeded == 2
    assert result.failed == 0
    assert result.cancelled == 0
    assert result.succeeded_task_ids == (0, 1)


def test_array_aggregation_partial_and_reindexed_manifest() -> None:
    result = parse_sacct_array_results(
        "\n".join(
            [
                "12345_0|COMPLETED|0:0",
                "12345_1|FAILED|1:0",
                "12345_2|CANCELLED by 12|0:15",
                "12345_3|COMPLETED|0:0",
            ]
        ),
        "12345",
    )
    manifest = [
        {"task_id": 0, "model_id": "a", "basin_version_id": "basin_a", "run_id": "run_a"},
        {"task_id": 1, "model_id": "b", "basin_version_id": "basin_b", "run_id": "run_b"},
        {"task_id": 2, "model_id": "c", "basin_version_id": "basin_c", "run_id": "run_c"},
        {"task_id": 3, "model_id": "d", "basin_version_id": "basin_d", "run_id": "run_d"},
    ]

    reindexed = build_reindexed_manifest(manifest, result.succeeded_task_ids)

    assert result.status == "partially_failed"
    assert result.succeeded == 2
    assert result.failed == 1
    assert result.cancelled == 1
    assert [(entry["task_id"], entry["original_task_id"], entry["model_id"]) for entry in reindexed] == [
        (0, 0, "a"),
        (1, 3, "d"),
    ]


def test_array_aggregation_all_fail() -> None:
    result = parse_sacct_array_results(
        "\n".join(
            [
                "12345_0|FAILED|1:0",
                "12345_1|NODE_FAIL|1:0",
            ]
        ),
        "12345",
    )

    assert result.status == "failed"
    assert result.succeeded == 0
    assert result.failed == 2


def test_output_river_contract_keys_same_named_segment_by_its_own_network() -> None:
    # Unit-level proof against the PRODUCTION segment-identity function. Two basins each declare a
    # segment named "seg_main" but under DIFFERENT river_network_version_ids. _output_river_contract
    # must stamp each segment with its OWN network, so the composite (river_network_version_id,
    # river_segment_id) stays distinct and name-only keying cannot merge them.
    contract_0 = _output_river_contract(
        {
            "river_network_version_id": "river_v0",
            "output_river": {"state": "ready", "river_segment_ids": ["seg_main"], "output_segment_count": 1},
        }
    )
    contract_1 = _output_river_contract(
        {
            "river_network_version_id": "river_v1",
            "output_river": {"state": "ready", "river_segment_ids": ["seg_main"], "output_segment_count": 1},
        }
    )

    composite_keys = [
        (contract["river_network_version_id"], segment_id)
        for contract in (contract_0, contract_1)
        for segment_id in contract["river_segment_ids"]
    ]
    # Same segment NAME, distinct production composite keys. FAILS if keying degrades to name-only.
    assert composite_keys == [("river_v0", "seg_main"), ("river_v1", "seg_main")]
    assert len(set(composite_keys)) == 2
    assert len({segment for _, segment in composite_keys}) == 1  # the shared segment name
    assert contract_0["river_network_version_id"] == "river_v0"
    assert contract_1["river_network_version_id"] == "river_v1"


def test_cumulative_partial_preserves_original_task_id() -> None:
    first_manifest = [
        {"task_id": 0, "model_id": "a", "basin_version_id": "basin_a", "run_id": "run_a"},
        {"task_id": 1, "model_id": "b", "basin_version_id": "basin_b", "run_id": "run_b"},
        {"task_id": 2, "model_id": "c", "basin_version_id": "basin_c", "run_id": "run_c"},
    ]
    after_first_partial = build_reindexed_manifest(first_manifest, [0, 2])
    after_second_partial = build_reindexed_manifest(after_first_partial, [1])

    assert [(entry["task_id"], entry["original_task_id"], entry["model_id"]) for entry in after_first_partial] == [
        (0, 0, "a"),
        (1, 2, "c"),
    ]
    assert after_second_partial == [
        {
            "task_id": 0,
            "original_task_id": 2,
            "model_id": "c",
            "basin_version_id": "basin_c",
            "run_id": "run_c",
        }
    ]
