"""Measured backing for the spelling-neutrality claim in the array-runner spec.

``openspec/specs/slurm-array-runner-integration/spec.md`` (scenario
"parent-segment loop no longer aborts construction on 3.11/3.12") admits the
db-free ``_safe_preserve_final_component`` arm under ADR 0009 clause 1, and
rests that admission on a measurement:

    "The product does differ between CPython 3.12 and 3.13+ where the loop sits
    behind a symlinked parent, but that difference is neutral at the
    dereference: the condition that makes the two spellings differ is precisely
    a loop surviving in the parent chain, so ``lstat`` reports ``ELOOP`` for
    both. That neutrality is measured on 3.11.14 and 3.13.3, not argued."

Nothing in the repository backed that sentence, which is the same shape of debt
ADR 0009 exists to remove -- a transcribed conclusion standing in for evidence.
This module supplies the half that CAN be measured here, on the pinned
interpreter, with real symlinks and a real ``os.lstat``.

STATED LIMIT -- the 3.13 half is still unbacked.  Two pieces of it are assumed
rather than measured by this module:

* the 3.13+ SPELLING is synthesised as ``os.path.realpath(parent) / name``,
  which is the right value only because CPython 3.13 reimplemented
  ``Path.resolve()`` on top of ``os.path.realpath``.  On a 3.13 interpreter
  :func:`test_both_spellings_of_a_parent_chain_loop_lstat_to_eloop` measures the
  real product instead, because the ``sys.version_info`` branch below then
  takes the other arm;
* the spec's "3.13.3" figure itself.  Running this module on 3.13 would back it;
  the project pin is 3.11 and no 3.13 oracle is wired up, so the claim about
  that specific interpreter remains transcribed.

What IS measured here on 3.11: the two spellings genuinely differ, the
<=3.12 product is the raw path (the real function's product, not a
reconstruction), and ``os.lstat`` reports ``ELOOP`` for both -- which is the
substance of the neutrality the spec leans on.
"""

from __future__ import annotations

import errno
import os
import sys
from pathlib import Path

import pytest

# ``services.orchestrator.scheduler`` first, deliberately: the scheduler package
# and its config package import each other, and reaching path_modes before the
# scheduler module is initialised raises AttributeError on the partially
# initialised package. This ordering matches how the other scheduler tests enter
# the package; it is not a style preference.
from services.orchestrator import scheduler as _scheduler  # noqa: F401  (import-order fixture)
from services.orchestrator.scheduler_config.path_modes import _safe_preserve_final_component


def _loop_behind_a_symlinked_parent(tmp_path: Path) -> Path:
    """A path whose parent chain reaches a symlink loop THROUGH a symlink.

    ``<base>/link/loop/child`` with ``link -> realdir`` and
    ``realdir/loop -> loop``.  The indirection through ``link`` is what makes
    the two spellings differ at all: without it the folded product and the raw
    path are the same bytes and the neutrality claim would be vacuous.

    ``tmp_path`` is canonicalised first.  On macOS ``/var`` is itself a symlink
    to ``/private/var``, and a difference introduced by THAT symlink has nothing
    to do with the loop under test.
    """

    base = Path(os.path.realpath(tmp_path))
    (base / "realdir").mkdir()
    os.symlink("realdir", base / "link")
    os.symlink("loop", base / "realdir" / "loop")
    return base / "link" / "loop" / "child"


def _assert_lstat_reports_eloop(path: Path) -> None:
    with pytest.raises(OSError) as excinfo:
        os.lstat(path)
    assert excinfo.value.errno == errno.ELOOP, (
        f"expected ELOOP from os.lstat({path}), got "
        f"{errno.errorcode.get(excinfo.value.errno, excinfo.value.errno)}"
    )


def test_both_spellings_of_a_parent_chain_loop_lstat_to_eloop(tmp_path: Path) -> None:
    """The spec's neutrality claim, measured on the running interpreter.

    The <=3.12 spelling is the RAW path, because ``Path.resolve(strict=False)``
    raises an errno-less ``RuntimeError`` on a symlink loop there and
    ``_safe_preserve_final_component`` returns its input unchanged.  The 3.13+
    spelling is the folded product, because ``Path.resolve`` stopped raising and
    delegates to ``os.path.realpath``.  Whichever arm the running interpreter
    takes, the function's real product is asserted against the expected
    spelling -- so this is a measurement of the code, not a restatement of it.
    """

    path = _loop_behind_a_symlinked_parent(tmp_path)
    raw_spelling = path
    folded_spelling = Path(os.path.realpath(path.parent)) / path.name

    assert raw_spelling != folded_spelling, (
        "the two spellings must differ for the neutrality claim to have content; "
        f"both are {raw_spelling}"
    )

    product = _safe_preserve_final_component(path)
    if sys.version_info >= (3, 13):
        assert product == folded_spelling
    else:
        assert product == raw_spelling

    _assert_lstat_reports_eloop(raw_spelling)
    _assert_lstat_reports_eloop(folded_spelling)


def test_a_missing_component_before_the_loop_is_not_the_neutral_shape(tmp_path: Path) -> None:
    """The neutrality holds under the spec's condition, and NOT one step wider.

    ``<missing>/../<loop>`` is the input class ADR 0009 adjudicates, and it is
    tempting to read the spec sentence as covering it too.  Measured, it does
    not: the kernel walks the raw spelling left to right and faults on the
    missing component with ``ENOENT`` long before it could see the loop, while
    the folded spelling -- where ``os.path.realpath`` has already collapsed the
    ``..`` -- faults with ``ELOOP``.  The two spellings therefore disagree at
    the dereference for this shape.

    That is not a defect in the admission: ``ENOENT`` and ``ELOOP`` both change
    the verdict at the dereference, which is all clause 1 requires.  It is a
    fence around the WORDING: the spec's neutrality is licensed by "a loop
    surviving in the parent chain", and must not be restated as a claim about
    ``<missing>/../<loop>``.
    """

    base = Path(os.path.realpath(tmp_path))
    os.symlink("loop", base / "loop")
    path = base / "gone" / ".." / "loop" / "child"

    raw_spelling = path
    folded_spelling = Path(os.path.realpath(path.parent)) / path.name
    assert raw_spelling != folded_spelling

    with pytest.raises(OSError) as raw_error:
        os.lstat(raw_spelling)
    assert raw_error.value.errno == errno.ENOENT

    _assert_lstat_reports_eloop(folded_spelling)
