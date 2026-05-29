"""Stage-4 grammar fuzzer smoke tests (rtl-buddy-cdc#222).

Foundation surface pin: a fixed seed renders byte-identical SV /
SDC, and the rendered case can be elaborated by Yosys without
error. The full per-rule directional check (the grammar's analog
of :mod:`tests.fuzz.test_mutants`) plus the cross-frontend
differential oracle (Layer C of Stage 3) come in the integration
PR — this file only pins the *surface* that PR builds on.

Tests
~~~~~

- :func:`test_generate_is_deterministic_for_seed` — issue #222
  Sketch point 4 (seeded reproducibility). Two calls with the
  same seed produce byte-identical :class:`RenderedCase` SV / SDC
  / top / params.
- :func:`test_different_seeds_diverge` — distinct seeds produce
  distinct SV bytes; the grammar isn't accidentally seed-
  independent.
- :func:`test_grammar_case_elaborates_and_runs` — the rendered case
  flows through Yosys + the analyzer. Gated on
  :func:`yosys_available` (skip if Yosys isn't on PATH) and the
  ``fuzz_grammar`` marker (default ``pytest -q`` doesn't run it).
- :func:`test_predictions_directional` — for each production
  alone, the analyzer's actual finding set covers every rule in
  ``Prediction.cdc_rules_added``. The xeno mutant test
  (:mod:`tests.fuzz.test_mutants`) uses the same lax directional
  contract — strict equality is the wrong oracle for a verdict
  that's a delta, not a finding-set claim.
"""

from __future__ import annotations

import pytest

from .grammar import PRODUCTIONS, generate
from .runner import run_case
from .yosys_cache import yosys_available

pytestmark = pytest.mark.fuzz_grammar


def test_generate_is_deterministic_for_seed() -> None:
    """Same seed → byte-identical SV + SDC + params."""
    case_a = generate(seed=42)
    case_b = generate(seed=42)
    assert case_a.sv == case_b.sv
    assert case_a.sdc == case_b.sdc
    assert case_a.top == case_b.top
    assert case_a.params == case_b.params
    assert case_a.expected == case_b.expected
    assert case_a.forbidden == case_b.forbidden


def test_different_seeds_diverge() -> None:
    """Distinct seeds produce distinct SV — the grammar isn't
    accidentally seed-independent (would silently happen if a
    production used ``random.choice`` against module-level state
    instead of ``ctx.rng``)."""
    sv_bytes = {generate(seed=s).sv for s in range(8)}
    assert len(sv_bytes) > 1, (
        "8 distinct seeds collapsed to one SV — the grammar is "
        "drawing from module-level random instead of ctx.rng"
    )


def test_n_productions_bounds() -> None:
    """``n_productions`` argument bounds composition length —
    coverage-steering hook (integration PR) calls this with
    ``n_productions=1`` to isolate a single production's verdict.
    """
    case = generate(seed=0, n_productions=1)
    assert len(case.params["productions"]) == 1

    case = generate(seed=0, n_productions=3)
    assert len(case.params["productions"]) == 3


@pytest.mark.skipif(not yosys_available(), reason="yosys not on PATH")
def test_grammar_case_elaborates_and_runs() -> None:
    """End-to-end smoke: Yosys parses the rendered SV, the
    analyzer runs without crashing, the expected / forbidden
    findings hold. Tests several seeds so a single unlucky seed
    can't false-pass via empty composition.
    """
    failures: list[str] = []
    for seed in range(8):
        case = generate(seed=seed)
        result = run_case(case)
        if result.failures:
            failures.extend(
                f"seed={seed} ({case.case_id}): {f}" for f in result.failures
            )
    if failures:
        pytest.fail(
            "grammar-rendered cases produced unexpected findings:\n  "
            + "\n  ".join(failures)
        )


@pytest.mark.skipif(not yosys_available(), reason="yosys not on PATH")
def test_predictions_directional() -> None:
    """For each non-trivial production, generate a single-production
    case and verify every rule in ``Prediction.cdc_rules_added``
    actually fires.

    This is the grammar's analog of :mod:`tests.fuzz.test_mutants`'
    prediction-directional check — same lax contract (the rule
    must fire ≥1 time) for the same reason: a verdict is a
    direction-of-change claim, not a finding-set claim.
    """
    failures: list[str] = []
    for idx, prod in enumerate(PRODUCTIONS):
        if not prod.declared.cdc_rules_added:
            continue
        # Pin a per-production seed so a single failure is locally
        # reproducible without depending on the full PRODUCTIONS
        # ordering.
        case = generate(
            seed=1000 + idx,
            productions=[prod],
            n_productions=1,
            top=f"fuzz_grammar_solo_{prod.name}",
        )
        result = run_case(case)
        fired = {r for r, n in result.fired.items() if n > 0}
        missing = set(prod.declared.cdc_rules_added) - fired
        if missing:
            failures.append(
                f"production={prod.name}: predicted {sorted(prod.declared.cdc_rules_added)}, "
                f"missing {sorted(missing)}, fired {sorted(fired)}"
            )
    if failures:
        pytest.fail(
            "grammar productions failed their directional prediction:\n  "
            + "\n  ".join(failures)
        )
