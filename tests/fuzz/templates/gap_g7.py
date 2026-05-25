"""Regression sentinel: RDC-007 deassertion-polarity check (rtl-buddy-cdc#202).

A reset-synchroniser chain whose head ``D`` is tied to the *asserted*
polarity instead of the deasserted one is a one-shot: the chain
reloads the asserted value on the deassertion edge and never
propagates "out of reset" to downstream consumers. Today the
structural recogniser
(:func:`rtl_buddy_cdc.reset_domain.find_reset_synchronizers`) accepts
the chain as a valid synchroniser — its check only requires
constant-fed head + same-domain + same-reset, not that the constant
matches the deassertion polarity.

Asserts RDC-007 fires once on the head-D mismatch shape. Before the
rule existed, ``run_all`` produced zero findings for this design (and
worse, every downstream consumer using the broken chain's output was
silently suppressed from RDC-001..-006 because the chain looked
structurally valid).
"""

from __future__ import annotations

from collections.abc import Iterator

from .base import ExpectedFinding, Op, RenderedCase

_TEMPLATE = """\
module {top} (
    input  logic dst_clk,
    input  logic raw_rst_n,
    output logic rst_n_sync
);
    // Active-low reset chain. Deassertion edge should load 1'b1
    // (so the chain's Q rises after raw_rst_n releases). This chain
    // loads 1'b0 instead — a one-shot stuck driving 0 forever.
    logic meta;
    always_ff @(posedge dst_clk or negedge raw_rst_n)
        if (!raw_rst_n) meta       <= 1'b0;
        else            meta       <= 1'b0;   // BUG: should be 1'b1
    always_ff @(posedge dst_clk or negedge raw_rst_n)
        if (!raw_rst_n) rst_n_sync <= 1'b0;
        else            rst_n_sync <= meta;
endmodule
"""

_SDC = """\
create_clock -name dst_clk -period 10.0 [get_ports dst_clk]
"""


class GapG7DeassertionPolarityBackwards:
    """Regression sentinel: head-D-on-asserted-value fires RDC-007 exactly once."""

    name = "gap_g7_deassertion_polarity_backwards"

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
            expected=(ExpectedFinding("RDC-007", Op.EQ, 1),),
            # The chain is otherwise structurally well-formed
            # (constant-fed head, same-domain, async-reset shared);
            # pin that no RDC-001/002 fires on the head's port-sourced
            # reset (the chain consumes a top-level port, no crossing).
            forbidden=(
                ExpectedFinding("RDC-001", Op.ZERO),
                ExpectedFinding("RDC-002", Op.ZERO),
            ),
        )
