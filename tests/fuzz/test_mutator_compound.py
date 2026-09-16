"""Unit tests for the second-order ``CHAIN_STAGE_INSERT`` pass.

No Yosys, no analyzer: these pin the *selection* logic in
:func:`tests.fuzz._mutator.compound_chain_insert` and the way
:func:`tests.fuzz._mutator.iter_mutants` folds the compound mutants
into a parent's stream. The end-to-end claim — that a compounded
2-deep chain actually lifts CDC-018 — is the fuzz-marked test in
:mod:`tests.fuzz.test_mutants`.

The inline parent below is the minimal shape with **exactly one**
``CHAIN_STAGE_INSERT`` site, which is what lets the "one compound per
first-order insertion" assertions be exact rather than relational:

- ``src_q``'s reader is on a different clock — a crossing, not the
  next link of a chain, so not a site;
- ``sync_meta``'s reader is ``sync_q`` on the same edge — the site;
- ``sync_q``'s only reader is a continuous ``assign`` — not a site
  (see xeno's "why the reader must exist" note).

Needs rtl-buddy-xeno plus its ``[verible]`` extra (the operator is
CST-based); the module skips as a whole when either is missing,
matching how :mod:`tests.fuzz.test_mutants` gates.
"""

from __future__ import annotations

import pytest

from ._mutator import MutantCase, compound_chain_insert, iter_mutants, xeno_available
from .templates.base import RenderedCase

_SV = """\
module compound_probe (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic d_in,
    output logic q_out
);
    logic src_q;
    always_ff @(posedge src_clk) src_q <= d_in;

    logic sync_meta;
    always_ff @(posedge dst_clk) sync_meta <= src_q;

    logic sync_q;
    always_ff @(posedge dst_clk) sync_q <= sync_meta;

    assign q_out = sync_q;
endmodule
"""

_SDC = """\
create_clock -name src_clk -period 10.0 [get_ports src_clk]
create_clock -name dst_clk -period 7.0 [get_ports dst_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
"""

_PARENT = RenderedCase(
    template_name="compound_probe",
    case_id="compound_probe",
    sv=_SV,
    sdc=_SDC,
    top="compound_probe",
    params={},
    expected=(),
)

_KIND = "chain_stage_insert"


def _mutants() -> list[MutantCase]:
    if not xeno_available():
        return []
    return list(iter_mutants(_PARENT, count=16, seed=0))


_ALL = _mutants()
_FIRST_ORDER = [mc for mc in _ALL if mc.mutant.kind.value == _KIND and not mc.compound]
_COMPOUND = [mc for mc in _ALL if mc.compound]


pytestmark = pytest.mark.skipif(
    not _FIRST_ORDER,
    reason=(
        "rtl-buddy-xeno's CHAIN_STAGE_INSERT unavailable "
        "(xeno not importable, or verible not on PATH)"
    ),
)


def test_one_compound_per_first_order_insertion() -> None:
    """Additive, not combinatorial: |compound| == |first-order inserts|."""
    assert len(_FIRST_ORDER) == 1
    assert len(_COMPOUND) == len(_FIRST_ORDER)


def test_only_chain_stage_insert_is_compounded() -> None:
    """No other kind gets a second-order pass."""
    assert {mc.mutant.kind.value for mc in _COMPOUND} == {_KIND}


def test_compound_cases_come_last_with_distinct_ids() -> None:
    """Compounds are appended, and carry the ``_x2`` marker."""
    flags = [mc.compound for mc in _ALL]
    assert flags == sorted(flags), "compound cases must trail the first-order ones"
    ids = [mc.case.case_id for mc in _ALL]
    assert len(set(ids)) == len(ids)
    for mc in _COMPOUND:
        assert mc.case.case_id.endswith("_x2")
        assert mc.case.top.endswith("_x2")
        assert mc.case.params["mutant_compound"] is True


def test_compound_sv_carries_both_inserted_stages() -> None:
    """The operator re-targets its own output, so the chain is +2 deep.

    xeno's ``fresh_identifier`` strips an existing ``_xeno_stage_<n>``
    suffix off the base before picking the next free ``n``, so the two
    registers are ``sync_meta_xeno_stage_1`` / ``_2`` rather than a
    doubly-suffixed name.
    """
    mc = _COMPOUND[0]
    assert "sync_meta_xeno_stage_1" in mc.mutant.sv
    assert "sync_meta_xeno_stage_2" in mc.mutant.sv
    assert "sync_meta_xeno_stage_1_xeno_stage_1" not in mc.mutant.sv
    # The second stage reads the first, and the original reader reads
    # the second — i.e. the chain really is deeper, not two parallel
    # dead flops.
    assert "sync_meta_xeno_stage_2 <= sync_meta_xeno_stage_1" in mc.mutant.sv
    assert "sync_q <= sync_meta_xeno_stage_2" in mc.mutant.sv


def test_compound_diff_summary_joins_both_steps() -> None:
    """``<first> ; then <second>``, with the second keyed on the first."""
    first = _FIRST_ORDER[0].mutant
    compound = _COMPOUND[0].mutant
    assert compound.diff_summary.startswith(f"{first.diff_summary} ; then ")
    assert compound.diff_summary.endswith("after `sync_meta_xeno_stage_1`")
    assert "insert sync stage `sync_meta_xeno_stage_2`" in compound.diff_summary


def test_compound_prediction_is_combined_and_conservative() -> None:
    """Rationale extended, signals unioned, ``cdc_rules_added`` empty."""
    first_pred = _FIRST_ORDER[0].mutant.prediction
    pred = _COMPOUND[0].mutant.prediction

    assert pred.rationale.startswith(first_pred.rationale)
    assert "two stages in total" in pred.rationale
    assert "CDC-018" in pred.rationale
    assert ">=4-stage threshold" in pred.rationale

    assert pred.perturbs_signals == first_pred.perturbs_signals | frozenset(
        {"sync_meta_xeno_stage_1"}
    )
    assert "sync_meta" in pred.perturbs_signals

    # Conservative: the harness can't verify the chain is a
    # cross-domain synchroniser, so no positive rule claim travels
    # into the runner's expected/forbidden encoding.
    assert pred.cdc_rules_added == frozenset()
    assert _COMPOUND[0].case.expected == ()


def test_compound_case_is_not_the_first_order_case() -> None:
    """Distinct SV and distinct top, so the Yosys cache can't collide."""
    first = _FIRST_ORDER[0]
    compound = _COMPOUND[0]
    assert compound.case.sv != first.case.sv
    assert compound.case.top != first.case.top
    assert compound.case.content_hash != first.case.content_hash
    assert compound.parent is first.parent


def test_unparseable_diff_summary_yields_no_compound() -> None:
    """A summary the regex can't read is a skip, never a guess."""
    import dataclasses
    import importlib

    xeno = importlib.import_module("rtl_buddy_xeno")
    bogus = dataclasses.replace(
        _FIRST_ORDER[0].mutant, diff_summary="line 1: something else entirely"
    )
    assert compound_chain_insert(xeno, bogus, count=16, seed=0) is None


def test_no_fallback_to_a_different_site() -> None:
    """Selection is by name: a non-site target is a skip, not a swap.

    The inline parent *does* have a live ``CHAIN_STAGE_INSERT`` site
    (``sync_meta``), so an implementation that took "the first mutant
    the second pass offers" would happily return one here. Naming a
    stage that isn't in the source must yield ``None`` instead —
    that's what keeps the pass to one compound per insertion on the
    chain it actually deepened.
    """
    import dataclasses
    import importlib

    xeno = importlib.import_module("rtl_buddy_xeno")
    off_site = dataclasses.replace(
        _FIRST_ORDER[0].mutant,
        diff_summary="line 3: insert sync stage `not_a_stage` after `sync_meta`",
    )
    assert compound_chain_insert(xeno, off_site, count=16, seed=0) is None
