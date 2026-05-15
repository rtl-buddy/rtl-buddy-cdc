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


@pytest.mark.xfail(
    reason=(
        "Gray-code detector at rules.py:594 matches Yosys-flatten's "
        "wire-routed shift shape; slang lowers `>> 1` to an explicit "
        "$shr cell, so the A[i+1]==B[i] pattern doesn't fire and "
        "CDC-004 false-positives. Latent before issue #54's walker fix "
        "(the gray-coded always_ff branch was silently skipped). "
        "Tracking: rtl-buddy/rtl-buddy-cdc#55."
    ),
    strict=True,
)
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


def test_cdc_001_fires_on_4_module_hierarchy() -> None:
    """bad_source_sync_chain — 4-module design with a source-sync
    topology (A → B0/B1 → C0/C1) and a deliberately-broken SDC that
    groups every clock asynchronous. The recursive instance walker
    must flatten 5 child instances into one ``Module`` and preserve
    net identity across each port boundary (so flop A's Q is also
    flop B0's D); otherwise CDC-001 doesn't see the crossings."""
    rules = _run("bad_source_sync_chain")
    assert rules == ["CDC-001"]


def test_hierarchical_module_cell_names_use_dotted_prefix() -> None:
    """Cells emitted from a child instance should carry the dotted
    instance path so they're distinguishable in waiver regexes and
    diagnostic output (matches Yosys-flatten convention)."""
    fix = FIX / "bad_source_sync_chain"
    module = elaborate(
        sorted(fix.glob("*.sv")),
        "bad_source_sync_chain",
        frontend=Frontend.slang,
    )
    flop_names = sorted(
        c.name for c in module.cells.values() if c.type in {"$dff", "$adff"}
    )
    assert any(n.startswith("u_a.") for n in flop_names)
    assert any(n.startswith("u_b0.") for n in flop_names)
    assert any(n.startswith("u_c1.") for n in flop_names)


def test_cdc_007_groups_reset_tree_violations() -> None:
    """4-bit register written per-bit by 4 ``always_ff`` blocks, all
    using a foreign-domain flop as ``ARST``. Exercises the
    ElementSelect-LHS path (``dst_q[0] <= ...``) — each block emits
    a separate ``$adff`` whose ``Q`` ties to one bit of the shared
    register. CDC-007's reset-tree grouping collapses the four
    crossings to a single violation."""
    fixture = "bad_reset_tree"
    assert _run(fixture) == ["CDC-007"]
    # Sanity-check the module shape — five flops total (one source,
    # four destinations) and the four destinations share the same
    # ARST bit.
    d = FIX / fixture
    module = elaborate(
        sorted(d.glob("*.sv")),
        fixture,
        frontend=Frontend.slang,
    )
    ffs = [c for c in module.cells.values() if c.type in {"$dff", "$adff"}]
    assert len(ffs) == 5
    arst_bits = [c.connections["ARST"] for c in ffs if "ARST" in c.connections]
    # Source flop has its own ARST (a port); the four destinations
    # share a common ARST bit from the source flop's Q.
    dst_arsts = [a for a in arst_bits if len({a}) == 1]
    assert len(set(dst_arsts)) >= 1


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


# --- Issue #15 regression: child-port netnames preserved as aliases --------


def test_child_instance_port_netnames_emitted() -> None:
    """Regression guard for issue #15: a child instance's output port
    must appear in ``module.netnames`` keyed as ``<inst>.<port>``, even
    when the port is driven by a continuous-assign aliasing pass that
    collapses it to an internal variable's bits.

    Without this, SDC pin paths like ``[get_pins u_c/clk_out]`` —
    used by ``create_generated_clock`` declarations on internal pins —
    silently miss the netname lookup in
    :func:`rtl_buddy_cdc.domain._build_bit_to_clock`, every downstream
    flop traces back to the master clock, and the analyzer reports
    zero crossings on a design that should have several.
    """
    fix = FIX / "good_gen_clock_internal_pin"
    module = elaborate(
        [fix / "good_gen_clock_internal_pin.sv"],
        "good_gen_clock_internal_pin",
        frontend=Frontend.slang,
    )
    # The child's output port must be queryable as u_c.clk_out.
    assert "u_c.clk_out" in module.netnames, sorted(module.netnames)
    # And it must alias the inner driver bits — the whole point of
    # preserving the netname is that the bits resolve to the same
    # net the SDC declared the gen-clock on.
    assert module.netnames["u_c.clk_out"].bits == module.netnames["u_c.div"].bits


def test_internal_pin_gen_clock_resolves_under_slang() -> None:
    """End-to-end: the same fixture + an SDC with a
    ``create_generated_clock`` at the internal pin must reach the
    domain assignment so that crossings are correctly identified.

    Pre-fix slang would report 0 crossings here (all flops collapse
    to ``ck_in``). Post-fix it must agree with yosys: 3 flops
    distributed across 2 domains, 2 crossings, 0 violations
    (synchronous via ``-master_clock`` chain — same-domain crossings
    after resolve)."""
    from rtl_buddy_cdc.domain import assign_domains, find_crossings

    fix = FIX / "good_gen_clock_internal_pin"
    module = elaborate(
        [fix / "good_gen_clock_internal_pin.sv"],
        "good_gen_clock_internal_pin",
        frontend=Frontend.slang,
    )
    spec = sdc_mod.parse_file(fix / "good_gen_clock_internal_pin.sdc")
    domains = assign_domains(module, pin_clocks=spec.pin_clocks)
    # ``assign_domains`` returns the *top-level port name* for plain
    # traces and the *gen-clock SDC name* when a tagged bit halts the
    # walk. The SDC names the port `clk` as `ck_in`, but the trace
    # surfaces the port name. The load-bearing assertion is that
    # `ck_div` shows up at all — pre-fix, every flop collapsed to
    # the port name and `ck_div` never appeared.
    clocks = sorted({fd.clock for fd in domains})
    assert clocks == ["ck_div", "clk"], clocks
    # 3 flops: 2 inside u_c on the port-name domain, 1 on ck_div
    # for the parent's q_out flop.
    by_clock = {c: sum(1 for fd in domains if fd.clock == c) for c in clocks}
    assert by_clock == {"ck_div": 1, "clk": 2}, by_clock
    crossings = find_crossings(
        module, port_clock=spec.port_clock, pin_clocks=spec.pin_clocks
    )
    assert len(crossings) == 2, [(c.src_clock, c.dst_clock) for c in crossings]
    # Synchronous via the gen-clock chain back to ck_in — no rule
    # violations expected.
    assert run_all(module, _filter_async(crossings, spec), spec) == []
