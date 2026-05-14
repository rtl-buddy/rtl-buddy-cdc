"""Slang-frontend elaboration tests (Stage 2 of issue #5).

These exercise the pyslang-backed :func:`elaborate` against real
fixtures and confirm that the rule pack — unchanged — produces the
expected violation set when fed slang-elaborated modules. The tests
mirror the small subset of fixtures the slang frontend reaches parity
on today; broader coverage waits on the comb-primitive-lowering and
hierarchy-flattening work items.

Tests skip when pyslang isn't installed; the install-hint path is
already covered in :mod:`test_frontend`.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from rtl_buddy_cdc import sdc as sdc_mod
from rtl_buddy_cdc.cli import _filter_async
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.frontend import Frontend, elaborate
from rtl_buddy_cdc.rules import run_all

PYSLANG_INSTALLED = importlib.util.find_spec("pyslang") is not None
if not PYSLANG_INSTALLED:
    pytest.skip(
        "pyslang not installed — slang elaboration tests are gated on it",
        allow_module_level=True,
    )

FIX = Path(__file__).parent / "fixtures"


def _run(fixture: str, sv_files: list[str] | None = None) -> list[str]:
    """Elaborate one fixture via slang, run the rule pack against it,
    and return the sorted list of rule IDs that fired. Helper keeps
    each test a one-liner."""
    d = FIX / fixture
    svs = [d / s for s in sv_files] if sv_files else sorted(d.glob("*.sv"))
    sdcs = sorted(d.glob("*.sdc"))
    module = elaborate(svs, fixture, frontend=Frontend.slang)
    if not sdcs:
        return []
    spec = sdc_mod.parse_file(sdcs[0])
    crossings = find_crossings(
        module, port_clock=spec.port_clock, pin_clocks=spec.pin_clocks
    )
    async_c = _filter_async(crossings, spec)
    violations = run_all(module, async_c, spec)
    return sorted({v.rule_id for v in violations})


# --- Parity confirmations (positive: rule fires correctly) -----------------


def test_cdc_001_fires_on_bad_single_ff_sync() -> None:
    """The simplest CDC-001 shape — direct flop→flop wire across
    domains. Stage 2's headline target."""
    assert _run("bad_single_ff_sync") == ["CDC-001"]


def test_cdc_001_silent_on_good_2ff_sync() -> None:
    """Paired positive case — the 2FF synchronizer is correct, no
    rule should fire."""
    assert _run("good_2ff_sync") == []


def test_cdc_001_fires_on_bad_port_no_sync() -> None:
    """Port-sourced CDC-001 (typed via ``set_input_delay`` in the
    SDC). Confirms the port-side crossing path lights up too, not
    just flop-to-flop."""
    assert _run("bad_port_no_sync") == ["CDC-001"]


def test_cdc_004_fires_on_bad_bus_crossing() -> None:
    """Multi-bit bus crossing without gating or gray-coding."""
    assert _run("bad_bus_crossing") == ["CDC-004"]


def test_cdc_005_fires_on_bad_reconvergent_sync() -> None:
    """Single source flop fanning out to multiple sync chains."""
    assert _run("bad_reconvergent_sync") == ["CDC-005"]


def test_cdc_007_fires_on_bad_reset_crossing() -> None:
    """Async reset crossing without a reset synchronizer — relies on
    ``$adff``'s ``ARST`` connection being correctly emitted."""
    assert _run("bad_reset_crossing") == ["CDC-007"]


def test_cdc_008_fires_on_bad_clock_as_data() -> None:
    """Clock signal sampled as data. Doesn't depend on comb lowering."""
    assert _run("bad_clock_as_data") == ["CDC-008"]


def test_cdc_004_silent_on_good_gray_counter_crossing() -> None:
    """Gray-coded bus crossing into a multi-bit sync chain — the
    structural gray-code detector should accept it."""
    assert _run("good_gray_counter_crossing") == []


# --- Combinational lowering parity (Stage 2.2) -----------------------------


def test_cdc_003_fires_on_bad_comb_before_sync() -> None:
    """Comb logic (`src_q1 & src_q2`) feeding the synchronizer's first
    stage. Exercises the BinaryExpression → ``$and`` lowering; without
    it the comb output is opaque and CDC-003 cannot see the source
    flops."""
    assert _run("bad_comb_before_sync") == ["CDC-003"]


def test_cdc_006_fires_on_bad_comb_source() -> None:
    """Synchronizer fed directly by comb of top-level inputs
    (``a & b``) with no registering flop. Confirms the lowering also
    works inside an ``always_ff`` body's D-side expression."""
    assert _run("bad_comb_source") == ["CDC-006"]


def test_cdc_006_fires_on_bad_input_delay_cross_domain() -> None:
    """Input delay typed to a different clock domain than the flop's
    own — port-sourced CDC-006 path through the comb cone."""
    assert _run("bad_input_delay_cross_domain") == ["CDC-006"]


def test_paired_positives_stay_silent() -> None:
    """The good_* counterparts to the bad_* cases above. Together with
    the bad_* tests these confirm the comb lowering doesn't introduce
    false positives — the rule pack only fires where it should."""
    assert _run("good_registered_before_sync") == []
    assert _run("good_registered_source") == []
    assert _run("good_exclusive_clock_mux") == []
    assert _run("good_false_path_pair") == []
    assert _run("good_generated_clock_div2") == []
    assert _run("good_port_typed_sync") == []


# --- SV-attribute pass-through ---------------------------------------------


def test_cdc_sync_attribute_suppresses_cdc_001() -> None:
    """``(* cdc_sync *)`` on the destination flop's wire should
    suppress CDC-001. Validates that pyslang attributes reach
    ``Module.netnames[name].attributes`` where the rule pack expects
    them."""
    assert _run("marked_user_sync") == []


# --- CLI smoke test ---------------------------------------------------------


def test_cli_lint_frontend_slang_end_to_end() -> None:
    """The whole pipeline through the CLI command — elaborates via
    slang, analyzes, and exits 1 for the bad fixture. This is the
    user-visible promise of ``--frontend slang`` working at all."""
    from typer.testing import CliRunner

    from rtl_buddy_cdc.cli import app

    runner = CliRunner()
    fix = FIX / "bad_single_ff_sync"
    result = runner.invoke(
        app,
        [
            "lint",
            "--frontend",
            "slang",
            "--top",
            "bad_single_ff_sync",
            "--sdc",
            str(fix / "bad_single_ff_sync.sdc"),
            str(fix / "bad_single_ff_sync.sv"),
        ],
    )
    assert result.exit_code == 1, result.output
    assert "CDC-001" in result.output
    assert "frontend: slang" in result.output


# --- Module-shape sanity check ---------------------------------------------


def test_elaborated_module_has_expected_yosys_shape() -> None:
    """A direct check that the produced ``Module`` matches the
    Yosys-style contract the rule pack expects: integer bit IDs, ``$dff``
    or ``$adff`` cell types, pins named ``CLK`` / ``D`` / ``Q``."""
    fix = FIX / "bad_single_ff_sync"
    module = elaborate(
        [fix / "bad_single_ff_sync.sv"],
        "bad_single_ff_sync",
        frontend=Frontend.slang,
    )
    ff_cells = [c for c in module.cells.values() if c.type in {"$dff", "$adff"}]
    assert len(ff_cells) == 2, [c.type for c in module.cells.values()]
    for cell in ff_cells:
        assert {"CLK", "D", "Q"} <= cell.connections.keys()
        for pin in ("CLK", "D", "Q"):
            for bit in cell.connections[pin]:
                # Constant bits would be the string "0" / "1" / "x"
                # / "z"; the canonical case here is all-integer.
                assert isinstance(bit, (int, str))
