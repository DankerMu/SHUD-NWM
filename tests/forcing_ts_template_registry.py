"""The register of renderer-input forcing fact-row templates and the census counter (#1990).

``packages/common/forcing_ts_render.py`` takes SQL text and has no registry of its
own — deliberately, and for the same reason the river renderer has none: a
production helper that knew about every call site would have to import them all.
The register is therefore test-side, and it is what makes the oracles exhaustive
rather than anecdotal.

This is the forcing mirror of ``tests/river_ts_template_registry.py``, with two
deliberate divergences:

* **no ``expected_aids``.** There is no ``#1342`` marker mechanism on the forcing
  side — the legacy forcing table has no key columns, so the two store variants
  are two independently authored templates rather than one template minus its
  marked lines (``design.md:126``, invariant I6). No forcing file may introduce a
  marker (must-preserve M3), so there is nothing per entry to count.
* **``source(store)`` returns a :class:`~packages.common.forcing_ts_render.ForcingTemplatePair`,
  not a ``str``.** The store argument is kept — a reader may still compose
  caller-owned store-specific literals around the pair — but the renderer selects
  between two texts instead of transforming one, so the pair is what a reader
  hands it.

THE NINE READERS ARE WIRED (cut (b) = task 7.2)
-----------------------------------------------

``tasks.md`` splits #1990 in two. Cut (a) landed the renderer, this register and
the discovery-set census with zero production callers and an EMPTY
:data:`FORCING_REGISTRY`; cut (b) — this state — wires all nine readers and
populates it, which is what makes the census's closure assertion
``registered + exempt == mentions`` non-vacuous for the first time.

Every wired reader contributes ``mentions=0``. That is not an omission: the
templates carry :data:`~packages.common.forcing_ts_render.FORCING_TABLE_TOKEN`
and the renderer substitutes the constant, so the five reader FILES stopped
containing a schema-qualified spelling of the fact table at all and dropped out
of the discovery sweep. The closure assertion's weight moved onto
``packages/common/forcing_ts_render.py``'s own two constants and onto the exempt
files. See the execution split in ``fixtures/I11-1990.md``.

The census counter
------------------

:func:`forcing_table_mentions` and :func:`discover_forcing_mentions` live here
rather than being imported from ``tests/test_river_ts_text_identity_cleanup.py``:
``tasks.md`` 6.3 collapses that module's oracles when the river contract lands,
and the forcing census outlives it (``tasks.md`` 8.3 is what finally retires
this one).
"""

from __future__ import annotations

import ast
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from packages.common import best_available, display_coverage, forecast_store
from scripts import reset_qhh_smoke_db
from workers.model_registry import qhh_production_bootstrap

if TYPE_CHECKING:
    from packages.common.forcing_ts_render import ForcingTemplatePair

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The roots the discovery sweep walks, from ``tasks.md`` 7.2a. ``db/`` is in the
#: set and is not decorative: ``db/seeds/seed_demo.py`` carries three mentions.
DISCOVERY_ROOTS: tuple[str, ...] = ("packages", "workers", "scripts", "services", "apps", "db")

#: Directory names the sweep never descends into. ``node_modules`` is the load
#: bearing one — ``apps/frontend/node_modules`` can hold vendored ``.py`` files
#: (node-gyp and friends) that are neither ours nor necessarily parseable.
_PRUNED_DIRECTORIES = frozenset({"__pycache__", ".git", ".venv", "node_modules", "dist", "build", ".mypy_cache"})


@dataclass(frozen=True)
class TemplateEntry:
    """One registered forcing read template pair.

    ``kind``
        ``statement`` is a raw renderer input, never composed executed SQL.
        ``dml`` is the same, for a template that writes rather than projects —
        the QHH smoke reset's forcing ``DELETE``. It is carried as a kind rather
        than left to the oracle to sniff from the text, because the projection
        invariant (I5, same column names in the same order across the pair) is
        meaningless for it and "this one has no SELECT list" must be a declared
        property, not an inferred one.
    ``params``
        ``positional`` (``%s``) or ``named`` (``%(name)s`` / ``:name``). Pinned
        per entry because no deletion computes it: the two variants of a pair are
        authored separately and must agree on their parameter shape.
    ``mentions``
        schema-qualified fact-table occurrences in the entry's own SOURCE FILE
        that this entry accounts for, which the closure check sums per file.
        Note that a wired reader's templates carry
        :data:`~packages.common.forcing_ts_render.FORCING_TABLE_TOKEN` and NOT the
        table's name (D1), so a wired reader's entries contribute ``0`` — the
        file's count drops to zero as it is wired, by design. See the execution
        split in ``fixtures/I11-1990.md``.
    ``source(store)``
        The pair the reader hands the renderer for that store, including any
        caller-owned routing literals. The store is required: there is no
        implicit legacy input for a narrow render.
    """

    key: str
    path: str
    kind: str
    params: str
    mentions: int
    source: Callable[[str], ForcingTemplatePair]


#: The nine readers ``tasks.md`` 7.2 wires, one block per reader module, in
#: stable path-sorted order, mirroring the river register's layout so several
#: PRs can touch this file without colliding on one tuple.
#:
#: ``source`` takes the store even though every pair here is store-independent:
#: the argument is the seam a reader that composes caller-owned store-specific
#: literals around its pair would use (river's ``_segment_rows_source_template``
#: has that shape), and dropping it would make such a reader unregisterable.
FORCING_REGISTRY: tuple[TemplateEntry, ...] = (
    TemplateEntry(
        key="best_available.forcing_inputs",
        path="packages/common/best_available.py",
        kind="statement",
        params="positional",
        mentions=0,
        source=lambda _store: best_available._FORCING_INPUTS_TEMPLATES,
    ),
    TemplateEntry(
        key="display_coverage.station_sample_rows",
        path="packages/common/display_coverage.py",
        kind="statement",
        params="named",
        mentions=0,
        source=lambda _store: display_coverage._STATION_SAMPLE_ROWS_TEMPLATES,
    ),
    TemplateEntry(
        key="forecast_store.forcing_readiness_overall",
        path="packages/common/forecast_store.py",
        kind="statement",
        params="positional",
        mentions=0,
        source=lambda _store: forecast_store._FORCING_READINESS_OVERALL_TEMPLATES,
    ),
    TemplateEntry(
        key="forecast_store.forcing_readiness_variable_rows",
        path="packages/common/forecast_store.py",
        kind="statement",
        params="positional",
        mentions=0,
        source=lambda _store: forecast_store._FORCING_READINESS_VARIABLE_ROWS_TEMPLATES,
    ),
    TemplateEntry(
        key="forecast_store.latest_product_station_source",
        path="packages/common/forecast_store.py",
        kind="statement",
        params="named",
        mentions=0,
        source=lambda _store: forecast_store._LATEST_PRODUCT_STATION_SOURCE_TEMPLATES,
    ),
    TemplateEntry(
        key="forecast_store.station_forcing_membership",
        path="packages/common/forecast_store.py",
        kind="statement",
        params="positional",
        mentions=0,
        source=lambda _store: forecast_store._STATION_FORCING_MEMBERSHIP_TEMPLATES,
    ),
    TemplateEntry(
        key="forecast_store.station_series_rows",
        path="packages/common/forecast_store.py",
        kind="statement",
        params="positional",
        mentions=0,
        source=lambda _store: forecast_store._STATION_SERIES_ROWS_TEMPLATES,
    ),
    TemplateEntry(
        key="reset_qhh_smoke_db.forcing_timeseries_delete",
        path="scripts/reset_qhh_smoke_db.py",
        kind="dml",
        params="positional",
        mentions=0,
        source=lambda _store: reset_qhh_smoke_db._FORCING_TIMESERIES_DELETE_TEMPLATES,
    ),
    TemplateEntry(
        key="qhh_production_bootstrap.dynamic_forcing_count",
        path="workers/model_registry/qhh_production_bootstrap.py",
        kind="statement",
        params="positional",
        mentions=0,
        source=lambda _store: qhh_production_bootstrap._DYNAMIC_FORCING_COUNT_TEMPLATES,
    ),
)


def entry_by_key(key: str) -> TemplateEntry:
    for entry in FORCING_REGISTRY:
        if entry.key == key:
            return entry
    raise KeyError(key)


# ---------------------------------------------------------------------------
# Exempt mentions: every occurrence of the qualified table name that is NOT a
# registered read template, enumerated with a NAMED OWNER so the closure check
# can stay an exact equality instead of an inequality nobody would notice going
# slack (``tasks.md`` 7.2a; shapes and owners from ``fixtures/I11-1990.md`` C3).
#
# Line numbers appear in ``note`` for orientation only. They are never asserted:
# they drift with any edit to the owning file, and a census that reddens on an
# unrelated refactor is a census people learn to silence.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExemptMentions:
    """A group of same-shape, same-owner mentions in one file."""

    path: str
    count: int
    shape: str
    owner: str
    note: str


#: NOTHING. Cut (a) carried five ``reader, unwired`` rows here — one per reader
#: FILE, nine mentions in total — because the readers still spelled the table
#: name themselves. Cut (b) wired all nine through the renderer, so those files
#: now carry the token instead of the name, score **0** in the sweep and leave the
#: discovery set entirely. The rows are deleted rather than zeroed:
#: :func:`test_every_exemption_row_names_a_shape_and_an_owner` refuses a
#: zero-count row, and a zero-count row would in any case claim an exemption for
#: a file that has nothing left to exempt.
#:
#: The name survives as an empty tuple so the census can assert the transition
#: HAPPENED rather than merely that the numbers add up — see
#: ``test_the_nine_readers_left_the_exemption_ledger``.
UNWIRED_READERS: tuple[ExemptMentions, ...] = ()

#: The 38 mentions that are not reads at all and are not this transition's to
#: convert. Measured, not estimated: the issue text's "the two write-side
#: verification queries" was six sites short by an order of magnitude.
NON_READ_MENTIONS: tuple[ExemptMentions, ...] = (
    # -- write side: converts with the writers, in 7.3 ----------------------
    ExemptMentions(
        path="workers/forcing_producer/store.py",
        count=2,
        shape="write-side pre-write probe and window read",
        owner="7.3 (I12) writers",
        note="existence probe and valid_time window read before the replace chain (:786, :794)",
    ),
    ExemptMentions(
        path="workers/forcing_producer/store.py",
        count=2,
        shape="write-side DML",
        owner="7.3 (I12) writers",
        note="the replace chain's DELETE and INSERT (:825, :829)",
    ),
    ExemptMentions(
        path="workers/forcing_producer/store.py",
        count=1,
        shape="write-side post-write row-count verification",
        owner="7.3 (I12) writers",
        note="verify_forcing_version_children's count (:675) — one of the issue's two named sites",
    ),
    ExemptMentions(
        path="packages/common/forcing_domain_handoff_apply.py",
        count=2,
        shape="write-side pre-write probe and window read",
        owner="7.3 (I12) writers",
        note="existence probe and valid_time window read (:797, :806)",
    ),
    ExemptMentions(
        path="packages/common/forcing_domain_handoff_apply.py",
        count=2,
        shape="write-side DML",
        owner="7.3 (I12) writers",
        note="the apply path's DELETE and INSERT (:827, :836)",
    ),
    ExemptMentions(
        path="packages/common/forcing_domain_handoff_apply.py",
        count=1,
        shape="write-side post-write row-count verification",
        owner="7.3 (I12) writers",
        note="_verify_apply_row_counts (:926) — the issue's other named site",
    ),
    # -- handoff protocol: the table name as a KEY, never as SQL ------------
    ExemptMentions(
        path="packages/common/forcing_domain_handoff_apply.py",
        count=11,
        shape="handoff protocol table-name key",
        owner="name-only, no SQL",
        note=(
            "payload dict keys, reason codes and table lists "
            "(:48, :341, :347, :369, :468, :471, :480, :494, :504, :511, :924)"
        ),
    ),
    ExemptMentions(
        path="packages/common/forcing_domain_handoff.py",
        count=3,
        shape="handoff protocol table-name key",
        owner="name-only, no SQL",
        note="the exported table list, the logical-name map and the payload projection (:93, :98, :572)",
    ),
    ExemptMentions(
        path="workers/forcing_producer/file_store.py",
        count=2,
        shape="handoff protocol table-name key",
        owner="name-only, no SQL",
        note="the file-store manifest entry and its row-count key (:740, :765)",
    ),
    # -- index and catalog metadata payloads --------------------------------
    ExemptMentions(
        path="packages/common/forecast_store.py",
        count=2,
        shape="index/catalog metadata payload",
        owner="7.3 (I12) index pins",
        note='the two `"table": …` index-metadata literals (:4009, :4507); they name the table but are not SQL',
    ),
    ExemptMentions(
        path="packages/common/node27_container_contract.py",
        count=1,
        shape="index/catalog metadata payload",
        owner="name-only, no SQL",
        note="SUPERVISED_HYPERTABLES (:226)",
    ),
    # -- seeds -------------------------------------------------------------
    #
    # A judgement call, recorded: C3's table sweeps seeds into its catch-all
    # "name-only / lifecycle tooling" row, but `seed_demo.py:594` is a real
    # INSERT and `:1022` a real SELECT against the fact table. `tasks.md` 7.3
    # names this file explicitly among the files it re-pins, so the owner is
    # named as 7.3 rather than as the catch-all.
    ExemptMentions(
        path="db/seeds/seed_demo.py",
        count=3,
        shape="demo seed write path and its row-count check",
        owner="7.3 (I12) seeds",
        note="the demo INSERT (:594), its verification SELECT (:1022) and that check's table-name key (:1021)",
    ),
    # -- lifecycle, capture and closure tooling ----------------------------
    ExemptMentions(
        path="scripts/node27_timeseries_compression_capture.py",
        count=2,
        shape="lifecycle tooling",
        owner="lifecycle tooling",
        note="HYPERTABLE_KEYS (:57) and the ownership probe's ::regclass literal (:329) — catalog, not fact rows",
    ),
    ExemptMentions(
        path="scripts/node27_timeseries_compression_live_evidence.py",
        count=1,
        shape="lifecycle tooling",
        owner="lifecycle tooling",
        note="HYPERTABLE_KEYS (:61). The plan-shape benchmark is river-only and stays so until 8.1 (C2)",
    ),
    ExemptMentions(
        path="scripts/node27_autopipeline.py",
        count=1,
        shape="lifecycle tooling",
        owner="name-only, no SQL",
        note="a row_counts lookup key in the pipeline summary (:2216)",
    ),
    ExemptMentions(
        path="services/production_closure/two_node_e2e_readonly_db_lane.py",
        count=2,
        shape="readonly lane table list",
        owner="name-only, no SQL",
        note="the lane's expected-table lists (:72, :88)",
    ),
)

#: The renderer's own two D1 constants. Two, not one, because both spell the
#: qualified name today — and it stays two after 7.3, because the counter's
#: spelling class has no trailing word boundary and therefore matches the
#: ``…_legacy`` literal as a prefix (the river register records the identical
#: fact for ``river_ts_render.py``).
RENDERER_CONSTANTS: tuple[ExemptMentions, ...] = (
    ExemptMentions(
        path="packages/common/forcing_ts_render.py",
        count=2,
        shape="renderer table-name constant",
        owner="renderer constants (D1)",
        note="FORCING_TABLE and FORCING_TABLE_LEGACY; 7.3's migration commit flips the second one",
    ),
)

EXEMPT_MENTIONS: tuple[ExemptMentions, ...] = (*UNWIRED_READERS, *NON_READ_MENTIONS, *RENDERER_CONSTANTS)


def exempt_by_path() -> dict[str, int]:
    """Total declared exempt mentions per file."""
    totals: dict[str, int] = {}
    for row in EXEMPT_MENTIONS:
        totals[row.path] = totals.get(row.path, 0) + row.count
    return totals


def registered_by_path() -> dict[str, int]:
    """Total registered-template mentions per file."""
    totals: dict[str, int] = {}
    for entry in FORCING_REGISTRY:
        totals[entry.path] = totals.get(entry.path, 0) + entry.mentions
    return totals


# ---------------------------------------------------------------------------
# The counter
# ---------------------------------------------------------------------------

#: A SCHEMA-QUALIFIED mention of the forcing fact table, however it is quoted,
#: spaced or cased. The census's closure is over this CLASS and not over one
#: spelling: ``str.count("met.forcing_station_timeseries")`` counts
#: ``"met"."forcing_station_timeseries"`` and ``MET . forcing_station_timeseries``
#: zero times, so a new read site could arrive in a discovered file without
#: moving the census and therefore without ever being forced into the register.
#:
#: Deliberately NOT the bare table token: several sites spell the table
#: unqualified on purpose (a logical name in a manifest, a column list), and
#: counting those would move most per-file numbers for no gain. What this census
#: therefore cannot see is a READ spelled ``FROM forcing_station_timeseries``; the
#: compensation is on the renderer side, where
#: ``forcing_ts_render._BARE_FORCING_TABLE`` refuses exactly that spelling (river
#: gets the same cover from its permissive counter disagreeing with its strict
#: walk). Neither guard is sufficient alone — this pairing is.
_FORCING_TABLE_SPELLING = re.compile(r'(?:"met"|\bmet)\s*\.\s*"?forcing_station_timeseries', re.IGNORECASE)


def _docstring_constants(tree: ast.AST) -> set[int]:
    """Identities of the ``ast.Constant`` nodes that are docstrings, not data."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            found.add(id(body[0].value))
    return found


def forcing_table_mentions(source: str, *, filename: str = "<unknown>") -> int:
    """Schema-qualified mentions of the fact table in non-docstring string constants.

    ``filename`` is threaded into :func:`ast.parse` for its diagnostic only, and
    defaults so that a direct caller holding a source string need not invent one.
    It matters to :func:`discover_forcing_mentions`, which parses every ``.py``
    file under the discovery roots and is documented as failing CLOSED on one
    that does not parse: without this, that failure reads ``invalid syntax
    (<unknown>, line 1)`` and names no file out of several hundred.

    PARSED rather than grepped, and the difference is measured, not theoretical:
    ``packages/common/timescale_write_guard.py``,
    ``scripts/node27_timeseries_compression.py`` and
    ``scripts/node27_timeseries_retention.py`` all contain the qualified name and
    all score **0** here, because every one of their mentions is in a docstring or
    a ``#`` comment. Grep puts them in the discovery set and they have nothing to
    exempt; the parsed count leaves them out, correctly. Budget from this number.

    KNOWN LIMIT, recorded rather than fixed (the river census records the same one
    for itself). A file that spells the schema and the table as SEPARATE string
    constants — ``("met", "forcing_station_timeseries")`` — cannot match a
    qualified-spelling regex, so this census cannot see it and therefore cannot
    FORCE such a site into the register. That is the boundary of the closure
    claim, and it is INSIDE the sweep domain rather than off in ``tests/``: at
    least six production files under the discovery roots have that shape —
    ``packages/common/timescale_write_guard.py:70``,
    ``packages/common/node27_timeseries_discovery.py:5``,
    ``services/production_closure/readonly_db_types.py:191``,
    ``scripts/node27_timeseries_compression_supervisor.py:1735-1738``,
    ``scripts/node27_timeseries_compression_live_evidence.py:1887-1890`` and
    ``scripts/node27_timeseries_compression_capture.py:392``. Each was read: all
    six are catalog, hypertable-identity or permission probes that name the
    relation and touch none of its fact columns, so no READ escapes the census
    today — but a future read written in that shape would, and a reviewer must
    check the shape rather than trust this sentence. Widening the pattern to the
    bare token instead would count every logical-name manifest key in the tree and
    make the numbers meaningless; the renderer's own
    ``_BARE_FORCING_TABLE`` guard is what covers the unqualified spelling.
    """
    tree = ast.parse(source, filename=filename)
    docstrings = _docstring_constants(tree)
    return sum(
        len(_FORCING_TABLE_SPELLING.findall(node.value))
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    )


def discover_forcing_mentions(
    root: Path | None = None,
    roots: tuple[str, ...] = DISCOVERY_ROOTS,
) -> dict[str, int]:
    """Sweep ``roots`` for every ``.py`` file with a parsed mention, path-keyed.

    This is the half the river census does NOT have: river asserts its declared
    file list against itself, so a reader in an unlisted file is invisible to it
    (``tests/test_river_ts_read_path_surrogate_keys.py:929`` names this task as
    the owner of a real sweep). Here the DISCOVERED set is compared to the
    declared one, which is what makes "census fails on an unregistered,
    unexempted site" true of a brand new file and not only of a new statement in
    an already-listed one.

    EVERY ``.py`` file under ``roots`` is PARSED. There is no raw-text prefilter,
    and there cannot be a correct one: :func:`forcing_table_mentions` counts over
    FOLDED constant values, so a source whose text contains no qualified spelling
    can still score above zero — implicit concatenation split at the schema dot or
    inside the table's name (this repository concatenates SQL that way in the
    forcing write path already), or a character written as an escape. The sweep
    previously skipped such a file WHOLE, which put its read site beyond both the
    register and the exemption list while the census stayed green; that is pinned
    by ``test_the_sweep_parses_every_file_instead_of_prefiltering_on_raw_text``.
    The price is parsing every file under the roots rather than the handful a
    prefilter admits, which costs this sweep about an order of magnitude in wall
    time (measured when written; budget by re-measuring, not by this sentence) —
    and a file that does not parse, or that is not UTF-8, now fails the sweep
    closed rather than being silently skipped. Both failures NAME THE FILE: the
    parse one through the ``filename`` handed to :func:`forcing_table_mentions`,
    the decode one through the re-raise below, because it fires in ``read_text``
    before the counter is entered. Fail-closed over a few hundred files is only
    usable if the diagnostic points at one of them.
    """
    base = REPO_ROOT if root is None else root
    found: dict[str, int] = {}
    for name in roots:
        for directory, subdirectories, filenames in os.walk(base / name):
            subdirectories[:] = [entry for entry in sorted(subdirectories) if entry not in _PRUNED_DIRECTORIES]
            for filename in sorted(filenames):
                if not filename.endswith(".py"):
                    continue
                path = Path(directory) / filename
                try:
                    source = path.read_text(encoding="utf-8")
                except UnicodeDecodeError as error:
                    raise ValueError(f"{path}: not valid UTF-8, so the census cannot count it ({error})") from error
                count = forcing_table_mentions(source, filename=str(path))
                if count:
                    found[path.relative_to(base).as_posix()] = count
    return found
