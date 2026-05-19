"""Unit tests for CDC-010's control-pin classifier (#135).

Exercises the three resolution paths in ``_control_pins_for``:

1. Explicit map — Yosys higher-level cells (phase 1) and gate-level
   mux family (phase 3).
2. Prefix path — Yosys gate-level latch / enable-flop families
   (``$_DLATCH*``, ``$_DFFE_*``, ``$_SDFFE_*``).
3. Heuristic fallback — case-insensitive pin-name match against
   ``{E, EN, CE, GATE, SE}``, gated by the ``use_heuristic`` kwarg.

Constructs ``Cell`` objects directly rather than going through a
fixture build, so each path is testable in isolation — the
fixture-level tests cover the end-to-end behaviour.
"""

from __future__ import annotations

from rtl_buddy_cdc.netlist import Cell
from rtl_buddy_cdc.rules import _control_pins_for


def _cell(cell_type: str, pins: dict[str, tuple[int, ...]]) -> Cell:
    return Cell(name="u", type=cell_type, connections=dict(pins))


# --- Explicit-map path -----------------------------------------------------


def test_phase1_yosys_higher_level_cells() -> None:
    assert _control_pins_for(_cell("$mux", {"S": (1,)})) == frozenset({"S"})
    assert _control_pins_for(_cell("$dffe", {"EN": (1,)})) == frozenset({"EN"})
    assert _control_pins_for(_cell("$dlatch", {"EN": (1,)})) == frozenset({"EN"})


def test_phase3_gate_level_mux_family() -> None:
    assert _control_pins_for(_cell("$_MUX_", {"S": (1,)})) == frozenset({"S"})
    assert _control_pins_for(_cell("$_MUX4_", {"S": (1,), "T": (2,)})) == frozenset(
        {"S", "T"}
    )
    assert _control_pins_for(
        _cell("$_MUX8_", {"S": (1,), "T": (2,), "U": (3,)})
    ) == frozenset({"S", "T", "U"})
    assert _control_pins_for(
        _cell("$_MUX16_", {"S": (1,), "T": (2,), "U": (3,), "V": (4,)})
    ) == frozenset({"S", "T", "U", "V"})


# --- Prefix path: $_DLATCH* / $_DFFE_* / $_SDFFE_* -------------------------


def test_dlatch_prefix_paths() -> None:
    """Every ``$_DLATCH*`` variant carries its enable on ``E`` — covered
    by a prefix path instead of enumerating per polarity / SR variant."""
    for cell_type in ("$_DLATCH_P_", "$_DLATCH_N_", "$_DLATCHSR_PPP_"):
        assert _control_pins_for(_cell(cell_type, {"E": (1,)})) == frozenset({"E"})


def test_dffe_and_sdffe_prefix_paths() -> None:
    """The polarity-/reset-shape variant explosion is covered by the
    ``$_DFFE_`` / ``$_SDFFE_`` prefix paths rather than enumerated."""
    for cell_type in (
        "$_DFFE_PP_",
        "$_DFFE_NN_",
        "$_DFFE_PP0P_",
        "$_DFFE_NN1N_",
        "$_SDFFE_PP0P_",
    ):
        assert _control_pins_for(_cell(cell_type, {"E": (1,)})) == frozenset({"E"})


# --- Heuristic fallback ----------------------------------------------------


def test_heuristic_matches_named_enable_pins() -> None:
    """An unknown library cell type with an input pin in the heuristic
    set is classified as control. Case-insensitive."""
    for pin in ("E", "EN", "CE", "GATE", "SE"):
        c = _cell("\\MY_VENDOR_ICG", {pin: (1,), "CK": (2,)})
        assert _control_pins_for(c) == frozenset({pin})

    # Lower-case + mixed-case pins must also match.
    for pin in ("en", "Ce", "gate", "Se"):
        c = _cell("\\MY_VENDOR_ICG", {pin: (1,)})
        assert _control_pins_for(c) == frozenset({pin})


def test_heuristic_multiple_pin_match() -> None:
    """An ICG with both ``EN`` (functional enable) and ``SE`` (scan
    enable) wired up — both are control pins by the heuristic, so a
    foreign-domain source on either fires the rule."""
    c = _cell("\\MY_VENDOR_ICG", {"EN": (1,), "SE": (2,), "CK": (3,), "Q": (4,)})
    assert _control_pins_for(c) == frozenset({"EN", "SE"})


def test_heuristic_ignores_unconnected_pins() -> None:
    """A pin present in connections but empty (no bits) doesn't count
    — same posture as the rule body's ``ctrl_bits`` short-circuit."""
    c = _cell("\\MY_VENDOR_ICG", {"EN": (), "CK": (1,)})
    assert _control_pins_for(c) == frozenset()


def test_heuristic_ignores_output_pins() -> None:
    """A library cell with a confusingly-named output (e.g. ``Q``) is
    not a control pin — outputs are skipped."""
    c = _cell("\\MY_LATCH", {"Q": (1,), "D": (2,), "CK": (3,)})
    assert _control_pins_for(c) == frozenset()


def test_heuristic_skips_mux_like_names() -> None:
    """Mux-style selects (``S``, ``SEL``, numbered variants) are
    intentionally *not* in the heuristic set — they collide with too
    many non-control pins on unrelated cells. Mux shapes have to be
    in the explicit map."""
    c = _cell("\\MY_VENDOR_MUX", {"S": (1,), "A": (2,), "B": (3,)})
    assert _control_pins_for(c) == frozenset()


def test_heuristic_silent_when_disabled() -> None:
    """``use_heuristic=False`` suppresses the heuristic; cells not in
    the explicit map / prefix paths get no control pins. Mirrors the
    behaviour ``--cdc-010-no-heuristic`` produces at the CLI."""
    c = _cell("\\MY_VENDOR_ICG", {"EN": (1,), "CK": (2,)})
    assert _control_pins_for(c, use_heuristic=False) == frozenset()


def test_explicit_map_overrides_heuristic_disable() -> None:
    """``use_heuristic=False`` only gates the heuristic — explicit-map
    entries (``$dffe.EN`` etc.) still fire."""
    c = _cell("$dffe", {"EN": (1,), "CLK": (2,), "D": (3,), "Q": (4,)})
    assert _control_pins_for(c, use_heuristic=False) == frozenset({"EN"})


def test_unknown_cell_no_match_no_pins() -> None:
    """A cell type outside the map / prefix paths whose pins don't
    match the heuristic set returns an empty set. Buffers, inverters,
    AND-tree gates etc. all fall into this case and the rule's outer
    loop short-circuits on them."""
    c = _cell("\\NAND2_X1", {"A": (1,), "B": (2,), "ZN": (3,)})
    assert _control_pins_for(c) == frozenset()


# --- End-to-end integration: heuristic path fires through run_all ----------


def test_heuristic_fires_through_run_all_and_silences_with_opt_out() -> None:
    """Drive a tiny synthetic netlist through ``run_all`` with a
    vendor-style ICG that's *only* recognised via the heuristic. The
    rule must fire by default and stay silent when the opt-out is on
    — proves the ``use_heuristic`` flag is plumbed end-to-end and
    isn't accidentally bypassed inside ``check_cdc_010``.
    """
    from rtl_buddy_cdc.netlist import Module, Port
    from rtl_buddy_cdc.rules import run_all
    from rtl_buddy_cdc.sdc import Clock, ClockSpec

    # Bit IDs: ck0=2, ck1=3, en_q=4, ck_out=5, d_in=6, q_out=7
    ports = {
        "ck0": Port(name="ck0", direction="input", bits=(2,)),
        "ck1": Port(name="ck1", direction="input", bits=(3,)),
        "d_in": Port(name="d_in", direction="input", bits=(6,)),
        "q_out": Port(name="q_out", direction="output", bits=(7,)),
    }
    cells = {
        # ck1-domain enable source flop.
        "en_flop": Cell(
            name="en_flop",
            type="$dff",
            connections={"CLK": (3,), "D": (6,), "Q": (4,)},
        ),
        # Vendor ICG — cell type isn't in the explicit map and
        # doesn't match a prefix path, so only the heuristic on
        # the ``EN`` pin name can classify the control.
        "u_icg": Cell(
            name="u_icg",
            type="\\ACME_CKGATE_X1",
            connections={"CK": (2,), "EN": (4,), "Q": (5,)},
        ),
        # Downstream flop runs on the gated clock.
        "out_flop": Cell(
            name="out_flop",
            type="$dff",
            connections={"CLK": (5,), "D": (6,), "Q": (7,)},
        ),
    }
    module = Module(name="acme_heuristic_icg", ports=ports, cells=cells, netnames={})

    spec = ClockSpec()
    spec.clocks["ck0"] = Clock(name="ck0", period=10.0, ports=("ck0",))
    spec.clocks["ck1"] = Clock(name="ck1", period=13.3, ports=("ck1",))
    spec.async_groups.append([{"ck0"}, {"ck1"}])

    on = [
        v
        for v in run_all(module, [], spec, cdc_010_heuristic=True)
        if v.rule_id == "CDC-010"
    ]
    off = [
        v
        for v in run_all(module, [], spec, cdc_010_heuristic=False)
        if v.rule_id == "CDC-010"
    ]

    assert len(on) == 1 and on[0].cell_name == "u_icg"
    assert "EN" in on[0].message
    assert off == []
