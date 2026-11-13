"""RDC-007 — reset-synchroniser chain whose head loads the asserted value.

``gap_g7`` pins the one shape the rule was written for (an active-low
2FF chain with its head tied to ``1'b0``, no consumer, SDC period
swept). RDC-007 compares the chain head's ``D`` constant with the
*deassertion* value of the chain's reset polarity; this template
sweeps that comparison (rtl-buddy-cdc#331):

- **polarity × head constant** — active-low (``negedge rst_n``,
  deasserts on 1) and active-high (``posedge rst``, deasserts on 0),
  each with the head tied to 0 and to 1. Fires iff the head constant is
  the *asserted* value.
- **chain depth** — 2 and 3 stages.

Every chain drives the async reset of a consumer flop, the way a reset
synchroniser is used, so the cases also pin that a structurally
recognised chain — correct or backwards — exempts its consumers from
RDC-001 / RDC-002 / RDC-006 / RDC-008.

Suppression variant: ``// rbcdc: disable-rule RDC-007`` above the
chain, run through the CLI analysis path; RDC-007 has no attribute
suppression.
"""

from __future__ import annotations

from collections.abc import Iterator

from .base import ExpectedFinding, Op, RenderedCase

_SDC = """\
create_clock -name clk -period 10.0 [get_ports clk]
set_input_delay -clock clk 1.0 [get_ports d_in]
"""


def _render(
    top: str, polarity: str, head: int, depth: int, *, pragma: bool = False
) -> str:
    low = polarity == "low"
    rst = "raw_rst_n" if low else "raw_rst"
    edge = f"negedge {rst}" if low else f"posedge {rst}"
    cond = f"!{rst}" if low else rst
    asserted = 0 if low else 1
    stages = []
    for i in range(depth):
        d = f"1'b{head}" if i == 0 else f"s{i - 1}"
        stages.append(
            f"""\
    always_ff @(posedge clk or {edge})
        if ({cond}) s{i} <= 1'b{asserted};
        else{" " * len(cond)} s{i} <= {d};"""
        )
    out = f"s{depth - 1}"
    c_edge = f"negedge {out}" if low else f"posedge {out}"
    c_cond = f"!{out}" if low else out
    lead = "    // rbcdc: disable-rule RDC-007 one-shot by design\n" if pragma else ""
    decls = ", ".join(f"s{i}" for i in range(depth))
    return f"""\
module {top} (
    input  logic clk,
    input  logic {rst},
    input  logic d_in,
    output logic q_out
);
    logic {decls};
{lead}{chr(10).join(stages)}

    always_ff @(posedge clk or {c_edge})
        if ({c_cond}) q_out <= 1'b0;
        else{" " * len(c_cond)} q_out <= d_in;
endmodule
"""


_QUIET = (
    ExpectedFinding("RDC-001", Op.ZERO),
    ExpectedFinding("RDC-002", Op.ZERO),
    ExpectedFinding("RDC-006", Op.ZERO),
    ExpectedFinding("RDC-008", Op.ZERO),
)


class ResetSyncHeadPolarity:
    """Reset-sync chain swept over polarity × head constant × depth."""

    name = "rdc007_reset_sync_head_polarity"

    @classmethod
    def cases(cls) -> Iterator[RenderedCase]:
        n = cls.name
        for polarity in ("low", "high"):
            asserted = 0 if polarity == "low" else 1
            # Backwards (firing) head first so the canonical parent fires.
            for head in (asserted, 1 - asserted):
                for depth in (2, 3):
                    fires = head == asserted
                    top = f"fuzz_{n}_{polarity}_head{head}_d{depth}"
                    yield RenderedCase(
                        template_name=n,
                        case_id=top,
                        sv=_render(top, polarity, head, depth),
                        sdc=_SDC,
                        top=top,
                        params={"polarity": polarity, "head": head, "depth": depth},
                        expected=(
                            (ExpectedFinding("RDC-007", Op.EQ, 1),) if fires else ()
                        ),
                        forbidden=_QUIET
                        + (() if fires else (ExpectedFinding("RDC-007", Op.ZERO),)),
                    )

        top = f"fuzz_{n}_low_head0_d2_pragma"
        yield RenderedCase(
            template_name=n,
            case_id=top,
            sv=_render(top, "low", 0, 2, pragma=True),
            sdc=_SDC,
            top=top,
            params={
                "polarity": "low",
                "head": 0,
                "depth": 2,
                "suppression": "pragma",
                "analysis": "cli",
            },
            expected=(),
            forbidden=(*_QUIET, ExpectedFinding("RDC-007", Op.ZERO)),
        )
