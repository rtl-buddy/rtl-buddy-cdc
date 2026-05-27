"""Cross-frontend differential oracle (Stage-3 Layer C of rtl-buddy-cdc#221).

For each rendered case in the corpus, drive the SV through **both**
the Yosys frontend and the slang frontend, run the rule pack on each
resulting :class:`netlist.Module`, and assert that the two finding
sets agree.

What "agree" means here
~~~~~~~~~~~~~~~~~~~~~~~

Equality of the per-rule fired-count dictionary. The hand-authored
:mod:`tests.test_slang_elaboration` suite already checks this on
~25 textbook fixtures; the corpus differential is the parametric
extension — same contract, three orders of magnitude more cases.

Known-divergence allowlist
~~~~~~~~~~~~~~~~~~~~~~~~~~

The corpus surfaces structural-parity gaps in the slang frontend
that the hand-authored fixtures don't reach. Each gap is tracked
under :data:`_KNOWN_SLANG_PARITY_GAPS` and links back to
rtl-buddy-cdc#224, the umbrella issue. The allowlist is **strict
in both directions**:

- A divergence on an *allowlisted* template family produces an
  expected ``xfail`` — the slang gap is known, the corpus pins it.
- A divergence on a *non-allowlisted* template fails immediately —
  a new gap (or an inadvertently broken contract).
- Convergence on an *allowlisted* template family produces a hard
  failure — slang has presumably been fixed; the allowlist row
  must be removed in the same change set.

This is the same shape :func:`pytest.mark.xfail(strict=True)` uses,
hand-rolled so the failure message can name the specific issue ref
and template family to update.

Skips (vs. divergence)
~~~~~~~~~~~~~~~~~~~~~~

- The whole module is skipped when pyslang isn't importable
  (mirrors the per-frontend ``pytest.mark.skipif`` shape in
  :mod:`tests.fuzz.test_corpus`).
- Individual cases skip when slang **rejects** the SV as illegal
  SystemVerilog. The two ``*_source_sync_internal`` fixtures in
  :mod:`tests.test_slang_elaboration` already document this
  pattern: slang is right to reject (the SV embeds Yosys
  internals); the differential is meaningless in that case.

Performance
~~~~~~~~~~~

The ``fuzz_diff`` selection runs *both* frontends per case, so the
budget per rtl-buddy-cdc#221 is "≤5–10× slower than the ``fuzz``
selection." Slang is in-process so the marginal cost above ``fuzz``
is the slang elaboration (~10–30 ms) plus a Module pickle — well
inside the budget.
"""

from __future__ import annotations

import pytest

from .runner import collect_cases, run_case, run_case_slang
from .slang_cache import slang_available
from .yosys_cache import yosys_available

# Collect at import time so pytest -k filtering can target case ids.
_CASES = collect_cases()


# Template families whose Yosys/slang disagreement is a *known*
# slang-frontend parity gap — tracked under rtl-buddy-cdc#224. Each
# row is one slang emission gap; the corpus template is the row's
# own regression sentinel. Once the slang frontend closes the gap,
# the entry here disappears in the same PR.
#
# This dict is consulted in *both* directions: an allowlisted family
# that starts agreeing produces a hard failure (XPASS), forcing the
# allowlist update — same semantics as ``pytest.mark.xfail(strict=True)``.
#
# Currently empty: the six rows that originally landed alongside this
# oracle (``cdc006_comb_source``, ``cdc014_comb_between_stages``,
# ``cdc016_opposite_edge``, ``rdc004_comb_driven_reset``,
# ``rdc005_multi_source_reset``, ``gap_g4_onehot_decode_independent_sync``)
# were all closed by the slang-frontend fixes in PRs #227 / #228 /
# #229 — see ``src/rtl_buddy_cdc/frontends/slang.py``'s
# ``_emit_net_initializer``, ``CLK_POLARITY`` thread, and
# ``_alias_indexed_assign`` for the three lowering additions.
# rtl-buddy-cdc#224 closed alongside them.
_KNOWN_SLANG_PARITY_GAPS: dict[str, str] = {}


pytestmark = [
    pytest.mark.fuzz_diff,
    pytest.mark.skipif(not slang_available(), reason="pyslang not importable"),
    pytest.mark.skipif(not yosys_available(), reason="yosys not on PATH"),
]


@pytest.mark.parametrize("case", _CASES, ids=[c.case_id for c in _CASES])
def test_frontends_agree(case) -> None:
    """Yosys and slang frontends must produce the same fired-rule
    counts on every corpus case — except for the templates in
    :data:`_KNOWN_SLANG_PARITY_GAPS`, which must consistently
    disagree until the linked issue closes the gap."""
    try:
        yosys_result = run_case(case)
    except Exception as exc:
        pytest.skip(f"yosys frontend failed: {exc}")
        return

    try:
        slang_result = run_case_slang(case)
    except Exception as exc:
        # Slang correctly rejects SV that isn't legal SystemVerilog;
        # the differential is undefined in that case.
        pytest.skip(f"slang frontend failed: {exc}")
        return

    yosys_fired = yosys_result.fired
    slang_fired = slang_result.fired
    disagrees = yosys_fired != slang_fired
    known_gap = _KNOWN_SLANG_PARITY_GAPS.get(case.template_name)

    if known_gap is not None:
        if disagrees:
            # Expected divergence — surface as xfail with the issue
            # ref so the report names the slang gap explicitly.
            pytest.xfail(f"known slang-frontend parity gap ({known_gap})")
        # Allowlist row no longer needed — slang got fixed.
        pytest.fail(
            f"\ncase: {case.case_id}\n"
            f"template: {case.template_name}\n"
            f"This template family is on _KNOWN_SLANG_PARITY_GAPS pointing at "
            f"{known_gap}, but the frontends now agree. Remove the row from "
            f"the allowlist (and close {known_gap} if all its rows are gone)."
        )

    if disagrees:
        all_rules = sorted(set(yosys_fired) | set(slang_fired))
        diff_rows = [
            f"  {r:<10} yosys={yosys_fired.get(r, 0):>3}  "
            f"slang={slang_fired.get(r, 0):>3}"
            for r in all_rules
            if yosys_fired.get(r, 0) != slang_fired.get(r, 0)
        ]
        pytest.fail(
            f"\ncase: {case.case_id}\n"
            f"template: {case.template_name}\n"
            f"params: {case.params}\n"
            "yosys vs slang per-rule disagreement:\n" + "\n".join(diff_rows) + "\n"
            "If this is a new slang-frontend gap, add "
            f"'{case.template_name}' to _KNOWN_SLANG_PARITY_GAPS "
            "and file (or extend) the slang-parity umbrella issue."
        )
