"""Soundness-audit fixtures for the blackbox / auto-abstract work (#259).

The audit of PR #259 found four silent false-negatives. These three
fixture pairs (run BOTH flat AND blackboxed through the CLI / analyze
core) pin the fixes:

- ``multi_clock_blackbox`` (FIX 1) — a dual-clock IP whose clock pins
  are NOT in the name allow-list (``wr_clk`` / ``rd_clk``) with a real
  internal wr_clk -> rd_clk crossing. FLAT: the crossing fires.
  BLACKBOXED: the block is DECLINED (>=2 distinct clock roots), a
  partial_warning names it, and the run does NOT silently pass it as
  clean (the headline test).
- ``reconvergence_two_inputs`` (FIX 3) — a single-clock(clk_d) block
  with TWO foreign-domain (clk_x) inputs that reconverge internally.
  FLAT: CDC-005 fires. BLACKBOXED: reconvergence-unsafe — the block is
  refused (opaque) and the reconvergence diagnostic is emitted.
- ``safe_single_input`` (FIX 3 over-fire guard) — a single-clock(clk_d)
  block with ONE foreign-domain input. FLAT: reports the crossing.
  BLACKBOXED: abstracted cleanly, SAME crossing reported (parity), and
  NO decline / reconvergence warning.

A fourth test exercises FIX 4 directly: a clock wired into a genuine
DATA input of a blackbox still fires CDC-008, while the instance's clock
pin does not.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from rtl_buddy_cdc import abstract, netlist, sdc as sdc_mod
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.hierarchy import (
    compose_boundaries,
    reconvergence_unsafe_instances,
)
from rtl_buddy_cdc.netlist import Cell, Module, Port
from rtl_buddy_cdc.rules import check_cdc_008

runner = CliRunner()

FIX = Path(__file__).parent / "fixtures"


def _paths(name: str) -> tuple[Path, Path, Path]:
    d = FIX / name
    return d / f"{name}.flat.json", d / f"{name}.json", d / f"{name}.sdc"


def _analyze(path: Path, sdc: Path) -> tuple[dict, str]:
    """Return ``(json_report, text_stderr)``.

    Two CLI runs: a ``-f json`` run for the structured report and a
    default text run for the warning surface (partial_warnings are
    echoed to stderr only in the text format, never the JSON report).
    """
    json_run = runner.invoke(
        app, ["analyze", "-n", str(path), "-s", str(sdc), "-f", "json"]
    )
    assert json_run.exit_code in (0, 1), json_run.output
    text_run = runner.invoke(app, ["analyze", "-n", str(path), "-s", str(sdc)])
    assert text_run.exit_code in (0, 1), text_run.output
    return json.loads(json_run.output), text_run.stderr


def _load(path: Path, sdc: Path):
    top, blackboxes = netlist.load_with_blackboxes(path)
    spec = sdc_mod.parse_file(sdc)
    sdc_mod.synthesize_unconstrained_inputs(spec, top)
    return top, blackboxes, spec


# --------------------------------------------------------------------------
# FIX 1 — multi-clock subtree must be DECLINED, never silently abstracted
# --------------------------------------------------------------------------


def test_multi_clock_flat_reports_internal_crossing() -> None:
    """FLAT: the dual-clock IP's internal wr_clk -> rd_clk crossing fires
    (CDC-004 bus crossing). This is the crossing the abstraction must not
    silently drop."""
    flat, bb, sdc = _paths("multi_clock_blackbox")
    report, _ = _analyze(flat, sdc)
    assert report["summary"]["async_crossings"] == 1
    assert any(v["rule_id"] in ("CDC-001", "CDC-004") for v in report["violations"])


def test_multi_clock_blackbox_is_declined() -> None:
    """The dual-clock block (clock pins ``wr_clk`` / ``rd_clk``, NOT in the
    allow-list) presents >=2 distinct clock roots, so the summariser
    DECLINES it — it is NOT abstracted (absent from the boundary map and
    recorded in ``declined_modules``)."""
    flat, bb, sdc = _paths("multi_clock_blackbox")
    top, blackboxes, spec = _load(bb, sdc)
    assert set(blackboxes) == {"afifo"}
    boundaries, stats = compose_boundaries(top, blackboxes, spec)
    # Not abstracted: no boundary summary for the instance.
    assert boundaries == {}
    assert "afifo" in stats.declined_modules
    # FIX 1 mechanism: the instance carries two distinct clock roots.
    inst = top.cells["u_afifo"]
    ic = abstract._instance_clocks(top, inst, blackboxes["afifo"], spec=spec)
    assert ic.roots == frozenset({"wr_clk", "rd_clk"})


def test_multi_clock_blackbox_warns_and_does_not_silently_pass() -> None:
    """BLACKBOXED end-to-end: a partial_warning NAMES the opaque block and
    the run does not present it as analysed-clean. The block is declined,
    so its internal crossing is no longer SILENTLY abstracted away — the
    drop is documented via the warning surface (FIX 1 + FIX 2)."""
    flat, bb, sdc = _paths("multi_clock_blackbox")
    report, stderr = _analyze(bb, sdc)
    assert "blackbox `afifo` left opaque" in stderr
    assert "internal crossings not analysed" in stderr


# --------------------------------------------------------------------------
# FIX 3 — single-clock block with >=2 foreign inputs is reconvergence-unsafe
# --------------------------------------------------------------------------


def test_reconvergence_flat_fires_cdc005() -> None:
    """FLAT: one clk_x source fans out to two internal sync chains that
    recombine — the textbook CDC-005 reconvergence the abstraction could
    hide."""
    flat, bb, sdc = _paths("reconvergence_two_inputs")
    report, _ = _analyze(flat, sdc)
    assert any(v["rule_id"] == "CDC-005" for v in report["violations"])


def test_reconvergence_blackbox_is_skipped_and_warned() -> None:
    """BLACKBOXED: the block has crossings into 2 distinct input ports, so
    the reconvergence gate (FIX 3) refuses to abstract it — it becomes
    opaque (no boundary crossings emitted) and the reconvergence
    diagnostic is emitted. It is NOT reported clean of the hazard."""
    flat, bb, sdc = _paths("reconvergence_two_inputs")
    # The gate's pure core: two distinct dst_boundary ports on one
    # instance => reconvergence-unsafe.
    top, blackboxes, spec = _load(bb, sdc)
    boundaries, _stats = compose_boundaries(top, blackboxes, spec)
    crossings = find_crossings(
        top,
        port_clock=spec.port_clock,
        pin_clocks=spec.pin_clocks,
        clock_for_port=spec.clock_for_port,
        boundaries=boundaries,
    )
    assert reconvergence_unsafe_instances(crossings) == {"u_recon"}

    # End-to-end: the diagnostic is surfaced and no boundary-sourced /
    # boundary-sink crossing leaks for the skipped instance.
    report, stderr = _analyze(bb, sdc)
    assert "blackbox `u_recon` (`recon`)" in stderr
    assert "reconvergence among them cannot be checked" in stderr
    for c in report["crossings"]:
        assert c.get("dst_boundary") is None
        assert c.get("src_boundary") is None


# --------------------------------------------------------------------------
# FIX 3 guard — a single-input block must NOT trip the gate (parity)
# --------------------------------------------------------------------------


def test_safe_single_input_flat_reports_crossing() -> None:
    flat, bb, sdc = _paths("safe_single_input")
    report, _ = _analyze(flat, sdc)
    assert report["summary"]["async_crossings"] == 1
    assert any(v["rule_id"] == "CDC-004" for v in report["violations"])


def test_safe_single_input_abstracted_with_parity_no_warning() -> None:
    """The single-input block IS abstracted (one incoming port => safe),
    the SAME crossing is reported (contract-count parity), and there is NO
    decline / reconvergence warning."""
    flat, bb, sdc = _paths("safe_single_input")
    flat_report, _ = _analyze(flat, sdc)
    bb_report, stderr = _analyze(bb, sdc)

    # No spurious blackbox warning on the safe path.
    assert "left opaque" not in stderr
    assert "reconvergence among them" not in stderr

    # Contract-count parity flat vs abstracted.
    for key in ("violations", "suppressed", "crossings", "async_crossings"):
        assert bb_report["summary"][key] == flat_report["summary"][key], key
    # Same rule set fires; the crossing is a CDC-004 in both.
    assert sorted(v["rule_id"] for v in bb_report["violations"]) == sorted(
        v["rule_id"] for v in flat_report["violations"]
    )
    # The abstracted run anchors the crossing on the boundary input pin
    # (scaling win: oneport's internal flops are gone).
    assert bb_report["summary"]["flops"] < flat_report["summary"]["flops"]
    (c,) = bb_report["crossings"]
    assert c["dst_boundary"] == {"instance": "u_oneport", "port": "d_in"}


def test_safe_single_input_is_abstracted() -> None:
    flat, bb, sdc = _paths("safe_single_input")
    top, blackboxes, spec = _load(bb, sdc)
    boundaries, stats = compose_boundaries(top, blackboxes, spec)
    assert set(boundaries) == {"u_oneport"}
    assert stats.declined_modules == frozenset()


# --------------------------------------------------------------------------
# FIX 4 — CDC-008 exemption is per-clock-pin, not per-instance
# --------------------------------------------------------------------------


def _clock_as_data_parent() -> tuple[Module, Module]:
    """A parent feeding the clock ``clk_a`` into BOTH a blackbox clock pin
    (``clk``, legitimate distribution) and a genuine DATA input
    (``d_in``, a clock-as-data bug).

    bits: clk_a=1 (top clock port), sub.clk=1, sub.d_in=1, sub.d_out=2.
    """
    sub_inst = Cell(
        name="u_sub", type="sub", connections={"clk": (1,), "d_in": (1,), "d_out": (2,)}
    )
    parent = Module(
        name="top",
        ports={"clk_a": Port(name="clk_a", direction="input", bits=(1,))},
        cells={"u_sub": sub_inst},
        netnames={},
    )
    sub = Module(
        name="sub",
        ports={
            "clk": Port(name="clk", direction="input", bits=()),
            "d_in": Port(name="d_in", direction="input", bits=()),
            "d_out": Port(name="d_out", direction="output", bits=()),
        },
        cells={},
        netnames={},
        is_blackbox=True,
    )
    return parent, sub


def test_cdc008_fires_on_clock_into_blackbox_data_pin() -> None:
    """FIX 4: a clock net wired into a genuine DATA input of a blackbox
    still fires CDC-008, while the same clock on the instance's CLOCK pin
    does not. The pre-#259 whole-instance exemption masked the data-pin
    bug."""
    parent, sub = _clock_as_data_parent()
    spec = sdc_mod.ClockSpec()
    spec.clocks["clk_a"] = sdc_mod.Clock(name="clk_a", period=10.0, ports=("clk_a",))
    clock_pins = abstract.instance_clock_pins(
        parent, parent.cells["u_sub"], sub, spec=spec
    )
    assert clock_pins == frozenset({"clk"})

    from rtl_buddy_cdc.rules import _build_context

    ctx = _build_context(
        parent,
        spec,
        blackbox_modules=frozenset({"sub"}),
        boundary_clock_pins={"u_sub": clock_pins},
    )
    violations = check_cdc_008(parent, [], spec, ctx=ctx)
    # The data pin fires; the clock pin does not.
    assert len(violations) == 1
    v = violations[0]
    assert v.rule_id == "CDC-008"
    assert "d_in" in v.message


def test_cdc008_silent_when_clock_only_on_clock_pin() -> None:
    """The control case: a clock that only reaches the blackbox's CLOCK
    pin (legitimate distribution) does NOT fire CDC-008."""
    sub_inst = Cell(name="u_sub", type="sub", connections={"clk": (1,), "d_out": (2,)})
    parent = Module(
        name="top",
        ports={"clk_a": Port(name="clk_a", direction="input", bits=(1,))},
        cells={"u_sub": sub_inst},
        netnames={},
    )
    sub = Module(
        name="sub",
        ports={
            "clk": Port(name="clk", direction="input", bits=()),
            "d_out": Port(name="d_out", direction="output", bits=()),
        },
        cells={},
        netnames={},
        is_blackbox=True,
    )
    spec = sdc_mod.ClockSpec()
    spec.clocks["clk_a"] = sdc_mod.Clock(name="clk_a", period=10.0, ports=("clk_a",))
    clock_pins = abstract.instance_clock_pins(parent, sub_inst, sub, spec=spec)

    from rtl_buddy_cdc.rules import _build_context

    ctx = _build_context(
        parent,
        spec,
        blackbox_modules=frozenset({"sub"}),
        boundary_clock_pins={"u_sub": clock_pins},
    )
    assert check_cdc_008(parent, [], spec, ctx=ctx) == []


def test_cdc008_blackbox_fallback_to_name_allow_list() -> None:
    """When no traced ``boundary_clock_pins`` map is supplied (legacy
    caller / module-type exemption), CDC-008 falls back to the
    ``_CLOCK_PIN_NAMES`` allow-list: the ``clk`` pin is exempt, a data
    pin carrying a clock still fires."""
    parent, sub = _clock_as_data_parent()
    spec = sdc_mod.ClockSpec()
    spec.clocks["clk_a"] = sdc_mod.Clock(name="clk_a", period=10.0, ports=("clk_a",))

    from rtl_buddy_cdc.rules import _build_context

    ctx = _build_context(parent, spec, blackbox_modules=frozenset({"sub"}))
    violations = check_cdc_008(parent, [], spec, ctx=ctx)
    assert len(violations) == 1
    assert "d_in" in violations[0].message


# --------------------------------------------------------------------------
# FIX 1 helper-level coverage
# --------------------------------------------------------------------------


def test_is_known_clock_variants() -> None:
    spec = sdc_mod.ClockSpec()
    spec.clocks["clk_a"] = sdc_mod.Clock(name="clk_a", period=10.0, ports=("ckp",))
    spec.port_clock["d_typed"] = "clk_a"
    spec.port_clock["d_unc"] = sdc_mod.UNCONSTRAINED_SENTINEL
    # No spec at all => never a known clock.
    assert abstract._is_known_clock("clk_a", None) is False
    # A declared clock name resolves directly.
    assert abstract._is_known_clock("clk_a", spec) is True
    # A create_clock port resolves via clock_for_port.
    assert abstract._is_known_clock("ckp", spec) is True
    # A port typed to a real clock resolves; an unconstrained-typed port
    # and an unrelated name do not.
    assert abstract._is_known_clock("d_typed", spec) is True
    assert abstract._is_known_clock("d_unc", spec) is False
    assert abstract._is_known_clock("nope", spec) is False


def test_looks_like_clock_name() -> None:
    assert abstract._looks_like_clock_name("wr_clk") is True
    assert abstract._looks_like_clock_name("rd_clk") is True
    assert abstract._looks_like_clock_name("core_clock") is True
    assert abstract._looks_like_clock_name("CK0") is True
    assert abstract._looks_like_clock_name("d_in") is False
    assert abstract._looks_like_clock_name("data") is False


def test_clock_named_data_pin_not_classified_without_known_clock() -> None:
    """A clock-NAMED non-allow-listed port whose net does NOT trace to a
    declared clock is not a clock pin (no root recorded)."""
    # ``aux_clk`` driven by an undriven, undeclared bit 9.
    inst = Cell(name="u_sub", type="sub", connections={"aux_clk": (9,), "d_out": (2,)})
    parent = Module(name="top", ports={}, cells={"u_sub": inst}, netnames={})
    sub = Module(
        name="sub",
        ports={
            "aux_clk": Port(name="aux_clk", direction="input", bits=()),
            "d_out": Port(name="d_out", direction="output", bits=()),
        },
        cells={},
        netnames={},
        is_blackbox=True,
    )
    spec = sdc_mod.ClockSpec()
    ic = abstract._instance_clocks(parent, inst, sub, spec=spec)
    assert ic.roots == frozenset()
    assert ic.clock_pins == frozenset()


def test_instance_clock_single_root_backcompat() -> None:
    """``_instance_clock`` returns the sole root for a single-clock
    instance and None for a multi-clock one."""
    flat, bb, sdc = _paths("safe_single_input")
    top, blackboxes, spec = _load(bb, sdc)
    assert abstract._instance_clock(top, top.cells["u_oneport"], spec=spec) == "clk_d"
    mflat, mbb, msdc = _paths("multi_clock_blackbox")
    mtop, _mbb, mspec = _load(mbb, msdc)
    # Two roots => the single-root view declines (None).
    assert abstract._instance_clock(mtop, mtop.cells["u_afifo"], spec=mspec) is None
