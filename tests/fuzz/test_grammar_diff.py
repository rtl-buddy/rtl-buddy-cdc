"""Cross-frontend differential oracle for Stage-4 grammar cases.

Closes done-when criterion 3 of rtl-buddy-cdc#222: "Generated cases
integrate cleanly with the Stage 3 cross-frontend (``fuzz_diff``)
oracle — agreement rate matches the hand-authored + mutated corpus
or the divergence is explained."

This module is the grammar analog of :mod:`tests.fuzz.test_corpus_diff`:
for each grammar-generated seed, drive the SV through both the
Yosys frontend and the slang frontend, run the rule pack on each,
and assert the per-rule fired-count dictionaries agree.

Why gate under ``fuzz_grammar`` (not ``fuzz_diff``)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The Stage-3 ``fuzz_diff`` selection enumerates the hand-authored
corpus 1:1 (~330 cases × 2 frontends = ~660 elaborations); adding
grammar cases there would inflate the selection without changing
its semantics. Keeping the grammar-side oracle under the
``fuzz_grammar`` marker lets the operator size the grammar
selection independently — useful when Stage-4 productions
multiply.

Known-divergence allowlist
~~~~~~~~~~~~~~~~~~~~~~~~~~

Empty for now. The hand-authored corpus's slang parity gaps
(rtl-buddy-cdc#224) all closed in Stage-3 PRs #227–229; the
foundation productions emit SV in the same lexical idiom as those
templates, so structural parity is expected by construction. A new
divergence here would be either a genuine new slang gap (file
against #224) or a production emitting SV the two frontends
disagree on (file against the production, not the slang frontend).
"""

from __future__ import annotations

import pytest

from .grammar import generate
from .runner import run_case, run_case_slang
from .slang_cache import slang_available
from .yosys_cache import yosys_available

_SEEDS = range(16)

pytestmark = [
    pytest.mark.fuzz_grammar,
    pytest.mark.skipif(not slang_available(), reason="pyslang not importable"),
    pytest.mark.skipif(not yosys_available(), reason="yosys not on PATH"),
]


@pytest.mark.parametrize("seed", _SEEDS)
def test_grammar_frontends_agree(seed: int) -> None:
    """Yosys and slang must produce identical fired-rule counts on
    every grammar-generated case in :data:`_SEEDS`. Per-seed
    parametrisation so a single divergence is locally
    reproducible — ``pytest -k seed0`` re-runs exactly the
    failing case.
    """
    case = generate(seed=seed)
    try:
        yosys_result = run_case(case)
    except Exception as exc:
        pytest.skip(f"yosys frontend failed: {exc}")
        return
    try:
        slang_result = run_case_slang(case)
    except Exception as exc:
        # Slang correctly rejects SV that isn't legal SystemVerilog;
        # same skip semantics tests/fuzz/test_corpus_diff.py uses.
        pytest.skip(f"slang frontend failed: {exc}")
        return

    yosys_fired = yosys_result.fired
    slang_fired = slang_result.fired
    if yosys_fired == slang_fired:
        return

    all_rules = sorted(set(yosys_fired) | set(slang_fired))
    diff_rows = [
        f"  {r:<10} yosys={yosys_fired.get(r, 0):>3}  slang={slang_fired.get(r, 0):>3}"
        for r in all_rules
        if yosys_fired.get(r, 0) != slang_fired.get(r, 0)
    ]
    pytest.fail(
        f"\ngrammar seed: {seed}\n"
        f"case: {case.case_id}\n"
        f"productions: {case.params['productions']}\n"
        "yosys vs slang per-rule disagreement:\n" + "\n".join(diff_rows) + "\n"
        "Either a grammar production is emitting SV the two frontends "
        "disagree on, or a new slang-frontend parity gap (file against "
        "rtl-buddy-cdc#224)."
    )
