"""Coverage tests for slang-frontend enable inference on conditional
nonblocking assignments.

Issue: rtl-buddy-cdc#64 — when an ``always_ff`` body contains a
conditional load like ``if (cond) q <= rhs;``, the slang frontend
emits ``$adff(D=rhs, Q=q)`` directly, ignoring the surrounding
``cond`` enable. Yosys's ``proc_dff`` pass infers a hold-feedback mux
(``$mux(S=cond, A=q-feedback, B=rhs)``) driving D so the rule pack's
``_is_gated_bus_crossing`` can recognise handshake-protected buses.
Without the inference, every conditionally-loaded multi-bit register
fanned out from a synchronizer hits CDC-004 — and the production
``ip_cdc_handshake`` shape generates the textbook example.

The tests below pin the contract: a conditional nonblocking assign
inside an ``always_ff`` body emits a ``$mux`` whose ``S`` traces back
to the condition expression.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from rtl_buddy_cdc.frontend import Frontend, elaborate

PYSLANG_INSTALLED = importlib.util.find_spec("pyslang") is not None
if not PYSLANG_INSTALLED:
    pytest.skip(
        "pyslang not installed — slang enable-inference tests are gated on it",
        allow_module_level=True,
    )


def _elaborate(tmp_path: Path, src: str, top: str = "m"):
    sv = tmp_path / f"{top}.sv"
    sv.write_text(src)
    return elaborate([sv], top, frontend=Frontend.slang)


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


def _flop_d_driver(mod, flop_name: str):
    """Cell that drives the first D-bit of ``flop_name``."""
    cell = mod.cells[flop_name]
    d_bits = cell.connections.get("D", ())
    drivers = _build_drivers(mod)
    return drivers.get(d_bits[0]) if d_bits else None


# --- single-level enable inference ----------------------------------------


def test_simple_if_enable_emits_mux(tmp_path: Path) -> None:
    """``if (en) q <= d;`` — emit a ``$mux`` with the enable on
    ``S``, the hold-feedback (from Q) on ``A``, and the RHS on
    ``B``. Without enable inference, the dst flop's D is wired
    straight to the source-domain ``d`` port and the rule pack's
    gated-bus detector can't recognise the protection."""
    src = """
    module m (input logic clk, rst_n, en, input logic [3:0] d, output logic [3:0] q);
        always_ff @(posedge clk or negedge rst_n) begin
            if (!rst_n) q <= 4'd0;
            else if (en) q <= d;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    flops = [c for c in mod.cells.values() if c.type.startswith("$adff")]
    assert len(flops) == 1, f"expected one $adff for q; got {len(flops)}"
    flop = flops[0]
    drv = _flop_d_driver(mod, flop.name)
    assert drv is not None, "no driver found for the flop's D"
    drv_cell = mod.cells[drv[0]]
    assert drv_cell.type == "$mux", (
        f"expected $mux driving D; got {drv_cell.type}. "
        "Without enable inference, the rule pack's gated-bus detector "
        "can't recognise handshake-protected crossings."
    )
    # S input should resolve to a single bit (the en port).
    s_bits = drv_cell.connections.get("S", ())
    assert len(s_bits) == 1, f"$mux S width = {len(s_bits)}, expected 1"


def test_nested_if_anded_enables(tmp_path: Path) -> None:
    """``if (a) if (b) q <= d;`` — nested enables AND together. The
    leaf mux's S must depend on both ``a`` and ``b``."""
    src = """
    module m (input logic clk, rst_n, a, b, d, output logic q);
        always_ff @(posedge clk or negedge rst_n) begin
            if (!rst_n) q <= 1'b0;
            else if (a)
                if (b) q <= d;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    flops = [c for c in mod.cells.values() if c.type.startswith("$adff")]
    assert len(flops) == 1
    drv = _flop_d_driver(mod, flops[0].name)
    assert drv is not None
    drv_cell = mod.cells[drv[0]]
    assert drv_cell.type == "$mux", f"expected $mux driving D; got {drv_cell.type}"
    # The mux S should connect (transitively) to BOTH a and b. The
    # simplest way to express that: the mux's S fanin (one level back)
    # should land on an $and cell whose inputs reach the two port bits.
    s_drv = _build_drivers(mod).get(drv_cell.connections["S"][0])
    assert s_drv is not None, "mux S has no driver"
    s_cell = mod.cells[s_drv[0]]
    assert s_cell.type == "$and", (
        f"nested if must AND its conditions; got driver type {s_cell.type}"
    )


# If/else with both arms writing the same LHS needs deferred-emission
# (one flop per LHS with a mux tree built from the per-arm tuples) —
# out of scope for #64. Tracked as a follow-up; the simple-if case
# below covers every shape the production handshake-gated buses use.


# --- the production handshake-protected-bus shape -------------------------


def test_handshake_protected_bus_gets_mux(tmp_path: Path) -> None:
    """Mimics the ip_demo_tiny_npu DMA's `rd_addr_q` capture: a
    multi-bit register loaded only when a synchronized cmd_valid
    pulses. After this fix, the dst flop's D should be driven by a
    mux gated by cmd_valid."""
    src = """
    module m (
        input  logic       a_clk, a_rst_n,
        input  logic       cmd_valid,
        input  logic [3:0] cmd_addr,
        output logic [3:0] addr_q
    );
        always_ff @(posedge a_clk or negedge a_rst_n) begin
            if (!a_rst_n) addr_q <= 4'd0;
            else if (cmd_valid) addr_q <= cmd_addr;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    flops = [c for c in mod.cells.values() if c.type.startswith("$adff")]
    assert len(flops) == 1
    drv = _flop_d_driver(mod, flops[0].name)
    assert drv is not None
    drv_cell = mod.cells[drv[0]]
    assert drv_cell.type == "$mux", (
        f"expected $mux driving D for handshake-gated bus; got {drv_cell.type}"
    )
    # Mux S = cmd_valid (single bit), B = cmd_addr, A = current Q.
    s_bits = drv_cell.connections.get("S", ())
    a_bits = drv_cell.connections.get("A", ())
    b_bits = drv_cell.connections.get("B", ())
    assert len(s_bits) == 1
    assert len(a_bits) == 4 and len(b_bits) == 4
    # The hold-feedback shape: A must equal the flop's own Q bits.
    q_bits = tuple(flops[0].connections["Q"])
    assert tuple(a_bits) == q_bits, (
        f"hold-feedback mux must wire A back to Q; got A={a_bits} Q={q_bits}"
    )
