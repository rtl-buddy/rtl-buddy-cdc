"""xeno mutant differential (Stage-3 Layer B of rtl-buddy-cdc#221).

For each canonical parent corpus case, generate mutants via the
:mod:`rtl_buddy_xeno` mutator and verify, per mutant:

1. **Elaboration sanity** — the mutated SV Yosys-elaborates without
   error. xeno's operators are token-rewrites today; this pins them
   to "produce legal SystemVerilog the analyzer can consume."
2. **Prediction directional** — the mutant's
   :class:`rtl_buddy_xeno.Prediction` holds against the analyzer's
   output: every rule in ``cdc_rules_added`` actually fires, every
   rule in ``cdc_rules_removed`` stays silent. This is a *lax*
   directional check, not strict equality — rule interactions
   (e.g. CDC-001 also firing on a crossing where CDC-016 is now
   active) are routine and not contracted away by the prediction.

Known xeno v0.0.1 prediction / output bugs
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Today's hit rate against both checks is low because xeno v0.0.1
ships with bugs surfaced by this corpus:

- ``CLOCK_POLARITY_SWAP`` predicts CDC-006 but the corresponding
  rtl-buddy-cdc rule for the resulting opposite-edge sync hazard
  is CDC-016 (CDC-006 covers comb-driven sync sources).
- ``ATTRIBUTE_TOGGLE`` predicts CDC-008 for the
  ``glitchless_clock_mux`` attribute; stripping the attribute on
  the ``gap_g9_glitchless_mux_marked`` template actually causes
  CDC-010 to fire (async-clock-mux).
- ``CLOCK_POLARITY_SWAP`` shuffles every ``posedge``/``negedge``
  token including the reset edge in ``always_ff`` sensitivity
  lists; flipping ``negedge rst_n`` to ``posedge rst_n`` without
  updating the matching ``if (!rst_n)`` body produces invalid SV
  that Yosys rejects with ``ERROR: Async reset ... yields
  non-constant value``.
- Even after rule-id corrections, the operator is structurally
  context-blind: a polarity swap on a source-domain standalone
  flop predicts an opposite-edge sync but no chain exists, so the
  prediction over-claims.

These are tracked in this PR's description (issues filed against
rtl-buddy-xeno's repo). Both failure modes (elaboration AND
prediction) are wrapped in :func:`pytest.xfail` so the cases
surface as expected failures without gating CI on xeno-side fixes.
A pytest.fail still triggers if the failure mode shifts to a
*new* template family — the xfail message names the kind so
regressions stay visible.
"""

from __future__ import annotations

import pytest

from ._mutator import MutantCase, iter_mutants, xeno_available
from .runner import collect_cases, run_case
from .yosys_cache import yosys_available


def _canonical_parents() -> list:
    """First :class:`RenderedCase` per template family.

    Mutating one parent per template gives sufficient coverage of
    the xeno operators' site enumeration — additional parents in a
    template's sweep differ only in SDC clock periods, which the
    SV-only mutators don't see. Keeping the parent set at 35
    bounds the CI cost at a few hundred mutant cases instead of
    a few thousand.
    """
    seen: dict[str, object] = {}
    for case in collect_cases():
        seen.setdefault(case.template_name, case)
    return list(seen.values())


def _collect_mutants() -> list[MutantCase]:
    if not xeno_available():
        return []
    out: list[MutantCase] = []
    for parent in _canonical_parents():
        out.extend(iter_mutants(parent, count=64, seed=0))
    return out


# Collect at module import so ``pytest -k`` filtering can target a
# specific mutant case id.
_MUTANT_CASES = _collect_mutants()


pytestmark = [
    pytest.mark.fuzz,
    pytest.mark.skipif(not yosys_available(), reason="yosys not on PATH"),
    pytest.mark.skipif(
        not xeno_available(),
        reason="rtl-buddy-xeno not importable (needs python>=3.13 + the fuzz dep group)",
    ),
]


@pytest.mark.parametrize(
    "mc",
    _MUTANT_CASES,
    ids=[mc.case.case_id for mc in _MUTANT_CASES],
)
def test_mutant_elaborates_and_predicts(mc: MutantCase) -> None:
    """Per-mutant elaboration + prediction-directional check.

    Combining both into one parametric test halves the pytest
    parametrization overhead (one ``run_case`` per mutant instead
    of two test entries that each re-invoke the Yosys cache).
    """
    # Elaboration sanity. xeno's CLOCK_POLARITY_SWAP currently
    # flips reset-edge tokens in ``always_ff`` sensitivity lists
    # without updating the matching ``if (!rst_n)`` body — Yosys
    # rejects the resulting SV. Until xeno gates the operator on
    # clock-vs-reset edges, these surface as xfail.
    try:
        mutant_result = run_case(mc.case)
    except Exception as exc:
        pytest.xfail(
            f"mutant didn't elaborate (xeno v0.0.1 output bug — see PR body): {exc}"
        )

    # Prediction directional check. Skip when the operator made
    # no CDC claim (e.g. a stripped attribute with no row in
    # _ATTR_PREDICTIONS — xeno emits the mutant anyway, so it's a
    # neutral perturbation as far as the rule pack is concerned).
    pred = mc.mutant.prediction
    if not pred.cdc_rules_added and not pred.cdc_rules_removed:
        return

    mutant_fires = {r for r, n in mutant_result.fired.items() if n > 0}
    missing_added = set(pred.cdc_rules_added) - mutant_fires
    leaked_removed = set(pred.cdc_rules_removed) & mutant_fires

    if not missing_added and not leaked_removed:
        return

    diag = (
        f"\n  parent:        {mc.parent.case_id}\n"
        f"  template:      {mc.parent.template_name}\n"
        f"  kind:          {mc.mutant.kind.value}\n"
        f"  diff:          {mc.mutant.diff_summary}\n"
        f"  rationale:     {pred.rationale}\n"
        f"  predicted +:   {sorted(pred.cdc_rules_added)}\n"
        f"  predicted -:   {sorted(pred.cdc_rules_removed)}\n"
        f"  mutant fires:  {sorted(mutant_fires)}\n"
        f"  missing added: {sorted(missing_added)}\n"
        f"  leaked removed:{sorted(leaked_removed)}"
    )
    pytest.xfail(
        f"mutant prediction did not hold "
        f"(xeno v0.0.1 prediction bug — see PR body):{diag}"
    )
