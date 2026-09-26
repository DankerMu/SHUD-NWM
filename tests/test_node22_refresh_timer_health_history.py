"""Node-22 refresh-timer probe: the bounded history fallback and cross-file constants.

Partition (#2532 partition of the 3456-line / 202-case
tests/test_node22_refresh_timer_health.py).
R9 / R9b / R9c / R9d / R9e: history answers when
``latest.json`` cannot, never masks a stopped / disabled / unscheduled timer,
is capped, ordered lexically and refuses symlinks; plus the constants the probe
shares with the refresh runner (history filename shape, ``SCHEMA_VERSION``,
``OUTCOMES``, ``MAX_HISTORY``), the consumer bound in
``services/orchestrator/scheduler_file_providers.py``, the refresh oneshot's
``TimeoutStartSec=``, and the production path defaults every file states --
including ``infra/env/compute.scheduler-provider-refresh.env.example``.
"""

from __future__ import annotations

import json
import os
import re
from datetime import timedelta
from pathlib import Path

import pytest

from scripts import node22_refresh_timer_health as probe
from scripts import scheduler_file_provider_refresh as runner_module
from tests.node22_refresh_timer_health_helpers import (
    NOW,
    PROBE_SOURCE,
    PROBE_UNITS,
    RUNBOOK,
    _probe_runbook_section,
    _properties,
    _refresh_receipt_payload,
    _run,
    _systemd_timestamp,
    _verdict,
    _write_refresh_receipt,
)

# ---------------------------------------------------------------------------
# R9 / R9b / R9c / R9d -- the bounded history fallback (design D3b)
# ---------------------------------------------------------------------------


def _receipt_root(tmp_path: Path) -> Path:
    root = tmp_path / "provider-refresh" / "receipts"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _history_root(tmp_path: Path) -> Path:
    history = _receipt_root(tmp_path) / "history"
    history.mkdir(parents=True, exist_ok=True)
    return history


def _history_name(hour: int, marker: int = 0) -> str:
    """The runner's own name shape (the ``run_id`` built in
    ``scripts/scheduler_refresh/runner.py``):
    ``refresh_<YYYYmmddTHHMMSSZ>_<uuid12>.json``."""
    return f"refresh_202609{11:02d}T{hour:02d}0000Z_{marker:012x}.json"


def _write_history_receipt(
    history: Path,
    name: str,
    *,
    manifest_age_hours: float | None = 20.0,
    raw: str | None = None,
    outcome: str = "published",
    providers: list[dict[str, object]] | None = None,
) -> Path:
    path = history / name
    if raw is not None:
        path.write_text(raw)
        return path
    path.write_text(
        json.dumps(
            _refresh_receipt_payload(
                manifest_age_hours=manifest_age_hours,
                providers=providers,
                outcome=outcome,
            )
        )
    )
    return path


def _unresolvable_latest(tmp_path: Path, shape: str) -> Path:
    """Every way the configured `latest.json` can fail to yield a manifest age.

    All four are shapes the live lane reaches: `latest.json` is overwritten by
    EVERY run, including one that fails before assembling a provider list.
    """
    latest = _receipt_root(tmp_path) / "latest.json"
    if shape == "missing":
        return latest
    if shape == "malformed":
        latest.write_text("{not json")
        return latest
    if shape == "schema_invalid":
        latest.write_text(json.dumps({"schema_version": "nhms.other.v1", "providers": []}))
        return latest
    if shape == "empty_providers":
        latest.write_text(
            json.dumps(_refresh_receipt_payload(providers=[], outcome="failed"))
        )
        return latest
    raise AssertionError(f"unknown shape {shape!r}")


UNRESOLVABLE_SHAPES = ["missing", "malformed", "schema_invalid", "empty_providers"]


@pytest.mark.parametrize("shape", UNRESOLVABLE_SHAPES)
def test_r9_history_answers_when_latest_cannot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    """A failed rehearsal receipt no longer produces an alarm at all: the
    previous receipt in the runner's own history still answers the question."""
    latest = _unresolvable_latest(tmp_path, shape)
    history = _history_root(tmp_path)
    name = _history_name(10, 1)
    _write_history_receipt(history, name, manifest_age_hours=20.0)

    status, root, _log = _run(tmp_path, monkeypatch, receipt=latest)
    payload = json.loads((root / "latest.json").read_text())

    assert status == 0, payload
    assert payload["verdict"] == "ok"
    assert payload["manifest_source"] == f"history:{name}"
    assert payload["manifest_age_hours"] == pytest.approx(20.0)


def test_r9_a_dry_run_history_receipt_is_an_ordinary_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `dry_run` history receipt answers with its `after_generated_at`.

    `dry_run` is one of the three outcomes whose `after_generated_at` the
    probe trusts (design D3b).  The fixture carries no `before_generated_at`,
    so an answer at all proves `after_generated_at` was the field read.
    """
    latest = _unresolvable_latest(tmp_path, "empty_providers")
    history = _history_root(tmp_path)
    name = _history_name(9, 2)
    _write_history_receipt(history, name, manifest_age_hours=11.0, outcome="dry_run")

    status, root, _log = _run(tmp_path, monkeypatch, receipt=latest)
    payload = json.loads((root / "latest.json").read_text())

    assert status == 0, payload
    assert payload["manifest_source"] == f"history:{name}"
    assert payload["manifest_age_hours"] == pytest.approx(11.0)


# Design D3b, restated here rather than read from the probe: the outcomes whose
# registry `after_generated_at` the probe trusts.
DESIGN_TRUSTED_AFTER_OUTCOMES = frozenset({"published", "published_receipt_failed", "dry_run"})


def _registry_evidence(
    *, after_hours: float | None, before_hours: float | None
) -> list[dict[str, object]]:
    registry: dict[str, object] = {"name": "registry", "entry_count": 18}
    for field, hours in (("after_generated_at", after_hours), ("before_generated_at", before_hours)):
        if hours is not None:
            registry[field] = (NOW - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
    return [registry]


@pytest.mark.parametrize("where", ["latest", "history"])
def test_r9e_a_replace_uncertain_receipt_never_grades_ok_from_its_after_generated_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    """A `replace_uncertain` receipt whose provider rollback was NOT verified
    still carries the fresh post-publish registry `after_generated_at` of bytes
    that may not be on disk (`test_an_unverified_rollback_keeps_the_committed_evidence`
    in the runner suite; #2297 fixed only the verified case).  Fresh after
    (1 h), stale before (150 h): the probe answers with before, from
    `latest.json` and from a history candidate alike.
    """
    providers = _registry_evidence(after_hours=1.0, before_hours=150.0)
    if where == "latest":
        latest = _receipt_root(tmp_path) / "latest.json"
        latest.write_text(
            json.dumps(_refresh_receipt_payload(providers=providers, outcome="replace_uncertain"))
        )
        expected_source = "latest"
    else:
        latest = _unresolvable_latest(tmp_path, "empty_providers")
        name = _history_name(10, 1)
        _write_history_receipt(
            _history_root(tmp_path), name, providers=providers, outcome="replace_uncertain"
        )
        expected_source = f"history:{name}"

    status, root, _log = _run(tmp_path, monkeypatch, receipt=latest)
    payload = json.loads((root / "latest.json").read_text())

    assert status != 0
    assert payload["verdict"] == "manifest_stale"
    assert payload["manifest_source"] == expected_source
    assert payload["manifest_age_hours"] == pytest.approx(150.0)


def test_r9e_a_replace_uncertain_latest_without_before_generated_at_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `before_generated_at` on an untrusted outcome: the candidate is
    unresolvable, its fresh `after_generated_at` is ignored, and history answers."""
    latest = _receipt_root(tmp_path) / "latest.json"
    latest.write_text(
        json.dumps(
            _refresh_receipt_payload(
                providers=_registry_evidence(after_hours=1.0, before_hours=None),
                outcome="replace_uncertain",
            )
        )
    )
    name = _history_name(10, 1)
    _write_history_receipt(_history_root(tmp_path), name, manifest_age_hours=20.0)

    status, root, _log = _run(tmp_path, monkeypatch, receipt=latest)
    payload = json.loads((root / "latest.json").read_text())

    assert status == 0, payload
    assert payload["verdict"] == "ok"
    assert payload["manifest_source"] == f"history:{name}"
    assert payload["manifest_age_hours"] == pytest.approx(20.0)


@pytest.mark.parametrize("outcome", sorted(runner_module.OUTCOMES))
def test_r9e_every_runner_outcome_lands_in_exactly_one_bucket(tmp_path: Path, outcome: str) -> None:
    """Every outcome the runner can write answers with exactly one of the two
    fields: `after_generated_at` for the design's trusted three, otherwise
    `before_generated_at`."""
    path = tmp_path / "receipt.json"
    path.write_text(
        json.dumps(
            _refresh_receipt_payload(
                providers=_registry_evidence(after_hours=1.0, before_hours=50.0),
                outcome=outcome,
            )
        )
    )

    answered = probe.read_manifest_generated_at(path)

    after, before = NOW - timedelta(hours=1.0), NOW - timedelta(hours=50.0)
    assert answered == (after if outcome in DESIGN_TRUSTED_AFTER_OUTCOMES else before)


def test_r9e_the_trusted_outcomes_are_the_designs_and_a_subset_of_the_runners() -> None:
    """A renamed or dropped runner outcome reds here instead of silently
    falling into the untrusted bucket."""
    assert probe.TRUSTED_AFTER_GENERATED_AT_OUTCOMES == DESIGN_TRUSTED_AFTER_OUTCOMES
    assert probe.TRUSTED_AFTER_GENERATED_AT_OUTCOMES <= runner_module.OUTCOMES
    # The probe comment on the constant cites this test, and the reader's
    # docstring cites this family.
    assert "``test_r9e_*``" in PROBE_SOURCE.read_text()
    assert (
        "test_r9e_the_trusted_outcomes_are_the_designs_and_a_subset_of_the_runners"
        in PROBE_SOURCE.read_text()
    )


def test_r9e_the_runbook_states_the_probes_outcome_rule() -> None:
    """Prose<->code: the runbook's manual `jq` recipe and its fallback bullet
    both name exactly the probe's trusted set, and the bullet names every other
    runner outcome as the `before_generated_at` bucket."""
    trusted = probe.TRUSTED_AFTER_GENERATED_AT_OUTCOMES
    recipes = [
        block
        for block in re.findall(r"```bash\n(.*?)```", RUNBOOK.read_text(), re.S)
        if 'select(.name == "registry")' in block
        and "provider-refresh/receipts/latest.json" in block
    ]
    assert len(recipes) == 1, recipes
    assert set(re.findall(r'\$o == "([^"]+)"', recipes[0])) == trusted
    assert "then .after_generated_at else .before_generated_at end" in recipes[0]

    section = _probe_runbook_section()
    start = section.index("按 receipt 的 `outcome` 取字段")
    bullet = section[start : section.index("\n\n", start)]
    trusted_part, rest = bullet.split("时取 `registry.after_generated_at`", 1)
    assert set(re.findall(r"`([a-z_]+)`", trusted_part)) - {"outcome"} == trusted
    others = re.search(r"其余 outcome（([^）]*)）", rest)
    assert others is not None, bullet
    assert set(re.findall(r"`([a-z_]+)`", others.group(1))) == runner_module.OUTCOMES - trusted
    assert "取 `registry.before_generated_at`" in rest


def test_r9b_an_unresolvable_latest_never_masks_a_stopped_timer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The A1 regression.

    Grading an unresolvable manifest at precedence 1 hid a genuinely stopped
    timer behind a generic `probe_failed` for as long as the failed receipt
    stayed newest -- up to ~24 h on the daily cadence.  This must fail if the
    manifest arm is ever moved back above the timer arms.
    """
    latest = _unresolvable_latest(tmp_path, "empty_providers")
    _history_root(tmp_path)  # present but empty: nothing resolves an age

    status, root, _log = _run(
        tmp_path,
        monkeypatch,
        receipt=latest,
        properties=_properties(
            active_state="inactive",
            sub_state="dead",
            inactive_enter=_systemd_timestamp(NOW - timedelta(days=6)),
            next_elapse="",
        ),
    )
    payload = json.loads((root / "latest.json").read_text())

    assert status != 0
    assert payload["verdict"] == "timer_stopped"
    assert payload["manifest_source"] == "unavailable"
    assert payload["manifest_age_hours"] is None


def test_r9b_an_unresolvable_latest_never_masks_a_disabled_timer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest = _unresolvable_latest(tmp_path, "missing")

    status, root, _log = _run(
        tmp_path,
        monkeypatch,
        receipt=latest,
        properties=_properties(unit_file_state="disabled"),
    )

    assert status != 0
    assert _verdict(root) == "timer_not_enabled"


@pytest.mark.parametrize(
    "next_elapse",
    ["", "n/a", "not-a-timestamp"],
    ids=["empty", "systemd-absent", "unparseable"],
)
def test_r9b_an_unresolvable_latest_never_masks_an_unscheduled_timer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, next_elapse: str
) -> None:
    """The THIRD timer arm, and the one the other two did not cover.

    The spec says the manifest arm "SHALL NOT mask ANY timer verdict", but only
    `timer_stopped` and `timer_not_enabled` were pinned under an unresolvable
    `latest.json`.  Verified: with just those two present, hoisting the
    precedence-7 `manifest_unavailable` arm ABOVE the `timer_not_scheduled`
    check leaves the whole suite green -- an `active` timer that will never
    tick again would have been reported as a missing receipt.
    """
    latest = _unresolvable_latest(tmp_path, "empty_providers")
    # No `history/` sibling at all: nothing can resolve an age, so precedence 7
    # is genuinely armed and is what this test proves does NOT win.
    assert not (latest.parent / "history").exists()

    status, root, _log = _run(
        tmp_path,
        monkeypatch,
        receipt=latest,
        properties=_properties(active_state="active", next_elapse=next_elapse),
    )
    payload = json.loads((root / "latest.json").read_text())

    assert status != 0
    assert payload["verdict"] == "timer_not_scheduled"
    assert payload["manifest_source"] == "unavailable"
    assert payload["manifest_age_hours"] is None


def test_r9c_no_history_directory_at_all_is_manifest_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest = _unresolvable_latest(tmp_path, "empty_providers")

    status, root, _log = _run(tmp_path, monkeypatch, receipt=latest)
    payload = json.loads((root / "latest.json").read_text())

    assert status != 0
    assert payload["verdict"] == "manifest_unavailable"
    assert payload["manifest_source"] == "unavailable"
    assert payload["manifest_age_hours"] is None


def test_r9c_history_present_but_every_candidate_unresolvable_is_manifest_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest = _unresolvable_latest(tmp_path, "malformed")
    history = _history_root(tmp_path)
    _write_history_receipt(history, _history_name(8, 1), raw="{nope")
    _write_history_receipt(history, _history_name(9, 2), providers=[])
    _write_history_receipt(
        history,
        _history_name(10, 3),
        providers=[{"name": "readiness", "after_generated_at": "2026-09-11T10:00:00Z"}],
    )

    status, root, _log = _run(tmp_path, monkeypatch, receipt=latest)
    payload = json.loads((root / "latest.json").read_text())

    assert status != 0
    assert payload["verdict"] == "manifest_unavailable"
    assert payload["manifest_source"] == "unavailable"


def test_r9d_the_directory_listing_is_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A history directory far larger than the runner's own `MAX_HISTORY = 32`
    must not be walked end to end by an hourly watchdog."""
    latest = _unresolvable_latest(tmp_path, "missing")
    history = _history_root(tmp_path)
    for index in range(250):
        _write_history_receipt(history, _history_name(index % 24, index), raw="{nope")

    yielded = [0]
    real_scandir = os.scandir

    def counting_scandir(path):  # type: ignore[no-untyped-def]
        iterator = real_scandir(path)
        # `os.scandir` is global once patched, and pytest's own tmp_path
        # teardown calls it with a directory FILE DESCRIPTOR; only count the
        # history directory and hand everything else straight back.
        if not isinstance(path, (str, os.PathLike)) or Path(path) != history:
            return iterator

        def counted():  # type: ignore[no-untyped-def]
            try:
                for entry in iterator:
                    yielded[0] += 1
                    yield entry
            finally:
                iterator.close()

        return counted()

    monkeypatch.setattr(probe.os, "scandir", counting_scandir)

    status, root, _log = _run(tmp_path, monkeypatch, receipt=latest)

    assert status != 0
    assert _verdict(root) == "manifest_unavailable"
    assert yielded[0] <= probe.MAX_HISTORY_ENTRIES_LISTED
    assert probe.MAX_HISTORY_ENTRIES_LISTED == 200


def test_r9d_at_most_ten_candidates_are_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest = _unresolvable_latest(tmp_path, "missing")
    history = _history_root(tmp_path)
    for index in range(30):
        _write_history_receipt(history, _history_name(index % 24, index), raw="{nope")

    opened: list[Path] = []
    real_read = probe.read_bounded_no_follow

    def spy(path: Path, *, max_bytes: int) -> bytes:
        opened.append(Path(path))
        return real_read(path, max_bytes=max_bytes)

    monkeypatch.setattr(probe, "read_bounded_no_follow", spy)

    status, root, _log = _run(tmp_path, monkeypatch, receipt=latest)
    from_history = [path for path in opened if path.parent == history]

    assert status != 0
    assert _verdict(root) == "manifest_unavailable"
    assert len(from_history) <= probe.MAX_HISTORY_CANDIDATES_OPENED
    assert probe.MAX_HISTORY_CANDIDATES_OPENED == 10


def test_r9d_off_shape_filenames_are_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the runner's own `refresh_<UTC>_<uuid12>.json` shape is a candidate.

    The off-shape files below all sort ABOVE the legitimate one, so a scan that
    did not filter would read them first.
    """
    latest = _unresolvable_latest(tmp_path, "missing")
    history = _history_root(tmp_path)
    legitimate = _history_name(10, 1)
    _write_history_receipt(history, legitimate, manifest_age_hours=31.0)
    for off_shape in (
        "zzz-operator-copy.json",
        "refresh_backup.json",
        "refresh_20260911T110000Z_short.json",
        "refresh_20260911T110000Z_0123456789ab.json.bak",
    ):
        (history / off_shape).write_text(
            json.dumps(_refresh_receipt_payload(manifest_age_hours=1.0))
        )

    status, root, _log = _run(tmp_path, monkeypatch, receipt=latest)
    payload = json.loads((root / "latest.json").read_text())

    assert status == 0, payload
    assert payload["manifest_source"] == f"history:{legitimate}"
    assert payload["manifest_age_hours"] == pytest.approx(31.0)


def test_r9d_a_symlinked_candidate_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest = _unresolvable_latest(tmp_path, "missing")
    history = _history_root(tmp_path)
    older = _history_name(9, 1)
    newer = _history_name(11, 2)
    _write_history_receipt(history, older, manifest_age_hours=42.0)
    target = tmp_path / "outside-the-store.json"
    target.write_text(json.dumps(_refresh_receipt_payload(manifest_age_hours=1.0)))
    (history / newer).symlink_to(target)

    status, root, _log = _run(tmp_path, monkeypatch, receipt=latest)
    payload = json.loads((root / "latest.json").read_text())

    assert status == 0, payload
    # The symlink was the newest candidate and was refused, not followed.
    assert payload["manifest_source"] == f"history:{older}"
    assert payload["manifest_age_hours"] == pytest.approx(42.0)


def test_r9d_ordering_is_lexical_by_filename_and_never_by_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fixed-width UTC prefix makes a lexical DESCENDING sort chronological,
    so no timestamp is parsed and no `mtime` is trusted.  Here `mtime`
    deliberately contradicts the filename order."""
    latest = _unresolvable_latest(tmp_path, "missing")
    history = _history_root(tmp_path)
    older_name = _history_name(9, 1)
    newer_name = _history_name(11, 2)
    older = _write_history_receipt(history, older_name, manifest_age_hours=90.0)
    newer = _write_history_receipt(history, newer_name, manifest_age_hours=30.0)
    # `newer_name` is lexically greatest but is given the OLDEST mtime.
    os.utime(newer, (1_000_000, 1_000_000))
    os.utime(older, (2_000_000_000, 2_000_000_000))

    status, root, _log = _run(tmp_path, monkeypatch, receipt=latest)
    payload = json.loads((root / "latest.json").read_text())

    assert status == 0, payload
    assert payload["manifest_source"] == f"history:{newer_name}"
    assert payload["manifest_age_hours"] == pytest.approx(30.0)


def test_the_history_filename_shape_matches_the_runner_that_writes_it() -> None:
    """Pins the probe's filter to the runner's own name, so a rename on either
    side reds here instead of silently emptying the fallback.

    Also pins the receipt SCHEMA VERSION the probe demands (audit F7).  The
    probe rejects any receipt whose `schema_version` differs, uniformly across
    `latest.json` AND every history candidate, so a runner schema bump with no
    pin here silently kills the whole manifest arm and leaves the probe at a
    permanent `manifest_unavailable`.  D4 forbids the PROBE importing repo
    packages; it says nothing about this test, so the comparison is against the
    runner's live constant rather than a restated literal.
    """
    # #1099 split the 3639-line runner into `scripts/scheduler_refresh/`; the
    # historical path is now a re-export facade and holds none of the literals
    # below.  The two owner modules are named EXPLICITLY rather than globbed:
    # the `history_dirs` pin below is an exact-list equality, so a glob would
    # quietly change what "the runner's source" means whenever the package gains
    # a module, and a literal moving to a third module must red here.
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    runner = "\n".join(
        (scripts_dir / "scheduler_refresh" / name).read_text()
        for name in ("runner.py", "receipt.py")
    )

    assert (
        "run_id = f\"refresh_{started.strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:12]}\""
        in runner
    )
    assert probe.REFRESH_RECEIPT_SCHEMA_VERSION == runner_module.SCHEMA_VERSION

    # The directory the runner writes history receipts into, next to
    # `latest.json`, is the one the probe falls back to.  The runner exposes no
    # constant for it, so its one literal is read from source.
    history_dirs = re.findall(r'^\s*history = root / "([^"]+)"$', runner, re.M)
    assert history_dirs == [probe.HISTORY_DIRECTORY_NAME]
    assert 'history / f"{run_id}.json"' in runner
    assert 'latest_path = root / "latest.json"' in runner


def test_the_consumer_bound_is_the_consumers_own_constant() -> None:
    """Audit F5: `CONSUMER_MAX_MANIFEST_AGE_HOURS` is a COPY, and until now the
    only thing joining the copy to the original was a comment.

    If the consumer's bound drops, the probe keeps grading against 168 and
    reports `ok` for a manifest the consumer has already fail-closed on -- the
    precise failure this probe exists to make impossible, reintroduced one
    level up.  D4 forbids the probe importing repo packages; the test is not
    the probe, so it reads both sides directly.
    """
    from services.orchestrator import scheduler_file_providers

    assert (
        probe.CONSUMER_MAX_MANIFEST_AGE_HOURS
        == scheduler_file_providers.DEFAULT_MAX_MANIFEST_AGE_HOURS
    )
    # And the derived ceilings move with it, so the margin rule cannot be left
    # describing a bound that no longer exists.
    assert (
        probe.MAX_THRESHOLD_HOURS
        == scheduler_file_providers.DEFAULT_MAX_MANIFEST_AGE_HOURS
        - probe.REFRESH_CADENCE_HOURS
    )


def test_the_history_listing_cap_is_above_the_runners_history_cap() -> None:
    """The probe comment and the runbook both say the 200-entry listing cap is
    above the runner's `MAX_HISTORY`; read the runner's constant for both."""
    assert probe.MAX_HISTORY_ENTRIES_LISTED > runner_module.MAX_HISTORY
    assert "test_the_history_listing_cap_is_above_the_runners_history_cap" in PROBE_SOURCE.read_text()
    assert f"`MAX_HISTORY = {runner_module.MAX_HISTORY}`" in PROBE_SOURCE.read_text()
    assert (
        f"目录列举封顶 {probe.MAX_HISTORY_ENTRIES_LISTED} 条"
        f"（高于 runner 自己的 `MAX_HISTORY = {runner_module.MAX_HISTORY}`），"
        f"最多打开最新的 {probe.MAX_HISTORY_CANDIDATES_OPENED} 份"
    ) in _probe_runbook_section()


def test_the_stopped_dwell_is_three_times_the_refresh_oneshots_start_timeout() -> None:
    """The runbook justifies the 6 h stopped-dwell as three times the refresh
    oneshot's `TimeoutStartSec=`; read that value from the refresh unit."""
    refresh_service = (
        Path(__file__).resolve().parents[1]
        / "infra"
        / "systemd"
        / "nhms-scheduler-file-provider-refresh.service"
    )
    timeouts = re.findall(r"^TimeoutStartSec=(\d+)$", refresh_service.read_text(), re.M)
    assert len(timeouts) == 1, timeouts
    seconds = int(timeouts[0])

    assert probe.DEFAULT_STOPPED_DWELL_HOURS * 3600 == 3 * seconds
    assert seconds == 2 * 3600
    section = _probe_runbook_section()
    assert f"`TimeoutStartSec={seconds}` 意味着合法窗口可以跑满两小时" in section
    assert f"{probe.DEFAULT_STOPPED_DWELL_HOURS} 小时是它的三倍" in section


def test_the_probe_units_start_timeout_comment_counts_every_receipt_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe unit's `TimeoutStartSec=` comment counts one receipt read plus
    up to `MAX_HISTORY_CANDIDATES_OPENED` on the fallback; count the reads a
    fully unresolvable run actually makes."""
    unit = Path(__file__).resolve().parents[1] / "infra" / "systemd" / PROBE_UNITS[0]
    comments = " ".join(
        line.lstrip("#").strip() for line in unit.read_text().splitlines() if line.startswith("#")
    )
    assert (
        "one bounded receipt read, and up to "
        f"{probe.MAX_HISTORY_CANDIDATES_OPENED} more on the history fallback"
    ) in comments

    latest = _unresolvable_latest(tmp_path, "malformed")
    history = _history_root(tmp_path)
    for index in range(probe.MAX_HISTORY_CANDIDATES_OPENED + 5):
        _write_history_receipt(history, _history_name(index, index), raw="{nope")
    opened: list[Path] = []
    real_read = probe.read_manifest_generated_at

    def counting_read(path: Path) -> object:
        opened.append(path)
        return real_read(path)

    monkeypatch.setattr(probe, "read_manifest_generated_at", counting_read)

    generated_at, source, _errors = probe.resolve_manifest_generated_at(latest)

    assert generated_at is None and source == probe.MANIFEST_SOURCE_UNAVAILABLE
    assert len(opened) == 1 + probe.MAX_HISTORY_CANDIDATES_OPENED


def test_the_production_path_defaults_match_every_file_that_states_them() -> None:
    """Audit P2: the probe's two production paths are restated in three other
    places, and nothing read both sides.

    `DEFAULT_HEALTH_RECEIPT_ROOT` is where the probe writes and where its
    installer creates a 0700 directory; `DEFAULT_REFRESH_RECEIPT` is the file
    the probe reads, inside the receipt root the refresh runner's env template
    names.  A drift on either makes the probe watch a path nothing writes --
    `manifest_unavailable` forever, or a receipt root the installer never made
    private.
    """
    repo = Path(__file__).resolve().parents[1]
    probe_installer = (repo / "scripts" / "install_node22_refresh_timer_health.sh").read_text()
    runbook = RUNBOOK.read_text()
    env_example = (
        repo / "infra" / "env" / "compute.scheduler-provider-refresh.env.example"
    ).read_text()

    assert (
        f"receipt_root=${{{probe.ENV_HEALTH_RECEIPT_ROOT}:-{probe.DEFAULT_HEALTH_RECEIPT_ROOT}}}"
        in probe_installer
    )
    assert probe.DEFAULT_HEALTH_RECEIPT_ROOT in runbook
    # The runner's env template names the RECEIPT ROOT; the probe reads
    # `latest.json` inside it, so the pin is the parent, not the file.
    assert (
        "NHMS_SCHEDULER_PROVIDER_REFRESH_RECEIPT_ROOT="
        f"{Path(probe.DEFAULT_REFRESH_RECEIPT).parent}" in env_example
    )
    assert Path(probe.DEFAULT_REFRESH_RECEIPT).name == "latest.json"
    assert probe.HISTORY_RECEIPT_NAME.match("refresh_20260912T103558Z_0123456789ab.json")
    assert not probe.HISTORY_RECEIPT_NAME.match("refresh_20260912T103558Z_0123456789ab.json.bak")
    assert not probe.HISTORY_RECEIPT_NAME.match("latest.json")


def test_the_manifest_source_is_always_from_the_closed_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R14: `manifest_source` is `latest` | `history:<filename>` | `unavailable`
    and nothing else, on every path that writes a receipt."""
    history = _history_root(tmp_path)
    name = _history_name(10, 7)
    _write_history_receipt(history, name, manifest_age_hours=20.0)

    observed = set()
    for shape, prepare in (
        ("latest", lambda: _write_refresh_receipt(tmp_path, name="provider-refresh/receipts/latest.json")),
        ("history", lambda: _unresolvable_latest(tmp_path, "malformed")),
    ):
        del shape
        receipt = prepare()
        _status, root, _log = _run(tmp_path, monkeypatch, receipt=receipt)
        observed.add(json.loads((root / "latest.json").read_text())["manifest_source"])
    for candidate in history.iterdir():
        candidate.unlink()
    _status, root, _log = _run(
        tmp_path, monkeypatch, receipt=_unresolvable_latest(tmp_path, "malformed")
    )
    observed.add(json.loads((root / "latest.json").read_text())["manifest_source"])

    assert observed == {
        probe.MANIFEST_SOURCE_LATEST,
        f"history:{name}",
        probe.MANIFEST_SOURCE_UNAVAILABLE,
    }
