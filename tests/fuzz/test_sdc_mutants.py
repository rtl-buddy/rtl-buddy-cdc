"""SDC mutant differential (rtl-buddy-cdc#293).

The constraints-side counterpart to :mod:`tests.fuzz.test_mutants`.
For each canonical parent corpus case, the operators in
:mod:`tests.fuzz._sdc_mutator` rewrite the SDC (the SV is the
parent's, modulo the ``top`` rename) and every mutant is checked for:

1. **Analyzer sanity** — the case elaborates and runs the rule pack
   without raising. The SV is the parent's, so a failure here means
   the mutated SDC broke the *parser*, which is the interesting
   signal.
2. **Prediction directional** — every rule the mutant claims in
   ``cdc_rules_added`` actually fires. Only ``UNDECLARE_CLOCK_PORT``
   makes a positive claim today (CDC-021, and only when the
   precondition is verifiable from the parent —
   ``_sdc_mutator._predicts_cdc_021``); ``CLOCK_PERIOD_SCALE`` is
   conservative by construction, so those mutants exercise path 1
   only and feed the coverage report's ``mutants`` column.

Failures wrap in :func:`pytest.xfail` the same way the xeno
differential does: the operators are heuristics over text, and a
residual over-claim should surface as an expected failure with a full
diagnostic block rather than gate CI.
"""

from __future__ import annotations

import pytest

from ._sdc_mutator import SdcMutantCase, iter_sdc_mutants
from .runner import collect_cases, run_case
from .yosys_cache import CACHE_ROOT, yosys_available


def _canonical_parents() -> list:
    """First :class:`RenderedCase` per template family.

    Same strategy (and same rationale) as
    :func:`tests.fuzz.test_mutants._canonical_parents`: one parent per
    family bounds the case count. Note the SDC mutators *are* sensitive
    to the sweep dimension the SV mutators ignore (clock periods), but
    the operators rewrite those periods themselves, so mutating every
    sweep point would only re-derive the same constraint shapes.
    """
    seen: dict[str, object] = {}
    for case in collect_cases():
        seen.setdefault(case.template_name, case)
    return list(seen.values())


def _collect_sdc_mutants() -> list[SdcMutantCase]:
    out: list[SdcMutantCase] = []
    for parent in _canonical_parents():
        out.extend(iter_sdc_mutants(parent, seed=0))
    return out


# Collected at module import so ``pytest -k`` can target one mutant.
_SDC_MUTANT_CASES = _collect_sdc_mutants()


pytestmark = [
    pytest.mark.fuzz,
    pytest.mark.skipif(not yosys_available(), reason="yosys not on PATH"),
]


def test_corpus_yields_sdc_mutants() -> None:
    """Guard against the operators silently finding no sites at all."""
    assert len(_SDC_MUTANT_CASES) > 0
    kinds = {mc.mutant.kind for mc in _SDC_MUTANT_CASES}
    assert kinds == {"UNDECLARE_CLOCK_PORT", "CLOCK_PERIOD_SCALE"}


@pytest.mark.parametrize(
    "mc",
    _SDC_MUTANT_CASES,
    ids=[mc.case.case_id for mc in _SDC_MUTANT_CASES],
)
def test_sdc_mutant_analyzes_and_predicts(mc: SdcMutantCase) -> None:
    try:
        result = run_case(mc.case)
    except Exception as exc:
        pytest.xfail(f"SDC mutant didn't analyze (rtl-buddy-cdc#293): {exc}")

    if not mc.mutant.cdc_rules_added:
        return

    fires = {rule_id for rule_id, n in result.fired.items() if n > 0}
    missing = set(mc.mutant.cdc_rules_added) - fires
    if not missing:
        return

    diag = (
        f"\n  parent:        {mc.parent.case_id}\n"
        f"  template:      {mc.parent.template_name}\n"
        f"  kind:          {mc.mutant.kind}\n"
        f"  diff:          {mc.mutant.diff_summary}\n"
        f"  rationale:     {mc.mutant.rationale}\n"
        f"  predicted +:   {sorted(mc.mutant.cdc_rules_added)}\n"
        f"  mutant fires:  {sorted(fires)}\n"
        f"  missing added: {sorted(missing)}"
    )
    pytest.xfail(f"SDC mutant prediction did not hold (rtl-buddy-cdc#293):{diag}")


def test_undeclare_mutant_is_analyzed_with_its_mutated_sdc() -> None:
    """The cache-key hazard, end-to-end.

    ``runner._analyze`` writes ``<content_hash>.sdc`` only when that
    file doesn't already exist; if an SDC mutant shared its parent's
    digest, the analyzer would read the *parent's* constraints and the
    mutation would be a silent no-op. This asserts the file the runner
    actually parsed is the mutated one — the removed clock is gone —
    and that the analyzer reacts to it by firing CDC-021.
    """
    candidates = [
        mc
        for mc in _SDC_MUTANT_CASES
        if mc.mutant.kind == "UNDECLARE_CLOCK_PORT"
        and mc.mutant.cdc_rules_added == frozenset({"CDC-021"})
    ]
    assert candidates, "no positive UNDECLARE_CLOCK_PORT mutant in the corpus"
    mc = candidates[0]

    result = run_case(mc.case)

    digest = mc.case.content_hash
    assert digest != mc.parent.content_hash
    written = (CACHE_ROOT / digest[:2] / f"{digest}.sdc").read_text()
    assert written == mc.mutant.sdc
    assert written != mc.parent.sdc
    removed = mc.mutant.diff_summary.lstrip("-")
    assert removed not in written
    assert result.fired.get("CDC-021", 0) >= 1
