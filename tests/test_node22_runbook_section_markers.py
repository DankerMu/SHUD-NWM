"""The node-22 deployment section of the two-node overview names `compute.env` as a leftover.

#2916 put the docker-compose lane list of ``docs/runbooks/two-node-deployment-overview.md``
section 5.2 behind a retirement sentence and a superseded section marker. #3231
deleted the lane, the list and the marker; the section (now 5.1) keeps the
retirement sentence, which sends the reader to the unit-to-file table of
``infra/env/README.md``.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = REPO_ROOT / "docs/runbooks/two-node-deployment-overview.md"

SECTION_HEADING = "### 5.1 22 节点部署入口"
NEXT_HEADING_PREFIX = "### 5.2 "
RETIREMENT_SENTENCE = (
    "`infra/env/compute.env` 是已退役的 docker-e2e 产物，node-22 没有任何单元加载它；"
    "单元到环境文件的权威见 "
    "[`infra/env/README.md`](../../infra/env/README.md#node-22-unit--environmentfile-authority-1694)。"
)


def _lines() -> list[str]:
    return RUNBOOK.read_text(encoding="utf-8").splitlines()


def _section_bounds(lines: list[str]) -> tuple[int, int]:
    """0-based indexes of the node-22 section heading and of the next section heading."""
    headings = [index for index, line in enumerate(lines) if line == SECTION_HEADING]
    following = [index for index, line in enumerate(lines) if line.startswith(NEXT_HEADING_PREFIX)]
    assert len(headings) == 1, f"expected one {SECTION_HEADING!r}, found {len(headings)}"
    assert len(following) == 1, f"expected one {NEXT_HEADING_PREFIX!r} heading, found {len(following)}"
    assert headings[0] < following[0]
    return headings[0], following[0]


def test_node22_section_opens_with_the_retirement_sentence() -> None:
    lines = _lines()
    heading, _ = _section_bounds(lines)

    assert lines[heading : heading + 3] == [SECTION_HEADING, "", RETIREMENT_SENTENCE]
