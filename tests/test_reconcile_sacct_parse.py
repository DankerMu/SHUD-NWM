"""Shape-validation tests for comment-based sacct row parsing (issue #300 LOW-2).

LOW-2: a malformed sacct JobID (e.g. leading ``_``) normalizes to ``""`` or a
non-numeric string. The live comment-sacct row parser
``_index_comment_sacct_matches`` MUST skip such rows and keep scanning rather
than index a bogus empty-id ``SacctRecord``, which would otherwise manufacture a
false confirmed-absent / mis-bind.
"""

from __future__ import annotations

from services.orchestrator.reconcile_comment_sacct import _index_comment_sacct_matches
from services.orchestrator.reconcile_records import SacctRecord

_COMMENT = "nhms_idem:abc123"


def _row(job_id: str, comment: str = _COMMENT) -> str:
    # JobID|JobName|State|ExitCode|Comment
    return f"{job_id}|stage.sh|RUNNING|0:0|{comment}"


def test_malformed_jobid_skipped_then_valid_array_row_indexed() -> None:
    """(a) A malformed leading-``_`` row whose Comment matches is SKIPPED, and a
    SUBSEQUENT well-formed array row with the same Comment is the one indexed —
    proving no short-circuit / no false-absent."""
    stdout = "\n".join(
        [
            _row("_4"),  # malformed: normalizes to "" -> must be skipped
            _row("99001_4"),  # real array element -> master id 99001
        ]
    )
    records = _index_comment_sacct_matches(stdout)[_COMMENT]
    assert [record.slurm_job_id for record in records] == ["99001"]
    assert records[0].comment == _COMMENT


def test_malformed_jobid_no_valid_followup_indexes_nothing() -> None:
    """(b) Malformed matching rows with NO valid follow-up leave the Comment
    unindexed (confirmed-absent), NOT a bogus empty-id record."""
    stdout = "\n".join(
        [
            _row("_4"),  # normalizes to "" -> skipped
            _row("abc_4"),  # non-numeric master -> skipped
            _row("12345_0", comment="nhms_idem:other"),  # comment mismatch
        ]
    )
    index = _index_comment_sacct_matches(stdout)
    assert _COMMENT not in index
    assert [record.slurm_job_id for record in index["nhms_idem:other"]] == ["12345"]


def test_normal_array_row_normalizes_to_master_id() -> None:
    """(c) Regression: a normal ``<master>_<task>`` array row still normalizes
    to the bare master id."""
    records = _index_comment_sacct_matches(_row("778899_12"))[_COMMENT]
    assert [record.slurm_job_id for record in records] == ["778899"]


def test_single_job_id_passes_through() -> None:
    """Regression: a single-job id (no ``_``) is well-formed and indexed."""
    (record,) = _index_comment_sacct_matches(_row("55555"))[_COMMENT]
    assert isinstance(record, SacctRecord)
    assert record.slurm_job_id == "55555"
