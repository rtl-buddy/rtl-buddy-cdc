"""Regression sentinel: CDC-021 flop CLK on undeclared port (rtl-buddy-cdc#206).

A flop whose ``CLK`` pin traces back to a top-level input port that
has no ``create_clock`` declaration. The analyzer silently accepts
the port name as the flop's clock domain, but ``are_async`` returns
False against every declared clock (no async-group entry), so
``_filter_async`` drops every crossing involving the undeclared
domain and the rule pack stays completely silent.

Stage-3 Layer A (rtl-buddy-cdc#221): the undeclared-port name
sweeps across :data:`_UNDECLARED_PORT_NAMES` so CDC-021 crosses
the ≥10× bar without changing the structural shape. The SDC stays
empty by design — the failure mode is the *absence* of any
``create_clock`` for the flop's clock source. Port-name renaming
is the attribute-style cheapest multiplier per the issue's
Layer-A guidance.

Asserts CDC-021 fires once per case on the undeclared port driving
the self-loop flop. Before the rule existed, ``run_all`` produced
zero findings on this shape — and worse, any legitimate
crossing involving the undeclared domain was silently dropped.
"""

from __future__ import annotations

from collections.abc import Iterator

from .base import ExpectedFinding, Op, RenderedCase

_TEMPLATE = """\
module {top} (
    input  logic {clk_port},
    output logic q_out
);
    // CLK is the undeclared port {clk_port}. Self-loop on Q keeps the
    // template minimal — no data port (which would also fire CDC-011)
    // and no inter-domain crossing (which would also fire CDC-001).
    // The single failure mode the sentinel pins is the undeclared
    // port driving a flop CLK.
    logic q_aux;
    always_ff @(posedge {clk_port}) q_aux <= ~q_aux;

    assign q_out = q_aux;
endmodule
"""

# SDC is intentionally empty across the sweep — the failure mode
# *is* the absence of a ``create_clock`` for the flop's clock.
_SDC = ""

# Ten distinct names for the undeclared clock port. Each variant
# yields a structurally identical design with a different port name
# so the Yosys content-hash key (which includes the SV body and the
# ``top`` name) doesn't dedupe them. All ten fire CDC-021 once.
_UNDECLARED_PORT_NAMES: list[str] = [
    "clk_aux",
    "clk_alt",
    "clk_misc",
    "clk_ext",
    "clk_other",
    "clk_spare",
    "clk_undef",
    "clk_extra",
    "clk_xtra",
    "clk_unkn",
]


class GapG10FlopClkUndeclaredPort:
    """Regression sentinel: undeclared port driving a flop CLK fires CDC-021."""

    name = "gap_g10_flop_clk_undeclared_port"

    @classmethod
    def cases(cls) -> Iterator[RenderedCase]:
        for clk_port in _UNDECLARED_PORT_NAMES:
            top = f"fuzz_{cls.name}_{clk_port}"
            sv = _TEMPLATE.format(top=top, clk_port=clk_port)
            yield RenderedCase(
                template_name=cls.name,
                case_id=top,
                sv=sv,
                sdc=_SDC,
                top=top,
                params={"clk_port": clk_port},
                expected=(ExpectedFinding("CDC-021", Op.EQ, 1),),
                # Today the d_main → q_aux crossing is silently dropped
                # by _filter_async (the port has no async-group entry,
                # so are_async returns False). Pin that CDC-001 stays
                # silent so a future ``treat unknown-domain as async``
                # change doesn't silently double-report.
                forbidden=(
                    ExpectedFinding("CDC-001", Op.ZERO),
                    ExpectedFinding("CDC-011", Op.ZERO),
                ),
            )
