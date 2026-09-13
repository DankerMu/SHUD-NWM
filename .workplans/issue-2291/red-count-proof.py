"""Temporary pre-cutover semantic proof; run from repository root with PYTHONPATH=."""
from __future__ import annotations

import tempfile
from pathlib import Path

from packages.common.node27_issue1895_census_bind import bind_pre_movement_census
from packages.common.node27_issue1895_post_target import observe_post_target
from packages.common.node27_issue1895_receipt import assert_sequential_tick_receipt
from packages.common.node27_issue1895_timer import assert_exact_cold_groups, persist_baseline_groups
from packages.common.node27_issue1895_types import Issue1895ReadinessError
from scripts import node27_cold_residency_census as census
from tests.cold_residency_fakes import CUTOFF, LAG, WATERMARK
from tests.test_issue2291_reviewed_census_count import SHA, document, frozen_files, population, receipt


def main() -> int:
    failures = []
    with tempfile.TemporaryDirectory(prefix="issue2291-red-") as temporary:
        root = Path(temporary)
        value = document(3)
        assert value["verdict"] == "GO", value["blockers"]
        assert value["required_group_count"] == value["resolved_group_count"] == 3
        assert all(len(group["members"]) == 8 and group["parity"]["row_count"] == 2 for group in value["groups"])
        print("PASS census: actual non-six complete population accepted (N=3)")
        original, current, bracket, _frozen = frozen_files(root / "private", value)
        cold = population(3, cold=True)
        observed = [dict(group, residency="already_target", complete_target=True) for group in value["groups"]]
        cases = [
            ("G5", "CENSUS_KEYS_DRIFT", lambda: bind_pre_movement_census(
                current_path=current, original_path=original, expected_digest=value["census_digest"],
                bracket_path=bracket, reviewed_sha=SHA,
            )),
            *[(f"G6-call-{call}", "RECEIPT_KEYS_INVALID", lambda call=call: assert_sequential_tick_receipt(
                receipt(value["groups"], call), ordered_keys=value["group_keys"], call_index=call,
            )) for call in range(1, 4)],
            ("G8-observe", "POST_TARGET_BASELINE_COUNT", lambda: observe_post_target(
                baseline_groups=value["groups"], execute=census.CensusObserver(cold).binder(),
                cutoff=CUTOFF, watermark=WATERMARK, lag_seconds=LAG, reviewed_sha=SHA,
            )),
            ("G8-persist", "BASELINE_COUNT_INVALID", lambda: persist_baseline_groups(value["groups"])),
            ("G8-reconcile", "BASELINE_COUNT_INVALID", lambda: assert_exact_cold_groups(
                observed, baseline=value["groups"],
            )),
        ]
        for label, expected_code, exercise in cases:
            try:
                exercise()
            except Issue1895ReadinessError as error:
                if error.code != expected_code:
                    raise AssertionError(f"invalid red fixture at {label}: {error.code}, expected {expected_code}") from error
                print(f"SEMANTIC RED {label}: valid reviewed N=3 refused with {error.code}")
                failures.append(label)
            else:
                print(f"PASS {label}: accepted reviewed N=3")
        # This is a census acceptance bug independently of the historical-six consumers:
        # 64 is syntactically accepted although the extra-slot scan can support only 63.
        try:
            census.require_count_from_arg("64")
        except census.CensusError:
            print("PASS census: unsupported N=64 refused at input")
        else:
            print("SEMANTIC RED census: accepted unsupported reviewed N=64")
            failures.append("census-count-ceiling")
    print(f"semantic failures: {len(failures)}; " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
