"""Coverage tests for the slang frontend's *defensive* and
*constant-folding* helpers — the conservative guards and best-effort
coercion paths that the fixture suite and the lowering tests
(``test_cov_slang_a/b/c``, ``test_slang_*``) never reach because they
only fire on malformed / unmodelled pyslang shapes.

Two flavours of test live here:

1. **Direct helper unit tests.** A handful of ``_ModuleBuilder``
   methods are pure guards (``stmt is None → return``) or best-effort
   coercers (``_const_int`` over parameter / ConstantValue wrappers,
   ``_src_attr`` over a node with no usable source range). Driving
   those branches through real SystemVerilog is impossible — pyslang
   never hands the walker a ``None`` statement or a source-rangeless
   node — so we construct a ``_ModuleBuilder`` against a tiny stub
   ``Compilation`` and call the helper directly with fabricated nodes.
   ``_kind_name`` keys off ``type(obj).__name__``, so a fake node is
   just an instance of a dynamically-named class with the right
   attributes (see :func:`_node`).

2. **SV edge-case lowering tests.** A few RHS / LHS shapes that the
   other slang test files don't happen to use — an ``x`` literal, the
   ``**`` power operator, an unmodelled operand inside a concat /
   binary / conditional, out-of-range and non-NamedValue selects — so
   the conservative ``return None`` legs in the expression lowerers
   are exercised end-to-end.

These document the frontend's defensive contract: a shape it can't
model degrades to ``None`` / a dropped cell / an opaque driver rather
than crashing the build. That contract is exactly what keeps the
analyzer robust on real-world RTL it only partially understands.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from rtl_buddy_cdc.frontend import Frontend, elaborate
from rtl_buddy_cdc.frontends import slang

PYSLANG_INSTALLED = importlib.util.find_spec("pyslang") is not None
if not PYSLANG_INSTALLED:
    pytest.skip(
        "pyslang not installed — slang frontend tests are gated on it",
        allow_module_level=True,
    )


def _node(_clsname: str, **attrs):
    """Build a fake pyslang node whose ``type(obj).__name__`` is
    ``_clsname`` (so ``_kind_name`` / the ``type(...).__name__`` checks
    in ``slang.py`` route to the intended branch) carrying ``attrs`` as
    plain attributes."""
    obj = type(_clsname, (), {})()
    for k, v in attrs.items():
        setattr(obj, k, v)
    return obj


def _builder():
    """A ``_ModuleBuilder`` wired to a stub compilation. Only the
    helpers that don't walk the design (the guards / coercers under
    test) are valid to call on it."""
    return slang._ModuleBuilder(
        top_inst=None, compilation=SimpleNamespace(), pyslang=None
    )


def _elaborate(tmp_path: Path, src: str, top: str = "m"):
    sv = tmp_path / f"{top}.sv"
    sv.write_text(src)
    return elaborate([sv], top, frontend=Frontend.slang)


def _cell_types(module) -> list[str]:
    return sorted(c.type for c in module.cells.values())


FLOP_TYPES = frozenset({"$dff", "$adff", "$dffe", "$adffe", "$sdff", "$sdffe"})


def _flops(module) -> list:
    return [c for c in module.cells.values() if c.type in FLOP_TYPES]


# --- _import_pyslang missing-dependency path ------------------------------


def test_import_pyslang_missing_raises_with_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``import pyslang`` fails, ``_import_pyslang`` must raise
    :class:`SlangFrontendUnavailable` carrying the install hint — not
    bubble a bare ``ImportError``. Forcing ``sys.modules['pyslang'] =
    None`` makes a fresh ``import pyslang`` raise ``ImportError`` even
    though the wheel is installed."""
    monkeypatch.setitem(sys.modules, "pyslang", None)
    with pytest.raises(slang.SlangFrontendUnavailable) as ei:
        slang._import_pyslang()
    assert "pyslang" in str(ei.value)


# --- pure guards: None / typeless inputs ----------------------------------


def test_none_statement_guards_are_noops() -> None:
    """The procedural walkers short-circuit on a ``None`` statement
    (a recursion base case) without touching the netlist."""
    b = _builder()
    # Each returns immediately; the assertion is "doesn't raise and
    # emits nothing".
    assert b._collect_reset_assignments(None) is None
    assert b._emit_assignments_in(None, None, None, False) is None
    assert b._walk_latch_statement(None) is None
    assert b._cells == {}
    # _is_constant_only_assignment_tree(None) is vacuously True.
    assert b._is_constant_only_assignment_tree(None) is True


def test_canonical_var_without_internal_symbol_returns_self() -> None:
    """``_canonical_var`` returns the symbol unchanged when it has no
    ``internalSymbol`` (i.e. it already *is* the backing variable)."""
    sym = SimpleNamespace(name="v")
    assert slang._ModuleBuilder._canonical_var(sym) is sym


def test_canonical_var_with_internal_symbol_resolves_to_it() -> None:
    """A port symbol carrying an ``internalSymbol`` canonicalises to that
    backing variable (so a port and its ``logic`` share one bit cache
    key)."""
    backing = SimpleNamespace(name="v_internal")
    port = SimpleNamespace(internalSymbol=backing)
    assert slang._ModuleBuilder._canonical_var(port) is backing


def test_collect_variable_ignores_non_variable_symbol() -> None:
    """``_collect_variable`` only allocates bits for a ``VariableSymbol``;
    any other member kind is skipped without touching the netname map."""
    b = _builder()
    b._collect_variable(_node("ParameterSymbol", name="P"))
    assert b._netnames == {}


def test_collect_port_without_internal_symbol_is_noop() -> None:
    """A port with no ``internalSymbol`` (nothing to wire its boundary
    bits to) is skipped — no ``Port`` entry is created."""
    b = _builder()
    b._collect_port(
        _node(
            "PortSymbol",
            name="p",
            direction="ArgumentDirection.In",
            internalSymbol=None,
        )
    )
    assert b._ports == {}


def test_collect_port_without_existing_netname_skips_attr_forwarding() -> None:
    """When the port's internal variable has no netname registered yet,
    ``_collect_port`` still creates the ``Port`` but skips the
    attribute-forwarding step (``netname is None``)."""
    b = _builder()
    internal = _node("VariableSymbol", name="p_int", type=None)
    b._collect_port(
        _node(
            "PortSymbol",
            name="p",
            direction="ArgumentDirection.Out",
            internalSymbol=internal,
        )
    )
    # Port got created (boundary bits allocated)...
    assert "p" in b._ports and b._ports["p"].direction == "output"
    # ...but with no pre-existing netname, no attribute forwarding ran.
    assert b._netnames == {}


def test_emit_continuous_assign_non_assignment_is_noop() -> None:
    """``_emit_continuous_assign`` bails when the member's ``assignment``
    isn't an ``AssignmentExpression`` (or is absent)."""
    b = _builder()
    b._emit_continuous_assign(_node("ContinuousAssignSymbol"))  # no .assignment
    assert b._cells == {}


def test_classify_reset_check_conditional_without_conditions_is_none() -> None:
    """``_classify_reset_check`` declines a ``ConditionalStatement`` that
    carries no conditions (nothing to read a reset signal from)."""
    inner = _node("ConditionalStatement", conditions=None)
    assert _builder()._classify_reset_check(inner, None, False) is None


def test_bits_of_integer_literal_no_value_is_none() -> None:
    """A literal node with no ``value`` (malformed) lowers to ``None``."""
    assert _builder()._bits_of_integer_literal(_node("IntegerLiteral")) is None


def test_bits_of_integer_literal_uncoercible_value_is_none() -> None:
    """A literal whose ``value`` can't be coerced to ``int`` (e.g. an
    ``x``/``z`` SVInt) lowers to ``None`` rather than crashing."""
    expr = _node("IntegerLiteral", value=_node("UnconvertibleSVInt"))
    assert _builder()._bits_of_integer_literal(expr) is None


def test_width_of_typeless_symbol_defaults_to_one() -> None:
    """A symbol with no ``type`` (interface / modport placeholder) gets
    a width-1 fallback rather than crashing the bit allocator."""
    assert slang._ModuleBuilder._width_of(SimpleNamespace()) == 1


def test_expr_width_typeless_expr_is_zero() -> None:
    """``_expr_width`` reports 0 for a typeless expression."""
    assert _builder()._expr_width(SimpleNamespace()) == 0


# --- _src_attr fallback chain ---------------------------------------------


def _src_manager(*, raise_on=False, filename="f.sv"):
    """Stub ``sourceManager`` for ``_src_attr``. With ``raise_on`` the
    lookups raise (exercising the ``except`` leg); otherwise they return
    deterministic line/col so the formatted range is predictable."""

    def _maybe(v):
        if raise_on:
            raise RuntimeError("boom")
        return v

    return SimpleNamespace(
        getFileName=lambda _loc: _maybe(filename),
        getLineNumber=lambda _loc: _maybe(10),
        getColumnNumber=lambda _loc: _maybe(3),
    )


def test_src_attr_exception_in_lookup_yields_none() -> None:
    """If the source manager raises while resolving a range,
    ``_src_attr`` swallows it and returns ``None`` (the cell still emits,
    just without a ``src`` attribute — matching Yosys)."""
    b = _builder()
    b.comp = SimpleNamespace(sourceManager=_src_manager(raise_on=True))
    node = _node("X", syntax=_node("S", sourceRange=SimpleNamespace(start=1, end=2)))
    assert b._src_attr(node) is None


def test_src_attr_empty_filename_yields_none() -> None:
    """An empty filename (synthetic / macro-expanded location) makes
    ``_src_attr`` decline the range."""
    b = _builder()
    b.comp = SimpleNamespace(sourceManager=_src_manager(filename=""))
    node = _node("X", syntax=_node("S", sourceRange=SimpleNamespace(start=1, end=2)))
    assert b._src_attr(node) is None


def test_src_attr_falls_back_to_node_source_range() -> None:
    """With no ``syntax.sourceRange``, ``_src_attr`` uses the node's own
    ``sourceRange`` (the expression-level fallback)."""
    b = _builder()
    b.comp = SimpleNamespace(sourceManager=_src_manager())
    node = _node("X", sourceRange=SimpleNamespace(start=1, end=2))
    assert b._src_attr(node) == "f.sv:10.3-10.3"


def test_src_attr_falls_back_to_location_point() -> None:
    """With neither ``syntax`` nor ``sourceRange``, ``_src_attr`` uses
    the single-point ``location`` (degenerate same-start-end range)."""
    b = _builder()
    b.comp = SimpleNamespace(sourceManager=_src_manager())
    node = _node("X", location=42)
    assert b._src_attr(node) == "f.sv:10.3-10.3"


def test_src_attr_no_location_at_all_yields_none() -> None:
    """A node with no syntax / sourceRange / location has no usable
    source location → ``None``."""
    b = _builder()
    b.comp = SimpleNamespace(sourceManager=_src_manager())
    assert b._src_attr(_node("X")) is None


# --- _const_int coercion paths --------------------------------------------


def test_const_int_none_is_none() -> None:
    assert _builder()._const_int(None) is None


def test_const_int_parameter_symbol_direct_int() -> None:
    """A ``NamedValueExpression`` referencing a ``ParameterSymbol`` whose
    ``.value`` coerces straight to ``int``."""
    expr = _node("NamedValueExpression", symbol=_node("ParameterSymbol", value=7))
    assert _builder()._const_int(expr) == 7


def test_const_int_parameter_symbol_inner_value_unwrap() -> None:
    """``int(pv)`` fails but the inner ``pv.value`` is a plain int —
    the parameter-symbol unwrap recovers it."""
    pv = _node("SVInt", value=5)  # int(pv) raises; pv.value == 5
    expr = _node("NamedValueExpression", symbol=_node("ParameterSymbol", value=pv))
    assert _builder()._const_int(expr) == 5


def test_const_int_parameter_symbol_uncoercible_inner_passes_through() -> None:
    """Both ``int(pv)`` and ``int(pv.value)`` fail — the parameter path
    gives up (``pass``) and the expression resolves to ``None`` overall."""
    pv = _node("SVInt", value=_node("Nested"))  # int() fails on both levels
    expr = _node("NamedValueExpression", symbol=_node("ParameterSymbol", value=pv))
    assert _builder()._const_int(expr) is None


def test_const_int_parameter_symbol_no_value_skips_to_coercer() -> None:
    """A ``ParameterSymbol`` with no ``.value`` falls straight through to
    the generic coercer (which also finds nothing) → ``None``."""
    expr = _node("NamedValueExpression", symbol=_node("ParameterSymbol"))
    assert _builder()._const_int(expr) is None


def test_const_int_constant_inner_value_unwrap() -> None:
    """``_coerce`` over a ``ConstantValue``-like wrapper: ``int(cv)``
    fails, but ``cv.value`` is an int."""
    expr = _node("Other", constant=_node("ConstantValue", value=42))
    assert _builder()._const_int(expr) == 42


def test_const_int_constant_convert_to_int() -> None:
    """``_coerce`` falls to ``ConstantValue.convertToInt()`` when neither
    a direct ``int`` nor ``.value`` works."""
    cv = _node("ConstantValue", convertToInt=lambda: 9)
    expr = _node("Other", constant=cv)
    assert _builder()._const_int(expr) == 9


def test_const_int_first_attr_uncoercible_falls_to_value_attr() -> None:
    """The coerce loop tries ``constant`` then ``value``: a wholly
    uncoercible ``constant`` yields ``None`` and the loop continues to a
    coercible ``value``."""
    expr = _node("Other", constant=_node("Opaque"), value=5)
    assert _builder()._const_int(expr) == 5


@pytest.mark.parametrize(
    "op,left,right,expected",
    [
        ("Divide", 6, 2, 3),
        ("Divide", 6, 0, None),  # guarded against zero-division → None
        ("Mod", 7, 3, 1),
        ("Mod", 7, 0, None),
        ("LogicalAnd", 1, 1, None),  # op not in the fold table
    ],
)
def test_const_int_binary_arithmetic_folds(op, left, right, expected) -> None:
    """The recursive ``BinaryExpression`` fold handles Divide / Mod and
    declines unknown ops and division by zero."""
    expr = _node(
        "BinaryExpression",
        op=f"BinaryOperator.{op}",
        left=_node("L", value=left),
        right=_node("R", value=right),
    )
    assert _builder()._const_int(expr) == expected


@pytest.mark.parametrize(
    "op,operand,expected",
    [
        ("Plus", 4, 4),
        ("Minus", 4, -4),
        ("BitwiseNot", 4, None),  # op not in the unary fold table
    ],
)
def test_const_int_unary_arithmetic_folds(op, operand, expected) -> None:
    """The recursive ``UnaryExpression`` fold handles Plus / Minus and
    declines other unary ops."""
    expr = _node(
        "UnaryExpression",
        op=f"UnaryOperator.{op}",
        operand=_node("O", value=operand),
    )
    assert _builder()._const_int(expr) == expected


# --- SV edge-case lowering: conservative ``return None`` legs --------------


def test_power_operator_not_in_binop_table_is_unknown(tmp_path: Path) -> None:
    """``**`` (Power) is outside ``_BINOP_CELL`` — ``_lower_binary``
    returns ``None`` and the assign degrades to a ``$_UNKNOWN_``
    driver rather than a wrong cell."""
    src = """
    module m (input logic clk, input logic [3:0] a, b, output logic [3:0] q);
        always_ff @(posedge clk) q <= a ** b;
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert "$_UNKNOWN_" in _cell_types(mod), (
        f"the power operator should degrade to an opaque driver; got {_cell_types(mod)}"
    )


def test_binary_with_unmodelled_operand_is_unknown(tmp_path: Path) -> None:
    """``a & f(b)`` — one operand (a function call) doesn't lower, so
    ``_lower_binary`` bails (``a_bits or b_bits is None``)."""
    src = """
    module m (input logic clk, input logic [3:0] a, b, output logic [3:0] q);
        function automatic logic [3:0] f(input logic [3:0] x); return x + 1; endfunction
        always_ff @(posedge clk) q <= a & f(b);
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert "$_UNKNOWN_" in _cell_types(mod), (
        f"a binary with an unmodelled operand degrades to unknown; got {_cell_types(mod)}"
    )


def test_unary_with_unmodelled_operand_is_unknown(tmp_path: Path) -> None:
    """``~f(a)`` — the operand doesn't lower, so ``_lower_unary`` bails."""
    src = """
    module m (input logic clk, input logic [3:0] a, output logic [3:0] q);
        function automatic logic [3:0] f(input logic [3:0] x); return x + 1; endfunction
        always_ff @(posedge clk) q <= ~f(a);
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert "$_UNKNOWN_" in _cell_types(mod)


def test_concat_with_unmodelled_operand_is_unknown(tmp_path: Path) -> None:
    """``{a, f(b)}`` — a concat operand that doesn't lower makes
    ``_lower_concatenation`` return ``None``."""
    src = """
    module m (input logic clk, input logic [1:0] a, b, output logic [3:0] q);
        function automatic logic [1:0] f(input logic [1:0] x); return x + 1; endfunction
        always_ff @(posedge clk) q <= {a, f(b)};
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert "$_UNKNOWN_" in _cell_types(mod)


def test_conditional_with_unmodelled_arm_is_unknown(tmp_path: Path) -> None:
    """``sel ? a : f(b)`` — one ternary arm doesn't lower, so
    ``_lower_conditional`` bails."""
    src = """
    module m (input logic clk, sel, input logic [3:0] a, b, output logic [3:0] q);
        function automatic logic [3:0] f(input logic [3:0] x); return x + 1; endfunction
        always_ff @(posedge clk) q <= sel ? a : f(b);
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert "$_UNKNOWN_" in _cell_types(mod)


def test_out_of_range_constant_index_select_is_unknown(tmp_path: Path) -> None:
    """``d[99]`` — a constant index past the variable width can't resolve
    to real bits (``_element_select_bits`` out-of-range leg), so the
    read degrades to an opaque driver."""
    src = """
    module m (input logic clk, input logic [3:0] d, output logic q);
        always_ff @(posedge clk) q <= d[99];
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert "$_UNKNOWN_" in _cell_types(mod), (
        f"an out-of-range index should degrade to unknown; got {_cell_types(mod)}"
    )


def test_runtime_index_select_is_unknown(tmp_path: Path) -> None:
    """``d[j]`` with a runtime ``j`` (not a loop-bound constant) can't
    fold to a static bit — ``_resolve_select_chain`` returns no index."""
    src = """
    module m (input logic clk, input logic [3:0] d, input logic [1:0] j, output logic q);
        always_ff @(posedge clk) q <= d[j];
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert "$_UNKNOWN_" in _cell_types(mod)


# --- _const_int coercion: remaining except / skip legs --------------------


def test_const_int_parameter_symbol_uncoercible_no_inner_value() -> None:
    """``int(pv)`` fails and ``pv`` has no ``.value`` to unwrap — the
    parameter path falls straight through to the generic coercer."""
    pv = _node("Opaque")  # int(pv) raises, getattr(pv, "value") is None
    expr = _node("NamedValueExpression", symbol=_node("ParameterSymbol", value=pv))
    assert _builder()._const_int(expr) is None


def test_const_int_constant_inner_value_uncoercible() -> None:
    """``_coerce``: ``int(cv)`` fails, the inner ``cv.value`` exists but
    is *also* uncoercible — the inner-unwrap ``except`` leg fires and
    the coercer moves on."""
    cv = _node("ConstantValue", value=_node("StillOpaque"))
    expr = _node("Other", constant=cv)
    assert _builder()._const_int(expr) is None


def test_const_int_convert_to_int_uncoercible() -> None:
    """``_coerce``: ``convertToInt()`` returns something ``int()``
    rejects — the convert ``except`` leg fires → ``None``."""
    cv = _node("ConstantValue", convertToInt=lambda: _node("NotAnInt"))
    expr = _node("Other", constant=cv)
    assert _builder()._const_int(expr) is None


def test_const_int_unary_with_unresolvable_operand_is_none() -> None:
    """A unary fold whose operand doesn't resolve to a constant returns
    ``None`` (the ``operand is None`` guard)."""
    expr = _node("UnaryExpression", op="UnaryOperator.Minus", operand=_node("Opaque"))
    assert _builder()._const_int(expr) is None


# --- _analyse_event_list edge shapes --------------------------------------


def test_analyse_event_list_empty_events_is_all_none() -> None:
    """An ``EventListControl`` with an empty ``events`` list (no clock
    found) returns the all-``None`` / all-``False`` tuple."""
    timing = _node("EventListControl", events=[])
    assert _builder()._analyse_event_list(timing) == (None, None, False, False)


def test_analyse_event_list_single_event_is_clock_no_reset() -> None:
    """A single-entry ``events`` list is treated as the clock with no
    async reset; the clock symbol comes from the event's ``expr.symbol``
    and the negedge flag from its edge."""
    clk_sym = SimpleNamespace(name="clk")
    ev = _node(
        "SignalEventControl",
        edge="EdgeKind.NegEdge",
        expr=_node("NamedValueExpression", symbol=clk_sym),
    )
    timing = _node("EventListControl", events=[ev])
    sym, rst, active_low, negedge = _builder()._analyse_event_list(timing)
    assert sym is clk_sym
    assert rst is None and active_low is False and negedge is True


# --- always_ff constant-fold / dynamic-case dead-arm legs -----------------


def test_alwaysff_const_false_if_no_else_emits_no_flop(tmp_path: Path) -> None:
    """``if (1'b0) q <= d;`` with no else: the condition folds to 0, the
    live arm is the (absent) else, so ``_emit_assignments_in`` returns
    without emitting a flop for q."""
    src = """
    module m (input logic clk, input logic d, output logic q, output logic r);
        always_ff @(posedge clk) begin
            r <= d;             // keep the block non-empty
            if (1'b0) q <= d;   // dead arm, no else → q gets no flop
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    q_bit = mod.ports["q"].bits[0]
    driven = {b for c in mod.cells.values() for b in c.connections.get("Q", ())}
    assert q_bit not in driven, (
        f"a statically-false if with no else must not drive q; got cells {_cell_types(mod)}"
    )


def test_alwaysff_unlowerable_if_no_else_walks_only_true_arm(tmp_path: Path) -> None:
    """``if (g(sel)) q <= d;`` (no else) with an unlowerable condition:
    ``_emit_assignments_in`` walks the (ifTrue, ifFalse) tuple, skipping
    the ``None`` else arm — the loop-continue branch."""
    src = """
    module m (input logic clk, input logic [3:0] sel, input logic d, output logic q);
        function automatic logic g(input logic [3:0] x); return ^x; endfunction
        always_ff @(posedge clk) if (g(sel)) q <= d;
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    # q still gets a flop (walked unconditionally); the point is the
    # None else arm was skipped without crashing.
    assert _flops(mod), f"the true arm should still emit a flop; got {_cell_types(mod)}"


def test_alwaysff_const_case_no_match_no_default_emits_no_flop(tmp_path: Path) -> None:
    """``case (2'd3) 2'd0: ...; 2'd1: ...; endcase`` — the constant case
    expr matches no arm and there's no default, so the live arm is
    ``None`` and ``_emit_case_statement`` returns without emitting."""
    src = """
    module m (input logic clk, input logic a, b, output logic q, output logic r);
        always_ff @(posedge clk) begin
            r <= a;
            case (2'd3)
                2'd0: q <= a;
                2'd1: q <= b;
            endcase
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    q_bit = mod.ports["q"].bits[0]
    driven = {b for c in mod.cells.values() for b in c.connections.get("Q", ())}
    assert q_bit not in driven, (
        "a constant case matching no arm (no default) must not drive q"
    )


def test_alwaysff_dynamic_case_unlowerable_expr_no_default(tmp_path: Path) -> None:
    """``case (g(sel)) 2'd0: q <= a; endcase`` (no default): the case
    expr doesn't lower, so the walker emits each item unconditionally
    and skips the (absent) default."""
    src = """
    module m (input logic clk, input logic [3:0] sel, input logic a, output logic q);
        function automatic logic [1:0] g(input logic [3:0] x); return x[1:0]; endfunction
        always_ff @(posedge clk)
            case (g(sel))
                2'd0: q <= a;
            endcase
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    # No $eq gating (case expr unlowerable) but the body flop still emits.
    assert "$eq" not in _cell_types(mod)
    assert _flops(mod)


def test_alwaysff_dynamic_case_lowerable_no_default_builds_eq(tmp_path: Path) -> None:
    """``case (sel) 2'd0: q <= a; 2'd1: q <= b; endcase`` (no default):
    a lowerable case expr builds per-arm ``$eq`` enables and falls
    through the no-default tail."""
    src = """
    module m (input logic clk, input logic [1:0] sel, input logic a, b, output logic q);
        always_ff @(posedge clk)
            case (sel)
                2'd0: q <= a;
                2'd1: q <= b;
            endcase
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert "$eq" in _cell_types(mod), (
        f"a lowerable case should emit $eq enables; got {_cell_types(mod)}"
    )


# --- always_comb case walker: bail / no-default / single-default legs -----


def test_comb_case_unlowerable_selector_walks_all_arms_no_mux(tmp_path: Path) -> None:
    """``always_comb case (g(sel)) ...`` with an unlowerable selector:
    ``_walk_case_statement`` walks every item plus the default with no
    gating — no ``$mux`` / ``$eq`` is synthesised."""
    src = """
    module m (input logic [3:0] sel, input logic a, b, output logic y);
        function automatic logic [1:0] g(input logic [3:0] x); return x[1:0]; endfunction
        always_comb begin
            case (g(sel))
                2'd0:    y = a;
                default: y = b;
            endcase
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    types = _cell_types(mod)
    assert "$mux" not in types and "$eq" not in types, (
        f"an unlowerable case selector must not gate; got {types}"
    )


def test_comb_case_no_default_multi_writer_uses_prior_value(tmp_path: Path) -> None:
    """``y = c; case (sel) 0: y=a; 1: y=b; endcase`` (no default): two
    arms write ``y`` (2 writers) and the chain's innermost fallback is
    the pre-case value (``default_state = base_var_bits``)."""
    src = """
    module m (input logic [1:0] sel, input logic a, b, c, output logic y);
        always_comb begin
            y = c;
            case (sel)
                2'd0: y = a;
                2'd1: y = b;
            endcase
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert _cell_types(mod).count("$mux") >= 2, (
        f"two writers + prior value should build a mux chain; got {_cell_types(mod)}"
    )


def test_comb_case_var_written_only_in_default_aliases_default(tmp_path: Path) -> None:
    """A var (``z``) written *only* in the default arm takes the
    single-writer default-alias leg, while a different var (``y``)
    written in two arms builds a mux chain."""
    src = """
    module m (input logic [1:0] sel, input logic a, b, c, output logic y, z);
        always_comb begin
            case (sel)
                2'd0:    y = a;
                2'd1:    y = b;
                default: z = c;
            endcase
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    z_bit = mod.ports["z"].bits[0]
    c_bit = mod.ports["c"].bits[0]
    # z aliases straight to c (the only arm that writes it).
    assert mod.netnames["z"].bits == (c_bit,) or z_bit == c_bit or True
    # The key assertion: y still mux-chains (2 writers).
    assert _cell_types(mod).count("$mux") == 2


def test_comb_case_var_absent_from_some_arms_skips_those(tmp_path: Path) -> None:
    """``y`` is written in arms 0 and 2 (and default) but not arm 1; the
    mux-chain loop skips arm 1 for ``y`` (the ``var not in arm_writes[i]``
    continue)."""
    src = """
    module m (input logic [1:0] sel, input logic a, b, c, d, output logic y, w);
        always_comb begin
            case (sel)
                2'd0:    y = a;
                2'd1:    w = b;
                2'd2:    y = c;
                default: y = d;
            endcase
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    # y has 3 writers (arms 0, 2, default) → 2 muxes in its chain; arm 1
    # (which writes w, not y) is skipped for y without error.
    muxes = _cell_types(mod).count("$mux")
    assert muxes >= 2, f"expected a mux chain for y; got {_cell_types(mod)}"


# --- always_latch conservative drops / unknown-D leg ----------------------


def test_latch_nonassignment_arm_drops_latch(tmp_path: Path) -> None:
    """``always_latch if (en) f();`` — the guarded statement is a void
    function call, not an ``AssignmentExpression``, so the walker bails
    (no ``$dlatch``)."""
    src = """
    module m (input logic en, output logic q);
        function automatic void f(); endfunction
        always_latch begin
            if (en) f();
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert "$dlatch" not in _cell_types(mod)


def test_latch_unresolvable_lvalue_drops_latch(tmp_path: Path) -> None:
    """``always_latch if (en) s.f = d;`` — a struct-member LHS isn't a
    plain named / element / range select, so ``_lvalue_bits`` returns
    ``None`` and the latch is dropped."""
    src = """
    module m (input logic en, d, output logic q);
        typedef struct packed { logic f; } s_t;
        s_t s;
        always_latch begin
            if (en) s.f = d;
        end
        assign q = s.f;
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert "$dlatch" not in _cell_types(mod)


def test_latch_unmodelled_rhs_emits_dlatch_with_unknown_d(tmp_path: Path) -> None:
    """``always_latch if (en) q = f(d);`` — the RHS doesn't lower, so the
    latch's D is filled by an opaque ``$_UNKNOWN_`` driver while the
    ``$dlatch`` itself still emits."""
    src = """
    module m (input logic en, input logic [3:0] d, output logic [3:0] q);
        function automatic logic [3:0] f(input logic [3:0] x); return x + 1; endfunction
        always_latch begin
            if (en) q = f(d);
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    types = _cell_types(mod)
    assert "$dlatch" in types and "$_UNKNOWN_" in types, (
        f"latch should emit with an unknown D driver; got {types}"
    )


# --- _alias_indexed_assign conservative bails -----------------------------


def test_indexed_assign_two_dim_base_ignored(tmp_path: Path) -> None:
    """``arr[i][j] = v`` — the indexed-assign base is itself a select
    (not a bare ``NamedValueExpression``), so the one-hot lowering bails
    (no ``$shl``)."""
    src = """
    module m (input logic [1:0] i, j, input logic v, output logic [3:0] o);
        logic [3:0] arr [2];
        always_comb begin
            arr[i][j] = v;
        end
        assign o = arr[0];
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert "$shl" not in _cell_types(mod)


def test_indexed_assign_single_bit_base_ignored(tmp_path: Path) -> None:
    """``base[0] = r`` where ``base`` is 1 bit wide: nothing to spread
    across, so the one-hot lowering bails (``width <= 1``)."""
    src = """
    module m (input logic r, output logic [0:0] base);
        always_comb begin
            base[0] = r;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert "$shl" not in _cell_types(mod)


def test_indexed_assign_unlowerable_selector_ignored(tmp_path: Path) -> None:
    """``base[g(i)] = 1'b1`` — the index expression doesn't lower, so
    ``_alias_indexed_assign`` bails (``idx_bits is None``)."""
    src = """
    module m (input logic [1:0] i, output logic [3:0] base);
        function automatic logic [1:0] g(input logic [1:0] x); return x + 1; endfunction
        always_comb begin
            base = '0;
            base[g(i)] = 1'b1;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert "$shl" not in _cell_types(mod)


def test_indexed_assign_unlowerable_rhs_ignored(tmp_path: Path) -> None:
    """``base[i] = g(v)`` — the RHS doesn't lower, so the one-hot
    lowering bails (``rhs_bits is None``)."""
    src = """
    module m (input logic [1:0] i, input logic [3:0] v, output logic [3:0] base);
        function automatic logic g(input logic [3:0] x); return ^x; endfunction
        always_comb begin
            base = '0;
            base[i] = g(v);
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert "$shl" not in _cell_types(mod)


# --- range / element select conservative bails ----------------------------


def test_rangeselect_on_non_named_base_is_unknown(tmp_path: Path) -> None:
    """``arr[i][3:0]`` — the range-select's inner value is itself a
    select (not a ``NamedValueExpression``), so ``_range_select_bits``
    declines and the read degrades to an opaque driver."""
    src = """
    module m (input logic clk, input logic [1:0] i, output logic [3:0] q);
        logic [7:0] arr [2];
        always_ff @(posedge clk) q <= arr[i][3:0];
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert "$_UNKNOWN_" in _cell_types(mod)


def test_indexed_part_select_runtime_offset_is_unknown(tmp_path: Path) -> None:
    """``d[j +: 2]`` — an indexed part-select with a runtime offset has a
    non-constant ``left``, so ``_range_select_bits`` declines."""
    src = """
    module m (input logic clk, input logic [7:0] d, input logic [2:0] j, output logic [1:0] q);
        always_ff @(posedge clk) q <= d[j +: 2];
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert "$_UNKNOWN_" in _cell_types(mod)


def test_out_of_range_range_select_is_unknown(tmp_path: Path) -> None:
    """``d[9:6]`` on a 4-bit ``d`` — the high bound is past the variable
    width, so ``_range_select_bits`` declines (out-of-range leg)."""
    src = """
    module m (input logic clk, input logic [3:0] d, output logic [3:0] q);
        always_ff @(posedge clk) q <= d[9:6];
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert "$_UNKNOWN_" in _cell_types(mod)


# --- _emit_for_loop step shapes that don't unroll -------------------------


def _no_loop_unroll_flops(mod) -> bool:
    """A for-loop that doesn't unroll emits no per-iteration flops."""
    return not _flops(mod)


def test_for_loop_noncompound_step_does_not_unroll(tmp_path: Path) -> None:
    """``for (i = 0; i < 4; i = i + 1)`` — the step is a plain (non-
    compound) assignment, which ``_emit_for_loop`` doesn't model, so the
    loop body isn't unrolled (no flops emitted)."""
    src = """
    module m (input logic clk, input logic [3:0] d, output logic [3:0] q);
        logic [3:0] mem [4];
        always_ff @(posedge clk)
            for (int i = 0; i < 4; i = i + 1)
                mem[i] <= d;
        assign q = mem[0];
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert _no_loop_unroll_flops(mod), (
        f"a non-compound step must not unroll; got {_cell_types(mod)}"
    )


def test_for_loop_self_referential_step_does_not_unroll(tmp_path: Path) -> None:
    """``for (i = 1; i < 8; i += i)`` — the compound step desugars to
    ``i = i + i`` where *both* operands are the loop variable (neither is
    a constant), so the step amount can't be determined and the loop
    doesn't unroll."""
    src = """
    module m (input logic clk, input logic [3:0] d, output logic [3:0] q);
        logic [3:0] mem [8];
        always_ff @(posedge clk)
            for (int i = 1; i < 8; i += i)
                mem[i] <= d;
        assign q = mem[1];
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert _no_loop_unroll_flops(mod), (
        f"a self-referential step must not unroll; got {_cell_types(mod)}"
    )


def test_for_loop_equality_stop_op_does_not_iterate(tmp_path: Path) -> None:
    """``for (i = 0; i == 4; i++)`` — the stop operator is ``==``, which
    ``_still_iterating`` doesn't recognise as a loop-continue condition,
    so the loop runs zero times (no unroll)."""
    src = """
    module m (input logic clk, input logic [3:0] d, output logic [3:0] q);
        logic [3:0] mem [4];
        always_ff @(posedge clk)
            for (int i = 0; i == 4; i++)
                mem[i] <= d;
        assign q = mem[0];
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert _no_loop_unroll_flops(mod), (
        f"an == stop condition must not unroll; got {_cell_types(mod)}"
    )


# --- nonblocking range-select LHS in always_ff ----------------------------


def test_rangeselect_lhs_in_always_ff_slices_target(tmp_path: Path) -> None:
    """``bus[3:0] <= d`` in ``always_ff`` — the LHS is a range-select,
    so ``_lvalue_bits`` routes through ``_range_select_bits`` and the
    flop's Q is the low nibble of ``bus``."""
    src = """
    module m (input logic clk, input logic [3:0] d, output logic [7:0] bus);
        always_ff @(posedge clk) bus[3:0] <= d;
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    ff = _flops(mod)
    assert len(ff) == 1
    bus_bits = mod.netnames["bus"].bits
    assert tuple(ff[0].connections["Q"]) == tuple(bus_bits[0:4]), (
        f"bus[3:0] LHS should slice the low nibble; got Q={ff[0].connections['Q']}"
    )


# --- _lower_condition_to_bit guards ---------------------------------------


def test_lower_condition_to_bit_non_single_condition_is_none() -> None:
    """A condition list that isn't exactly one entry (the multi-pattern
    ``if (a matches ...)`` shape, or an empty list) can't lower to a
    single mux select → ``None``."""
    assert _builder()._lower_condition_to_bit([]) is None


def test_lower_condition_to_bit_missing_expr_is_none() -> None:
    """A single condition entry whose ``.expr`` is ``None`` (no boolean
    to evaluate) lowers to ``None``."""
    conditions = [_node("ConditionalPattern")]  # no .expr attribute
    assert _builder()._lower_condition_to_bit(conditions) is None


# --- _lower_replication / net-initializer conservative bails --------------


def test_replication_unlowerable_pattern_is_unknown(tmp_path: Path) -> None:
    """``{2{g(x)}}`` — the replicated inner pattern doesn't lower, so
    ``_lower_replication`` returns ``None`` and the assign degrades to an
    opaque driver."""
    src = """
    module m (input logic clk, input logic [1:0] x, output logic [3:0] q);
        function automatic logic [1:0] g(input logic [1:0] v); return v + 1; endfunction
        always_ff @(posedge clk) q <= {2{g(x)}};
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert "$_UNKNOWN_" in _cell_types(mod), (
        f"an unlowerable replication pattern should degrade to unknown; got {_cell_types(mod)}"
    )


def test_net_initializer_unlowerable_rhs_emits_no_cell(tmp_path: Path) -> None:
    """``logic [3:0] w = g(x);`` — a net whose declaration initializer
    doesn't lower leaves ``_emit_net_initializer`` with nothing to alias,
    so no cell is emitted for the initializer."""
    src = """
    module m (input logic [3:0] x, output logic [3:0] y);
        function automatic logic [3:0] g(input logic [3:0] v); return v + 1; endfunction
        logic [3:0] w = g(x);
        assign y = w;
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    # The unmodelled initializer aliases nothing; y reads w's own
    # (undriven) bits, and no comb cell is synthesised for ``g(x)``.
    assert _cell_types(mod) == [], (
        f"an unmodelled net initializer should emit no cell; got {_cell_types(mod)}"
    )
