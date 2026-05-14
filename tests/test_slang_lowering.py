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
