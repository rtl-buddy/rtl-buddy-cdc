"""CDC-022 — CDC primitive declaring fewer stages than ``--sync-depth``.

A recognised synchroniser macro (the ``xpm_cdc_*`` family, or a module
registered with ``--sync-primitive``) carries its stage count as a
*parameter*; CDC-022 compares that parameter against the required depth
(rtl-buddy-cdc#275). This template sweeps the comparison
(rtl-buddy-cdc#331):

- **``DEST_SYNC_FF`` vs ``--sync-depth``** on ``xpm_cdc_single``:
  2, 3, 4 or *left at its default* against required depths 2..5.
  Fires iff the declared depth is below the requirement; an omitted
  parameter is UG974's default of 4, so the default only fires at 5.
- **macro type** — ``xpm_cdc_array_single``, ``xpm_cdc_gray`` and
  ``xpm_cdc_pulse`` at one shallow and one adequate point each, and
  ``xpm_cdc_handshake``, whose returning ack path carries its own
  ``SRC_SYNC_FF``: one finding per shallow parameter.
- **site primitive** — a non-XPM ``my_sync`` macro with
  ``DEST_SYNC_FF=2`` is checked only when registered via
  ``--sync-primitive`` (``params["sync_primitives"]``).

Suppression variant: ``// rbcdc: disable-rule CDC-022`` on the
instantiation; CDC-022 has no attribute suppression.

Cases the Yosys path reports on are family
``cdc022_primitive_sync_depth``; silent ones are
``cdc022_primitive_sync_depth_ok``. Until rtl-buddy-cdc#345 the slang
frontend had no blackbox support, so the first family was on the
cross-frontend allowlist; both frontends now keep the macro instance and
its overridden parameters, and agree on every case.

The macros are bodyless ``(* blackbox *)`` stubs in the same source,
the way a filelist without the vendor tree presents them, so every case
runs through the CLI analysis path (``params["analysis"] = "cli"``):
only that path composes blackbox siblings into boundaries, which is
what makes the crossing *through* the macro disappear instead of
surfacing as a CDC-001 on the flops either side of it.
"""

from __future__ import annotations

from collections.abc import Iterator

from .base import ExpectedFinding, Op, RenderedCase

_SDC = """\
create_clock -name clk_a -period 10.0 [get_ports clk_a]
create_clock -name clk_b -period 7.0 [get_ports clk_b]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b}
set_input_delay -clock clk_a 1.0 [get_ports d_in]
"""

# UG974 port / parameter lists, trimmed to what the instantiations use.
_STUBS = {
    "xpm_cdc_single": """\
(* blackbox *)
module xpm_cdc_single #(
    parameter integer DEST_SYNC_FF   = 4,
    parameter integer INIT_SYNC_FF   = 0,
    parameter integer SIM_ASSERT_CHK = 0,
    parameter integer SRC_INPUT_REG  = 1
) (
    input  wire src_clk,
    input  wire src_in,
    input  wire dest_clk,
    output wire dest_out
);
endmodule
""",
    "xpm_cdc_array_single": """\
(* blackbox *)
module xpm_cdc_array_single #(
    parameter integer DEST_SYNC_FF   = 4,
    parameter integer INIT_SYNC_FF   = 0,
    parameter integer SIM_ASSERT_CHK = 0,
    parameter integer SRC_INPUT_REG  = 1,
    parameter integer WIDTH          = 2
) (
    input  wire             src_clk,
    input  wire [WIDTH-1:0] src_in,
    input  wire             dest_clk,
    output wire [WIDTH-1:0] dest_out
);
endmodule
""",
    "xpm_cdc_gray": """\
(* blackbox *)
module xpm_cdc_gray #(
    parameter integer DEST_SYNC_FF          = 4,
    parameter integer INIT_SYNC_FF          = 0,
    parameter integer REG_OUTPUT            = 0,
    parameter integer SIM_ASSERT_CHK        = 0,
    parameter integer SIM_LOSSLESS_GRAY_CHK = 0,
    parameter integer WIDTH                 = 2
) (
    input  wire             src_clk,
    input  wire [WIDTH-1:0] src_in_bin,
    input  wire             dest_clk,
    output wire [WIDTH-1:0] dest_out_bin
);
endmodule
""",
    "xpm_cdc_pulse": """\
(* blackbox *)
module xpm_cdc_pulse #(
    parameter integer DEST_SYNC_FF   = 4,
    parameter integer INIT_SYNC_FF   = 0,
    parameter integer REG_OUTPUT     = 0,
    parameter integer RST_USED       = 0,
    parameter integer SIM_ASSERT_CHK = 0
) (
    input  wire src_clk,
    input  wire src_rst,
    input  wire src_pulse,
    input  wire dest_clk,
    input  wire dest_rst,
    output wire dest_pulse
);
endmodule
""",
    "xpm_cdc_handshake": """\
(* blackbox *)
module xpm_cdc_handshake #(
    parameter integer DEST_EXT_HSK   = 0,
    parameter integer DEST_SYNC_FF   = 4,
    parameter integer INIT_SYNC_FF   = 0,
    parameter integer SIM_ASSERT_CHK = 0,
    parameter integer SRC_SYNC_FF    = 4,
    parameter integer WIDTH          = 1
) (
    input  wire             src_clk,
    input  wire [WIDTH-1:0] src_in,
    input  wire             src_send,
    output wire             src_rcv,
    input  wire             dest_clk,
    output wire [WIDTH-1:0] dest_out,
    output wire             dest_req,
    input  wire             dest_ack
);
endmodule
""",
    "my_sync": """\
(* blackbox *)
module my_sync #(
    parameter integer DEST_SYNC_FF = 3
) (
    input  wire src_clk,
    input  wire src_in,
    input  wire dest_clk,
    output wire dest_out
);
endmodule
""",
}

# Per macro: (src-side data width, instance port map body). ``{w}`` is
# the data width, ``s``/``d`` the src register and dst wire.
_INSTANCES = {
    "xpm_cdc_single": (
        1,
        ".src_clk(clk_a), .src_in(s), .dest_clk(clk_b), .dest_out(d)",
    ),
    "xpm_cdc_array_single": (
        4,
        ".src_clk(clk_a), .src_in(s), .dest_clk(clk_b), .dest_out(d)",
    ),
    "xpm_cdc_gray": (
        4,
        ".src_clk(clk_a), .src_in_bin(s), .dest_clk(clk_b), .dest_out_bin(d)",
    ),
    "xpm_cdc_pulse": (
        1,
        ".src_clk(clk_a), .src_rst(1'b0), .src_pulse(s), "
        ".dest_clk(clk_b), .dest_rst(1'b0), .dest_pulse(d)",
    ),
    "xpm_cdc_handshake": (
        4,
        ".src_clk(clk_a), .src_in(s), .src_send(send), .src_rcv(rcv), "
        ".dest_clk(clk_b), .dest_out(d), .dest_req(req), .dest_ack(1'b0)",
    ),
    "my_sync": (
        1,
        ".src_clk(clk_a), .src_in(s), .dest_clk(clk_b), .dest_out(d)",
    ),
}

_WIDTH_PARAM = {"xpm_cdc_array_single", "xpm_cdc_gray", "xpm_cdc_handshake"}


def _render(
    top: str,
    macro: str,
    overrides: dict[str, int],
    *,
    pragma: bool = False,
) -> str:
    width, ports = _INSTANCES[macro]
    params = dict(overrides)
    if macro in _WIDTH_PARAM:
        params["WIDTH"] = width
    if macro in ("xpm_cdc_single", "xpm_cdc_array_single"):
        params["SRC_INPUT_REG"] = 0
    plist = ", ".join(f".{k}({v})" for k, v in params.items())
    inst_params = f" #({plist})" if plist else ""
    rng = f"[{width - 1}:0] " if width > 1 else ""
    hsk = macro == "xpm_cdc_handshake"
    hsk_decl = (
        "    logic send, rcv, req;\n    always_ff @(posedge clk_a) send <= ~rcv;\n"
        if hsk
        else ""
    )
    lead = "    // rbcdc: disable-rule CDC-022 depth signed off for this macro\n"
    return f"""\
{_STUBS[macro]}
module {top} (
    input  logic clk_a,
    input  logic clk_b,
    input  logic {rng}d_in,
    output logic {rng}q_out
);
    logic {rng}s;
    always_ff @(posedge clk_a) s <= d_in;
{hsk_decl}
    logic {rng}d;
{lead if pragma else ""}    {macro}{inst_params} u_sync ({ports});

    logic {rng}q;
    always_ff @(posedge clk_b) q <= d;
    assign q_out = q;
endmodule
"""


# The macro absorbs the crossing: nothing but CDC-022 may speak.
_QUIET = (
    ExpectedFinding("CDC-001", Op.ZERO),
    ExpectedFinding("CDC-002", Op.ZERO),
    ExpectedFinding("CDC-004", Op.ZERO),
    ExpectedFinding("CDC-BBX", Op.ZERO),
)


def _case(
    name: str,
    tag: str,
    macro: str,
    overrides: dict[str, int],
    required: int,
    findings: int,
    *,
    pragma: bool = False,
    sync_primitives: tuple[str, ...] = (),
    quiet: tuple[ExpectedFinding, ...] = _QUIET,
) -> RenderedCase:
    top = f"fuzz_{name}_{tag}"
    # Cases where the Yosys path reports something live in ``name``;
    # silent ones in ``<name>_ok``. The split was for the cross-frontend
    # oracle while the slang frontend had no blackbox support (the
    # ``name`` family diverged, ``_ok`` agreed vacuously); since
    # rtl-buddy-cdc#345 both agree, and the split just groups by verdict.
    family = name if (findings or not quiet) else f"{name}_ok"
    params: dict = {
        "analysis": "cli",
        "macro": macro,
        "required_depth": required,
        **{k.lower(): v for k, v in overrides.items()},
    }
    if pragma:
        params["suppression"] = "pragma"
    if sync_primitives:
        params["sync_primitives"] = list(sync_primitives)
    return RenderedCase(
        template_name=family,
        case_id=top,
        sv=_render(top, macro, overrides, pragma=pragma),
        sdc=_SDC,
        top=top,
        params=params,
        expected=(ExpectedFinding("CDC-022", Op.EQ, findings),) if findings else (),
        forbidden=quiet + (() if findings else (ExpectedFinding("CDC-022", Op.ZERO),)),
    )


class XpmShallowSyncDepth:
    """Synchroniser macro whose depth parameter is swept against ``--sync-depth``."""

    name = "cdc022_primitive_sync_depth"

    @classmethod
    def cases(cls) -> Iterator[RenderedCase]:
        n = cls.name
        # Canonical (firing) case first: DEST_SYNC_FF=2 under depth 3.
        sweep = [(2, 3)] + [
            (dsf, req)
            for dsf in (2, 3, 4, None)
            for req in (2, 3, 4, 5)
            if (dsf, req) != (2, 3)
        ]
        for dsf, req in sweep:
            declared = 4 if dsf is None else dsf
            overrides = {} if dsf is None else {"DEST_SYNC_FF": dsf}
            tag = f"single_dsf{'def' if dsf is None else dsf}_req{req}"
            yield _case(n, tag, "xpm_cdc_single", overrides, req, int(declared < req))

        for macro in ("xpm_cdc_array_single", "xpm_cdc_gray", "xpm_cdc_pulse"):
            short = macro.removeprefix("xpm_cdc_")
            for dsf in (2, 3):
                yield _case(
                    n,
                    f"{short}_dsf{dsf}_req3",
                    macro,
                    {"DEST_SYNC_FF": dsf},
                    3,
                    int(dsf < 3),
                )

        # Handshake: DEST_SYNC_FF and SRC_SYNC_FF are checked separately.
        for dsf, ssf in ((2, 2), (3, 2), (3, 3)):
            yield _case(
                n,
                f"handshake_dsf{dsf}_ssf{ssf}_req3",
                "xpm_cdc_handshake",
                {"DEST_SYNC_FF": dsf, "SRC_SYNC_FF": ssf},
                3,
                int(dsf < 3) + int(ssf < 3),
            )

        # Site primitive: checked only once registered. Unregistered it is
        # an ordinary dual-clock blackbox (CDC-BBX's business, see
        # cdc_bbx.py), so only CDC-022's silence is pinned there.
        yield _case(
            n,
            "mysync_registered_dsf2_req3",
            "my_sync",
            {"DEST_SYNC_FF": 2},
            3,
            1,
            sync_primitives=("my_sync",),
        )
        yield _case(
            n,
            "mysync_unregistered_dsf2_req3",
            "my_sync",
            {"DEST_SYNC_FF": 2},
            3,
            0,
            quiet=(),
        )

        yield _case(
            n,
            "single_dsf2_req3_pragma",
            "xpm_cdc_single",
            {"DEST_SYNC_FF": 2},
            3,
            0,
            pragma=True,
        )
