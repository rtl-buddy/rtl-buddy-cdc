"""Coverage tests for slang-frontend constant-shift folding.

Issue: rtl-buddy-cdc#55 — the slang frontend emits ``LogicalShiftRight``
and ``LogicalShiftLeft`` as explicit ``$shr`` / ``$shl`` cells, even
when the shift amount is a compile-time constant. Yosys-flatten
collapses constant shifts into wire-routing, and the rule pack's
gray-code detector (``rules.py:_is_gray_encoded_source``) leans on
that shape: ``B = bin_next >> 1`` is expected as a wire-rerouting
``(A[1], A[2], ..., A[N-1], '0')``.

When slang emits an actual ``$shr`` cell, the ``A`` and ``B`` of the
downstream ``$xor`` no longer satisfy the ``A[i+1] == B[i]``
signature, so the detector misses the gray encoding and CDC-004
false-positives on every gray-counter crossing. Tiny-NPU's
``ip_dtnpu_credit_cdc`` is the production example.

Tests below pin the contract: constant-shift expressions resolve to
wire-routed bit tuples, not separate ``$shr`` / ``$shl`` cells.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from rtl_buddy_cdc.frontend import Frontend, elaborate

PYSLANG_INSTALLED = importlib.util.find_spec("pyslang") is not None
if not PYSLANG_INSTALLED:
    pytest.skip(
        "pyslang not installed — slang shift-fold tests are gated on it",
        allow_module_level=True,
    )


def _elaborate(tmp_path: Path, src: str, top: str = "m"):
    sv = tmp_path / f"{top}.sv"
    sv.write_text(src)
    return elaborate([sv], top, frontend=Frontend.slang)


# --- constant-shift folding -----------------------------------------------


def test_logical_shift_right_constant_folds_to_wire_routing(
    tmp_path: Path,
) -> None:
    """``x >> 1`` with a compile-time constant amount must not emit a
    ``$shr`` cell — the result is a wire-rerouting (a[1], a[2], …,
    a[N-1], '0'). The downstream ``$xor`` that gray-encoding uses
    needs the operand bits to line up structurally with the source
    counter, which is impossible if a ``$shr`` cell sits in the way.
    """
    src = """
    module m (input logic [3:0] data, output logic [3:0] shifted);
        assign shifted = data >> 1;
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    cell_types = sorted(c.type for c in mod.cells.values())
    assert "$shr" not in cell_types, (
        f"constant-amount $shr should fold to wire-routing; "
        f"got cell types: {cell_types}"
    )


def test_logical_shift_left_constant_folds_to_wire_routing(
    tmp_path: Path,
) -> None:
    """Same contract for ``<<``. ``x << 1`` → (`'0'`, a[0], a[1], …)."""
    src = """
    module m (input logic [3:0] data, output logic [3:0] shifted);
        assign shifted = data << 1;
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    cell_types = sorted(c.type for c in mod.cells.values())
    assert "$shl" not in cell_types, (
        f"constant-amount $shl should fold to wire-routing; "
        f"got cell types: {cell_types}"
    )


def test_runtime_shift_amount_still_emits_cell(tmp_path: Path) -> None:
    """Defensive: a shift by a runtime signal can't be folded. The
    walker must still emit ``$shr`` / ``$shl`` for that case so the
    netlist is well-formed (just opaque to the gray-code detector,
    which is fine — runtime-shifted bus crossings aren't gray
    counters)."""
    src = """
    module m (
        input  logic [3:0] data,
        input  logic [1:0] amt,
        output logic [3:0] shifted
    );
        assign shifted = data >> amt;
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    cell_types = sorted(c.type for c in mod.cells.values())
    assert "$shr" in cell_types, (
        f"runtime shift must remain an explicit $shr cell; got: {cell_types}"
    )


# --- the production gray-encoding shape -----------------------------------


def test_gray_encoding_matches_rule_pack_shape(tmp_path: Path) -> None:
    """The canonical gray-counter shape: ``g = b ^ (b >> 1)``. After
    folding, the ``$xor`` cell's ``A`` and ``B`` operands satisfy
    ``A[i+1] == B[i]`` for ``i < N-1`` and ``B[N-1]`` is a constant
    — the exact pattern ``_is_gray_encoded_source`` matches on."""
    src = """
    module m (input logic [3:0] bin, output logic [3:0] gray);
        assign gray = bin ^ (bin >> 1);
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    xor_cells = [c for c in mod.cells.values() if c.type == "$xor"]
    assert len(xor_cells) == 1, f"expected one $xor; got {len(xor_cells)}"
    xor = xor_cells[0]
    a_bits = xor.connections["A"]
    b_bits = xor.connections["B"]
    assert len(a_bits) == 4 and len(b_bits) == 4
    # The gray-encoding signature: A[i+1] == B[i] for the inner bits,
    # and B[N-1] is a constant char (Yosys uses '0' / '1' strings to
    # encode constant bits in the netlist).
    for i in range(3):
        assert a_bits[i + 1] == b_bits[i], (
            f"gray shape requires A[{i + 1}] == B[{i}]; "
            f"got A={a_bits} B={b_bits}"
        )
    assert isinstance(b_bits[3], str), (
        f"gray shape requires B[N-1] to be a constant; got B[3]={b_bits[3]!r}"
    )
