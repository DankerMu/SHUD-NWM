from __future__ import annotations

from services.orchestrator.chain import build_reindexed_manifest, parse_sacct_array_results


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


def test_same_named_segment_in_different_networks_is_not_merged() -> None:
    # Two basins whose river networks each contain a segment named "seg_main" but live in
    # DIFFERENT river_network_version_ids. Identity must be keyed by the composite
    # (river_network_version_id, river_segment_id), NOT by segment name alone, so a row from
    # network-1 is never attributed to network-2.
    manifest = [
        {
            "task_id": 0,
            "model_id": "a",
            "basin_version_id": "basin_a",
            "run_id": "run_a",
            "river_network_version_id": "river_v1",
            "river_segment_id": "seg_main",
        },
        {
            "task_id": 1,
            "model_id": "b",
            "basin_version_id": "basin_b",
            "run_id": "run_b",
            "river_network_version_id": "river_v2",
            "river_segment_id": "seg_main",
        },
    ]

    reindexed = build_reindexed_manifest(manifest, [0, 1])

    composite_keys = [(entry["river_network_version_id"], entry["river_segment_id"]) for entry in reindexed]
    # Same segment name, but the composite keys stay distinct -> no merge.
    assert composite_keys == [("river_v1", "seg_main"), ("river_v2", "seg_main")]
    assert len(set(composite_keys)) == 2
    # Name-only keying would collapse to one entry; prove it would have lost a row.
    assert len({entry["river_segment_id"] for entry in reindexed}) == 1
    # Each network keeps its own basin/run identity; nothing from network-1 leaks into network-2.
    by_network = {entry["river_network_version_id"]: entry for entry in reindexed}
    assert by_network["river_v1"]["run_id"] == "run_a"
    assert by_network["river_v2"]["run_id"] == "run_b"
    assert by_network["river_v1"]["basin_version_id"] != by_network["river_v2"]["basin_version_id"]


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
