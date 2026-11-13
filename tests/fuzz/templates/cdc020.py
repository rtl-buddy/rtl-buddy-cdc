"""CDC-020 — sliced-bus reconvergence, swept by bus width and slice pattern.

``gap_g6`` pins the one shape the rule was written for (a 4-bit bus
sliced into four 2FF lanes, SDC periods swept). CDC-020 groups the
per-lane crossings of one multi-bit source flop by **destination clock**
and fires when a group holds ≥2 lanes; this template sweeps the
dimensions that grouping depends on (rtl-buddy-cdc#331):

- **bus width** — 2, 4 and 8 bits.
- **slice pattern** — which bits cross: ``all`` of them, a contiguous
  ``pair``, every other bit (``stride``), or a ``single`` bit (silent:
  one lane cannot reconverge).
- **destination split** — a 3-clock variant sends the lanes to two
  destination domains. Two lanes into each domain is two findings (one
  per ``(src, dst_clock)`` group); one lane into each is none.

Silent variants:

- ``unsliced`` — the whole bus lands on one multi-bit dst register.
  CDC-020 stays silent; CDC-004 owns that shape and fires.
- ``cdc_gray`` / ``cdc_static`` / ``cdc_handshake`` on the source
  register — the rule's documented attribute suppressions.
"""

from __future__ import annotations

from collections.abc import Iterator

from .base import ExpectedFinding, Op, RenderedCase

_SDC_TWO = """\
create_clock -name src_clk -period 10.0 [get_ports src_clk]
create_clock -name dst_clk -period 7.5 [get_ports dst_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
set_input_delay -clock src_clk 1.0 [get_ports d_in]
"""

_SDC_THREE = """\
create_clock -name src_clk -period 10.0 [get_ports src_clk]
create_clock -name dst_clk -period 7.5 [get_ports dst_clk]
create_clock -name dst2_clk -period 13.0 [get_ports dst2_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk} -group {dst2_clk}
set_input_delay -clock src_clk 1.0 [get_ports d_in]
"""


def _pattern(name: str, width: int) -> list[int]:
    if name == "all":
        return list(range(width))
    if name == "pair":
        return [0, 1]
    if name == "stride":
        return list(range(0, width, 2))
    if name == "single":
        return [width - 1]
    raise ValueError(name)  # pragma: no cover - template bug


def _render(
    top: str,
    width: int,
    lanes: list[int],
    *,
    src_attr: str = "",
    dst2_lanes: list[int] | None = None,
) -> str:
    """``width``-bit src register; each bit in ``lanes`` crosses to
    ``dst_clk`` through its own 1-bit 2FF chain, each bit in
    ``dst2_lanes`` likewise to ``dst2_clk``."""
    dst2 = dst2_lanes or []
    clk2 = "    input  logic dst2_clk,\n" if dst2 else ""

    def chains(clk: str, bits: list[int], tag: str) -> str:
        if not bits:
            return ""
        decls = "\n".join(f"    logic {tag}{b}_m, {tag}{b}_s;" for b in bits)
        regs = "\n".join(
            f"        {tag}{b}_m <= data[{b}]; {tag}{b}_s <= {tag}{b}_m;" for b in bits
        )
        return f"""{decls}
    always_ff @(posedge {clk}) begin
{regs}
    end
"""

    outs = [f"a{b}_s" for b in lanes] + [f"b{b}_s" for b in dst2]
    return f"""\
module {top} (
    input  logic src_clk,
    input  logic dst_clk,
{clk2}    input  logic [{width - 1}:0] d_in,
    output logic [{len(outs) - 1}:0] lanes_out
);
    {src_attr}logic [{width - 1}:0] data;
    always_ff @(posedge src_clk) data <= d_in;

{chains("dst_clk", lanes, "a")}{chains("dst2_clk", dst2, "b")}
    assign lanes_out = {{{", ".join(reversed(outs))}}};
endmodule
"""


def _render_unsliced(top: str, width: int) -> str:
    return f"""\
module {top} (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic [{width - 1}:0] d_in,
    output logic [{width - 1}:0] lanes_out
);
    logic [{width - 1}:0] data;
    always_ff @(posedge src_clk) data <= d_in;

    logic [{width - 1}:0] bus_m, bus_s;
    always_ff @(posedge dst_clk) begin
        bus_m <= data;
        bus_s <= bus_m;
    end

    assign lanes_out = bus_s;
endmodule
"""


_SILENT = (ExpectedFinding("CDC-020", Op.ZERO),)
# Every lane is a 1-bit 2FF-synced crossing: nothing else may fire.
_QUIET = (
    ExpectedFinding("CDC-001", Op.ZERO),
    ExpectedFinding("CDC-004", Op.ZERO),
    ExpectedFinding("CDC-019", Op.ZERO),
)

# (width, pattern) sweep. ``pair`` / ``stride`` at width 2 would
# duplicate ``all``.
_SWEEP: list[tuple[int, str]] = [
    (4, "all"),
    (2, "all"),
    (8, "all"),
    (4, "pair"),
    (8, "pair"),
    (4, "stride"),
    (8, "stride"),
    (2, "single"),
    (8, "single"),
]


class SlicedBusLanes:
    """Multi-bit src register sliced into per-lane 2FF chains."""

    name = "cdc020_sliced_bus"

    @classmethod
    def cases(cls) -> Iterator[RenderedCase]:
        for width, pattern in _SWEEP:
            lanes = _pattern(pattern, width)
            top = f"fuzz_{cls.name}_w{width}_{pattern}"
            fires = len(lanes) >= 2
            yield RenderedCase(
                template_name=cls.name,
                case_id=top,
                sv=_render(top, width, lanes),
                sdc=_SDC_TWO,
                top=top,
                params={"width": width, "pattern": pattern, "lanes": lanes},
                expected=(ExpectedFinding("CDC-020", Op.EQ, 1),) if fires else (),
                forbidden=_QUIET + (() if fires else _SILENT),
            )

        # Two destination domains: findings are per (src, dst_clock).
        for split, (to_a, to_b), count in (
            ("split2x2", ([0, 1], [2, 3]), 2),
            ("split1x1", ([0], [1]), 0),
        ):
            top = f"fuzz_{cls.name}_w4_{split}"
            yield RenderedCase(
                template_name=cls.name,
                case_id=top,
                sv=_render(top, 4, to_a, dst2_lanes=to_b),
                sdc=_SDC_THREE,
                top=top,
                params={"width": 4, "pattern": split},
                expected=(ExpectedFinding("CDC-020", Op.EQ, count),) if count else (),
                forbidden=_QUIET + (() if count else _SILENT),
            )

        top = f"fuzz_{cls.name}_w4_unsliced"
        yield RenderedCase(
            template_name=cls.name,
            case_id=top,
            sv=_render_unsliced(top, 4),
            sdc=_SDC_TWO,
            top=top,
            params={"width": 4, "pattern": "unsliced"},
            expected=(ExpectedFinding("CDC-004", Op.GE, 1),),
            forbidden=_SILENT,
        )

        for attr in ("cdc_gray", "cdc_static", "cdc_handshake"):
            top = f"fuzz_{cls.name}_w4_all_{attr}"
            yield RenderedCase(
                template_name=cls.name,
                case_id=top,
                sv=_render(top, 4, list(range(4)), src_attr=f"(* {attr} *) "),
                sdc=_SDC_TWO,
                top=top,
                params={"width": 4, "pattern": "all", "suppression": attr},
                expected=(),
                forbidden=_SILENT,
            )
