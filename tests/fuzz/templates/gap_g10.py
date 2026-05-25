"""Regression sentinel: CDC-021 flop CLK on undeclared port (rtl-buddy-cdc#206).

A flop whose ``CLK`` pin traces back to a top-level input port that
has no ``create_clock`` declaration. The analyzer silently accepts
the port name as the flop's clock domain, but ``are_async`` returns
False against every declared clock (no async-group entry), so
``_filter_async`` drops every crossing involving the undeclared
domain and the rule pack stays completely silent.

Asserts CDC-021 fires once on the undeclared ``clk_aux``. Before the
rule existed, ``run_all`` produced zero findings on this shape — and
worse, the legitimate ``d_main → q_aux`` crossing was silently
dropped.
"""

from __future__ import annotations

from collections.abc import Iterator

from .base import ExpectedFinding, Op, RenderedCase

_TEMPLATE = """\
module {top} (
    input  logic clk_aux,
    output logic q_out
);
    // CLK is the undeclared port clk_aux. Self-loop on Q keeps the
    // template minimal — no data port (which would also fire CDC-011)
    // and no inter-domain crossing (which would also fire CDC-001).
    // The single failure mode the sentinel pins is the undeclared
    // port driving a flop CLK.
    logic q_aux;
    always_ff @(posedge clk_aux) q_aux <= ~q_aux;

    assign q_out = q_aux;
endmodule
"""

# SDC is intentionally empty — clk_aux has no create_clock declaration.
_SDC = ""


class GapG10FlopClkUndeclaredPort:
    """Regression sentinel: undeclared port driving a flop CLK fires CDC-021."""

    name = "gap_g10_flop_clk_undeclared_port"

    @classmethod
    def cases(cls) -> Iterator[RenderedCase]:
        top = f"fuzz_{cls.name}"
        yield RenderedCase(
            template_name=cls.name,
            case_id=top,
            sv=_TEMPLATE.format(top=top),
            sdc=_SDC,
            top=top,
            params={},
            expected=(ExpectedFinding("CDC-021", Op.EQ, 1),),
            # Today the d_main → q_aux crossing is silently dropped
            # by _filter_async (clk_aux has no async-group entry,
            # so are_async returns False). Pin that CDC-001 stays
            # silent so a future ``treat unknown-domain as async``
            # change doesn't silently double-report.
            forbidden=(
                ExpectedFinding("CDC-001", Op.ZERO),
                ExpectedFinding("CDC-011", Op.ZERO),
            ),
        )
