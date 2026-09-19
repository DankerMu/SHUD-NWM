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

AND THAT IS WHY THERE IS A SECOND SWEEP. ``mentions=0`` everywhere makes
``registered + exempt == mentions`` degenerate to ``exempt == mentions``: the
closure keeps every grip it had on a new SPELLING and loses the one river still
has on a new TEMPLATE (river's entries carry ``mentions=1``, so a tenth
statement in a registered file moves that file's count and must be registered to
close). :func:`discover_forcing_template_pairs` and
:func:`registered_template_pairs` are what replaces it — a structural walk over
the same files insisting that every ``ForcingTemplatePair`` constructed under
the discovery roots is one this register names.

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
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from importlib import import_module
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
#:
#: The seam is necessary but NOT sufficient for river's exact shape. River builds
#: the pair inside a function body, and :func:`discover_forcing_template_pairs`
#: rejects that outright: a construction with no module-level binding raises and
#: aborts the whole sweep, by design (see its docstring and the guard at the end
#: of its loop). So registering a function-body-construction reader takes a
#: deliberate edit to ``discover_forcing_template_pairs`` in the SAME change —
#: and to :func:`registered_template_pairs`, whose identity join against
#: ``vars(module)`` cannot see a pair built fresh on every call either. Writing
#: the ``source`` callable alone will turn the census red, not green.
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

#: The 39 mentions that are not reads at all. Measured, not estimated: the issue
#: text's "the two write-side verification queries" was six sites short by an
#: order of magnitude.
#:
#: The twelve write-side rows below CONVERTED WITH THE WRITERS in task 7.3 and
#: are now narrow, key-predicated statements. They stay EXEMPTIONS rather than
#: becoming registered templates, which is fixture `I12-1991.md` R3's ruling:
#: `render_forcing_ts_sql` takes a PAIR, and the writers are narrow-only from
#: 7.3 on, so registering them would mean authoring legacy variants that can
#: never be rendered. Task 8.3 clears these rows together with the legacy
#: templates.
NON_READ_MENTIONS: tuple[ExemptMentions, ...] = (
    # -- write side: NARROW since 7.3, still exempt (R3) ---------------------
    ExemptMentions(
        path="workers/forcing_producer/store.py",
        count=2,
        shape="write-side pre-write probe and window read",
        owner="7.3 (I12) writers",
        note="existence probe and valid_time window read, both by forcing_version_key (:858, :865)",
    ),
    ExemptMentions(
        path="workers/forcing_producer/store.py",
        count=2,
        shape="write-side DML",
        owner="7.3 (I12) writers",
        note="the replace chain's narrow DELETE and INSERT (:897, :900)",
    ),
    ExemptMentions(
        path="workers/forcing_producer/store.py",
        count=1,
        shape="write-side post-write row-count verification",
        owner="7.3 (I12) writers",
        note=(
            "verify_forcing_version_children's narrow read by forcing_version_key (:691) — "
            "one of the issue's two named sites, and invariant I4"
        ),
    ),
    ExemptMentions(
        path="packages/common/forcing_domain_handoff_apply.py",
        count=2,
        shape="write-side pre-write probe and window read",
        owner="7.3 (I12) writers",
        note="existence probe and valid_time window read, both by forcing_version_key (:874, :882)",
    ),
    ExemptMentions(
        path="packages/common/forcing_domain_handoff_apply.py",
        count=2,
        shape="write-side DML",
        owner="7.3 (I12) writers",
        note="the apply path's narrow DELETE and INSERT (:904, :930)",
    ),
    ExemptMentions(
        path="packages/common/forcing_domain_handoff_apply.py",
        count=1,
        shape="write-side post-write row-count verification",
        owner="7.3 (I12) writers",
        note=(
            "_verify_apply_row_counts's narrow count by forcing_version_key (:1037) — "
            "the issue's other named site, and invariant I4"
        ),
    ),
    # -- handoff protocol: the table name as a KEY, never as SQL ------------
    ExemptMentions(
        path="packages/common/forcing_domain_handoff_apply.py",
        count=12,
        shape="handoff protocol table-name key",
        owner="name-only, no SQL",
        note=(
            "payload dict keys, reason codes and table lists "
            "(:68, :361, :367, :389, :488, :491, :500, :514, :524, :531, :854, :1035). "
            "Twelve since 7.3: the narrow writer's station-key resolution names the table "
            "in its own shape-conflict reason (:854)"
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
        note=(
            "the demo INSERT, its verification SELECT and that check's table-name key. "
            "All three are NARROW since 7.3, and the seed's own vocabulary moved to the "
            "production one in the same change (fixture `I12-1991.md` R1)"
        ),
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
        note=(
            "FORCING_TABLE and FORCING_TABLE_LEGACY. Still two after 7.3 flipped the second "
            "one to `…_legacy`: the counter's spelling class has no trailing word boundary, "
            "so it matches that literal as a prefix (the river register records the identical "
            "fact for `river_ts_render.py`)"
        ),
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
    the decode one through the re-raise in :func:`_iter_python_sources`, because
    it fires in ``read_text`` before the counter is entered. Fail-closed over a
    few hundred files is only usable if the diagnostic points at one of them.

    This is the census's TEXT half. :func:`discover_forcing_template_pairs` is
    the structural half over the same walk, and since D1 it is the one that can
    see a new template at all — the token means a wired reader spells the table
    name nowhere, so it contributes nothing here.
    """
    found: dict[str, int] = {}
    for path, relative, source in _iter_python_sources(REPO_ROOT if root is None else root, roots):
        count = forcing_table_mentions(source, filename=str(path))
        if count:
            found[relative] = count
    return found


def _iter_python_sources(base: Path, roots: tuple[str, ...]) -> Iterator[tuple[Path, str, str]]:
    """Every ``.py`` file under ``roots``, as ``(absolute path, base-relative path, source)``.

    The ONE walk both sweeps below are defined over, factored out rather than
    copied so "the exhaustiveness guard looks where the census looks" is true by
    construction: same roots, same :data:`_PRUNED_DIRECTORIES`, same sorted
    order, same fail-closed contract on a file that cannot be decoded. A second
    walk with its own prune list would let a directory be added to one and not
    the other, which is precisely the escape both sweeps exist to close.
    """
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
                yield path, path.relative_to(base).as_posix(), source


# ---------------------------------------------------------------------------
# The exhaustiveness guard
#
# The closure check above is the census's grip on "a new SPELLING of the table
# name must be registered or exempt". Since D1 it has no grip at all on "a new
# TEMPLATE must be registered": every wired reader carries
# `FORCING_TABLE_TOKEN` and therefore `mentions=0`, so `registered + exempt ==
# mentions` degenerates to `exempt == mentions` and the `registered` term
# constrains nothing. River does not have this hole — its entries carry
# `mentions=1`, so a new statement in a registered file moves its count and the
# closure forces a registration. D1 bought a one-line 7.3 flip and paid for it
# with exactly that grip.
#
# This is what replaces it, in river's own shape ("counter permissive, walk
# strict"): the counter stays blind to the token, and a STRUCTURAL walk over the
# same files insists that every `ForcingTemplatePair` constructed under the
# discovery roots is one the register names. A tenth pair added beside the nine
# then gets `test_i1_i2_*`, `test_i5_*`, the byte-identity pins and the `params`
# claim — or it gets a red census. It is also what covers
# `test_forcing_read_path_store_routing.WIRED_READER_PATHS`, which is DERIVED
# from the register and therefore cannot see an unregistered sixth file on its
# own.
#
# `tasks.md` 7.3 is where this stops being theoretical: the write-side row-count
# payloads (`forcing_ts_render.py:266-272`) become templates then, in files the
# register does not cover today.
# ---------------------------------------------------------------------------

#: The pair class's name as written at a construction site. Matched on the NAME
#: rather than resolved through an import graph, so a construction site is found
#: in a file the sweep has never imported and never will.
TEMPLATE_PAIR_CLASS = "ForcingTemplatePair"


def _template_pair_names(tree: ast.AST) -> frozenset[str]:
    """Local names bound to :data:`TEMPLATE_PAIR_CLASS` in one module.

    The bare class name is ALWAYS in the set, whether or not the module imports
    it: a file that defines its own unrelated ``ForcingTemplatePair`` and
    instantiates it would be flagged, which is the fail-closed direction. The
    import walk on top of that is what catches
    ``from … import ForcingTemplatePair as Pair``, which would otherwise be a
    one-word escape from the whole guard.
    """
    names = {TEMPLATE_PAIR_CLASS}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom | ast.Import):
            for alias in node.names:
                if alias.name.rsplit(".", 1)[-1] == TEMPLATE_PAIR_CLASS:
                    names.add(alias.asname or alias.name)
    return frozenset(names)


def _is_template_pair_construction(node: ast.AST, names: frozenset[str]) -> bool:
    """``ForcingTemplatePair(...)``, however the class was named at the call site.

    Both callee forms, for the same reason the M6 store sweep accepts both: an
    ``ast.Name``-only check makes
    ``forcing_ts_render.ForcingTemplatePair(...)`` invisible, and a guard with a
    published one-token bypass is not a guard.
    """
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Name):
        return node.func.id in names
    return isinstance(node.func, ast.Attribute) and node.func.attr == TEMPLATE_PAIR_CLASS


def _module_level_pair_bindings(tree: ast.Module, names: frozenset[str]) -> list[str]:
    """Names a module binds AT MODULE SCOPE to a freshly constructed pair."""
    bound: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets: tuple[ast.expr, ...] = tuple(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = (node.target,)
            value = node.value
        else:
            continue
        if value is None or not _is_template_pair_construction(value, names):
            continue
        bound.extend(target.id for target in targets if isinstance(target, ast.Name))
    return bound


def discover_forcing_template_pairs(
    root: Path | None = None,
    roots: tuple[str, ...] = DISCOVERY_ROOTS,
) -> dict[str, tuple[str, ...]]:
    """Every module-level ``ForcingTemplatePair`` under ``roots``, path-keyed and name-sorted.

    FAILS CLOSED, naming the file, on a pair the register could not name even if
    its author wanted to: one constructed inside a function, in a comprehension,
    inside a container literal or bound by unpacking. Such a pair has no
    module-level identity for :func:`registered_template_pairs` to resolve, so
    silently ignoring it would reopen the whole escape — the counter would say
    "no sites here" about a file with a live template in it. The registered nine
    are all plain module-level assignments; a reader that needs another shape has
    to make that a deliberate edit here.
    """
    found: dict[str, tuple[str, ...]] = {}
    for path, relative, source in _iter_python_sources(REPO_ROOT if root is None else root, roots):
        tree = ast.parse(source, filename=str(path))
        names = _template_pair_names(tree)
        constructions = sum(1 for node in ast.walk(tree) if _is_template_pair_construction(node, names))
        if not constructions:
            continue
        bound = _module_level_pair_bindings(tree, names)
        if constructions > len(bound):
            raise ValueError(
                f"{path}: {constructions} {TEMPLATE_PAIR_CLASS} construction(s) but only {len(bound)} bound at "
                "module scope. A pair the register cannot name is a pair no shape oracle covers; bind it to a "
                "module-level name and register it in FORCING_REGISTRY."
            )
        found[relative] = tuple(sorted(bound))
    return found


def registered_template_pairs() -> dict[str, tuple[str, ...]]:
    """What :data:`FORCING_REGISTRY` resolves to, in the discovered sweep's shape.

    Resolved by IDENTITY against the production module's namespace and then
    reported as a NAME, which is the only way the two sides can be compared: the
    sweep never imports the files it walks (it must work on a file that does not
    import), and the register never spells a variable name (its ``source`` is a
    callable). ``vars(module)`` is the join.
    """
    resolved: dict[str, set[str]] = {}
    for entry in FORCING_REGISTRY:
        module = import_module(entry.path.removesuffix(".py").replace("/", "."))
        pair = entry.source("legacy")
        bound = {name for name, value in vars(module).items() if value is pair}
        if not bound:
            raise ValueError(
                f"{entry.key}: source() returns a pair {entry.path} does not hold under any module-level name"
            )
        resolved.setdefault(entry.path, set()).update(bound)
    return {path: tuple(sorted(names)) for path, names in resolved.items()}
