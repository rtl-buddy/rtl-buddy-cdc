"""Concatenation targets — ``{q, meta} <= {meta, a};`` and ``{c, s} = x + y;``.

The slang frontend used to drop any assignment whose target is a
concatenation (rtl-buddy-cdc#370): a packed-shift synchroniser lost all
its flops and its crossing without a word, and a carry/sum split lost
the adder (and, through dead-logic pruning, the register feeding it).
This template keeps the two frontends honest on the shape.

Parameter sweep:

- ``form``:
  - ``nba``: ``always @(posedge dst_clk) {s2, s1} <= {s1, a};``
  - ``blocking``: the same with ``=`` in a plain clocked ``always``
    (one statement, so still a shift register);
  - ``carry``: a 4-bit ``src_clk`` counter split by ``always @*
    {carry, sum} = acc + inc;`` with only ``carry`` crossing — a
    combinational source: CDC-001 into a bare capture flop, CDC-003
    ahead of a two-flop synchroniser.
- ``stages``: 1 — no synchroniser, CDC-001 — or 2, a textbook 2FF.
"""

from __future__ import annotations

from collections.abc import Iterator
from itertools import product

from .base import ExpectedFinding, Op, RenderedCase

_TEMPLATE = """\
module {top} (
    input  wire       src_clk,
    input  wire       dst_clk,
    input  wire       d_in,
    input  wire [3:0] inc,
    output wire       q_out
);
    reg a;
    reg [3:0] acc, sum;
    reg carry;
    always @(posedge src_clk) begin a <= d_in; acc <= acc + inc; end
    always @* {{carry, sum}} = acc + inc;
    reg {regs};
    always @(posedge dst_clk) {lhs} {op} {rhs};
    assign q_out = s{stages};
endmodule
"""

_SDC = """\
create_clock -name src_clk -period 10.0 [get_ports src_clk]
create_clock -name dst_clk -period 7.5 [get_ports dst_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
set_input_delay -clock src_clk 1.0 [get_ports {d_in inc}]
"""


class ConcatLvalueSync:
    """A synchroniser (or bare capture) written as one concatenation
    assignment."""

    name = "concat_lvalue_sync"

    @classmethod
    def cases(cls) -> Iterator[RenderedCase]:
        for form, stages in product(("nba", "blocking", "carry"), (1, 2)):
            top = f"fuzz_{cls.name}_{form}_s{stages}"
            regs = ", ".join(f"s{i}" for i in range(1, stages + 1))
            source = "carry" if form == "carry" else "a"
            stage_list = [f"s{i}" for i in range(stages, 0, -1)]
            lhs = "{" + ", ".join(stage_list) + "}"
            rhs = "{" + ", ".join([*stage_list[1:], source]) + "}"
            sv = _TEMPLATE.format(
                top=top,
                regs=regs,
                lhs=lhs,
                op="=" if form == "blocking" else "<=",
                rhs=rhs,
                stages=stages,
            )
            if form == "carry":
                # Into a bare capture flop the adder's carry is an
                # unsynchronised crossing; ahead of a 2FF it is
                # combinational logic before the synchroniser.
                rule = "CDC-001" if stages == 1 else "CDC-003"
                expected: tuple[ExpectedFinding, ...] = (
                    ExpectedFinding(rule, Op.GE, 1),
                )
                forbidden: tuple[ExpectedFinding, ...] = ()
            elif stages == 1:
                expected = (ExpectedFinding("CDC-001", Op.EQ, 1),)
                forbidden = (ExpectedFinding("CDC-002", Op.ZERO),)
            else:
                expected = ()
                forbidden = (
                    ExpectedFinding("CDC-001", Op.ZERO),
                    ExpectedFinding("CDC-002", Op.ZERO),
                )
            yield RenderedCase(
                template_name=cls.name,
                case_id=top,
                sv=sv,
                sdc=_SDC,
                top=top,
                params={"form": form, "stages": stages},
                expected=expected,
                forbidden=forbidden,
            )
