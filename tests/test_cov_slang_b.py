"""Coverage tests for the slang frontend's statement walker and
procedural-block lowering (``frontends/slang.py``, lines ~905-1610).

The existing ``test_slang_*`` suite pins the *happy paths* of the
walker — canonical for-loops, both-arms if/else, dynamic case items,
always_latch, enable inference. This module deliberately drives the
*bail / fall-through / fold* arms that the happy-path tests skip:

- statement shapes the walker can't model (struct-member LHS,
  function-call RHS / case-expr / case-match) must degrade gracefully
  rather than crash, producing either no flop or a ``$_UNKNOWN_``
  driver as documented in the source;
- the synchronous-reset heuristic
  (``_classify_reset_check`` → ``_is_constant_only_assignment_tree``)
  must recognise an all-constant ``if (rst)`` arm and emit ``$sdff``,
  recursing through for-loop / case reset arms;
- an async-reset event-list whose body's outer ``if`` gates on a
  *different* symbol than the reset must NOT be classified as a reset
  check — the body walks as a normal mux tree;
- every ``for``-loop header shape the unroller supports
  (``<=`` / ``>`` / ``!=`` stop ops, ``-=`` compound step) must
  unroll to the right trip count, and every shape it deliberately
  refuses (multi-var header, multi-step header, ``*=`` step,
  non-binary stop) must bail with zero flops rather than throw.

Each test asserts on concrete netlist content — flop count, cell
type set, ``$sdff`` vs ``$adff`` reset shape, ``$_UNKNOWN_`` presence,
mux-tree structure — so a regression in any branch is diagnostic, not
a silent coverage drop.

Gated on pyslang (same module-level skip as ``test_slang_lowering``);
the coverage job has pyslang installed so these run there.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from rtl_buddy_cdc.frontend import Frontend, elaborate

PYSLANG_INSTALLED = importlib.util.find_spec("pyslang") is not None
if not PYSLANG_INSTALLED:
    pytest.skip(
        "pyslang not installed — slang walker bail-path tests are gated on it",
        allow_module_level=True,
    )

FLOP_TYPES = frozenset({"$dff", "$adff", "$dffe", "$adffe", "$sdff", "$sdffe"})


def _elaborate(tmp_path: Path, src: str, top: str = "m"):
    """Write ``src`` to a temp .sv file and elaborate via the slang
    frontend — the same entry point the CLI's ``lint`` command uses."""
    sv = tmp_path / f"{top}.sv"
    sv.write_text(src)
    return elaborate([sv], top, frontend=Frontend.slang)


def _flops(module) -> list:
    return [c for c in module.cells.values() if c.type in FLOP_TYPES]


def _flop_count(module) -> int:
    return sum(1 for c in module.cells.values() if c.type in FLOP_TYPES)


def _cell_types(module) -> set[str]:
    return {c.type for c in module.cells.values()}


def _build_drivers(mod) -> dict:
    """Bit → (cell_name, port_name) for every cell output bit."""
    drv = {}
    for name, cell in mod.cells.items():
        for port, bits in cell.connections.items():
            if port in ("Q", "Y"):
                for b in bits:
                    if isinstance(b, int):
                        drv[b] = (name, port)
    return drv


# --- timing-control edge classification -----------------------------------


def test_negedge_clock_single_event_sets_clk_polarity_zero(tmp_path: Path) -> None:
    """``always_ff @(negedge clk)`` (no reset) must lower to a plain
    ``$dff`` with ``CLK_POLARITY`` = 0 — the negedge edge threads
    through ``_classify_timing`` into the flop's parameter, closing
    the CDC-016 parity gap. The single-event no-reset shape exercises
    the ``clk_edge == "NegEdge"`` branch of the classifier."""
    src = """
    module m (input logic clk, input logic [3:0] d, output logic [3:0] q);
        always_ff @(negedge clk) q <= d;
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    flops = _flops(mod)
    assert len(flops) == 1, f"expected one $dff for q; got {len(flops)}"
    flop = flops[0]
    assert flop.type == "$dff", (
        f"no-reset always_ff should be a plain $dff; got {flop.type}"
    )
    clk_pol = flop.parameters.get("CLK_POLARITY")
    assert clk_pol is not None, "CLK_POLARITY parameter missing on $dff"
    assert int(clk_pol, 2) == 0, (
        f"negedge clock should set CLK_POLARITY=0; got {clk_pol!r}"
    )


# --- non-lowerable condition: both arms walk unconditionally --------------


def test_multibit_if_condition_walks_both_arms(tmp_path: Path) -> None:
    """When the ``else if`` condition is multi-bit (``if (sel)`` with a
    2-bit ``sel``), ``_lower_condition_to_bit`` can't reduce it to a
    single mux-select bit and returns ``None``. The walker then falls
    back to walking *both* arms unconditionally (the conservative
    pre-enable-inference policy). With each arm writing a distinct LHS
    that yields two independent flops — proving the fall-through path
    still surfaces every assignment site rather than dropping the
    block."""
    src = """
    module m (
        input  logic       clk, rst_n,
        input  logic [1:0] sel,
        input  logic       da, db,
        output logic       qa, qb
    );
        always_ff @(posedge clk or negedge rst_n) begin
            if (!rst_n) begin qa <= 1'b0; qb <= 1'b0; end
            else if (sel) qa <= da;
            else          qb <= db;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    flops = _flops(mod)
    assert len(flops) == 2, (
        f"expected two flops (qa, qb) from the unconditional both-arms walk "
        f"of a non-lowerable multi-bit condition; got {len(flops)}"
    )
    # Both LHS ports must each be driven by a flop's Q — confirming the
    # fall-through reached both arms.
    q_outputs = set()
    for f in flops:
        q_outputs.update(f.connections.get("Q", ()))
    qa_bit = mod.ports["qa"].bits[0]
    qb_bit = mod.ports["qb"].bits[0]
    assert qa_bit in q_outputs and qb_bit in q_outputs, (
        "both qa and qb must be flop Q outputs; the non-lowerable condition "
        "must not silently drop an arm"
    )


# --- non-lowerable RHS: $_UNKNOWN_ driver ---------------------------------


def test_function_call_rhs_emits_unknown_driver(tmp_path: Path) -> None:
    """A nonblocking assign whose RHS is a function call
    (``q <= inc(d)``) can't be lowered by ``_bits_of_expression``
    (function calls aren't modelled), so the drain substitutes a
    ``$_UNKNOWN_`` stub driver for D. The flop must still be emitted —
    the netlist stays well-formed and the rule pack treats the D input
    as opaque rather than crashing."""
    src = """
    module m (input logic clk, rst_n, input logic [3:0] d, output logic [3:0] q);
        function automatic logic [3:0] inc(input logic [3:0] x);
            inc = x + 4'd1;
        endfunction
        always_ff @(posedge clk or negedge rst_n)
            if (!rst_n) q <= 4'd0;
            else        q <= inc(d);
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    types = _cell_types(mod)
    assert "$adff" in types, f"expected an $adff for q; got {sorted(types)}"
    assert "$_UNKNOWN_" in types, (
        f"function-call RHS should fall back to a $_UNKNOWN_ driver; "
        f"got cell types {sorted(types)}"
    )
    # The flop's D must trace to the $_UNKNOWN_ output (opaque upstream).
    flop = _flops(mod)[0]
    d_bit = flop.connections["D"][0]
    drv = _build_drivers(mod).get(d_bit)
    assert drv is not None, "flop D has no driver"
    assert mod.cells[drv[0]].type == "$_UNKNOWN_", (
        f"flop D should be driven by the $_UNKNOWN_ stub; got {mod.cells[drv[0]].type}"
    )


# --- non-lowerable LHS: assignment site skipped ---------------------------


def test_struct_member_lhs_is_skipped(tmp_path: Path) -> None:
    """``q.a <= d`` writes a struct member — ``_lvalue_bits`` only
    resolves NamedValue / ElementSelect / RangeSelect lvalues, so a
    struct-member LHS returns ``None`` and the write is skipped. The
    block contributes no flop (rather than emitting a malformed cell)
    — the documented "other LHS shapes are skipped" contract."""
    src = """
    typedef struct packed { logic [3:0] a; } s_t;
    module m (input logic clk, rst_n, input logic [3:0] d, output s_t q);
        always_ff @(posedge clk or negedge rst_n)
            if (!rst_n) q.a <= 4'd0;
            else        q.a <= d;
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert _flop_count(mod) == 0, (
        f"struct-member LHS should be skipped (no flop emitted); "
        f"got {_flop_count(mod)} flops, cells {sorted(_cell_types(mod))}"
    )


# --- non-lowerable case-expr: conservative bail ---------------------------


def test_case_on_function_call_bails_to_unconditional_walk(tmp_path: Path) -> None:
    """``case (f(sel))`` — the case expression is a function call that
    ``_bits_of_expression`` can't lower, so the walker takes the
    conservative bail: it walks every item (and the default) with no
    per-arm enable. The single ``q <= d`` arm still surfaces as a flop;
    no ``$eq`` gating cell is emitted because the case-expr never
    lowered to bits."""
    src = """
    module m (
        input  logic       clk, rst_n,
        input  logic [1:0] sel,
        input  logic [3:0] d,
        output logic [3:0] q
    );
        function automatic logic [1:0] f(input logic [1:0] x);
            f = x ^ 2'b01;
        endfunction
        always_ff @(posedge clk or negedge rst_n) begin
            if (!rst_n) q <= 4'd0;
            else case (f(sel))
                2'd0:    q <= d;
                default: q <= 4'd0;
            endcase
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    types = _cell_types(mod)
    assert "$adff" in types, f"expected an $adff for q; got {sorted(types)}"
    assert "$eq" not in types, (
        f"non-lowerable case-expr must NOT emit per-arm $eq gating cells "
        f"(bail path walks unconditionally); got {sorted(types)}"
    )
    assert _flop_count(mod) == 1, (
        f"distinct lvalue ``q`` collapses to one flop; got {_flop_count(mod)}"
    )


# --- non-lowerable case-match: item walks without enable ------------------


def test_case_match_function_call_walks_item_without_enable(tmp_path: Path) -> None:
    """The case-expr *is* lowerable (a 2-bit signal) but the item's
    match expression is a function call. ``_bits_of_expression`` returns
    ``None`` for that match, so the per-match ``$eq`` is skipped, the
    item's combined enable is ``None``, and the arm walks with no
    enable pushed. The arm's ``q <= d`` write still emits a flop."""
    src = """
    module m (
        input  logic       clk, rst_n,
        input  logic [1:0] sel,
        input  logic [3:0] d,
        output logic [3:0] q
    );
        function automatic logic [1:0] thresh();
            thresh = 2'd1;
        endfunction
        always_ff @(posedge clk or negedge rst_n) begin
            if (!rst_n) q <= 4'd0;
            else case (sel)
                thresh(): q <= d;
                default:  ;
            endcase
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert _flop_count(mod) == 1, (
        f"the unmatched-shape item should still emit its q-flop "
        f"(walk-without-enable path); got {_flop_count(mod)}"
    )
    types = _cell_types(mod)
    assert "$eq" not in types, (
        f"a function-call match expr should not produce an $eq gating cell; "
        f"got {sorted(types)}"
    )


# --- synchronous-reset heuristic: $sdff -----------------------------------


def test_sync_reset_emits_sdff(tmp_path: Path) -> None:
    """A no-async-event ``always_ff @(posedge clk)`` whose body is
    ``if (rst) q <= 0; else q <= d;`` must be classified as a
    synchronous reset: the ``ifTrue`` arm assigns only constants
    (``_is_constant_only_assignment_tree`` → True) so the flop
    materialises as ``$sdff``, not a plain ``$dff`` with a mux."""
    src = """
    module m (input logic clk, rst, input logic [3:0] d, output logic [3:0] q);
        always_ff @(posedge clk) begin
            if (rst) q <= 4'd0;
            else     q <= d;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    types = _cell_types(mod)
    assert "$sdff" in types, (
        f"all-constant if (rst) arm should fold to $sdff; got {sorted(types)}"
    )
    assert "$adff" not in types, (
        "no async event in the sensitivity list — must not be $adff"
    )


def test_sync_reset_with_forloop_reset_arm(tmp_path: Path) -> None:
    """The synchronous-reset detector recurses through a ``for``-loop
    in the reset arm: ``if (rst) for (i) q[i] <= 0; else ...`` — the
    loop body assigns only constants, so
    ``_is_constant_only_assignment_tree`` returns True through its
    ForLoopStatement branch and every unrolled bit becomes an
    ``$sdff``."""
    src = """
    module m #(parameter int N = 3) (
        input  logic clk, rst,
        input  logic [N-1:0] d,
        output logic [N-1:0] q
    );
        always_ff @(posedge clk) begin
            if (rst) begin
                for (int i = 0; i < N; i++) q[i] <= 1'b0;
            end else begin
                for (int i = 0; i < N; i++) q[i] <= d[i];
            end
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    flops = _flops(mod)
    assert len(flops) == 3, f"expected 3 unrolled flops (N=3); got {len(flops)}"
    assert all(f.type == "$sdff" for f in flops), (
        f"for-loop reset arm of constants must classify as sync reset ($sdff); "
        f"got {[f.type for f in flops]}"
    )


def test_sync_reset_with_case_reset_arm(tmp_path: Path) -> None:
    """The detector also recurses through a ``case`` whose every arm
    (items + default) assigns only constants in the reset branch:
    ``if (rst) case (mode) ... endcase`` folds to ``$sdff`` via the
    CaseStatement branch of ``_is_constant_only_assignment_tree``."""
    src = """
    module m (
        input  logic       clk, rst,
        input  logic [1:0] mode,
        input  logic [3:0] d,
        output logic [3:0] q
    );
        always_ff @(posedge clk) begin
            if (rst) begin
                case (mode)
                    2'd0:    q <= 4'd0;
                    default: q <= 4'd0;
                endcase
            end else q <= d;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    types = _cell_types(mod)
    assert "$sdff" in types, (
        f"case-of-constants reset arm should fold to $sdff; got {sorted(types)}"
    )


def test_reset_arm_with_task_call_is_not_sync(tmp_path: Path) -> None:
    """The sync-reset detector walks the ``ifTrue`` arm via
    ``_is_constant_only_assignment_tree``. When that arm contains a
    non-assignment statement (a void task call), the helper bails to
    ``False`` (the call isn't a constant assign), so the block is NOT
    classified as a synchronous reset — it lowers as a plain ``$dff``
    with a mux tree instead of ``$sdff``."""
    src = """
    module m (input logic clk, rst, input logic [3:0] d, output logic [3:0] q);
        task automatic noop(); endtask
        always_ff @(posedge clk) begin
            if (rst) begin noop(); q <= 4'd0; end
            else     q <= d;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    types = _cell_types(mod)
    assert "$sdff" not in types, (
        f"a reset arm containing a task call must not classify as sync; "
        f"got {sorted(types)}"
    )
    assert "$dff" in types and "$mux" in types, (
        f"non-classified block lowers as $dff + mux tree; got {sorted(types)}"
    )


def test_reset_arm_with_blocking_assign_is_not_sync(tmp_path: Path) -> None:
    """A *blocking* assign in the candidate reset arm (``if (rst) q =
    0;``) likewise fails the constant-only check — the helper requires
    nonblocking assigns (``isNonBlocking``), so it returns ``False``
    and the block is not folded to ``$sdff``."""
    src = """
    module m (input logic clk, rst, input logic [3:0] d, output logic [3:0] q);
        always_ff @(posedge clk) begin
            if (rst) q = 4'd0;
            else     q <= d;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    types = _cell_types(mod)
    assert "$sdff" not in types, (
        f"a blocking-assign reset arm must not classify as sync; got {sorted(types)}"
    )
    assert "$dff" in types, f"expected a plain $dff; got {sorted(types)}"


# --- async event list but mismatched reset symbol -------------------------


def test_async_event_with_mismatched_if_symbol_is_not_a_reset(tmp_path: Path) -> None:
    """``always_ff @(posedge clk or negedge rst_n)`` whose body's outer
    ``if`` gates on a *different* signal (``en``, not ``rst_n``) must
    NOT be classified as a reset check: ``_classify_reset_check``
    returns ``None`` because ``cond_sym is not reset_sym_from_event_list``.
    The body then walks as an ordinary if/else, building a mux tree
    (with a ``$not`` for the else arm) — not a clean reset fold."""
    src = """
    module m (
        input  logic       clk, rst_n, en,
        input  logic [3:0] d,
        output logic [3:0] q
    );
        always_ff @(posedge clk or negedge rst_n) begin
            if (en) q <= d;
            else    q <= 4'd0;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    types = _cell_types(mod)
    # The async event still gives an $adff binding, but the body's
    # if/else is walked as a mux tree rather than folded into the
    # reset value — so a $mux and a $not appear.
    assert "$mux" in types, (
        f"mismatched-symbol if/else must build a mux tree (not a reset fold); "
        f"got {sorted(types)}"
    )
    assert "$not" in types, (
        f"the else arm pushes $not(en) onto the enable stack; got {sorted(types)}"
    )
    en_bit = mod.ports["en"].bits[0]
    # The select of the mux must trace back to ``en`` (the gating
    # condition), confirming en — not rst_n — drives the conditional.
    seen: set = set()
    frontier = [
        b
        for c in mod.cells.values()
        if c.type == "$mux"
        for b in c.connections.get("S", ())
    ]
    drivers = _build_drivers(mod)
    walk = {"$mux", "$not", "$and", "$or"}
    while frontier:
        b = frontier.pop()
        if b in seen or not isinstance(b, int):
            continue
        seen.add(b)
        drv = drivers.get(b)
        if drv is None or mod.cells[drv[0]].type not in walk:
            continue
        for port, bits in mod.cells[drv[0]].connections.items():
            if port in ("Q", "Y"):
                continue
            frontier.extend(bits)
    assert en_bit in seen, (
        "the mux select must trace back to the ``en`` port — confirming the "
        "body gated on en, distinct from the rst_n reset event"
    )


# --- for-loop header shapes the unroller supports -------------------------


def test_for_loop_less_than_equal_stop(tmp_path: Path) -> None:
    """``for (int i = 0; i <= 3; i++)`` — the ``LessThanEqual`` stop op
    includes the bound, so the trip count is 4 (i = 0,1,2,3)."""
    src = """
    module m (input logic clk, input logic [4:0] d, output logic [4:0] q);
        always_ff @(posedge clk)
            for (int i = 0; i <= 3; i++) q[i] <= d[i];
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert _flop_count(mod) == 4, (
        f"i <= 3 should unroll 4 times (0..3); got {_flop_count(mod)}"
    )


def test_for_loop_greater_than_stop(tmp_path: Path) -> None:
    """``for (int i = 3; i > 0; i--)`` — the ``GreaterThan`` stop op
    with a decrement: trip count 3 (i = 3,2,1; i=0 excluded)."""
    src = """
    module m (input logic clk, input logic [4:0] d, output logic [4:0] q);
        always_ff @(posedge clk)
            for (int i = 3; i > 0; i--) q[i] <= d[i];
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert _flop_count(mod) == 3, (
        f"i > 0 from 3 should unroll 3 times (3,2,1); got {_flop_count(mod)}"
    )


def test_for_loop_inequality_stop(tmp_path: Path) -> None:
    """``for (int i = 0; i != 3; i++)`` — the ``Inequality`` stop op
    runs while i differs from 3: trip count 3 (i = 0,1,2)."""
    src = """
    module m (input logic clk, input logic [4:0] d, output logic [4:0] q);
        always_ff @(posedge clk)
            for (int i = 0; i != 3; i++) q[i] <= d[i];
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert _flop_count(mod) == 3, (
        f"i != 3 from 0 should unroll 3 times (0,1,2); got {_flop_count(mod)}"
    )


def test_for_loop_compound_subtract_step(tmp_path: Path) -> None:
    """``for (int i = 5; i > 0; i -= 2)`` — the compound ``-=`` step
    desugars to ``i = i - 2`` (lvalue on the left of the Subtract), so
    the step amount is -2: trip count 3 (i = 5,3,1)."""
    src = """
    module m (input logic clk, input logic [5:0] d, output logic [5:0] q);
        always_ff @(posedge clk)
            for (int i = 5; i > 0; i -= 2) q[i] <= d[i];
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert _flop_count(mod) == 3, (
        f"i -= 2 from 5 (i>0) should unroll 3 times (5,3,1); got {_flop_count(mod)}"
    )


def test_nested_for_loops_unroll_product(tmp_path: Path) -> None:
    """Two procedural for-loops nested with distinct loop variables
    must unroll to the product of their trip counts (2 x 3 = 6),
    confirming the inner loop's binding is re-pushed and restored on
    each outer iteration without leaking state."""
    src = """
    module m (input logic clk, input logic [5:0] d, output logic [5:0] q);
        always_ff @(posedge clk) begin
            for (int i = 0; i < 2; i++)
                for (int j = 0; j < 3; j++)
                    q[i*3 + j] <= d[i*3 + j];
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert _flop_count(mod) == 6, (
        f"nested 2x3 for-loops should unroll to 6 flops; got {_flop_count(mod)}"
    )


# --- for-loop header shapes the unroller deliberately refuses -------------


def test_for_loop_multiple_loop_vars_bails(tmp_path: Path) -> None:
    """``for (int i = 0, j = 0; ...; ...)`` declares two loop vars. The
    unroller only handles a single loop variable, so it bails with no
    flop emitted (rather than guessing). Must not raise."""
    src = """
    module m (input logic clk, input logic [3:0] d, output logic [3:0] q);
        always_ff @(posedge clk)
            for (int i = 0, j = 0; i < 4; i++) q[i] <= d[i];
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert _flop_count(mod) == 0, (
        f"multi-loop-var header must bail with 0 flops; got {_flop_count(mod)}"
    )


def test_for_loop_multiple_steps_bails(tmp_path: Path) -> None:
    """``for (...; ...; i++, j++)`` has two step expressions. The
    unroller only supports a single step, so it bails cleanly."""
    src = """
    module m (input logic clk, input logic [3:0] d, output logic [3:0] q);
        int j;
        always_ff @(posedge clk)
            for (int i = 0; i < 4; i++, j++) q[i] <= d[i];
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert _flop_count(mod) == 0, (
        f"multi-step header must bail with 0 flops; got {_flop_count(mod)}"
    )


def test_for_loop_multiplicative_step_bails(tmp_path: Path) -> None:
    """``for (int i = 1; i < 8; i *= 2)`` — the ``*=`` compound step is
    not one of the supported (``+=`` / ``-=`` / ``++`` / ``--``) shapes,
    so ``step_amount`` stays ``None`` and the loop bails. (A real
    geometric loop would otherwise need a non-constant trip model.)"""
    src = """
    module m (input logic clk, input logic [7:0] d, output logic [7:0] q);
        always_ff @(posedge clk)
            for (int i = 1; i < 8; i *= 2) q[i] <= d[i];
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert _flop_count(mod) == 0, (
        f"multiplicative step must bail with 0 flops; got {_flop_count(mod)}"
    )


def test_for_loop_runtime_stop_expr_bails(tmp_path: Path) -> None:
    """A stop expression that isn't a ``BinaryExpression`` against the
    loop var (here a bare runtime signal ``go``) can't yield a
    compile-time trip count, so the unroller bails. Must not crash."""
    src = """
    module m (input logic clk, go, input logic [3:0] d, output logic [3:0] q);
        always_ff @(posedge clk)
            for (int i = 0; go; i++) q[i] <= d[i];
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert _flop_count(mod) == 0, (
        f"non-BinaryExpression stop must bail with 0 flops; got {_flop_count(mod)}"
    )
    # Elaboration still produced a well-formed module.
    assert mod.name == "m"


def test_for_loop_runtime_start_bails(tmp_path: Path) -> None:
    """A loop whose *initial* value is a runtime signal (``i = base``)
    has no compile-time start, so ``_const_int(init_expr)`` returns
    ``None`` and the unroller bails before computing a trip count."""
    src = """
    module m (
        input  logic       clk,
        input  logic [3:0] base,
        input  logic [3:0] d,
        output logic [3:0] q
    );
        always_ff @(posedge clk)
            for (int i = base; i < 4; i++) q[i] <= d[i];
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert _flop_count(mod) == 0, (
        f"runtime loop start must bail with 0 flops; got {_flop_count(mod)}"
    )


def test_for_loop_runtime_stop_bound_bails(tmp_path: Path) -> None:
    """A stop bound that's a runtime signal (``i < n`` with ``n`` an
    ``int`` so no width-conversion is inserted around the loop-var
    reference) is a ``BinaryExpression`` whose left operand resolves
    to the loop var — but ``_const_int`` of the right bound returns
    ``None``. The unroller bails at the ``stop_val is None`` guard
    (the documented fall-through-on-unknown policy) rather than
    spinning an unbounded unroll."""
    src = """
    module m (
        input  logic        clk,
        input  int          n,
        input  logic [15:0] d,
        output logic [15:0] q
    );
        always_ff @(posedge clk)
            for (int i = 0; i < n; i++) q[i] <= d[i];
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert _flop_count(mod) == 0, (
        f"runtime stop bound must bail with 0 flops; got {_flop_count(mod)}"
    )


# --- non-assignment statements inside always_ff ---------------------------


def test_task_call_statement_in_always_ff_is_skipped(tmp_path: Path) -> None:
    """A non-assignment ``ExpressionStatement`` (a void task call) in
    the data branch must be tolerated: ``_emit_assignment_expression``
    sees a non-``AssignmentExpression`` and returns without emitting,
    while the sibling ``q <= d`` still produces its flop. Confirms the
    walker doesn't choke on procedural statements it can't model."""
    src = """
    module m (input logic clk, rst_n, input logic [3:0] d, output logic [3:0] q);
        task automatic noop(); endtask
        always_ff @(posedge clk or negedge rst_n) begin
            if (!rst_n) q <= 4'd0;
            else begin
                noop();
                q <= d;
            end
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert _flop_count(mod) == 1, (
        f"the task-call statement must be skipped while q<=d still emits "
        f"its flop; got {_flop_count(mod)} flops"
    )
    assert "$adff" in _cell_types(mod)


def test_nonconstant_reset_arm_is_not_a_sync_reset(tmp_path: Path) -> None:
    """A no-async-event block whose outer ``if`` arm assigns a *signal*
    (``if (rst) q <= dd;``) — not a constant — must NOT be treated as a
    synchronous reset. ``_is_constant_only_assignment_tree`` returns
    ``False`` (the RHS is a runtime value), so the classifier declines
    the sync fold and the block lowers as a plain ``$dff`` driven by a
    mux tree (the runtime-``if`` shape), distinguishing it from the
    genuine ``if (rst) q <= 0`` reset that yields ``$sdff``."""
    src = """
    module m (
        input  logic       clk, rst,
        input  logic [3:0] d, dd,
        output logic [3:0] q
    );
        always_ff @(posedge clk) begin
            if (rst) q <= dd;
            else     q <= d;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    types = _cell_types(mod)
    assert "$sdff" not in types, (
        f"a non-constant reset arm must NOT fold to $sdff; got {sorted(types)}"
    )
    assert "$dff" in types, (
        f"the block should lower as a plain $dff with a mux tree; got {sorted(types)}"
    )
    assert "$mux" in types, (
        f"the runtime-if shape must build a mux tree; got {sorted(types)}"
    )


# --- blocking assignment inside always_ff ---------------------------------


def test_blocking_assign_in_always_ff_is_skipped(tmp_path: Path) -> None:
    """A *blocking* assignment (``q = d``) inside an ``always_ff`` body
    is an SV style violation; ``_emit_assignment_expression`` checks
    ``isNonBlocking`` and skips it rather than modelling it. The data
    arm here is blocking, so no flop is accumulated for it — the block
    emits nothing once the reset fold has nothing to attach to."""
    src = """
    module m (input logic clk, rst_n, input logic [3:0] d, output logic [3:0] q);
        always_ff @(posedge clk or negedge rst_n) begin
            if (!rst_n) q <= 4'd0;
            else        q = d;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert _flop_count(mod) == 0, (
        f"blocking assign in always_ff must be skipped (not emitted); "
        f"got {_flop_count(mod)} flops"
    )
