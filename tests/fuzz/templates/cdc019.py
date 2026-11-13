"""CDC-019 — independently-synced decoder lanes, swept by lane count and decoder shape.

``gap_g4`` pins the one shape the rule was written for (a 2-to-4
``one_hot[sel] = 1`` decoder, SDC periods swept). This template sweeps
what the detector keys on (rtl-buddy-cdc#331): CDC-019 groups
single-bit source flops by the **comb cell driving their D**, and only
counts a driver whose ``Y`` is at least two bits wide.

- **lane count** — 2, 3, 4 and 8 lanes (3 exercises a non-power-of-two
  decode with a default arm).
- **decoder shape** — how the shared decode lowers:

  - ``case``: ``case (sel)`` one-hot table (a ``$pmux``-style
    multi-bit driver);
  - ``shift``: ``1 << sel`` (a ``$shl``);
  - ``priority``: an ``if / else if`` priority arbiter over a request
    vector (a chain of multi-bit ``$mux`` cells).

  All three must fire exactly once per case — one decoder, one dst
  domain.

Silent variants:

- ``onelane`` — only one decoded lane crosses (the rule needs ≥2).
- ``dstdecode`` — the textbook fix: carry the *encoded* select across
  (``(* cdc_gray *)``, 2FF per bit) and decode in the destination.
- ``cdc_gray`` / ``cdc_static`` / ``cdc_sync`` on the source lane
  flops — the rule's three documented attribute suppressions.

**Pinned gap** (``gap_note`` on ``case_n8``): Yosys ``proc`` lowers a
full 8-entry ``case`` table to a ROM (``$memrd_v2``), whose ``DATA``
port CDC-019 does not accept as a decoder driver — a miss on the Yosys
path that the slang frontend (which keeps the mux) does not have.

**Pinned gap** (``gap_note`` on the ``perbit`` cases): the same decode
written bit by bit (``assign oh[i] = (sel == i)``) lowers to one 1-bit
``$eq`` per lane, so there is no shared ≥2-bit driver and CDC-019 stays
silent although the hazard is identical. The rule's docstring scopes
1-bit drivers out deliberately (plain fan-out is not a decode), so this
is a documented detector limit, not a regression — the sentinel makes
it visible.
"""

from __future__ import annotations

from collections.abc import Iterator

from .base import ExpectedFinding, Op, RenderedCase

_SDC = """\
create_clock -name src_clk -period 10.0 [get_ports src_clk]
create_clock -name dst_clk -period 13.0 [get_ports dst_clk]
set_clock_groups -asynchronous -group {{src_clk}} -group {{dst_clk}}
set_input_delay -clock src_clk 1.0 [get_ports {sel_port}]
"""

_LANES = [2, 3, 4, 8]
_SHAPES = ["case", "shift", "priority"]


def _sel_width(lanes: int) -> int:
    return max(1, (lanes - 1).bit_length())


def _decoder(shape: str, lanes: int) -> tuple[str, str, str]:
    """Return ``(input port decl, decode body, sel port name)``."""
    sw = _sel_width(lanes)
    if shape == "priority":
        arms = "\n".join(
            f"        {'if' if i == 0 else 'else if'} (req[{i}]) "
            f"oh = {lanes}'d{1 << i};"
            for i in range(lanes)
        )
        body = f"""\
    always_comb begin
        oh = '0;
{arms}
    end
"""
        return f"input  logic [{lanes - 1}:0] req", body, "req"
    port = f"input  logic [{sw - 1}:0] sel"
    if shape == "case":
        arms = "\n".join(
            f"            {sw}'d{i}: oh = {lanes}'d{1 << i};" for i in range(lanes)
        )
        body = f"""\
    always_comb begin
        case (sel)
{arms}
            default: oh = '0;
        endcase
    end
"""
    elif shape == "shift":
        body = f"    assign oh = {lanes}'d1 << sel;\n"
    elif shape == "perbit":
        body = "".join(
            f"    assign oh[{i}] = (sel == {sw}'d{i});\n" for i in range(lanes)
        )
    else:  # pragma: no cover - template bug
        raise ValueError(shape)
    return port, body, "sel"


def _render(
    top: str,
    shape: str,
    lanes: int,
    *,
    lane_attr: str = "",
    crossing_lanes: int | None = None,
) -> tuple[str, str]:
    """Decoder → ``lanes`` 1-bit src flops → per-lane 2FF in dst.

    ``crossing_lanes`` limits how many lanes cross; the rest are read
    in the source domain only (an OR into ``src_any``).
    """
    port, body, sel_port = _decoder(shape, lanes)
    crossing = lanes if crossing_lanes is None else crossing_lanes
    lane_decls = "\n".join(f"    {lane_attr}logic l{i};" for i in range(lanes))
    lane_regs = "\n".join(f"        l{i} <= oh[{i}];" for i in range(lanes))
    sync_decls = "\n".join(f"    logic l{i}_m, l{i}_s;" for i in range(crossing))
    sync_regs = "\n".join(
        f"        l{i}_m <= l{i}; l{i}_s <= l{i}_m;" for i in range(crossing)
    )
    out_bits = ", ".join(f"l{i}_s" for i in reversed(range(crossing)))
    local = [f"l{i}" for i in range(crossing, lanes)]
    src_any = " | ".join(local) if local else "1'b0"
    sv = f"""\
module {top} (
    input  logic src_clk,
    input  logic dst_clk,
    {port},
    output logic src_any,
    output logic [{crossing - 1}:0] sync_out
);
    logic [{lanes - 1}:0] oh;
{body}
{lane_decls}
    always_ff @(posedge src_clk) begin
{lane_regs}
    end

{sync_decls}
    always_ff @(posedge dst_clk) begin
{sync_regs}
    end

    assign sync_out = {{{out_bits}}};
    assign src_any = {src_any};
endmodule
"""
    return sv, _SDC.format(sel_port=sel_port)


def _render_dst_decode(top: str, lanes: int) -> tuple[str, str]:
    """Textbook fix: gray-tagged encoded select crosses, decode in dst."""
    sw = _sel_width(lanes)
    sv = f"""\
module {top} (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic [{sw - 1}:0] sel,
    output logic [{lanes - 1}:0] oh_dst
);
    (* cdc_gray *) logic [{sw - 1}:0] sel_q;
    always_ff @(posedge src_clk) sel_q <= sel;

    logic [{sw - 1}:0] sel_m, sel_s;
    always_ff @(posedge dst_clk) begin
        sel_m <= sel_q;
        sel_s <= sel_m;
    end

    assign oh_dst = {lanes}'d1 << sel_s;
endmodule
"""
    return sv, _SDC.format(sel_port="sel")


# Yosys ``proc`` (its ``proc_rom`` step) turns a full 8-entry constant
# ``case`` table into a ROM: the lanes' D pins are driven by a
# ``$memrd_v2`` ``DATA`` port instead of a ``$pmux`` ``Y``, and CDC-019
# only accepts a ``Y``-port driver. The slang frontend keeps the mux and
# fires, so this case is also on the cross-frontend allowlist.
_ROM_LOWERED = {("case", 8)}
_ROM_GAP = (
    "rtl-buddy-cdc#331: Yosys lowers the full 8-way case table to a ROM "
    "($memrd_v2 DATA drives the lanes); CDC-019 only recognises a comb "
    "cell's Y port as a shared driver, so it misses the decode on the "
    "Yosys path. Correct verdict is CDC-019 x1 (slang fires it)."
)

_FIRES = (ExpectedFinding("CDC-019", Op.EQ, 1),)
_SILENT = (ExpectedFinding("CDC-019", Op.ZERO),)
# Each lane is a structurally 1-bit, properly 2FF-synced crossing.
_QUIET = (
    ExpectedFinding("CDC-001", Op.ZERO),
    ExpectedFinding("CDC-004", Op.ZERO),
    ExpectedFinding("CDC-020", Op.ZERO),
)


class DecoderLanesIndependentSync:
    """Shared decoder → N 1-bit src flops → N independent 2FF chains."""

    name = "cdc019_decoder_lanes"

    @classmethod
    def cases(cls) -> Iterator[RenderedCase]:
        for shape in _SHAPES:
            for lanes in _LANES:
                top = f"fuzz_{cls.name}_{shape}_n{lanes}"
                sv, sdc = _render(top, shape, lanes)
                rom = (shape, lanes) in _ROM_LOWERED
                yield RenderedCase(
                    template_name=cls.name,
                    case_id=top,
                    sv=sv,
                    sdc=sdc,
                    top=top,
                    params={"shape": shape, "lanes": lanes},
                    expected=() if rom else _FIRES,
                    forbidden=_QUIET + (_SILENT if rom else ()),
                    gap_note=_ROM_GAP if rom else None,
                )

        for lanes in (2, 4):
            top = f"fuzz_{cls.name}_perbit_n{lanes}"
            sv, sdc = _render(top, "perbit", lanes)
            yield RenderedCase(
                template_name=cls.name,
                case_id=top,
                sv=sv,
                sdc=sdc,
                top=top,
                params={"shape": "perbit", "lanes": lanes},
                expected=(),
                forbidden=_SILENT + _QUIET,
                gap_note=(
                    "rtl-buddy-cdc#331: a bit-by-bit decode lowers to one "
                    "1-bit $eq per lane, so CDC-019 finds no shared >=2-bit "
                    "driver and misses the same hazard the case/shift shapes "
                    "fire on. Documented detector scope (1-bit drivers are "
                    "excluded as plain fan-out)."
                ),
            )

        top = f"fuzz_{cls.name}_case_n4_onelane"
        sv, sdc = _render(top, "case", 4, crossing_lanes=1)
        yield RenderedCase(
            template_name=cls.name,
            case_id=top,
            sv=sv,
            sdc=sdc,
            top=top,
            params={"shape": "case", "lanes": 4, "crossing_lanes": 1},
            expected=(),
            forbidden=_SILENT + _QUIET,
        )

        top = f"fuzz_{cls.name}_dstdecode_n4"
        sv, sdc = _render_dst_decode(top, 4)
        yield RenderedCase(
            template_name=cls.name,
            case_id=top,
            sv=sv,
            sdc=sdc,
            top=top,
            params={"shape": "dstdecode", "lanes": 4},
            expected=(),
            forbidden=_SILENT + _QUIET,
        )

        for attr in ("cdc_gray", "cdc_static", "cdc_sync"):
            top = f"fuzz_{cls.name}_case_n4_{attr}"
            sv, sdc = _render(top, "case", 4, lane_attr=f"(* {attr} *) ")
            yield RenderedCase(
                template_name=cls.name,
                case_id=top,
                sv=sv,
                sdc=sdc,
                top=top,
                params={"shape": "case", "lanes": 4, "suppression": attr},
                expected=(),
                forbidden=_SILENT,
            )
