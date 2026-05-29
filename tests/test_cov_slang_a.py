"""Coverage tests for the slang frontend's *early* lowering paths
(``frontends/slang.py`` lines 171-905): the elaborate() entry point's
error surfaces, generate-block expansion, port / netname collection,
async-reset-arm value capture, and the procedural-block dispatch
guards.

These complement ``test_slang_lowering.py`` (operator zoo) and
``test_slang_const_cond_fold.py`` (mux folding) by pinning the
*structural-edge* behaviours those files skip: what happens on a
parse error, an unknown top, an unconnected child port, a blocking /
non-constant reset assignment, a plain ``always @(*)`` block, and the
generate-array / generate-if naming convention.

Each test drives the public ``elaborate(paths, top,
frontend=Frontend.slang)`` factory exactly as the CLI does — the same
``_elaborate_inline`` / ``_elaborate_full`` helper shape used by
``test_slang_lowering.py`` — and asserts on the produced ``Module``'s
cells / netnames / parameters or on the raised
``SlangElaborationError`` text. No Yosys binary is involved; pyslang
reads the SV directly.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from rtl_buddy_cdc.frontend import Frontend, elaborate
from rtl_buddy_cdc.frontends.slang import SlangElaborationError

PYSLANG_INSTALLED = importlib.util.find_spec("pyslang") is not None
if not PYSLANG_INSTALLED:
    pytest.skip(
        "pyslang not installed — slang early-lowering tests are gated on it",
        allow_module_level=True,
    )


def _elaborate_full(tmp_path: Path, src: str, top: str = "m"):
    """Write ``src`` to a temp .sv file and elaborate via the slang
    frontend, returning the full :class:`Module`."""
    sv = tmp_path / f"{top}.sv"
    sv.write_text(src)
    return elaborate([sv], top, frontend=Frontend.slang)


def _elaborate_inline(tmp_path: Path, src: str, top: str = "m") -> dict[str, str]:
    """Flat ``{cell_name: cell_type}`` map for membership assertions."""
    module = _elaborate_full(tmp_path, src, top)
    return {name: cell.type for name, cell in module.cells.items()}


# --- elaborate() error surfaces (lines 190-209) ---------------------------


def test_fatal_diagnostic_raises_elaboration_error(tmp_path: Path) -> None:
    """A genuine SV syntax error must be surfaced as a
    :class:`SlangElaborationError` whose message carries the rendered
    ``TextDiagnosticClient`` summary — not swallowed, not a bare
    pyslang crash. Exercises the fatal-diagnostic branch
    (``DiagnosticEngine`` + ``TextDiagnosticClient`` rendering)."""
    sv = tmp_path / "broken.sv"
    # ``assign a = ;`` is a parse error (missing RHS expression).
    sv.write_text("module m (output logic a);\n    assign a = ;\nendmodule\n")
    with pytest.raises(SlangElaborationError) as exc:
        elaborate([sv], "m", frontend=Frontend.slang)
    assert "pyslang elaboration produced errors" in str(exc.value)


def test_unknown_top_lists_available_instances(tmp_path: Path) -> None:
    """Asking for a top that doesn't exist must raise with a message
    that names the requested top *and* enumerates the instances that
    were actually elaborated — the diagnostic the CLI surfaces when a
    user fat-fingers ``--top``."""
    sv = tmp_path / "real.sv"
    sv.write_text(
        "module real_top (input logic a, output logic b);\n"
        "    assign b = a;\nendmodule\n"
    )
    with pytest.raises(SlangElaborationError) as exc:
        elaborate([sv], "does_not_exist", frontend=Frontend.slang)
    msg = str(exc.value)
    assert "does_not_exist" in msg
    assert "real_top" in msg, msg


# --- generate-block expansion (lines 394-410) ------------------------------


def test_generate_for_array_uses_indexed_dotted_prefix(tmp_path: Path) -> None:
    """A ``for (genvar ...)`` generate array
    (:class:`GenerateBlockArraySymbol`) must expand into one body per
    iteration, each emitting cells under the Yosys-flatten
    ``g_label[i].`` dotted prefix. Confirms the array-entry naming path
    (``arrayIndex`` → ``g_stage[0].`` … ``g_stage[3].``)."""
    src = """module m (
    input  logic clk,
    input  logic [3:0] d,
    output logic [3:0] q
);
    genvar i;
    generate
        for (i = 0; i < 4; i = i + 1) begin : g_stage
            always_ff @(posedge clk) q[i] <= d[i];
        end
    endgenerate
endmodule
"""
    module = _elaborate_full(tmp_path, src)
    flop_names = sorted(
        c.name for c in module.cells.values() if c.type in {"$dff", "$adff"}
    )
    assert len(flop_names) == 4, flop_names
    assert flop_names[0].startswith("g_stage[0]."), flop_names
    assert flop_names[3].startswith("g_stage[3]."), flop_names


def test_generate_if_emits_only_the_taken_branch(tmp_path: Path) -> None:
    """A ``generate if`` keeps only the elaborated
    (:attr:`isUninstantiated` ``False``) branch. With ``EN = 1`` the
    ``g_on`` body's flop is emitted under the ``g_on.`` prefix and the
    pruned ``g_off`` branch contributes nothing — proving the
    uninstantiated-branch skip and the single-named-block prefix path
    both fire."""
    src = """module m #(parameter bit EN = 1) (
    input  logic clk, d,
    output logic q
);
    generate
        if (EN) begin : g_on
            always_ff @(posedge clk) q <= d;
        end else begin : g_off
            always_ff @(posedge clk) q <= 1'b0;
        end
    endgenerate
endmodule
"""
    module = _elaborate_full(tmp_path, src)
    flop_names = sorted(
        c.name for c in module.cells.values() if c.type in {"$dff", "$adff"}
    )
    assert len(flop_names) == 1, flop_names
    assert flop_names[0].startswith("g_on."), flop_names
    assert not any("g_off" in n for n in flop_names), flop_names


# --- async-reset-arm value capture (lines 857-892) -------------------------
#
# The reset arm is walked by ``_collect_reset_assignments`` to fill the
# ``$adff`` ``ARST_VALUE`` parameter. A recognised constant literal is
# captured (control below shows 4'd5 → "0101"); the blocking,
# non-constant, and non-assignment shapes each early-return and fall
# back to the conservative ARST_VALUE = "0". Asserting "0" against the
# captured-value control proves the fallback, not coincidence.


def test_reset_arm_constant_literal_is_captured(tmp_path: Path) -> None:
    """Control: a recognised ``q <= 4'd5;`` reset arm populates
    ``ARST_VALUE`` with the literal's bits (length tracks WIDTH). This
    is the value the early-return cases below deliberately do *not*
    capture."""
    src = """module m (
    input  logic clk, rst_n,
    input  logic [3:0] d,
    output logic [3:0] q
);
    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n) q <= 4'd5;
        else        q <= d;
endmodule
"""
    module = _elaborate_full(tmp_path, src)
    adff = next(c for c in module.cells.values() if c.type == "$adff")
    assert adff.parameters["ARST_VALUE"] == "0101", adff.parameters


def test_reset_arm_blocking_assign_falls_back_to_zero(tmp_path: Path) -> None:
    """A *blocking* assign in the reset arm (``q = 1'b0;`` — an SV
    style slip) is not a nonblocking write, so the reset-value walker
    skips it and ``ARST_VALUE`` defaults to ``"0"``. The flop still
    emits as an ``$adff`` (the event list still carries the async
    reset)."""
    src = """module m (
    input  logic clk, rst_n, d,
    output logic q
);
    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n) q = 1'b0;
        else        q <= d;
endmodule
"""
    module = _elaborate_full(tmp_path, src)
    adff = next(c for c in module.cells.values() if c.type == "$adff")
    assert adff.parameters["ARST_VALUE"] == "0", adff.parameters


def test_reset_arm_non_constant_rhs_falls_back_to_zero(tmp_path: Path) -> None:
    """A reset arm whose RHS isn't a compile-time constant
    (``q <= e;`` where ``e`` is a runtime input) can't be folded, so
    ``_const_int`` returns None and ``ARST_VALUE`` defaults to
    ``"0"``."""
    src = """module m (
    input  logic clk, rst_n, d, e,
    output logic q
);
    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n) q <= e;
        else        q <= d;
endmodule
"""
    module = _elaborate_full(tmp_path, src)
    adff = next(c for c in module.cells.values() if c.type == "$adff")
    assert adff.parameters["ARST_VALUE"] == "0", adff.parameters


def test_reset_arm_non_assignment_statement_is_skipped(tmp_path: Path) -> None:
    """A non-assignment statement in the reset arm (a ``$display``
    system task) is not an :class:`AssignmentExpression`, so the
    reset-value walker bails and ``ARST_VALUE`` defaults to ``"0"`` —
    while the flop itself still emits with the data-arm semantics
    intact."""
    src = """module m (
    input  logic clk, rst_n, d,
    output logic q
);
    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n) $display("in reset");
        else        q <= d;
endmodule
"""
    module = _elaborate_full(tmp_path, src)
    adff = next(c for c in module.cells.values() if c.type == "$adff")
    assert adff.parameters["ARST_VALUE"] == "0", adff.parameters
    # The data arm still wires d → D.
    d_bits = module.netnames["d"].bits
    assert adff.connections["D"] == tuple(d_bits), adff.connections


# --- procedural-block dispatch guards (lines 743-759) ----------------------


def test_plain_always_star_block_is_not_modelled(tmp_path: Path) -> None:
    """A bare ``always @(*)`` (``procedureKind == Always``, neither
    ``AlwaysFF`` nor ``AlwaysComb`` nor ``AlwaysLatch``) falls through
    the dispatch and emits no cell — the continuous-assign aliasing on
    ``b`` is what carries the signal. Pins that the unmodelled-kind
    guard doesn't crash and doesn't fabricate a flop."""
    src = """module m (
    input  logic a,
    output logic b
);
    logic y;
    always @(*) y = a;
    assign b = y;
endmodule
"""
    cells = _elaborate_inline(tmp_path, src)
    assert "$dff" not in cells.values()
    assert "$adff" not in cells.values()
    assert "$dlatch" not in cells.values()


def test_always_latch_emits_dlatch(tmp_path: Path) -> None:
    """An ``always_latch`` with the canonical single-arm
    ``if (en) q = d;`` shape lowers to a ``$dlatch`` (active-high EN,
    1-bit WIDTH). Exercises the ``AlwaysLatch`` dispatch leg of
    ``_emit_procedural_block``."""
    src = """module m (
    input  logic en, d,
    output logic q
);
    always_latch
        if (en) q = d;
endmodule
"""
    module = _elaborate_full(tmp_path, src)
    latch = next(c for c in module.cells.values() if c.type == "$dlatch")
    assert latch.parameters["EN_POLARITY"] == "1", latch.parameters
    assert latch.parameters["WIDTH"] == "0" * 31 + "1", latch.parameters
    assert set(latch.connections) == {"EN", "D", "Q"}


def test_always_ff_without_else_emits_no_flop(tmp_path: Path) -> None:
    """A reset-style ``if (!rst_n) q <= 0;`` with *no* ``else`` data
    arm has no data assignment to model, so the block drains to
    nothing — no flop is emitted. Pins the ``data_branch is None``
    early-return."""
    src = """module m (
    input  logic clk, rst_n,
    output logic q
);
    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n) q <= 1'b0;
endmodule
"""
    cells = _elaborate_inline(tmp_path, src)
    assert "$adff" not in cells.values(), cells
    assert "$dff" not in cells.values(), cells


# --- child-instance port collection (lines 662-707) ------------------------


def test_unconnected_child_output_port_allocates_own_net(tmp_path: Path) -> None:
    """A child instance with its output port left ``.q()`` (unconnected)
    must still flatten: the child's flop allocates its own bits rather
    than aliasing to a parent net, and the child port's netname appears
    under the ``u_c.`` dotted prefix. Exercises the
    ``EmptyArgumentExpression`` / ``None``-expression port-connection
    branches."""
    src = """module child (
    input  logic clk,
    input  logic d,
    output logic q
);
    always_ff @(posedge clk) q <= d;
endmodule

module m (
    input  logic clk,
    input  logic d,
    output logic top_q
);
    child u_c (.clk(clk), .d(d), .q());
    always_ff @(posedge clk) top_q <= d;
endmodule
"""
    module = _elaborate_full(tmp_path, src)
    # Both flops still flatten in.
    flop_types = sorted(
        c.type for c in module.cells.values() if c.type in {"$dff", "$adff"}
    )
    assert flop_types == ["$dff", "$dff"], flop_types
    # The unconnected child port still gets a dotted netname entry.
    assert "u_c.q" in module.netnames, sorted(module.netnames)


# --- inline net initializer (lines 633-660) --------------------------------


def test_inline_wire_initializer_emits_comb_cell(tmp_path: Path) -> None:
    """``wire s1n = ~s1;`` between two flops lowers via the
    :class:`NetSymbol` initializer path to a real ``$not`` cell whose
    Y bits alias the wire — same shape the explicit ``assign s1n =
    ~s1;`` form produces. Pins the net-initializer dispatch leg."""
    src = """module m (
    input  logic clk, a,
    output logic q
);
    logic s1;
    always_ff @(posedge clk) s1 <= a;
    wire s1n = ~s1;
    always_ff @(posedge clk) q <= s1n;
endmodule
"""
    module = _elaborate_full(tmp_path, src)
    not_cells = [c for c in module.cells.values() if c.type == "$not"]
    assert len(not_cells) == 1, [c.type for c in module.cells.values()]
    not_cell = not_cells[0]
    flops = [c for c in module.cells.values() if c.type == "$dff"]
    assert len(flops) == 2, [c.type for c in module.cells.values()]
    by_q = {f.connections["Q"]: f for f in flops}
    # The $not bridges the two flops: its A reads the first flop's Q
    # (s1), its Y drives the second flop's D. Without the net-initializer
    # lowering the second flop's D would be driven by nothing.
    src_q = module.netnames["s1"].bits
    assert not_cell.connections["A"] == tuple(src_q), (
        not_cell.connections,
        src_q,
    )
    dst_flop = next(f for q, f in by_q.items() if q != tuple(src_q))
    assert dst_flop.connections["D"] == not_cell.connections["Y"], (
        dst_flop.connections,
        not_cell.connections,
    )
