"""#2916 -- the node-22 compose-lane entry in the two-node overview is superseded.

``docs/runbooks/two-node-deployment-overview.md`` section 5.2 still lists the
docker-compose lane (``infra/env/compute.env`` and friends) that no node-22
unit loads. The list stays as design evidence, behind a retirement sentence
and a complete DOC_STATUS section marker. These cases parse the real file with
the entropy audit's own section-marker parser, so the marker is "complete"
by the same rules the audit applies, not by a re-implementation of them.
"""

from __future__ import annotations

from pathlib import Path

from scripts.governance.entropy_audit.archive_status import _section_archive_status_marker_ranges

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = REPO_ROOT / "docs/runbooks/two-node-deployment-overview.md"

SECTION_HEADING = "### 5.2 22 节点部署入口"
NEXT_HEADING_PREFIX = "### 5.3 "
LEAD_IN = "22 使用："
RETIREMENT_SENTENCE = (
    "`infra/env/compute.env` 是已退役的 docker-e2e 产物，node-22 没有任何单元加载它；"
    "单元到环境文件的权威见 "
    "[`infra/env/README.md`](../../infra/env/README.md#node-22-unit--environmentfile-authority-1694)。"
)
MARKER_LINES = (
    "Archive status:",
    "- status: superseded",
    "- current_authority: infra/env/README.md#node-22-unit--environmentfile-authority-1694",
    "- superseded_by: docs/runbooks/node22-control-plane-manual-recovery.md",
    "- status_since: 2026-10-16",
    "- archive_scope: section",
    "- retained_for: compose-lane design evidence",
)
# Verbatim as they stood before #2916; the marker must not rewrite them.
COMPOSE_LANE_LIST = (
    "- Compose 文件：[`infra/compose.compute.yml`](../../infra/compose.compute.yml)",
    "- 环境样例：[`infra/env/compute.example`](../../infra/env/compute.example)",
    "- 本地真实环境文件：`infra/env/compute.env`，不提交",
    "- systemd 样例：[`infra/systemd/nhms-compute-compose.service`](../../infra/systemd/nhms-compute-compose.service)",
)
COMPUTE_ENV_LIST_LINE = COMPOSE_LANE_LIST[2]


def _lines() -> list[str]:
    return RUNBOOK.read_text(encoding="utf-8").splitlines()


def _section_bounds(lines: list[str]) -> tuple[int, int]:
    """0-based indexes of the ``### 5.2`` heading and of the ``### 5.3`` heading."""
    headings = [index for index, line in enumerate(lines) if line == SECTION_HEADING]
    following = [index for index, line in enumerate(lines) if line.startswith(NEXT_HEADING_PREFIX)]
    assert len(headings) == 1, f"expected one {SECTION_HEADING!r}, found {len(headings)}"
    assert len(following) == 1, f"expected one {NEXT_HEADING_PREFIX!r} heading, found {len(following)}"
    assert headings[0] < following[0]
    return headings[0], following[0]


def _list_start(lines: list[str]) -> int:
    """0-based index of the first compose-lane list line inside section 5.2."""
    heading, following = _section_bounds(lines)
    window = lines[heading + 1 : following]
    hits = [offset for offset, line in enumerate(window) if line == COMPOSE_LANE_LIST[0]]
    assert len(hits) == 1, f"expected the compose-lane list once in section 5.2, found {len(hits)}"
    return heading + 1 + hits[0]


def test_compose_lane_list_is_verbatim_and_contiguous_in_section_5_2() -> None:
    lines = _lines()
    _, following = _section_bounds(lines)
    start = _list_start(lines)

    assert tuple(lines[start : start + len(COMPOSE_LANE_LIST)]) == COMPOSE_LANE_LIST
    assert start + len(COMPOSE_LANE_LIST) <= following


def test_section_5_2_head_is_sentence_lead_in_marker_then_list() -> None:
    lines = _lines()
    heading, following = _section_bounds(lines)
    window = lines[heading + 1 : following]

    assert RETIREMENT_SENTENCE in window
    expected_head = [
        SECTION_HEADING,
        "",
        RETIREMENT_SENTENCE,
        "",
        LEAD_IN,
        "",
        *MARKER_LINES,
        "",
        *COMPOSE_LANE_LIST,
    ]
    assert lines[heading : heading + len(expected_head)] == expected_head


def test_one_complete_section_range_covers_the_compose_lane_list() -> None:
    lines = _lines()
    heading, following = _section_bounds(lines)
    list_start = _list_start(lines)
    # Every number below is 1-based, the parser's convention.
    heading_line = heading + 1
    next_heading_line = following + 1
    first_list_line = list_start + 1
    last_list_line = list_start + len(COMPOSE_LANE_LIST)
    compute_env_line = list_start + COMPOSE_LANE_LIST.index(COMPUTE_ENV_LIST_LINE) + 1
    window = lines[heading + 1 : following]
    assert RETIREMENT_SENTENCE in window
    assert MARKER_LINES[0] in window
    sentence_line = heading + 1 + window.index(RETIREMENT_SENTENCE) + 1
    marker_line = heading + 1 + window.index(MARKER_LINES[0]) + 1

    ranges = _section_archive_status_marker_ranges(lines)
    covering = [r for r in ranges if r.start_line <= first_list_line and last_list_line <= r.end_line]
    in_section = [r for r in ranges if heading_line < r.start_line < next_heading_line]

    assert len(covering) == 1, f"ranges covering the compose-lane list: {covering}"
    assert len(in_section) == 1, f"ranges starting inside section 5.2: {in_section}"
    (marker_range,) = covering
    assert in_section == [marker_range]
    # 1-based closed interval: opens on the first list line, closes on the line
    # before the next heading (the parser runs to the next heading of any level).
    assert marker_range.start_line == first_list_line
    assert marker_range.end_line == next_heading_line - 1
    assert marker_range.start_line <= compute_env_line <= marker_range.end_line
    for outside in (heading_line, sentence_line, marker_line):
        assert not marker_range.start_line <= outside <= marker_range.end_line, outside
