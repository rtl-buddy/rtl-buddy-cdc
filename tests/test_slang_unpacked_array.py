"""Coverage tests for slang-frontend handling of SV unpacked arrays.

Issue: rtl-buddy-cdc#62 — :meth:`_width_of` sizes a variable's bit
pool from its packed width only, so unpacked-dim variables
(``logic chain [N]`` or the production
``logic [W-1:0] sync_chain [STAGES]`` shape used by ``ip_cdc_sync``)
collapse to a single shared bit pool. ``chain[0]`` and ``chain[1]``
alias to the same bit; downstream the rule pack sees a sync chain
of "depth = 1" and CDC-001 false-positives for every standard
synchronizer instance in a design.

The tests below pin the contract: an N-element unpacked array
(possibly with a packed inner dim) allocates ``N * inner_width``
distinct bits, and element selects on it pick the right
``inner_width``-bit stripe.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from rtl_buddy_cdc.frontend import Frontend, elaborate

PYSLANG_INSTALLED = importlib.util.find_spec("pyslang") is not None
if not PYSLANG_INSTALLED:
    pytest.skip(
        "pyslang not installed — slang unpacked-array tests are gated on it",
        allow_module_level=True,
    )

FLOP_TYPES = frozenset({"$dff", "$adff", "$dffe", "$adffe", "$sdff", "$sdffe"})


def _elaborate(tmp_path: Path, src: str, top: str = "m"):
    sv = tmp_path / f"{top}.sv"
    sv.write_text(src)
    return elaborate([sv], top, frontend=Frontend.slang)


def _flops(module) -> list:
    return [c for c in module.cells.values() if c.type in FLOP_TYPES]


# --- bit allocation -------------------------------------------------------


def test_unpacked_scalar_array_allocates_distinct_bits(tmp_path: Path) -> None:
    """``logic chain [N]`` — N single-bit elements. Each element
    select must land on its own bit, otherwise a chain of length-N
    synchronizers collapses to one flop."""
    src = """
    module m (input logic clk, d, output logic q);
        logic chain [3];
        always_ff @(posedge clk) begin
            chain[0] <= d;
            chain[1] <= chain[0];
            chain[2] <= chain[1];
        end
        assign q = chain[2];
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    flops = _flops(mod)
    assert len(flops) == 3, f"expected 3 flops, got {len(flops)}"
    # The three flops must have three *distinct* Q bits.
    q_bits = {f.connections.get("Q", ())[0] for f in flops}
    assert len(q_bits) == 3, (
        f"expected three distinct Q bits; got {q_bits} "
        "(collapse implies unpacked-array elements alias)"
    )
    # And each successive flop's D must equal the previous flop's Q —
    # this is the cascade that makes a sync chain *be* a sync chain.
    sorted_flops = sorted(flops, key=lambda c: c.name)
    for prev, curr in zip(sorted_flops, sorted_flops[1:]):
        prev_q = prev.connections["Q"][0]
        curr_d = curr.connections["D"][0]
        assert prev_q == curr_d, (
            f"cascade broken: {curr.name}.D={curr_d} ≠ {prev.name}.Q={prev_q}"
        )


def test_packed_and_unpacked_array_correct_stride(tmp_path: Path) -> None:
    """``logic [W-1:0] chain [STAGES]`` — the production ip_cdc_sync
    shape. Each unpacked element holds W packed bits; an element
    select must return W bits, and consecutive elements occupy
    non-overlapping W-bit stripes."""
    src = """
    module m #(parameter int W = 4, parameter int STAGES = 3) (
        input  logic clk,
        input  logic [W-1:0] d,
        output logic [W-1:0] q
    );
        logic [W-1:0] chain [STAGES];
        always_ff @(posedge clk) begin
            chain[0] <= d;
            chain[1] <= chain[0];
            chain[2] <= chain[1];
        end
        assign q = chain[STAGES-1];
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    flops = _flops(mod)
    # Each flop is multi-bit (WIDTH=W). Width-4 flops at 3 sites = 3
    # cells (each flop is a single ``$dff`` of width W with W-bit Q/D).
    assert len(flops) == 3, f"expected 3 flops (one per chain entry); got {len(flops)}"
    for f in flops:
        q = f.connections.get("Q", ())
        d = f.connections.get("D", ())
        assert len(q) == 4, f"flop {f.name} Q width = {len(q)}, expected 4"
        assert len(d) == 4, f"flop {f.name} D width = {len(d)}, expected 4"
    # Distinct stripes: each flop's Q bits must be a disjoint 4-bit set.
    q_sets = [set(f.connections["Q"]) for f in flops]
    all_q = set().union(*q_sets)
    assert len(all_q) == 12, (
        f"expected 12 distinct Q bits across the 3 stripes; got {len(all_q)}: {all_q}"
    )


def test_ip_cdc_sync_shape_under_for_loop(tmp_path: Path) -> None:
    """Matches the production ``ip_cdc_sync`` body verbatim: packed-
    and-unpacked array iterated by a procedural for-loop. Crosses
    issue #59 (for-loop unroll) and #62 (unpacked bit allocation) —
    one flop per stage, each W bits wide, with the cascade
    connecting consecutive stripes."""
    src = """
    module m #(parameter int W = 1, parameter int STAGES = 2) (
        input  logic clk,
        input  logic [W-1:0] d,
        output logic [W-1:0] q
    );
        logic [W-1:0] chain [STAGES];
        always_ff @(posedge clk) begin
            chain[0] <= d;
            for (int i = 1; i < STAGES; i++)
                chain[i] <= chain[i-1];
        end
        assign q = chain[STAGES-1];
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    flops = _flops(mod)
    assert len(flops) == 2, (
        f"expected STAGES=2 flops; got {len(flops)} "
        "(if 1 → unpacked-array collapse; if 0 → for-loop unroll missing)"
    )
    q_bits = [f.connections["Q"][0] for f in flops]
    assert len(set(q_bits)) == 2, (
        f"sync stages must have distinct Q bits; got Qs={q_bits}"
    )
    # The two D bits must include one external (the d port bit) and
    # one internal (the first stage's Q). Validates the cascade.
    sorted_flops = sorted(flops, key=lambda c: c.name)
    stage0_q = sorted_flops[0].connections["Q"][0]
    stage1_d = sorted_flops[1].connections["D"][0]
    assert stage0_q == stage1_d, (
        f"cascade broken: stage1.D={stage1_d} should equal stage0.Q={stage0_q}"
    )
