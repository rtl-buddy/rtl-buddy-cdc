"""A clock gate resolves to its clock leg whichever input carries it.

Found by the gate-level corpus (rtl-buddy-cdc#336): ``abc`` lowered
``bad_clock_gating``'s ``clk & (en_a & en_b)`` to
``$_AND_(A=en_b, B=$_AND_(clk, en_a))``. The rule context traced clocks
without the SDC's clock set, so first-leg-wins resolved the flop to the
enable port ``en_b`` and CDC-021 reported it as an undeclared clock. The
RTL form ``en & clk`` did the same. Only ``create_clock`` ports count as
clocks here: ``en`` is typed with ``set_input_delay -clock clk`` and must
not win.
"""

from __future__ import annotations

import pytest

from rtl_buddy_cdc import sdc
from rtl_buddy_cdc.netlist import Cell, Module, Port
from rtl_buddy_cdc.rules import _build_context, run_all

_SDC = """
create_clock -name clk -period 10.0 [get_ports clk]
set_input_delay -clock clk 1.0 [get_ports {en d}]
"""


def _gated_flop(gate_type: str, a: int, b: int) -> Module:
    # Bits: clk=2, en=3, d=4, q=5, gated clock=6.
    return Module(
        name="top",
        ports={
            "clk": Port("clk", "input", (2,)),
            "en": Port("en", "input", (3,)),
            "d": Port("d", "input", (4,)),
            "q": Port("q", "output", (5,)),
        },
        cells={
            "g": Cell("g", gate_type, {"A": (a,), "B": (b,), "Y": (6,)}),
            "f": Cell("f", "$_DFF_P_", {"C": (6,), "D": (4,), "Q": (5,)}),
        },
        netnames={},
    )


@pytest.mark.parametrize("gate_type", ["$and", "$_AND_"])
@pytest.mark.parametrize(("a", "b"), [(2, 3), (3, 2)], ids=["clk-first", "en-first"])
def test_clock_gate_domain_is_the_clock_leg(gate_type: str, a: int, b: int) -> None:
    spec = sdc.parse(_SDC, backend="tokenizer")
    module = _gated_flop(gate_type, a, b)
    ctx = _build_context(module, spec)
    assert ctx.domains["f"] == "clk"
    assert not [v for v in run_all(module, [], spec) if v.rule_id == "CDC-021"]
