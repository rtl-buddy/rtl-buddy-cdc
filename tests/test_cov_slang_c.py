"""Coverage tests for the slang-frontend expression / statement lowering
helpers in the ``1740-2860`` line band of ``frontends/slang.py``.

The existing slang test files (``test_slang_lowering`` and friends)
cover the headline shapes — binary/unary operators, ternary mux,
unpacked arrays, sync-reset, shift fold, ``always_latch``,
``always_ff`` case completeness. This file fills the remaining gaps
in the same band that those tests happen not to touch:

- ``always_comb`` ``if`` / ``else`` lowering (the procedural-mux merge
  in :meth:`_walk_conditional_statement`): both-arms,
  same-value-both-arms (no mux), only-true, only-false.
- ``always_comb`` ``case`` lowering (:meth:`_walk_case_statement`) —
  distinct from the ``always_ff`` case walker exercised elsewhere.
- The indexed-assign CDC-019 one-hot decoder shape
  (:meth:`_alias_indexed_assign`).
- RHS range-selects (``d[hi:lo]``), arithmetic / arithmetic-shift /
  division / reduction operators not in the lowering table tested
  elsewhere.
- ``_const_int`` folding over a procedural ``i*N`` index and a
  parameter-driven replication count.
- The conservative fall-throughs: an unmodelled RHS becomes a
  ``$_UNKNOWN_`` driver, a multi-bit ``always_latch`` enable / a
  non-blocking latch body / a multi-statement latch arm all drop the
  latch, and a non-blocking write in ``always_comb`` is ignored.

Each test elaborates a tiny SV snippet through the public
``elaborate`` entry point (same path the CLI uses) and asserts on the
concrete netlist shape the lowering must produce — cell types, the
bit identity wired into ``D`` / ``Q`` / mux operands — not merely that
a call returned.
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

FLOP_TYPES = frozenset({"$dff", "$adff", "$dffe", "$adffe", "$sdff", "$sdffe"})


def _elaborate(tmp_path: Path, src: str, top: str = "m"):
    """Write ``src`` to a temp .sv file and elaborate via the slang
    frontend, returning the full :class:`Module`."""
    sv = tmp_path / f"{top}.sv"
    sv.write_text(src)
    return elaborate([sv], top, frontend=Frontend.slang)


def _cell_types(module) -> list[str]:
    return sorted(c.type for c in module.cells.values())


def _flops(module) -> list:
    return [c for c in module.cells.values() if c.type in FLOP_TYPES]


def _one_dff(module):
    dffs = [c for c in module.cells.values() if c.type == "$dff"]
    assert len(dffs) == 1, (
        f"expected one $dff; got {[c.type for c in module.cells.values()]}"
    )
    return dffs[0]


def _build_drivers(module) -> dict:
    """Map each int bit-id to the (cell_name, port) that drives it via
    a ``Q`` / ``Y`` output pin."""
    drv: dict = {}
    for name, cell in module.cells.items():
        for port, bits in cell.connections.items():
            if port in ("Q", "Y"):
                for b in bits:
                    if isinstance(b, int):
                        drv[b] = (name, port)
    return drv


# --- always_comb if/else procedural-mux merge -----------------------------


def test_comb_if_else_both_arms_emit_mux(tmp_path: Path) -> None:
    """``always_comb if (sel) y = a; else y = b;`` — both arms write
    ``y`` with *different* values, so the merge in
    :meth:`_walk_conditional_statement` must emit a ``$mux`` whose A is
    the false arm (b), B is the true arm (a), and S is the selector."""
    src = """
    module m (input logic sel, a, b, output logic y);
        always_comb begin
            if (sel) y = a;
            else     y = b;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    muxes = [c for c in mod.cells.values() if c.type == "$mux"]
    assert len(muxes) == 1, (
        f"expected one $mux for the merged branches; got {_cell_types(mod)}"
    )
    mux = muxes[0]
    a_bit = mod.ports["a"].bits[0]
    b_bit = mod.ports["b"].bits[0]
    sel_bit = mod.ports["sel"].bits[0]
    # Yosys $mux: A selected when S=0 (false arm), B when S=1 (true arm).
    assert mux.connections["A"] == (b_bit,), (
        f"$mux A (sel=0) should be the else-arm value b; got {mux.connections['A']}"
    )
    assert mux.connections["B"] == (a_bit,), (
        f"$mux B (sel=1) should be the if-arm value a; got {mux.connections['B']}"
    )
    assert mux.connections["S"] == (sel_bit,)


def test_comb_if_else_same_value_both_arms_no_mux(tmp_path: Path) -> None:
    """When both arms assign the *same* bits (``if (sel) y = a; else y =
    a;``) the merge takes the ``t == f`` short-circuit: no ``$mux`` is
    emitted and the destination flop reads ``a`` directly."""
    src = """
    module m (input logic clk, sel, a, output logic q);
        logic y;
        always_comb begin
            if (sel) y = a;
            else     y = a;
        end
        always_ff @(posedge clk) q <= y;
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert "$mux" not in _cell_types(mod), (
        f"identical both-arm values must not emit a $mux; got {_cell_types(mod)}"
    )
    a_bit = mod.ports["a"].bits[0]
    assert _one_dff(mod).connections["D"] == (a_bit,), (
        "destination flop should read a directly (no mux) when both arms agree"
    )


def test_comb_if_only_true_branch_keeps_single_branch_alias(tmp_path: Path) -> None:
    """``y = b; if (sel) y = a;`` — only the true branch rewrites ``y``
    relative to its prior value, so :meth:`_walk_conditional_statement`
    keeps the single-branch aliasing (issue #36): no mux, the flop reads
    the true-arm value ``a``."""
    src = """
    module m (input logic clk, sel, a, b, output logic q);
        logic y;
        always_comb begin
            y = b;
            if (sel) y = a;
        end
        always_ff @(posedge clk) q <= y;
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert "$mux" not in _cell_types(mod), (
        f"single written branch must not emit a $mux; got {_cell_types(mod)}"
    )
    a_bit = mod.ports["a"].bits[0]
    assert _one_dff(mod).connections["D"] == (a_bit,), (
        "with only the true arm rewriting y, the flop should read a"
    )


def test_comb_if_only_false_branch_keeps_single_branch_alias(tmp_path: Path) -> None:
    """Mirror of the above for the ``f_changed`` leg: ``y = b; if (sel)
    ; else y = a;`` rewrites ``y`` only on the else side, so the flop
    reads ``a`` with no mux emitted."""
    src = """
    module m (input logic clk, sel, a, b, output logic q);
        logic y;
        always_comb begin
            y = b;
            if (sel) ;
            else     y = a;
        end
        always_ff @(posedge clk) q <= y;
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert "$mux" not in _cell_types(mod), (
        f"single written (else) branch must not emit a $mux; got {_cell_types(mod)}"
    )
    a_bit = mod.ports["a"].bits[0]
    assert _one_dff(mod).connections["D"] == (a_bit,)


# --- always_comb case -> chained $mux -------------------------------------


def test_comb_case_builds_chained_mux_with_eq_selectors(tmp_path: Path) -> None:
    """``always_comb case (sel) 0: y=a; 1: y=b; default: y=c;`` —
    :meth:`_walk_case_statement` must build a chained ``$mux`` per LHS
    with ``$eq`` selectors comparing ``sel`` against each arm constant.
    Two non-default arms → two ``$mux`` cells, two ``$eq`` cells."""
    src = """
    module m (input logic [1:0] sel, input logic a, b, c, output logic y);
        always_comb begin
            case (sel)
                2'd0:    y = a;
                2'd1:    y = b;
                default: y = c;
            endcase
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    types = _cell_types(mod)
    assert types.count("$mux") == 2, (
        f"two non-default arms should yield two chained $mux cells; got {types}"
    )
    assert types.count("$eq") == 2, (
        f"each arm constant should drive an $eq against sel; got {types}"
    )
    # The selector of each $eq must read the case expression (sel).
    sel_bits = set(mod.ports["sel"].bits)
    eqs = [c for c in mod.cells.values() if c.type == "$eq"]
    for eq in eqs:
        a_in = set(b for b in eq.connections.get("A", ()) if isinstance(b, int))
        assert a_in & sel_bits, (
            f"$eq A operand should be the case expression sel; got {eq.connections['A']}"
        )
    # The outermost mux's data inputs must all reach a / b / c so no arm
    # value got dropped.
    drivers = _build_drivers(mod)
    muxes = [c for c in mod.cells.values() if c.type == "$mux"]

    def reaches(start: int) -> set:
        seen: set = set()
        frontier = [start]
        while frontier:
            x = frontier.pop()
            if x in seen or not isinstance(x, int):
                continue
            seen.add(x)
            d = drivers.get(x)
            if d is None:
                continue
            for port, bits in mod.cells[d[0]].connections.items():
                if port in ("Q", "Y"):
                    continue
                frontier.extend(bits)
        return seen

    # Find the outermost mux (the one not feeding another mux's A/B data).
    inner_mux_outputs = set()
    for mux in muxes:
        for port in ("A", "B"):
            inner_mux_outputs |= set(mux.connections[port])
    outer = next(m_ for m_ in muxes if not set(m_.connections["Y"]) & inner_mux_outputs)
    reachable = reaches(outer.connections["Y"][0])
    for label, port in (("a", "a"), ("b", "b"), ("c", "c")):
        bit = mod.ports[port].bits[0]
        assert bit in reachable, (
            f"arm value {label} should remain reachable from the mux chain; "
            f"reachable={sorted(b for b in reachable if isinstance(b, int))}"
        )


def test_comb_case_single_writer_per_lhs_keeps_single_arm_alias(tmp_path: Path) -> None:
    """When each LHS is written in exactly one arm (no pre-case default,
    empty ``default``), :meth:`_walk_case_statement` takes the
    ``total_writers == 1`` single-arm-aliasing leg: no ``$mux`` and no
    ``$eq`` selector is emitted — the var simply aliases to that arm's
    value."""
    src = """
    module m (
        input  logic       clk,
        input  logic [1:0] sel,
        input  logic       a, b,
        output logic       q1, q2
    );
        logic y1, y2;
        always_comb begin
            case (sel)
                2'd0:    y1 = a;
                2'd1:    y2 = b;
                default: ;
            endcase
        end
        always_ff @(posedge clk) begin q1 <= y1; q2 <= y2; end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    types = _cell_types(mod)
    assert "$mux" not in types and "$eq" not in types, (
        f"single-writer-per-LHS arms must not synthesise mux/eq gating; got {types}"
    )
    by_name = sorted(_flops(mod), key=lambda c: c.name)
    assert len(by_name) == 2
    assert by_name[0].connections["D"] == (mod.ports["a"].bits[0],), (
        "y1 (written only in arm 0) should alias to a"
    )
    assert by_name[1].connections["D"] == (mod.ports["b"].bits[0],), (
        "y2 (written only in arm 1) should alias to b"
    )


def test_comb_case_multilabel_item_ors_equalities(tmp_path: Path) -> None:
    """``2'd0, 2'd1: y = a;`` — a multi-label item's selector is the OR
    of per-label ``$eq`` cells (:meth:`_lower_case_arm_selector`). The
    mux's S must be driven by a ``$or`` whose two inputs are ``$eq``
    cells."""
    src = """
    module m (input logic [1:0] sel, input logic a, c, output logic y);
        always_comb begin
            case (sel)
                2'd0, 2'd1: y = a;
                default:    y = c;
            endcase
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    types = _cell_types(mod)
    assert "$or" in types, (
        f"multi-label item selector should OR the per-label equalities; got {types}"
    )
    assert types.count("$eq") == 2, f"two labels → two $eq cells; got {types}"
    drivers = _build_drivers(mod)
    or_cell = next(c for c in mod.cells.values() if c.type == "$or")
    for port in ("A", "B"):
        src_drv = drivers.get(or_cell.connections[port][0])
        assert src_drv is not None and mod.cells[src_drv[0]].type == "$eq", (
            f"$or input {port} should be driven by an $eq (per-label match)"
        )


# --- indexed-assign one-hot decoder (CDC-019 shape) -----------------------


def test_indexed_assign_onehot_emits_shift(tmp_path: Path) -> None:
    """``oh = '0; oh[idx] = 1'b1;`` — :meth:`_alias_indexed_assign`
    models ``oh = oh | (1 << idx)``, emitting a ``$shl`` whose B is the
    index and (because the prior alias is not the literal zero constant)
    an ``$or`` folding the shift onto the previous bits."""
    src = """
    module m (input logic [1:0] idx, output logic [3:0] oh);
        always_comb begin
            oh = '0;
            oh[idx] = 1'b1;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    types = _cell_types(mod)
    assert "$shl" in types, (
        f"one-hot ``oh[idx] = 1`` should emit a $shl (1 << idx); got {types}"
    )
    shl = next(c for c in mod.cells.values() if c.type == "$shl")
    idx_bits = mod.ports["idx"].bits
    assert tuple(shl.connections["B"]) == tuple(idx_bits), (
        f"$shl B should be the index bits {idx_bits}; got {shl.connections['B']}"
    )
    # The $shl Y must be 4 bits wide (matches oh width) so each of the 4
    # one-hot positions has its own output bit.
    assert len(shl.connections["Y"]) == 4, (
        f"$shl Y width must match oh width (4); got {len(shl.connections['Y'])}"
    )
    assert "$or" in types, (
        f"non-constant prior alias takes the ``base | shl`` OR path; got {types}"
    )
    or_cell = next(c for c in mod.cells.values() if c.type == "$or")
    assert set(shl.connections["Y"]).issubset(set(or_cell.connections["B"])), (
        "the $or B operand should be the $shl result"
    )


# --- RHS range-select -----------------------------------------------------


def test_range_select_rhs_picks_contiguous_lsb_first_slice(tmp_path: Path) -> None:
    """``q <= d[5:2]`` — :meth:`_range_select_bits` must return the
    contiguous LSB-first slice ``d_bits[2:6]`` of the underlying
    variable, wired straight onto the flop's D (no extra cell)."""
    src = """
    module m (input logic clk, input logic [7:0] d, output logic [3:0] q);
        always_ff @(posedge clk) q <= d[5:2];
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    ff = _flops(mod)
    assert len(ff) == 1
    d_bits = mod.netnames["d"].bits
    assert ff[0].connections["D"] == tuple(d_bits[2:6]), (
        f"d[5:2] should slice d_bits[2:6] LSB-first; got {ff[0].connections['D']} "
        f"vs expected {tuple(d_bits[2:6])}"
    )


# --- arithmetic / shift / division / reduction operator lowering ----------


@pytest.mark.parametrize(
    "op,expected_cell",
    [
        ("+", "$add"),
        ("-", "$sub"),
        ("*", "$mul"),
        ("/", "$div"),
        ("%", "$mod"),
    ],
)
def test_arithmetic_binary_ops_lower_to_expected_cell(
    tmp_path: Path, op: str, expected_cell: str
) -> None:
    """Each arithmetic ``BinaryOperator`` must lower to its Yosys cell
    so the rule pack treats the operator as a transit node rather than
    an opaque ``$_UNKNOWN_`` driver."""
    src = f"""
    module m (input logic [3:0] a, b, output logic [3:0] y);
        assign y = a {op} b;
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert expected_cell in _cell_types(mod), (
        f"op {op!r} should lower to {expected_cell}; got {_cell_types(mod)}"
    )


def test_unary_plus_is_identity_no_cell(tmp_path: Path) -> None:
    """``+a`` is the SV identity unary; :meth:`_lower_unary` peeks
    through it (no ``$pos`` cell). The destination flop must read ``a``'s
    bits directly."""
    src = """
    module m (input logic clk, input logic [3:0] a, output logic [3:0] q);
        always_ff @(posedge clk) q <= +a;
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    ff = _flops(mod)
    assert len(ff) == 1
    a_bits = mod.netnames["a"].bits
    assert ff[0].connections["D"] == tuple(a_bits), (
        f"unary plus should pass a through unchanged; got D={ff[0].connections['D']} "
        f"a={a_bits}"
    )
    assert "$pos" not in _cell_types(mod), "unary plus must not emit a cell"


def test_continuous_assign_unmodelled_rhs_emits_no_cell(tmp_path: Path) -> None:
    """``assign y = f(a);`` where the RHS isn't modelled —
    :meth:`_emit_continuous_assign` bails when ``_bits_of_expression``
    returns ``None`` (the comb-driven-port leg), so no aliasing happens
    and no cell is emitted for the assign."""
    src = """
    module m (input logic [3:0] a, output logic [3:0] y);
        function automatic logic [3:0] f(input logic [3:0] x);
            return x + 1;
        endfunction
        assign y = f(a);
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert _cell_types(mod) == [], (
        f"an unmodelled continuous-assign RHS should emit no cell; got {_cell_types(mod)}"
    )


def test_arithmetic_shift_left_emits_sshl(tmp_path: Path) -> None:
    """``a <<< 1`` on a signed operand must emit ``$sshl`` — the
    arithmetic-shift family is *not* constant-folded (only the logical
    ``$shl`` / ``$shr`` fold to wire-routing), so the cell survives."""
    src = """
    module m (input logic clk, input logic signed [3:0] a, output logic signed [3:0] q);
        always_ff @(posedge clk) q <= a <<< 1;
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert "$sshl" in _cell_types(mod), (
        f"arithmetic shift-left should emit $sshl (not folded); got {_cell_types(mod)}"
    )


@pytest.mark.parametrize(
    "op,expected_cell",
    [
        ("&", "$reduce_and"),
        ("~&", "$reduce_and"),
        ("|", "$reduce_or"),
        ("~|", "$reduce_or"),
        ("^", "$reduce_xor"),
        ("~^", "$reduce_xor"),
    ],
)
def test_reduction_unary_ops_lower_and_collapse_to_one_bit(
    tmp_path: Path, op: str, expected_cell: str
) -> None:
    """Each reduction ``UnaryOperator`` collapses its multi-bit operand
    to a single-bit ``$reduce_*`` cell. ``~&`` / ``~|`` / ``~^`` share a
    cell with their non-inverting form (the rule pack only reads
    category membership), and the Y width is exactly 1."""
    src = f"""
    module m (input logic [3:0] a, output logic y);
        assign y = {op}a;
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    reduces = [c for c in mod.cells.values() if c.type == expected_cell]
    assert reduces, (
        f"reduction {op!r} should lower to {expected_cell}; got {_cell_types(mod)}"
    )
    assert len(reduces[0].connections["Y"]) == 1, (
        f"reduction output must be a single bit; got {reduces[0].connections['Y']}"
    )
    assert len(reduces[0].connections["A"]) == 4, (
        f"reduction A should be the full 4-bit operand; got {reduces[0].connections['A']}"
    )


# --- _const_int folding ---------------------------------------------------


def test_procedural_multiply_index_folds_to_distinct_stripes(tmp_path: Path) -> None:
    """``mem[i*2]`` inside an unrolled for-loop exercises the ``Multiply``
    arm of :meth:`_const_int` (pyslang leaves ``i*2`` unfolded because
    ``i`` is a loop variable). Two iterations must land on the
    non-overlapping ``mem[0]`` and ``mem[2]`` stripes."""
    src = """
    module m #(parameter int STAGES = 2) (
        input  logic clk,
        input  logic [3:0] d0, d1,
        output logic [3:0] q
    );
        logic [3:0] mem [4];
        always_ff @(posedge clk) begin
            mem[0*2] <= d0;
            for (int i = 1; i < STAGES; i++)
                mem[i*2] <= d1;
        end
        assign q = mem[0];
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    flops = _flops(mod)
    assert len(flops) == 2, f"expected two flops (mem[0] and mem[2]); got {len(flops)}"
    q_sets = [set(f.connections["Q"]) for f in flops]
    assert q_sets[0].isdisjoint(q_sets[1]), (
        f"mem[0] and mem[i*2]=mem[2] must occupy disjoint stripes; "
        f"got {q_sets} (collapse implies the *2 stride folded wrong)"
    )


def test_parameter_replication_count_folds(tmp_path: Path) -> None:
    """``{N{x}}`` with ``N`` a parameter exercises the ``ParameterSymbol``
    arm of :meth:`_const_int` (used as the replication count). With
    ``N=3`` the result must be ``x`` repeated three times."""
    src = """
    module m #(parameter int N = 3) (input logic x, output logic [2:0] y);
        assign y = {N{x}};
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    x_bit = mod.netnames["x"].bits[0]
    assert mod.ports["y"].bits == (x_bit, x_bit, x_bit), (
        f"{{N{{x}}}} with N=3 should repeat x three times; got {mod.ports['y'].bits}"
    )


# --- conservative fall-throughs -------------------------------------------


def test_unmodelled_rhs_becomes_unknown_driver(tmp_path: Path) -> None:
    """A function-call RHS isn't modelled by ``_bits_of_expression``, so
    the flop's D must be driven by a fresh ``$_UNKNOWN_`` cell — the
    driver exists (so the rule pack's lookup finds *something*) but its
    type stops the data-cone walk, and the real source ``d`` does not
    reach D."""
    src = """
    module m (input logic clk, input logic [3:0] d, output logic [3:0] q);
        function automatic logic [3:0] f(input logic [3:0] x);
            return x + 1;
        endfunction
        always_ff @(posedge clk) q <= f(d);
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    unknowns = [c for c in mod.cells.values() if c.type == "$_UNKNOWN_"]
    assert len(unknowns) == 1, (
        f"unmodelled RHS should emit one $_UNKNOWN_ driver; got {_cell_types(mod)}"
    )
    ff = _flops(mod)
    assert len(ff) == 1
    assert tuple(ff[0].connections["D"]) == tuple(unknowns[0].connections["Y"]), (
        "the flop's D must be wired to the $_UNKNOWN_ driver's Y"
    )
    d_bits = set(mod.netnames["d"].bits)
    assert d_bits.isdisjoint(set(ff[0].connections["D"])), (
        "the real source d must NOT reach D through the opaque $_UNKNOWN_ stub"
    )


def test_multibit_latch_enable_drops_latch(tmp_path: Path) -> None:
    """A multi-bit ``always_latch`` enable can't lower to a single mux
    select (``_lower_condition_to_bit`` bails on width != 1), so the
    latch is conservatively dropped — no ``$dlatch`` emitted."""
    src = """
    module m (input logic [1:0] en, input logic d, output logic q);
        always_latch begin
            if (en) q = d;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert "$dlatch" not in _cell_types(mod), (
        f"a multi-bit latch enable can't be lowered; latch should drop. got {_cell_types(mod)}"
    )


def test_nonblocking_latch_body_drops_latch(tmp_path: Path) -> None:
    """A non-blocking write inside ``always_latch`` is a style violation;
    :meth:`_walk_latch_statement` bails rather than model it, so no
    ``$dlatch`` is emitted."""
    src = """
    module m (input logic en, d, output logic q);
        always_latch begin
            if (en) q <= d;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert "$dlatch" not in _cell_types(mod), (
        f"non-blocking latch body must drop the latch; got {_cell_types(mod)}"
    )


def test_no_conditional_latch_body_drops_latch(tmp_path: Path) -> None:
    """An ``always_latch`` body with no ``if`` (a bare ``q = d;``) isn't
    the single-arm-implicit-hold shape, so the walker returns without
    emitting a ``$dlatch``."""
    src = """
    module m (input logic d, output logic q);
        always_latch begin
            q = d;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert "$dlatch" not in _cell_types(mod), (
        f"latch body with no if-guard isn't a clean $dlatch shape; got {_cell_types(mod)}"
    )


def test_multistatement_latch_arm_drops_latch(tmp_path: Path) -> None:
    """``if (en) begin q = a; r = b; end`` — the ifTrue arm is a multi-
    statement block, not a single ``ExpressionStatement``, so
    :meth:`_walk_latch_statement` bails and emits no ``$dlatch``."""
    src = """
    module m (input logic en, a, b, output logic q, r);
        always_latch begin
            if (en) begin q = a; r = b; end
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert "$dlatch" not in _cell_types(mod), (
        f"multi-statement latch arm isn't the single-arm shape; got {_cell_types(mod)}"
    )


def test_latch_with_else_still_emits_single_dlatch(tmp_path: Path) -> None:
    """``if (en) q = a; else q = b;`` — the explicit ``else`` is ignored
    (it would break single-arm implicit-hold semantics), but the ifTrue
    arm still produces exactly one ``$dlatch`` whose D reads ``a``."""
    src = """
    module m (input logic en, a, b, output logic q);
        always_latch begin
            if (en) q = a;
            else    q = b;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    latches = [c for c in mod.cells.values() if c.type == "$dlatch"]
    assert len(latches) == 1, (
        f"latch-with-else should still emit one $dlatch from the ifTrue arm; "
        f"got {_cell_types(mod)}"
    )
    a_bit = mod.ports["a"].bits[0]
    assert latches[0].connections["D"] == (a_bit,), (
        f"the $dlatch D should read the ifTrue value a (else ignored); "
        f"got {latches[0].connections['D']}"
    )


def test_nonblocking_assign_in_always_comb_is_ignored(tmp_path: Path) -> None:
    """A non-blocking ``y <= a`` inside ``always_comb`` is a style
    violation; :meth:`_alias_assign` bails on it, so ``y`` keeps its own
    fresh bits and the downstream flop does NOT alias through to ``a``."""
    src = """
    module m (input logic clk, a, output logic q);
        logic y;
        always_comb begin
            y <= a;
        end
        always_ff @(posedge clk) q <= y;
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    a_bit = mod.ports["a"].bits[0]
    ff = _one_dff(mod)
    assert ff.connections["D"] != (a_bit,), (
        "a non-blocking write in always_comb must NOT alias y onto a; "
        f"got D={ff.connections['D']} a={a_bit}"
    )


def test_comb_if_with_unlowerable_selector_descends_without_mux(tmp_path: Path) -> None:
    """When the ``if`` condition can't be lowered to bits (a function
    call here), :meth:`_walk_conditional_statement` takes the
    conservative-descend leg: it walks both arms unconditionally and
    emits **no** ``$mux``. Contrast :func:`test_comb_if_else_both_arms_emit_mux`,
    where a lowerable selector *does* produce a mux — the only
    difference is whether the selector lowered."""
    src = """
    module m (input logic clk, input logic [3:0] a, b, output logic [3:0] q);
        logic [3:0] y;
        function automatic logic g(input logic [3:0] x); return ^x; endfunction
        always_comb begin
            if (g(a)) y = a;
            else      y = b;
        end
        always_ff @(posedge clk) q <= y;
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert "$mux" not in _cell_types(mod), (
        "an unlowerable if-selector must skip the mux merge and descend "
        f"conservatively; got {_cell_types(mod)}"
    )


def test_latch_body_with_two_if_arms_emits_two_dlatches(tmp_path: Path) -> None:
    """An ``always_latch`` body holding two sibling ``if`` statements is
    a ``StatementList``; :meth:`_walk_latch_statement` recurses over the
    list and emits one ``$dlatch`` per single-arm guarded write."""
    src = """
    module m (input logic en1, en2, a, b, output logic q, r);
        always_latch begin
            if (en1) q = a;
            if (en2) r = b;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    latches = [c for c in mod.cells.values() if c.type == "$dlatch"]
    assert len(latches) == 2, (
        f"two guarded writes in a latch body should emit two $dlatch cells; "
        f"got {_cell_types(mod)}"
    )
    # Each latch's D should read its own source (a / b respectively).
    d_sources = {latches[0].connections["D"], latches[1].connections["D"]}
    assert {(mod.ports["a"].bits[0],), (mod.ports["b"].bits[0],)} == d_sources, (
        f"the two latches should read a and b on their D pins; got {d_sources}"
    )


def test_rangeselect_lhs_in_always_comb_is_not_aliased(tmp_path: Path) -> None:
    """An ``always_comb`` blocking assign whose LHS is a range-select
    (``y[3:0] = d``) isn't a bare ``NamedValueExpression`` nor an
    element-select, so :meth:`_alias_assign` ignores it — ``y`` is not
    rewritten to ``d`` and the downstream flop reads ``y``'s own bits."""
    src = """
    module m (input logic clk, input logic [3:0] d, output logic [3:0] q);
        logic [3:0] y;
        always_comb begin
            y[3:0] = d;
        end
        always_ff @(posedge clk) q <= y;
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    d_bits = set(mod.netnames["d"].bits)
    ff = _flops(mod)
    assert len(ff) == 1
    assert d_bits.isdisjoint(set(ff[0].connections["D"])), (
        "a range-select LHS in always_comb is not modelled, so d must NOT "
        f"reach the flop D; got D={ff[0].connections['D']} d={sorted(d_bits)}"
    )
