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
import logging
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


def test_comb_if_only_true_branch_muxes_with_prior_value(tmp_path: Path) -> None:
    """``y = b; if (sel) y = a;`` — the arm that does not write ``y``
    keeps its value before the ``if``, so the merge is ``$mux(S=sel,
    A=b, B=a)``, as Yosys ``proc`` builds it (rtl-buddy-cdc#377; this
    used to alias ``y`` to ``a`` and drop ``b`` and ``sel``)."""
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
    (mux,) = [c for c in mod.cells.values() if c.type == "$mux"]
    assert mux.connections["S"] == mod.ports["sel"].bits
    assert mux.connections["A"] == mod.ports["b"].bits
    assert mux.connections["B"] == mod.ports["a"].bits
    assert _one_dff(mod).connections["D"] == mux.connections["Y"]


def test_comb_if_only_false_branch_muxes_with_prior_value(tmp_path: Path) -> None:
    """Mirror of the above for the ``else`` arm: ``y = b; if (sel) ;
    else y = a;`` is ``$mux(S=sel, A=a, B=b)``."""
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
    (mux,) = [c for c in mod.cells.values() if c.type == "$mux"]
    assert mux.connections["A"] == mod.ports["a"].bits
    assert mux.connections["B"] == mod.ports["b"].bits
    assert _one_dff(mod).connections["D"] == mux.connections["Y"]


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


def test_comb_case_single_writer_per_lhs_infers_latches(tmp_path: Path) -> None:
    """Each variable written in exactly one arm (empty ``default``) is held
    in the others: a latch loading its arm's value when that arm is
    selected (rtl-buddy-cdc#377; this used to alias each variable
    straight to its arm's value). ``always_comb`` forbids the latch, and
    Yosys rejects it; this frontend lowers it with a warning."""
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
    latches = {c.connections["Q"]: c for c in mod.cells.values() if c.type == "$dlatch"}
    eqs = {
        c.connections["B"]: c.connections["Y"][0]
        for c in mod.cells.values()
        if c.type == "$eq"
    }
    y1 = mod.netnames["y1"].bits
    y2 = mod.netnames["y2"].bits
    assert latches[y1].connections["D"] == mod.ports["a"].bits
    assert latches[y1].connections["EN"] == (eqs[("0", "0")],)
    assert latches[y2].connections["D"] == mod.ports["b"].bits
    # Item 1 is selected when item 0 is not: ``!(sel == 0) & (sel == 1)``.
    (en2,) = latches[y2].connections["EN"]
    (and2,) = [c for c in mod.cells.values() if c.connections.get("Y") == (en2,)]
    assert and2.type == "$and"
    assert and2.connections["B"] == (eqs[("1", "0")],)
    by_q = {c.connections["Q"]: c for c in _flops(mod)}
    assert by_q[mod.ports["q1"].bits].connections["D"] == y1
    assert by_q[mod.ports["q2"].bits].connections["D"] == y2


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
    """``oh = m; oh[idx] = 1'b1;`` — :meth:`_alias_indexed_assign`
    models ``oh = oh | (1 << idx)``, emitting a ``$shl`` whose B is the
    index and (because the prior value is not the all-zero constant) an
    ``$or`` folding the shift onto the previous bits."""
    src = """
    module m (input logic [1:0] idx, input logic [3:0] m, output logic [3:0] oh);
        always_comb begin
            oh = m;
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


def test_continuous_assign_unmodelled_rhs_is_opaque(tmp_path: Path, caplog) -> None:
    """``assign y = $countones(a);`` where the RHS isn't modelled: ``y``
    is driven by a reported ``$_UNKNOWN_`` cell (rtl-buddy-cdc#377; it
    used to be left silently undriven)."""
    src = """
    module m (input logic [3:0] a, output logic [3:0] y);
        assign y = $countones(a);
    endmodule
    """
    with caplog.at_level(logging.WARNING):
        mod = _elaborate(tmp_path, src)
    (unknown,) = [c for c in mod.cells.values() if c.type == "$_UNKNOWN_"]
    assert mod.ports["y"].bits == unknown.connections["Y"]
    assert any("$countones(a)" in r.getMessage() for r in caplog.records)


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
        ("~^", "$reduce_xnor"),
    ],
)
def test_reduction_unary_ops_lower_and_collapse_to_one_bit(
    tmp_path: Path, op: str, expected_cell: str
) -> None:
    """Each reduction ``UnaryOperator`` collapses its multi-bit operand
    to a single-bit ``$reduce_*`` cell, and the Y width is exactly 1.
    ``~&`` / ``~|`` are that cell then a ``$logic_not`` and ``~^`` is a
    ``$reduce_xnor``, as Yosys lowers them (rtl-buddy-cdc#405: the
    inversion was dropped)."""
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
    nots = [c for c in mod.cells.values() if c.type == "$logic_not"]
    if op in ("~&", "~|"):
        (inv,) = nots
        assert inv.connections["A"] == reduces[0].connections["Y"]
    else:
        assert not nots


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
    """A system-call RHS isn't modelled by ``_bits_of_expression``, so
    the flop's D must be driven by a fresh ``$_UNKNOWN_`` cell — the
    driver exists (so the rule pack's lookup finds *something*) but its
    type stops the data-cone walk, and the real source ``d`` does not
    reach D."""
    src = """
    module m (input logic clk, input logic [3:0] d, output logic [3:0] q);
        always_ff @(posedge clk) q <= $countones(d);
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


def test_multibit_latch_enable_reduces_to_one_bit(tmp_path: Path) -> None:
    """A multi-bit ``always_latch`` enable is true when non-zero: the
    latch's ``EN`` is the ``$reduce_bool`` of it, as Yosys lowers the
    condition (rtl-buddy-cdc#377; the latch used to be dropped)."""
    src = """
    module m (input logic [1:0] en, input logic d, output logic q);
        always_latch begin
            if (en) q = d;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    (red,) = [c for c in mod.cells.values() if c.type == "$reduce_bool"]
    assert red.connections["A"] == mod.ports["en"].bits
    (latch,) = [c for c in mod.cells.values() if c.type == "$dlatch"]
    assert latch.connections["EN"] == red.connections["Y"]


def test_nonblocking_latch_body_is_a_latch(tmp_path: Path) -> None:
    """A non-blocking write inside ``always_latch`` is a style violation
    synthesis lowers anyway: one ``$dlatch`` (rtl-buddy-cdc#377; it was
    dropped without a word)."""
    src = """
    module m (input logic en, d, output logic q);
        always_latch begin
            if (en) q <= d;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert _cell_types(mod).count("$dlatch") == 1


def test_no_conditional_latch_body_infers_no_latch(tmp_path: Path) -> None:
    """An ``always_latch`` body with no ``if`` (a bare ``q = d;``) holds
    nothing: no ``$dlatch``, ``q`` is ``d`` (as on the Yosys path)."""
    src = """
    module m (input logic d, output logic q);
        always_latch begin
            q = d;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert "$dlatch" not in _cell_types(mod)
    assert mod.ports["q"].bits == mod.ports["d"].bits


def test_multistatement_latch_arm_emits_two_latches(tmp_path: Path) -> None:
    """``if (en) begin q = a; r = b; end`` — a multi-statement arm: one
    ``$dlatch`` per variable, both enabled by ``en`` (rtl-buddy-cdc#377;
    the block used to be skipped silently)."""
    src = """
    module m (input logic en, a, b, output logic q, r);
        always_latch begin
            if (en) begin q = a; r = b; end
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    latches = [c for c in mod.cells.values() if c.type == "$dlatch"]
    assert {c.connections["D"] for c in latches} == {
        mod.ports["a"].bits,
        mod.ports["b"].bits,
    }
    assert {c.connections["EN"] for c in latches} == {mod.ports["en"].bits}


def test_latch_with_else_is_combinational(tmp_path: Path) -> None:
    """``if (en) q = a; else q = b;`` assigns ``q`` on every path: no
    latch, a ``$mux`` (as on the Yosys path; rtl-buddy-cdc#377 — the
    ``else`` arm used to be ignored and a latch on ``a`` emitted)."""
    src = """
    module m (input logic en, a, b, output logic q);
        always_latch begin
            if (en) q = a;
            else    q = b;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert "$dlatch" not in _cell_types(mod)
    (mux,) = [c for c in mod.cells.values() if c.type == "$mux"]
    assert mux.connections["S"] == mod.ports["en"].bits
    assert mod.ports["q"].bits == mux.connections["Y"]


def test_nonblocking_assign_in_always_comb_is_lowered(tmp_path: Path) -> None:
    """A non-blocking ``y <= a`` inside ``always_comb`` is a style
    violation Yosys lowers like ``y = a``, and so does this frontend
    (rtl-buddy-cdc#377; it was skipped, leaving ``y`` undriven)."""
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
    assert _one_dff(mod).connections["D"] == mod.ports["a"].bits


def test_comb_if_with_unlowerable_selector_is_reported(tmp_path: Path, caplog) -> None:
    """When the ``if`` condition can't be lowered to bits (a system call
    here), :meth:`_walk_conditional_statement` muxes the arms on a
    reported opaque select (rtl-buddy-cdc#377; it used to walk both arms
    as if unconditional, so ``y`` silently read ``b``)."""
    src = """
    module m (input logic clk, input logic [3:0] a, b, output logic [3:0] q);
        logic [3:0] y;
        always_comb begin
            if ($countones(a)) y = a;
            else      y = b;
        end
        always_ff @(posedge clk) q <= y;
    endmodule
    """
    with caplog.at_level(logging.WARNING):
        mod = _elaborate(tmp_path, src)
    (unknown,) = [c for c in mod.cells.values() if c.type == "$_UNKNOWN_"]
    (mux,) = [c for c in mod.cells.values() if c.type == "$mux"]
    assert mux.connections["S"] == unknown.connections["Y"]
    assert _one_dff(mod).connections["D"] == mux.connections["Y"]
    assert any("`if` condition" in r.getMessage() for r in caplog.records)


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


def test_rangeselect_lhs_in_always_comb_is_aliased(tmp_path: Path) -> None:
    """An ``always_comb`` blocking assign whose LHS is a range-select
    (``y[3:0] = d``) splices ``d`` into ``y``'s bits
    (rtl-buddy-cdc#370; it used to be ignored), so the downstream flop
    reads ``d``."""
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
    ff = _flops(mod)
    assert len(ff) == 1
    assert ff[0].connections["D"] == mod.netnames["d"].bits
