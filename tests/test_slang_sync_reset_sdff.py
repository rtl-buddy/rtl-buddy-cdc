"""Coverage tests for slang-frontend sync-reset cell emission.

Issue: rtl-buddy-cdc#86 — after PR #74 added sync-reset shape
detection (``_classify_reset_check`` with the constant-only-ifTrue
heuristic), sync-reset bodies — ``always_ff @(posedge clk) begin if
(!rst_n) <constants> else <data> end`` — walk only the ifFalse arm
but still emit ``$adff`` cells wired to the reset signal, with
``ARST_POLARITY`` taken from the event list (which never saw the
reset). That's two bugs:

1. The cell type lies — synthesizers materialise ``$sdff`` for this
   shape; emitting ``$adff`` makes the slang output diverge from
   Yosys-flatten in cell-type-sensitive consumers (SARIF and JSON
   round-trip cell types verbatim).
2. The polarity comes from the event list which never carried an
   async-reset event, so it defaults to "1" (active high) — wrong
   for the canonical ``if (!rst_n)`` body.

The tests pin the expected ``$sdff`` shape: ``SRST`` wired to the
reset signal, ``SRST_POLARITY`` derived from the if-condition's
shape (``!rst_n`` → "0", bare ``rst`` → "1"), and ``SRST_VALUE``
matching the constant the reset arm assigned. Async and no-reset
paths are guarded as regressions.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from rtl_buddy_cdc.frontend import Frontend, elaborate

PYSLANG_INSTALLED = importlib.util.find_spec("pyslang") is not None
if not PYSLANG_INSTALLED:
    pytest.skip(
        "pyslang not installed — slang sync-reset tests are gated on it",
        allow_module_level=True,
    )

FLOP_TYPES = frozenset({"$dff", "$adff", "$dffe", "$adffe", "$sdff", "$sdffe"})


def _elaborate(tmp_path: Path, src: str, top: str = "m"):
    sv = tmp_path / f"{top}.sv"
    sv.write_text(src)
    return elaborate([sv], top, frontend=Frontend.slang)


def _flops(module) -> list:
    return [c for c in module.cells.values() if c.type in FLOP_TYPES]


# --- sync reset, active low ------------------------------------------------


def test_sync_reset_active_low_zero_emits_sdff(tmp_path: Path) -> None:
    """``always_ff @(posedge clk) if (!rst_n) q <= 1'b0; else q <= d;``
    must emit a ``$sdff`` with the reset signal on ``SRST``,
    ``SRST_POLARITY=0`` (active low), ``SRST_VALUE`` matching the
    flop's width and all-zero, no ``ARST`` connection."""
    src = """
    module m (input logic clk, rst_n, d, output logic q);
        always_ff @(posedge clk) begin
            if (!rst_n) q <= 1'b0;
            else        q <= d;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    flops = _flops(mod)
    assert len(flops) == 1, f"expected one flop; got {[c.type for c in flops]}"
    flop = flops[0]
    assert flop.type == "$sdff", (
        f"expected $sdff for sync-reset shape; got {flop.type}. "
        "$adff would be wrong (no event-list async-reset event); "
        "$dff would drop the reset value entirely."
    )
    assert "SRST" in flop.connections, (
        f"$sdff must wire the sync-reset signal onto SRST; got "
        f"connections={list(flop.connections.keys())}"
    )
    rst_bit = mod.ports["rst_n"].bits[0]
    assert flop.connections["SRST"][0] == rst_bit
    assert "ARST" not in flop.connections, (
        "sync-reset $sdff must not carry ARST; that would imply async semantics"
    )
    # SRST_POLARITY uses Yosys' 32-bit binary-string convention; "0"
    # means active-low. SRST_VALUE width matches the flop's data width.
    assert flop.parameters.get("SRST_POLARITY", "").endswith("0"), (
        f"SRST_POLARITY should be active-low ('...0'); got "
        f"{flop.parameters.get('SRST_POLARITY')!r}"
    )
    width = len(flop.connections["Q"])
    srst_value = flop.parameters.get("SRST_VALUE")
    assert srst_value is not None, "SRST_VALUE missing"
    assert len(srst_value) == width, (
        f"SRST_VALUE width ({len(srst_value)}) must match flop width ({width})"
    )
    assert set(srst_value) == {"0"}, (
        f"reset value 0 → all-zero bits; got {srst_value!r}"
    )


def test_sync_reset_with_nonzero_value(tmp_path: Path) -> None:
    """``if (!rst_n) q <= 4'hA;`` — ``SRST_VALUE`` should encode the
    constant via :func:`_param_bits` (MSB-first binary, length matches
    the flop width). 4'hA = decimal 10 = ``"1010"``. Mirrors the
    convention ``$adff``'s ``ARST_VALUE`` uses on the async path."""
    src = """
    module m (input logic clk, rst_n, input logic [3:0] d, output logic [3:0] q);
        always_ff @(posedge clk) begin
            if (!rst_n) q <= 4'hA;
            else        q <= d;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    flops = _flops(mod)
    assert len(flops) == 1
    flop = flops[0]
    assert flop.type == "$sdff"
    srst_value = flop.parameters.get("SRST_VALUE", "")
    assert len(srst_value) == 4
    assert srst_value == "1010", (
        f"4'hA MSB-first → '1010'; got {srst_value!r}. If this fails, the "
        "encoder convention may have shifted — cross-check $adff ARST_VALUE "
        "on the async-reset path."
    )


def test_sync_reset_active_high(tmp_path: Path) -> None:
    """Bare ``if (rst)`` condition → active-high reset. ``SRST_POLARITY``
    should end in "1"."""
    src = """
    module m (input logic clk, rst, d, output logic q);
        always_ff @(posedge clk) begin
            if (rst) q <= 1'b0;
            else     q <= d;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    flops = _flops(mod)
    assert len(flops) == 1
    flop = flops[0]
    assert flop.type == "$sdff"
    assert flop.parameters.get("SRST_POLARITY", "").endswith("1"), (
        f"bare ``if (rst)`` → active-high; got SRST_POLARITY="
        f"{flop.parameters.get('SRST_POLARITY')!r}"
    )


# --- regression: async reset still emits $adff ----------------------------


def test_async_reset_still_adff(tmp_path: Path) -> None:
    """The ``or negedge rst_n`` async-reset shape must still emit
    ``$adff`` with the ARST family of parameters — adding sync-reset
    support must not regress the canonical async path."""
    src = """
    module m (input logic clk, rst_n, d, output logic q);
        always_ff @(posedge clk or negedge rst_n) begin
            if (!rst_n) q <= 1'b0;
            else        q <= d;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    flops = _flops(mod)
    assert len(flops) == 1
    flop = flops[0]
    assert flop.type == "$adff", (
        f"async reset (event-list) should keep emitting $adff; got {flop.type}"
    )
    assert "ARST" in flop.connections
    assert "SRST" not in flop.connections
    assert "ARST_VALUE" in flop.parameters
    assert "SRST_VALUE" not in flop.parameters


# --- regression: no-reset $dff path unchanged -----------------------------


def test_no_reset_still_dff(tmp_path: Path) -> None:
    """``always_ff @(posedge clk) q <= d;`` (no reset at all) must
    keep emitting a plain ``$dff`` — no SRST/ARST wiring, no reset
    parameters."""
    src = """
    module m (input logic clk, d, output logic q);
        always_ff @(posedge clk) q <= d;
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    flops = _flops(mod)
    assert len(flops) == 1
    flop = flops[0]
    assert flop.type == "$dff"
    assert "ARST" not in flop.connections
    assert "SRST" not in flop.connections
    assert "ARST_VALUE" not in flop.parameters
    assert "SRST_VALUE" not in flop.parameters
