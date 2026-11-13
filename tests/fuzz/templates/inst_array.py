"""Instance arrays — a synchroniser instantiated as ``sync u[1:0] (...)``.

The slang frontend used to drop an array of instances without a word
(rtl-buddy-cdc#353), so every crossing through it vanished from the
pyslang path while the Yosys path reported it. This template keeps the
two frontends honest on the shape: two independent source flops
(``{a, b}``, so no multi-bit source for CDC-020 to group) feed a
two-element array of ``STAGES``-deep synchronisers. ``dst_clk`` fans out
to both elements; ``d`` and ``q`` are sliced per element.

Parameter sweep:

- ``range``: ``[1:0]`` (descending) and ``[0:1]`` (ascending). Yosys
  and the LRM disagree on which element gets which slice of an
  ascending array, but the verdict is the same either way.
- ``stages``: 1 — no synchroniser, CDC-001 fires once per element — or
  2, a textbook 2FF per element: silent.
"""

from __future__ import annotations

from collections.abc import Iterator
from itertools import product

from .base import ExpectedFinding, Op, RenderedCase

_TEMPLATE = """\
module {top}_sync (input clk, input d, output q);
    reg {regs};
    always @(posedge clk) begin {chain} end
    assign q = s{stages};
endmodule

module {top} (
    input  wire       src_clk,
    input  wire       dst_clk,
    input  wire [1:0] d_in,
    output wire [1:0] q_out
);
    reg a, b;
    always @(posedge src_clk) begin a <= d_in[1]; b <= d_in[0]; end
    {top}_sync u_sync{range} (.clk(dst_clk), .d({{a, b}}), .q(q_out));
endmodule
"""

_SDC = """\
create_clock -name src_clk -period 10.0 [get_ports src_clk]
create_clock -name dst_clk -period 7.5 [get_ports dst_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
set_input_delay -clock src_clk 1.0 [get_ports d_in]
"""

_SILENT = ("CDC-002", "CDC-003", "CDC-004", "CDC-005", "CDC-020")


class InstanceArraySync:
    """A two-element array of 1- or 2-stage synchronisers."""

    name = "inst_array_sync"

    @classmethod
    def cases(cls) -> Iterator[RenderedCase]:
        for stages, (rng_id, rng) in product(
            (1, 2), (("desc", "[1:0]"), ("asc", "[0:1]"))
        ):
            top = f"fuzz_{cls.name}_{rng_id}_s{stages}"
            regs = ", ".join(f"s{i}" for i in range(1, stages + 1))
            chain = " ".join(
                f"s{i} <= {'d' if i == 1 else f's{i - 1}'};"
                for i in range(1, stages + 1)
            )
            sv = _TEMPLATE.format(
                top=top, regs=regs, chain=chain, stages=stages, range=rng
            )
            unsynced = ExpectedFinding("CDC-001", Op.EQ, 2)
            silent = tuple(ExpectedFinding(r, Op.ZERO) for r in _SILENT)
            yield RenderedCase(
                template_name=cls.name,
                case_id=top,
                sv=sv,
                sdc=_SDC,
                top=top,
                params={"range": rng, "stages": stages},
                expected=(unsynced,) if stages == 1 else (),
                forbidden=silent
                if stages == 1
                else (ExpectedFinding("CDC-001", Op.ZERO), *silent),
            )
