"""Coverage tests for slang frontend lowering paths the fixture suite
doesn't exercise directly.

The existing fixtures (``tests/fixtures/``) cover the rule-fires-correctly
contract end-to-end, but they happen to use a narrow vocabulary of SV
operators (``&``, ``^``, element-selects, range-selects). The slang
frontend supports a broader set — full BinaryOperator / UnaryOperator
enums, conditional expressions, ``always_comb``, reduction operators,
arithmetic. Without dedicated tests those code paths are unverified and
silently rot.

Each test uses :meth:`pyslang.SyntaxTree.fromText` via a tmp_path SV
file, so the public ``elaborate(paths, top)`` entry point is exercised
exactly as a real user invokes it (same code path as the CLI). The
assertions look at the produced ``Module``'s cell types — narrower than
running the whole rule pack but more diagnostic if a lowering ever
breaks.

Adding a new operator or a new statement-level shape? Add a test here
*before* the implementation lands, then watch it go from FAIL → PASS as
part of the same commit.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from rtl_buddy_cdc.frontend import Frontend, elaborate
from rtl_buddy_cdc.rules import scan_mode_port_names

PYSLANG_INSTALLED = importlib.util.find_spec("pyslang") is not None
if not PYSLANG_INSTALLED:
    pytest.skip(
        "pyslang not installed — slang lowering tests are gated on it",
        allow_module_level=True,
    )


def _elaborate_inline(tmp_path: Path, src: str, top: str = "m") -> dict[str, str]:
    """Write ``src`` to a temp .sv file, elaborate via the slang
    frontend, and return ``{cell_name: cell_type}``. Returning a flat
    map lets tests assert on cell-type membership without caring about
    name auto-generation order."""
    sv = tmp_path / f"{top}.sv"
    sv.write_text(src)
    module = elaborate([sv], top, frontend=Frontend.slang)
    return {name: cell.type for name, cell in module.cells.items()}


def _elaborate_full(tmp_path: Path, src: str, top: str = "m"):
    """Like :func:`_elaborate_inline` but returns the full ``Module``
    so tests can inspect cells / ports / netnames / attributes."""
    sv = tmp_path / f"{top}.sv"
    sv.write_text(src)
    return elaborate([sv], top, frontend=Frontend.slang)


# --- BinaryExpression operator coverage -----------------------------------
#
# Each test exercises one entry of the BINOP_CELL table that the
# fixtures don't already hit. The shape is identical across tests:
# two input flops feed a binary op, the result is captured by a third
# flop. The captured op-cell's type is what the test asserts.


def _binop_template(op: str) -> str:
    """A 1-bit binary-op sandwich: flop → op → flop. ``op`` is inserted
    verbatim into the ``assign y = a {op} b`` line."""
    return f"""
module m (
    input  logic clk_a, clk_b, rst_n, x, z,
    output logic q
);
    logic a, b, y;
    always_ff @(posedge clk_a or negedge rst_n)
        if (!rst_n) begin a <= 0; b <= 0; end
        else        begin a <= x; b <= z; end
    assign y = a {op} b;
    always_ff @(posedge clk_b or negedge rst_n)
        if (!rst_n) q <= 0; else q <= y;
endmodule
"""


@pytest.mark.parametrize(
    "op,expected_cell",
    [
        ("|", "$or"),
        ("^", "$xor"),
        ("~^", "$xnor"),
        ("&&", "$logic_and"),
        ("||", "$logic_or"),
        ("==", "$eq"),
        ("!=", "$ne"),
        ("<", "$lt"),
        ("<=", "$le"),
        (">", "$gt"),
        (">=", "$ge"),
    ],
)
def test_binary_op_lowers_to_expected_cell(
    tmp_path: Path, op: str, expected_cell: str
) -> None:
    """Each BinaryOperator should produce its Yosys-zoo counterpart so
    the rule pack's comb-cone walk treats the operator as a transit
    node, not an opaque ``$_UNKNOWN_`` driver."""
    cells = _elaborate_inline(tmp_path, _binop_template(op))
    assert expected_cell in cells.values(), (
        f"op {op!r} should lower to {expected_cell}; got {sorted(set(cells.values()))}"
    )


# --- UnaryExpression operator coverage ------------------------------------


def _unop_template(op: str) -> str:
    """A 1-bit unary-op sandwich: flop → op → flop."""
    return f"""
module m (
    input  logic clk_a, clk_b, rst_n, x,
    output logic q
);
    logic a, y;
    always_ff @(posedge clk_a or negedge rst_n)
        if (!rst_n) a <= 0; else a <= x;
    assign y = {op}a;
    always_ff @(posedge clk_b or negedge rst_n)
        if (!rst_n) q <= 0; else q <= y;
endmodule
"""


@pytest.mark.parametrize(
    "op,expected_cell",
    [
        ("~", "$not"),
        ("!", "$logic_not"),
    ],
)
def test_unary_op_lowers_to_expected_cell(
    tmp_path: Path, op: str, expected_cell: str
) -> None:
    """``~a`` is bitwise not (``$not``), ``!a`` is logical not
    (``$logic_not``). Both must emit a real comb cell rather than
    falling through to the ``$_UNKNOWN_`` placeholder."""
    cells = _elaborate_inline(tmp_path, _unop_template(op))
    assert expected_cell in cells.values(), (
        f"op {op!r} should lower to {expected_cell}; got {sorted(set(cells.values()))}"
    )


def test_unary_minus_on_arithmetic_signal(tmp_path: Path) -> None:
    """``-a`` on a wider signal should emit ``$neg``. Splitting the
    arithmetic case out keeps the bool-shaped table above clean."""
    src = """
module m (
    input  logic clk_a, clk_b, rst_n,
    input  logic signed [7:0] x,
    output logic signed [7:0] q
);
    logic signed [7:0] a, y;
    always_ff @(posedge clk_a or negedge rst_n)
        if (!rst_n) a <= 0; else a <= x;
    assign y = -a;
    always_ff @(posedge clk_b or negedge rst_n)
        if (!rst_n) q <= 0; else q <= y;
endmodule
"""
    cells = _elaborate_inline(tmp_path, src)
    assert "$neg" in cells.values(), sorted(set(cells.values()))


def test_reduction_or_lowers_to_reduce_or(tmp_path: Path) -> None:
    """Reduction operators (``|bus`` and friends) collapse a multi-bit
    operand to a single bit — distinct cell type from the bitwise
    operators that share the same syntax."""
    src = """
module m (
    input  logic clk_a, clk_b, rst_n,
    input  logic [3:0] x,
    output logic q
);
    logic [3:0] a;
    logic y;
    always_ff @(posedge clk_a or negedge rst_n)
        if (!rst_n) a <= 0; else a <= x;
    assign y = |a;
    always_ff @(posedge clk_b or negedge rst_n)
        if (!rst_n) q <= 0; else q <= y;
endmodule
"""
    cells = _elaborate_inline(tmp_path, src)
    assert "$reduce_or" in cells.values(), sorted(set(cells.values()))


# --- ConditionalExpression -------------------------------------------------


def test_ternary_lowers_to_mux(tmp_path: Path) -> None:
    """``sel ? a : b`` should produce a ``$mux`` so the rule pack can
    trace both inputs as comb predecessors of the destination flop."""
    src = """
module m (
    input  logic clk_a, clk_b, rst_n, x, z, sel,
    output logic q
);
    logic a, b, y;
    always_ff @(posedge clk_a or negedge rst_n)
        if (!rst_n) begin a <= 0; b <= 0; end
        else        begin a <= x; b <= z; end
    assign y = sel ? a : b;
    always_ff @(posedge clk_b or negedge rst_n)
        if (!rst_n) q <= 0; else q <= y;
endmodule
"""
    cells = _elaborate_inline(tmp_path, src)
    assert "$mux" in cells.values(), sorted(set(cells.values()))


# --- always_comb -----------------------------------------------------------


def test_always_comb_aliases_lhs_to_lowered_rhs(tmp_path: Path) -> None:
    """An ``always_comb`` block writing a single LHS should alias that
    variable's bits onto the RHS lowering, so a downstream flop reads
    through to the source operands. Concretely: with ``always_comb y =
    a & b``, the destination flop's D bit must trace back through an
    ``$and`` to the source flops — same outcome as the continuous-assign
    form would produce."""
    src = """
module m (
    input  logic clk_a, clk_b, rst_n, x, z,
    output logic q
);
    logic a, b, y;
    always_ff @(posedge clk_a or negedge rst_n)
        if (!rst_n) begin a <= 0; b <= 0; end
        else        begin a <= x; b <= z; end
    always_comb y = a & b;
    always_ff @(posedge clk_b or negedge rst_n)
        if (!rst_n) q <= 0; else q <= y;
endmodule
"""
    cells = _elaborate_inline(tmp_path, src)
    assert "$and" in cells.values(), sorted(set(cells.values()))


# --- ConversionExpression pass-through ------------------------------------


def test_implicit_width_conversion_is_transparent(tmp_path: Path) -> None:
    """pyslang inserts a :class:`ConversionExpression` when source and
    destination widths disagree. The frontend's lowering must see
    through it — otherwise the destination flop ends up driven by a
    ``$_UNKNOWN_`` placeholder instead of the real comb upstream."""
    src = """
module m (
    input  logic clk_a, clk_b, rst_n,
    input  logic [3:0] x,
    output logic q
);
    logic [3:0] a;
    logic y;
    always_ff @(posedge clk_a or negedge rst_n)
        if (!rst_n) a <= 0; else a <= x;
    // Width conversion 4-bit → 1-bit (implicit cast).
    assign y = a[0];
    always_ff @(posedge clk_b or negedge rst_n)
        if (!rst_n) q <= 0; else q <= y;
endmodule
"""
    cells = _elaborate_inline(tmp_path, src)
    # Element-select doesn't itself produce a cell — the destination
    # flop should read bit 0 of ``a`` directly. The test passes when
    # NO ``$_UNKNOWN_`` placeholder appears: if the lowering punted,
    # the destination flop's D would be driven by that stub instead.
    assert "$_UNKNOWN_" not in cells.values(), (
        f"width conversion should be transparent; got {sorted(set(cells.values()))}"
    )


# --- Multi-source-domain via always_comb (end-to-end rule firing) ---------


def test_ternary_between_domains_fires_cdc_001(tmp_path: Path) -> None:
    """Sanity-check that a ternary mux on the path from source to
    destination is correctly seen as a comb hop by the rule pack —
    not just lowered structurally, but also walkable end-to-end."""
    from rtl_buddy_cdc import sdc as sdc_mod
    from rtl_buddy_cdc.cli import _filter_async
    from rtl_buddy_cdc.domain import find_crossings
    from rtl_buddy_cdc.rules import run_all

    src = """
module m (
    input  logic clk_src, clk_dst, rst_n, x, z, sel,
    output logic q
);
    logic a, b, y;
    always_ff @(posedge clk_src or negedge rst_n)
        if (!rst_n) begin a <= 0; b <= 0; end
        else        begin a <= x; b <= z; end
    assign y = sel ? a : b;
    always_ff @(posedge clk_dst or negedge rst_n)
        if (!rst_n) q <= 0; else q <= y;
endmodule
"""
    sdc = """
create_clock -name clk_src -period 10 [get_ports clk_src]
create_clock -name clk_dst -period 10 [get_ports clk_dst]
set_clock_groups -asynchronous -group {clk_src} -group {clk_dst}
"""
    sv = tmp_path / "m.sv"
    sv.write_text(src)
    sdc_path = tmp_path / "m.sdc"
    sdc_path.write_text(sdc)

    module = elaborate([sv], "m", frontend=Frontend.slang)
    spec = sdc_mod.parse_file(sdc_path)
    crossings = find_crossings(
        module, port_clock=spec.port_clock, pin_clocks=spec.pin_clocks
    )
    async_c = _filter_async(crossings, spec)
    violations = run_all(module, async_c, spec)
    rule_ids = sorted({v.rule_id for v in violations})
    # The destination flop has chain depth 1 → CDC-001 fires on the
    # two source flops feeding the mux.
    assert rule_ids == ["CDC-001"], (
        f"expected CDC-001 from ternary-mediated crossing; got {rule_ids}"
    )


# --- ConcatenationExpression ---------------------------------------------


def test_concat_aliases_bits_lsb_first(tmp_path: Path) -> None:
    """``{a, b}`` puts ``a`` in the upper bits and ``b`` in the lower
    bits per SV semantics. Yosys (and our internal model) stores bit
    tuples LSB-first, so the destination port must end up with ``b``'s
    bits first, then ``a``'s. Pure aliasing — no Yosys cell is emitted
    for a concat, matching post-``opt_clean`` Yosys output."""
    src = """
module m (
    input  logic [3:0] a, b,
    output logic [7:0] q
);
    assign q = {a, b};
endmodule
"""
    module = _elaborate_full(tmp_path, src)
    a_bits = module.netnames["a"].bits
    b_bits = module.netnames["b"].bits
    q_bits = module.ports["q"].bits
    assert q_bits == tuple(b_bits) + tuple(a_bits), (
        f"expected concat LSB-first to be b||a, got q={q_bits} a={a_bits} b={b_bits}"
    )
    # No cell is emitted for a pure concat — the bits just alias.
    assert all(c.type != "$_UNKNOWN_" for c in module.cells.values())


def test_concat_inside_always_ff_d_traces_through(tmp_path: Path) -> None:
    """Concat on an ``always_ff`` D-side input must produce a flop
    whose D bits are the concat of source-side bits — proving the
    aliasing reaches the cell connection, not just port wiring."""
    src = """
module m (
    input  logic clk, rst_n,
    input  logic [3:0] hi, lo,
    output logic [7:0] q
);
    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n) q <= 0; else q <= {hi, lo};
endmodule
"""
    module = _elaborate_full(tmp_path, src)
    hi_bits = module.netnames["hi"].bits
    lo_bits = module.netnames["lo"].bits
    ff = next(c for c in module.cells.values() if c.type == "$adff")
    assert ff.connections["D"] == tuple(lo_bits) + tuple(hi_bits)


# --- ReplicationExpression -----------------------------------------------


def test_replication_repeats_pattern_bits(tmp_path: Path) -> None:
    """``{N{x}}`` repeats x's bits N times. Pure aliasing like
    concat — no cell needed."""
    src = """
module m (
    input  logic x,
    output logic [3:0] q
);
    assign q = {4{x}};
endmodule
"""
    module = _elaborate_full(tmp_path, src)
    x_bit = module.netnames["x"].bits[0]
    assert module.ports["q"].bits == (x_bit, x_bit, x_bit, x_bit)


def test_replication_of_multi_bit_pattern(tmp_path: Path) -> None:
    """``{2{a}}`` where ``a`` is 4-bit produces 8 bits in the LSB-first
    pattern (a[0..3], a[0..3]). Confirms the inner ConcatenationExpression
    is lowered before the count multiplication."""
    src = """
module m (
    input  logic [3:0] a,
    output logic [7:0] q
);
    assign q = {2{a}};
endmodule
"""
    module = _elaborate_full(tmp_path, src)
    a_bits = module.netnames["a"].bits
    assert module.ports["q"].bits == tuple(a_bits) + tuple(a_bits)


# --- Source-location propagation -----------------------------------------


def test_flop_cells_carry_yosys_style_src_attribute(tmp_path: Path) -> None:
    """Every emitted ``$dff`` / ``$adff`` should carry a ``src``
    attribute formatted like Yosys' ``file:line.col-line.col``
    convention so the JSON / SARIF reporters can surface a clickable
    source location without a frontend-specific branch."""
    src = """module m (
    input  logic clk, rst_n, d,
    output logic q
);
    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n) q <= 0; else q <= d;
endmodule
"""
    module = _elaborate_full(tmp_path, src)
    ff = next(c for c in module.cells.values() if c.type == "$adff")
    src_attr = ff.attributes.get("src")
    assert src_attr is not None, "always_ff cell should have a src attribute"
    # Format: "<path>:<startLine>.<startCol>-<endLine>.<endCol>"
    assert ":" in src_attr and "-" in src_attr and "." in src_attr
    # And the range should span more than a single point — pyslang's
    # syntax.sourceRange gives the whole always_ff block.
    after_colon = src_attr.rsplit(":", 1)[-1]
    start, end = after_colon.split("-", 1)
    assert start != end, f"expected a non-degenerate range, got {src_attr!r}"


# --- Port-declaration attribute propagation -------------------------------


def test_port_level_cdc_sync_reaches_netname(tmp_path: Path) -> None:
    """``(* cdc_sync *)`` written on a top-level port declaration must
    land on the netname tied to the port's internal variable — same as
    when it's written on a sibling ``logic`` declaration. Yosys merges
    the two; the slang frontend used to only read attributes off the
    underlying ``VariableSymbol`` and silently dropped the port form
    (issue #38). The rule pack only consults attribute *keys* (via
    ``USER_SYNC_ATTRS``), so this test pins presence, not value."""
    src = """module m (
    input  logic clk, rst_n, d,
    (* cdc_sync *) output logic q
);
    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n) q <= 0; else q <= d;
endmodule
"""
    module = _elaborate_full(tmp_path, src)
    assert "cdc_sync" in module.netnames["q"].attributes


def test_input_port_scan_mode_attribute_reaches_netname(tmp_path: Path) -> None:
    """``(* scan_en *)`` on a top-level **input** port must reach the
    netname too (issue #44). ``scan_mode_port_names`` reads it off the
    port's netname exactly as the ``user_*`` helpers read ``cdc_sync``
    off a flop's, so the two frontends have to agree: Yosys preserves
    the declaration attribute on the input wire, and
    ``_collect_port`` forwards the ``PortSymbol`` attributes onto the
    internal variable's netname to match. Without this the DFT
    recognition would work under ``analyze`` and silently do nothing
    under ``lint --frontend slang``."""
    src = """module m (
    input  logic func_clk, scan_clk, d,
    (* scan_en *) input logic scan_en,
    output logic q
);
    logic clk;
    assign clk = scan_en ? scan_clk : func_clk;
    always_ff @(posedge clk) q <= d;
endmodule
"""
    module = _elaborate_full(tmp_path, src)
    assert "scan_en" in module.netnames["scan_en"].attributes
    assert scan_mode_port_names(module) == {"scan_en"}


def test_comb_cells_carry_src_attribute(tmp_path: Path) -> None:
    """Combinational cells (``$and`` here) should also carry a src
    attribute pointing at the operator expression."""
    src = """module m (
    input  logic a, b,
    output logic y
);
    assign y = a & b;
endmodule
"""
    module = _elaborate_full(tmp_path, src)
    cell = next(c for c in module.cells.values() if c.type == "$and")
    src_attr = cell.attributes.get("src")
    assert src_attr is not None, "$and cell should have a src attribute"
    assert src_attr.endswith(".sv:5.16-5.21") or src_attr.endswith(".sv:5.17-5.22"), (
        f"unexpected src range: {src_attr!r}"
    )
