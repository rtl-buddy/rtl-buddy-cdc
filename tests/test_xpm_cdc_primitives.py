"""XPM CDC macro recognition (rtl-buddy-cdc#275).

Real FPGA designs synchronise with a vendor macro, not a hand-rolled
2FF chain. The Xilinx XPM CDC family is the dominant case, and its
sources live in the vendor install tree — so the analyzer sees a
bodyless, dual-clock blackbox.

Four behaviours are pinned here:

- **No false flood.** A recognised macro is summarised as a
  synchroniser rather than declined as "not provably single-clock", so
  no ``CDC-BBX`` error and no violation on the crossing it handles.
- **No new blind spot.** Each output port is stamped with the domain
  that really drives it, so a ``dest_out`` consumed in a *third* domain
  still fires CDC-001.
- **Depth stays checkable.** The macro's stage count is a parameter, not
  a walkable chain; CDC-022 reads it so ``--sync-depth`` keeps meaning
  something on an XPM design.
- **Extensible.** ``--sync-primitive`` registers a site macro, and the
  uppercase ``(* ASYNC_REG *)`` case-fold serves users who do have the
  XPM sources.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import abstract, netlist, primitives, sdc as sdc_mod
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.hierarchy import compose_boundaries
from rtl_buddy_cdc.netlist import Cell, Module, Port
from rtl_buddy_cdc.rules import check_cdc_022, user_sync_flop_names

runner = CliRunner()
FIX = Path(__file__).parent / "fixtures"


def _paths(name: str) -> tuple[Path, Path]:
    d = FIX / name
    return d / f"{name}.json", d / f"{name}.sdc"


def _run(name: str, *extra: str) -> tuple[dict, int]:
    """Run the CLI on a fixture and return ``(json_report, exit_code)``."""
    nl, sdc = _paths(name)
    res = runner.invoke(
        app, ["analyze", "-n", str(nl), "-s", str(sdc), "-f", "json", *extra]
    )
    assert res.exit_code in (0, 1), res.output
    return json.loads(res.output), res.exit_code


def _rule_ids(report: dict) -> list[str]:
    return sorted(v["rule_id"] for v in report["violations"])


def _load(name: str):
    nl, sdc = _paths(name)
    top, blackboxes = netlist.load_with_blackboxes(nl)
    spec = sdc_mod.parse_file(sdc)
    sdc_mod.synthesize_unconstrained_inputs(spec, top)
    return top, blackboxes, spec


# --------------------------------------------------------------------------
# Registry — pure lookups
# --------------------------------------------------------------------------


def test_every_xpm_cdc_module_is_recognised() -> None:
    """All seven UG974 macros resolve, and a lookalike does not."""
    for name in (
        "xpm_cdc_single",
        "xpm_cdc_array_single",
        "xpm_cdc_gray",
        "xpm_cdc_handshake",
        "xpm_cdc_pulse",
        "xpm_cdc_sync_rst",
        "xpm_cdc_async_rst",
    ):
        assert primitives.is_xpm_primitive(name), name
        assert primitives.is_sync_primitive(name), name
    # Not a CDC macro — XPM has plenty of non-CDC families (memory, fifo).
    assert not primitives.is_sync_primitive("xpm_memory_sdpram")
    assert not primitives.is_sync_primitive("my_cdc_single")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("xpm_cdc_single", "xpm_cdc_single"),
        ("\\xpm_cdc_single", "xpm_cdc_single"),
        ("$paramod\\xpm_cdc_gray\\WIDTH=4", "xpm_cdc_gray"),
        ("$paramod$deadbeef\\xpm_cdc_handshake\\DEST_SYNC_FF=2", "xpm_cdc_handshake"),
        ("$paramod", "$paramod"),
    ],
)
def test_normalise_type_strips_yosys_decoration(raw: str, expected: str) -> None:
    """Escaped ids and ``$paramod`` derivations resolve to the plain name.

    Yosys derives a parameterised module into ``$paramod\\<name>\\...``;
    without unwrapping, a design that overrides ``WIDTH`` would lose
    recognition and regress to the CDC-BBX flood.
    """
    assert primitives.normalise_type(raw) == expected


def test_paramod_derived_xpm_still_recognised() -> None:
    assert primitives.is_xpm_primitive("$paramod\\xpm_cdc_gray\\WIDTH=4")


def test_extra_registration_widens_recognition() -> None:
    """``--sync-primitive`` names join the registry, XPM-ness aside."""
    extra = frozenset({"acme_cdc_sync"})
    assert primitives.is_sync_primitive("acme_cdc_sync", extra)
    assert not primitives.is_xpm_primitive("acme_cdc_sync")
    assert not primitives.is_sync_primitive("acme_cdc_sync")


def test_port_domain_honours_src_prefix_only_for_xpm() -> None:
    """XPM's rigid ``src_``/``dest_`` naming is a promise we only make
    for XPM. A registered macro's outputs all read as destination-side —
    the conservative attribution, since nothing constrains its naming."""
    assert primitives.port_domain("xpm_cdc_handshake", "src_rcv") == "src"
    assert primitives.port_domain("xpm_cdc_handshake", "dest_out") == "dest"
    assert primitives.port_domain("xpm_cdc_gray", "dest_out_bin") == "dest"
    assert primitives.port_domain("acme_cdc_sync", "src_rcv") == "dest"


@pytest.mark.parametrize(
    "pin,role",
    [
        ("dest_clk", "dest"),
        ("DEST_CLK", "dest"),
        ("dst_clk", "dest"),
        ("src_clk", "src"),
        ("clk", None),
        ("clk_i", None),
    ],
)
def test_clock_pin_role(pin: str, role: str | None) -> None:
    assert primitives.clock_pin_role(pin) == role


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("00000000000000000000000000000100", 4),
        ("10", 2),  # Yosys bit-vector spelling wins over decimal
        ("1", 1),
        ("7", 7),
        (3, 3),
        ("", None),
        ("xxxx", None),
        ("0000000000000000000000000000010x", None),
        (True, None),
        (None, None),
        (2.5, None),
    ],
)
def test_parse_param_int(raw: object, expected: int | None) -> None:
    assert primitives.parse_param_int(raw) == expected


def test_sync_depths_uses_documented_default_when_unset() -> None:
    """Yosys records only *overridden* parameters on a cell, so an
    instantiation that takes XPM's default must still be checkable —
    otherwise ``--sync-depth 5`` would silently pass every default
    instance."""
    cell = Cell(name="u", type="xpm_cdc_single", connections={}, parameters={})
    assert primitives.sync_depths(cell) == {"DEST_SYNC_FF": 4}


def test_sync_depths_reads_both_handshake_parameters() -> None:
    cell = Cell(
        name="u",
        type="xpm_cdc_handshake",
        connections={},
        parameters={"DEST_SYNC_FF": "00000000000000000000000000000010"},
    )
    assert primitives.sync_depths(cell) == {"DEST_SYNC_FF": 2, "SRC_SYNC_FF": 4}


def test_sync_depths_skips_unparseable_and_unregistered() -> None:
    """No default is invented for a site-registered macro, and a
    parameter we can't read is skipped rather than guessed."""
    extra = frozenset({"acme_cdc_sync"})
    bare = Cell(name="u", type="acme_cdc_sync", connections={}, parameters={})
    assert primitives.sync_depths(bare, extra) == {}
    unknown = Cell(name="u", type="not_a_primitive", connections={}, parameters={})
    assert primitives.sync_depths(unknown) == {}
    junk = Cell(
        name="u",
        type="xpm_cdc_single",
        connections={},
        parameters={"DEST_SYNC_FF": "zzz"},
    )
    assert primitives.sync_depths(junk) == {}


# --------------------------------------------------------------------------
# The headline: an XPM design reports CLEAN
# --------------------------------------------------------------------------


def test_generic_summariser_would_decline_the_same_instances() -> None:
    """Pins the before-state this change fixes.

    ``summarise_subtree`` — the single-clock summariser every other
    blackbox goes through — returns ``None`` for an XPM macro, because a
    macro with ``src_clk`` + ``dest_clk`` is genuinely not single-clock.
    That decline is what produced the CDC-BBX flood. The primitive path
    is a *different* summariser, not a loosening of this one.
    """
    top, blackboxes, spec = _load("xpm_cdc_blackbox")
    for inst in ("u_flag_sync", "u_cnt_sync"):
        cell = top.cells[inst]
        assert (
            abstract.summarise_subtree(
                top, cell, blackboxes[cell.type], spec, pin_clocks=spec.pin_clocks
            )
            is None
        )


def test_xpm_design_is_clean() -> None:
    """The headline. Two XPM macros carrying the design's only two
    crossings: no CDC-BBX, no CDC-001/-002/-004, exit 0."""
    report, code = _run("xpm_cdc_blackbox")
    assert _rule_ids(report) == []
    assert code == 0
    assert report["summary"]["violations"] == 0


def test_xpm_boundary_summary_shape() -> None:
    """Each output lands at the resolved ``dest_clk`` root, marked
    synchronised, at the *instantiated* width — and no input sink is
    seeded."""
    top, blackboxes, spec = _load("xpm_cdc_blackbox")
    boundaries, stats = compose_boundaries(top, blackboxes, spec)

    assert stats.primitive_modules == {"xpm_cdc_single", "xpm_cdc_gray"}
    assert stats.declined_modules == frozenset()
    assert stats.summarised == 2

    single = boundaries["u_flag_sync"]
    assert single.clock == "clk_b"
    assert single.input_ports == {}
    out = single.ports["dest_out"]
    assert (out.src_clock, out.synchronised, out.width) == ("clk_b", True, 1)

    gray = boundaries["u_cnt_sync"]
    gray_out = gray.ports["dest_out_bin"]
    assert gray_out.src_clock == "clk_b"
    assert gray_out.synchronised is True
    # The blackbox stub is `dynports`, so its declared port width is the
    # DEFAULT-parameter width (2). The real instantiation is WIDTH=4 and
    # the summary must reflect the connection, not the stub.
    assert len(blackboxes["xpm_cdc_gray"].ports["dest_out_bin"].bits) == 2
    assert gray_out.width == 4


def test_xpm_seeds_no_boundary_sink_crossings() -> None:
    """A crossing INTO a recognised synchroniser is safe by
    construction, so no virtual sink is seeded — which also means the
    reconvergence gate can never trip on a macro with two data inputs."""
    top, blackboxes, spec = _load("xpm_cdc_blackbox")
    boundaries, _ = compose_boundaries(top, blackboxes, spec)
    crossings = find_crossings(
        top,
        port_clock=spec.port_clock,
        pin_clocks=spec.pin_clocks,
        clock_for_port=spec.clock_for_port,
        boundaries=boundaries,
    )
    assert [c for c in crossings if c.dst_boundary is not None] == []


# --------------------------------------------------------------------------
# The guard: recognition must not become a blind spot
# --------------------------------------------------------------------------


def test_third_domain_consumer_still_fires() -> None:
    """``dest_out`` (clk_b) read by a clk_c flop is a real
    unsynchronised crossing. Suppression falls out of stamping the
    *correct* domain, not of an unconditional skip — so this one
    survives."""
    report, code = _run("xpm_cdc_third_domain")
    assert _rule_ids(report) == ["CDC-001"]
    assert code == 1
    (v,) = report["violations"]
    assert "clk_b" in v["message"] and "clk_c" in v["message"]


def test_declines_when_destination_clock_is_unidentifiable() -> None:
    """Two unclassifiable clock pins means we cannot tell which side is
    the destination. Refuse to vouch for the macro — falling back to the
    generic path (which declines, and says so) beats guessing."""
    sub = Module(
        name="acme_cdc_sync",
        ports={
            "clk_x": Port(name="clk_x", direction="input", bits=(1,)),
            "clk_y": Port(name="clk_y", direction="input", bits=(2,)),
            "d_out": Port(name="d_out", direction="output", bits=(3,)),
        },
        cells={},
        netnames={},
        is_blackbox=True,
    )
    inst = Cell(
        name="u_acme",
        type="acme_cdc_sync",
        connections={"clk_x": (1,), "clk_y": (2,), "d_out": (3,)},
    )
    top = Module(
        name="top",
        ports={
            "clk_x": Port(name="clk_x", direction="input", bits=(1,)),
            "clk_y": Port(name="clk_y", direction="input", bits=(2,)),
        },
        cells={"u_acme": inst},
        netnames={},
    )
    spec = sdc_mod.parse(
        "create_clock -name clk_x -period 10 [get_ports clk_x]\n"
        "create_clock -name clk_y -period 7 [get_ports clk_y]\n"
        "set_clock_groups -asynchronous -group {clk_x} -group {clk_y}\n"
    )
    assert (
        abstract.summarise_sync_primitive(top, inst, sub, spec, pin_clocks=None) is None
    )


def test_declines_when_instance_has_no_clock_pin() -> None:
    """A purely combinational instance is not a synchroniser."""
    sub = Module(
        name="acme_cdc_sync",
        ports={
            "d_in": Port(name="d_in", direction="input", bits=(1,)),
            "d_out": Port(name="d_out", direction="output", bits=(2,)),
        },
        cells={},
        netnames={},
        is_blackbox=True,
    )
    inst = Cell(
        name="u_acme",
        type="acme_cdc_sync",
        connections={"d_in": (1,), "d_out": (2,)},
    )
    top = Module(name="top", ports={}, cells={"u_acme": inst}, netnames={})
    spec = sdc_mod.parse("create_clock -name c -period 10 [get_ports c]\n")
    assert (
        abstract.summarise_sync_primitive(top, inst, sub, spec, pin_clocks=None) is None
    )


def test_single_unclassified_clock_pin_is_the_destination() -> None:
    """A macro with exactly one clock pin has no ambiguity to resolve —
    that pin is the destination clock even when its name says neither
    ``src`` nor ``dest``."""
    sub = Module(
        name="acme_sync",
        ports={
            "clk": Port(name="clk", direction="input", bits=(1,)),
            "d_in": Port(name="d_in", direction="input", bits=(2,)),
            "d_out": Port(name="d_out", direction="output", bits=(3,)),
        },
        cells={},
        netnames={},
        is_blackbox=True,
    )
    inst = Cell(
        name="u_acme",
        type="acme_sync",
        connections={"clk": (1,), "d_in": (2,), "d_out": (3,)},
    )
    top = Module(
        name="top",
        ports={"clk": Port(name="clk", direction="input", bits=(1,))},
        cells={"u_acme": inst},
        netnames={},
    )
    spec = sdc_mod.parse("create_clock -name clk -period 10 [get_ports clk]\n")
    summary = abstract.summarise_sync_primitive(top, inst, sub, spec, pin_clocks=None)
    assert summary is not None
    assert summary.clock == "clk"
    assert summary.ports["d_out"].src_clock == "clk"
    assert summary.ports["d_out"].synchronised is True


# --------------------------------------------------------------------------
# CDC-022 — the depth parameter
# --------------------------------------------------------------------------


def test_cdc_022_silent_at_the_default_depth() -> None:
    """``DEST_SYNC_FF=2`` under the default ``--sync-depth 2`` is fine.
    CDC-022 inherits CDC-002's default-silent posture."""
    report, code = _run("bad_xpm_shallow_sync_depth")
    assert _rule_ids(report) == []
    assert code == 0


def test_cdc_022_fires_below_required_depth() -> None:
    report, code = _run("bad_xpm_shallow_sync_depth", "--sync-depth", "3")
    assert _rule_ids(report) == ["CDC-022"]
    assert code == 1
    (v,) = report["violations"]
    assert v["severity"] == "warning"
    assert "DEST_SYNC_FF=2" in v["message"]
    assert "XPM accepts 2..10" in v["message"]
    assert v["cell_name"] == "u_flag_sync"


def test_cdc_022_promoted_by_strict() -> None:
    report, _ = _run("bad_xpm_shallow_sync_depth", "--sync-depth", "3", "--strict")
    assert [v["severity"] for v in report["violations"]] == ["error"]


def test_cdc_022_fires_on_the_documented_default() -> None:
    """The XPM default (4) is short of a project asking for 5, and the
    parameter isn't on the cell at all in that case."""
    report, _ = _run("xpm_cdc_blackbox", "--sync-depth", "5")
    assert _rule_ids(report) == ["CDC-022", "CDC-022"]


def test_cdc_022_needs_no_blackbox_sibling() -> None:
    """The rule reads ``Cell.type`` / ``Cell.parameters`` only, so it
    speaks even for a macro that arrived as a bare unresolved cell."""
    module = Module(
        name="top",
        ports={},
        cells={
            "u": Cell(
                name="u",
                type="xpm_cdc_single",
                connections={},
                parameters={"DEST_SYNC_FF": "00000000000000000000000000000010"},
            )
        },
        netnames={},
    )
    violations = check_cdc_022(module, [], None, 3)
    assert [v.rule_id for v in violations] == ["CDC-022"]


def test_cdc_022_omits_the_xpm_range_hint_for_a_site_macro() -> None:
    module = Module(
        name="top",
        ports={},
        cells={
            "u": Cell(
                name="u",
                type="acme_cdc_sync",
                connections={},
                parameters={"DEST_SYNC_FF": "00000000000000000000000000000010"},
            )
        },
        netnames={},
    )
    (v,) = check_cdc_022(
        module, [], None, 3, sync_primitives=frozenset({"acme_cdc_sync"})
    )
    assert "XPM accepts" not in v.message


# --------------------------------------------------------------------------
# Extensibility + the elaborated-internals route
# --------------------------------------------------------------------------


def test_site_macro_needs_the_opt_in() -> None:
    """Unregistered, ``acme_cdc_sync`` is just a dual-clock blackbox."""
    report, code = _run("sync_primitive_optin")
    assert _rule_ids(report) == ["CDC-BBX"]
    assert code == 1


def test_sync_primitive_option_registers_a_site_macro() -> None:
    report, code = _run("sync_primitive_optin", "--sync-primitive", "acme_cdc_sync")
    assert _rule_ids(report) == []
    assert code == 0


def test_sync_primitive_option_feeds_cdc_022() -> None:
    report, _ = _run(
        "sync_primitive_optin",
        "--sync-primitive",
        "acme_cdc_sync",
        "--sync-depth",
        "4",
    )
    assert _rule_ids(report) == ["CDC-022"]
    assert "DEST_SYNC_FF=3" in report["violations"][0]["message"]


def test_uppercase_async_reg_marks_a_synchroniser() -> None:
    """``USER_SYNC_ATTRS`` carries an ``async_reg`` alias for the Xilinx
    attribute, but Xilinx writes ``(* ASYNC_REG = "TRUE" *)`` and Yosys
    preserves attribute names verbatim — so the alias never fired on the
    idiom it was added for. This fixture is a single-stage crossing held
    silent purely by the case-fold."""
    nl, _ = _paths("marked_async_reg_upper")
    module = netlist.load(nl)
    # The attribute really is uppercase in the netlist.
    assert any("ASYNC_REG" in nn.attributes for nn in module.netnames.values()), (
        "fixture no longer carries the uppercase spelling"
    )
    assert len(user_sync_flop_names(module)) == 1

    report, code = _run("marked_async_reg_upper")
    assert _rule_ids(report) == []
    assert code == 0


def test_unidentifiable_destination_falls_through_to_the_generic_path() -> None:
    """Registering a macro is not a licence to skip it.

    ``multi_clock_blackbox``'s ``afifo`` has ``wr_clk`` / ``rd_clk`` —
    two clock pins, neither of which says which side is the
    destination. Registering it via ``--sync-primitive`` must NOT
    silently vouch for it: the primitive summariser declines, the
    instance falls through to the generic single-clock path, and the
    existing CDC-BBX coverage finding still fires.
    """
    report, code = _run("multi_clock_blackbox", "--sync-primitive", "afifo")
    assert "CDC-BBX" in _rule_ids(report)
    assert code == 1

    top, blackboxes, spec = _load("multi_clock_blackbox")
    boundaries, stats = compose_boundaries(
        top, blackboxes, spec, sync_primitives=frozenset({"afifo"})
    )
    assert boundaries == {}
    assert stats.primitive_modules == frozenset()
    assert stats.declined_modules == frozenset({"afifo"})


def test_inout_clock_pin_is_never_emitted_as_a_boundary_source() -> None:
    """An ``inout`` port that resolves to a clock is clock distribution,
    not data. Emitting a boundary source for it would re-create the
    clock-as-data shape (CDC-008) the blackbox summariser is careful to
    avoid, so the output walk skips anything already classified as a
    clock pin.
    """
    sub = Module(
        name="acme_cdc_sync",
        ports={
            "src_clk": Port(name="src_clk", direction="input", bits=(1,)),
            "dest_clk": Port(name="dest_clk", direction="inout", bits=(2,)),
            "dest_out": Port(name="dest_out", direction="output", bits=(3,)),
        },
        cells={},
        netnames={},
        is_blackbox=True,
    )
    inst = Cell(
        name="u_acme",
        type="acme_cdc_sync",
        connections={"src_clk": (1,), "dest_clk": (2,), "dest_out": (3,)},
    )
    top = Module(
        name="top",
        ports={
            "clk_a": Port(name="clk_a", direction="input", bits=(1,)),
            "clk_b": Port(name="clk_b", direction="input", bits=(2,)),
        },
        cells={"u_acme": inst},
        netnames={},
    )
    spec = sdc_mod.parse(
        "create_clock -name clk_a -period 10 [get_ports clk_a]\n"
        "create_clock -name clk_b -period 7 [get_ports clk_b]\n"
        "set_clock_groups -asynchronous -group {clk_a} -group {clk_b}\n"
    )
    summary = abstract.summarise_sync_primitive(top, inst, sub, spec, pin_clocks=None)
    assert summary is not None
    assert summary.clock == "clk_b"
    # The inout clock pin is absorbed as a clock, never summarised as a
    # data source; only the genuine data output survives.
    assert set(summary.ports) == {"dest_out"}
