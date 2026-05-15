"""Coverage tests for the always_ff body walker
(:func:`_emit_assignments_in`).

Issue: rtl-buddy-cdc#54 — the walker only descends through
:class:`BlockStatement` / :class:`StatementList` /
:class:`ExpressionStatement`. Any nonblocking assignment buried inside
a ``case``, a nested ``if/else`` (in the data branch), or a case
:class:`ItemGroup` is silently dropped — so state-machine flops in
real designs go undetected even though their ``always_ff`` block is
reached by the procedural-block lowering.

The tests below pin the expected behaviour: every nonblocking lvalue
that *can* fire on the clock — regardless of which control-flow shape
encloses it — must produce a ``$adff`` cell in the emitted ``Module``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from rtl_buddy_cdc.frontend import Frontend, elaborate

PYSLANG_INSTALLED = importlib.util.find_spec("pyslang") is not None
if not PYSLANG_INSTALLED:
    pytest.skip(
        "pyslang not installed — slang statement-walker tests are gated on it",
        allow_module_level=True,
    )

FLOP_TYPES = frozenset({"$dff", "$adff", "$dffe", "$adffe", "$sdff", "$sdffe"})


def _flop_count(module) -> int:
    return sum(1 for c in module.cells.values() if c.type in FLOP_TYPES)


def _elaborate(tmp_path: Path, src: str, top: str = "m"):
    sv = tmp_path / f"{top}.sv"
    sv.write_text(src)
    return elaborate([sv], top, frontend=Frontend.slang)


# --- CaseStatement ---------------------------------------------------------


def test_case_statement_emits_all_lvalues(tmp_path: Path) -> None:
    """A small state machine: 3 case items, each writes ``state`` and
    one other reg. The walker must produce a $adff per *lvalue* (not
    per case item), so the cell count is the set of distinct lvalues
    across all items — here ``state``, ``a``, ``b``, ``c`` = 4 flops."""
    src = """
    module m (
        input  logic clk, rst_n,
        input  logic [1:0] sel,
        output logic [1:0] state,
        output logic a, b, c
    );
        always_ff @(posedge clk or negedge rst_n) begin
            if (!rst_n) begin
                state <= 2'd0;
                a <= 1'b0; b <= 1'b0; c <= 1'b0;
            end else begin
                case (sel)
                    2'd0: begin state <= 2'd1; a <= 1'b1; end
                    2'd1: begin state <= 2'd2; b <= 1'b1; end
                    2'd2: begin state <= 2'd3; c <= 1'b1; end
                    default: state <= 2'd0;
                endcase
            end
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    # Each <= LHS produces one $adff (the walker emits per nonblocking
    # assignment site, not per distinct lvalue — Yosys equivalent does
    # the same and lets opt_clean collapse the dup-driver muxes). For
    # this FSM there are 7 ``<=`` sites in the data branch (1 default
    # + 2*3 case items) but only 4 distinct lvalues, and ``state`` is
    # written in 4 places. The contract here is "no silent skips" —
    # the walker must reach every site, so flop count ≥ 4 (one per
    # distinct lvalue).
    n = _flop_count(mod)
    assert n >= 4, (
        f"expected ≥ 4 flops (distinct lvalues state/a/b/c) reachable from "
        f"the case-statement; got {n}"
    )


def test_case_statement_with_default_only(tmp_path: Path) -> None:
    """Edge case: only a ``default`` arm. The walker must still find
    the assignment inside it."""
    src = """
    module m (
        input  logic clk, rst_n,
        input  logic [1:0] sel,
        output logic q
    );
        always_ff @(posedge clk or negedge rst_n) begin
            if (!rst_n) q <= 1'b0;
            else begin
                case (sel)
                    default: q <= 1'b1;
                endcase
            end
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert _flop_count(mod) >= 1, (
        f"expected ≥ 1 flop from default-only case; got {_flop_count(mod)}"
    )


# --- nested ConditionalStatement -------------------------------------------


def test_nested_if_in_data_branch(tmp_path: Path) -> None:
    """The data branch (else of the outer reset-check if) contains its
    own ``if (cond) ... else ...``. Both inner branches assign — the
    walker must reach both."""
    src = """
    module m (
        input  logic clk, rst_n, cond, d_a, d_b,
        output logic q_a, q_b
    );
        always_ff @(posedge clk or negedge rst_n) begin
            if (!rst_n) begin
                q_a <= 1'b0; q_b <= 1'b0;
            end else begin
                if (cond) begin
                    q_a <= d_a;
                end else begin
                    q_b <= d_b;
                end
            end
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert _flop_count(mod) >= 2, (
        f"expected ≥ 2 flops from both arms of the nested if; got {_flop_count(mod)}"
    )


def test_if_else_if_chain(tmp_path: Path) -> None:
    """``if/else if/else`` chain — the walker must reach every arm.
    pyslang represents this as nested ConditionalStatements."""
    src = """
    module m (
        input  logic clk, rst_n,
        input  logic [1:0] sel,
        output logic q
    );
        always_ff @(posedge clk or negedge rst_n) begin
            if (!rst_n) q <= 1'b0;
            else begin
                if (sel == 2'd0) q <= 1'b1;
                else if (sel == 2'd1) q <= 1'b0;
                else if (sel == 2'd2) q <= 1'b1;
                else q <= 1'b0;
            end
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert _flop_count(mod) >= 1, (
        f"expected ≥ 1 flop reachable through the if/else-if chain; "
        f"got {_flop_count(mod)}"
    )


# --- mixed: case inside if inside case -------------------------------------


def test_case_inside_if_inside_case(tmp_path: Path) -> None:
    """Three control-flow levels: outer case → if/else → inner case.
    The leaf nonblocking assigns sit four levels deep from the
    always_ff body."""
    src = """
    module m (
        input  logic clk, rst_n, cond,
        input  logic [1:0] outer_sel, inner_sel,
        output logic q
    );
        always_ff @(posedge clk or negedge rst_n) begin
            if (!rst_n) q <= 1'b0;
            else begin
                case (outer_sel)
                    2'd0:
                        if (cond) begin
                            case (inner_sel)
                                2'd0: q <= 1'b1;
                                default: q <= 1'b0;
                            endcase
                        end else begin
                            q <= 1'b0;
                        end
                    default: q <= 1'b0;
                endcase
            end
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert _flop_count(mod) >= 1, (
        f"expected ≥ 1 flop from case-inside-if-inside-case shape; "
        f"got {_flop_count(mod)}"
    )
