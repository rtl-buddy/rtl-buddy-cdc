"""Per-bit port typing end to end (rtl-buddy-cdc#394, item 4).

``set_input_delay`` / ``set_output_delay`` type port *bits*: a statement
whose target names some bits of a bus — a bit select, or a
``remove_from_collection`` that takes some bits out — types only those
bits. A bit no statement types behaves exactly like an untyped scalar
port: an input bit reads ``<unconstrained>`` (CDC-011's), an output bit
carries no contract (CDC-025's). Before, the whole port took the clock,
with only a warning.

The fixture registers two 4-bit buses in ``clk_b``. Its SDC types
``cfg_in[2:0]`` to ``clk_b`` with ``cfg_in[3]`` removed, and
``mix_in[1:0]`` / ``mix_in[3:2]`` to ``clk_a`` / ``clk_b``. Every check
runs on both SDC backends.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.domain import Crossing, filter_async, find_crossings
from rtl_buddy_cdc.rules import run_all

NAME = "bad_partial_bus_port_typing"
FIX = Path(__file__).parent / "fixtures" / NAME
NETLIST = FIX / f"{NAME}.json"
SDC_TEXT = (FIX / f"{NAME}.sdc").read_text()
_CLOCKS = "\n".join(SDC_TEXT.splitlines()[:3]) + "\n"


@pytest.fixture(autouse=True)
def _both_backends(sdc_backend: str) -> str:
    return sdc_backend


def _run(sdc_text: str) -> tuple[list[Crossing], Counter[str], list]:
    module = netlist.load(NETLIST)
    spec = sdc_mod.parse(sdc_text)
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = filter_async(
        find_crossings(module, port_clock=spec.port_clock, **spec.clock_root_inputs()),
        spec,
    )
    violations = run_all(module, crossings, spec)
    return crossings, Counter(v.rule_id for v in violations), violations


def _cli(tmp_path: Path, sdc_text: str, *extra: str) -> dict:
    sdc = tmp_path / "x.sdc"
    sdc.write_text(sdc_text)
    result = CliRunner().invoke(
        app, ["analyze", "-n", str(NETLIST), "-s", str(sdc), "-f", "json", *extra]
    )
    assert result.exit_code in (0, 1), result.output
    return json.loads(result.stdout)


def test_port_clock_is_keyed_per_bit() -> None:
    module = netlist.load(NETLIST)
    spec = sdc_mod.parse(SDC_TEXT)
    stamped = sdc_mod.synthesize_unconstrained_inputs(spec, module)
    assert stamped == ["cfg_in[3]"]
    assert {k: v for k, v in spec.port_clock.items() if "[" in k} == {
        "cfg_in[0]": "clk_b",
        "cfg_in[1]": "clk_b",
        "cfg_in[2]": "clk_b",
        "cfg_in[3]": sdc_mod.UNCONSTRAINED_SENTINEL,
        "mix_in[0]": "clk_a",
        "mix_in[1]": "clk_a",
        "mix_in[2]": "clk_b",
        "mix_in[3]": "clk_b",
    }
    assert "cfg_in" not in spec.port_clock and "mix_in" not in spec.port_clock
    # The whole-port answer: no single set_*_delay clock.
    assert spec.clock_for_port("cfg_in") is None
    assert not any("per port" in w for w in spec.partial_warnings)


def test_excluded_bit_crosses_and_typed_bits_do_not() -> None:
    """The verdict on the excluded bit differs from the typed bits':
    ``cfg_in[3]`` is an unconstrained-input crossing, ``cfg_in[2:0]``
    (typed to the capture clock) is no crossing at all."""
    crossings, fired, violations = _run(SDC_TEXT)
    port_sourced = sorted(
        (c.src_port, c.src_port_bits, c.src_clock, c.width)
        for c in crossings
        if c.src_port is not None
    )
    assert port_sourced == [
        ("cfg_in", (3,), sdc_mod.UNCONSTRAINED_SENTINEL, 1),
        ("mix_in", (0, 1), "clk_a", 2),
    ]
    assert fired == Counter({"CDC-011": 1, "CDC-004": 1})
    (cdc_011,) = [v for v in violations if v.rule_id == "CDC-011"]
    assert "'cfg_in[3]'" in cdc_011.message and cdc_011.severity == "warning"
    assert "[get_ports {cfg_in[3]}]" in cdc_011.message
    (cdc_004,) = [v for v in violations if v.rule_id == "CDC-004"]
    assert "port mix_in[1:0]" in cdc_004.message


def test_whole_port_typing_reports_neither() -> None:
    """The pre-#394 reading of the same SDC — each bus typed whole to
    ``clk_b`` — sees no crossing at all."""
    whole = _CLOCKS + "set_input_delay -clock clk_b 1 [get_ports {cfg_in mix_in}]\n"
    crossings, fired, _ = _run(whole)
    assert [c for c in crossings if c.src_port is not None] == []
    assert fired == Counter()


def test_retyping_the_excluded_bit_clears_cdc_011() -> None:
    sdc = SDC_TEXT + "set_input_delay -clock clk_b 1 {cfg_in[3]}\n"
    crossings, fired, _ = _run(sdc)
    assert not any(c.src_port == "cfg_in" for c in crossings)
    assert fired == Counter({"CDC-004": 1})


def test_json_and_domain_map_name_the_bits(tmp_path: Path) -> None:
    dm = tmp_path / "dm.json"
    report = _cli(tmp_path, SDC_TEXT, "--emit-domain-map", str(dm))
    by_port = {c["src_port"]: c for c in report["crossings"] if "src_port" in c}
    assert by_port["cfg_in"]["src_port_bits"] == [3]
    assert by_port["mix_in"]["src_port_bits"] == [0, 1]
    domain_map = json.loads(dm.read_text())
    assert domain_map["schema_version"] == "1.3"
    typed = [(p["port"], p["clock"], p.get("bits")) for p in domain_map["port_domains"]]
    assert typed == [
        ("cfg_in", "clk_b", [0, 1, 2]),
        ("mix_in", "clk_a", [0, 1]),
        ("mix_in", "clk_b", [2, 3]),
    ]
    dm_by_port = {c["src_port"]: c for c in domain_map["crossings"] if "src_port" in c}
    assert dm_by_port["cfg_in"]["src_port_bits"] == [3]


def test_whole_port_crossings_carry_no_bits_key(tmp_path: Path) -> None:
    """Additive only: a port typed whole serialises exactly as before."""
    report = _cli(tmp_path, _CLOCKS + "set_input_delay -clock clk_a 1 {cfg_in}\n")
    port_crossings = [c for c in report["crossings"] if "src_port" in c]
    assert port_crossings and all("src_port_bits" not in c for c in port_crossings)


def test_output_contracts_per_bit(tmp_path: Path) -> None:
    """``mix_q[1:0]`` contracted to clk_a (async to its clk_b launch) is
    CDC-024's; ``mix_q[3:2]`` on clk_b meets its contract; ``cfg_q[3]``,
    removed from a contract over ``cfg_q``, is CDC-025's alone."""
    sdc = (
        SDC_TEXT
        + "set_output_delay -clock clk_a 1 [get_ports {mix_q[0] mix_q[1]}]\n"
        + "set_output_delay -clock clk_b 1 {mix_q[3] mix_q[2]}\n"
        + "set_output_delay -clock clk_b 1 "
        + "[remove_from_collection [get_ports cfg_q] [get_ports {cfg_q[3]}]]\n"
    )
    _, fired, violations = _run(sdc)
    (cdc_024,) = [v for v in violations if v.rule_id == "CDC-024"]
    assert "`mix_q[1:0]`" in cdc_024.message and "(2 of 4 bits)" in cdc_024.message
    (cdc_025,) = [v for v in violations if v.rule_id == "CDC-025"]
    assert "`cfg_q[3]`" in cdc_025.message and "(1 of 4 bits)" in cdc_025.message
    assert "[get_ports {cfg_q[3]}]" in cdc_025.message
    report = _cli(tmp_path, sdc)
    assert Counter(v["rule_id"] for v in report["violations"]) == fired


def test_false_path_on_the_typed_bits_acknowledges_the_crossing(
    tmp_path: Path,
) -> None:
    """An object-scoped false path names the crossing's own bits: the
    ``mix_in[1:0]`` crossing is fully covered by a ``-from`` over exactly
    those bits, and not by one over a single bit of them."""
    both = SDC_TEXT + "set_false_path -from [get_ports {mix_in[0] mix_in[1]}]\n"
    report = _cli(tmp_path, both)
    assert [v["rule_id"] for v in report["suppressed"]] == ["CDC-004"]
    one = SDC_TEXT + "set_false_path -from [get_ports {mix_in[0]}]\n"
    report = _cli(tmp_path, one)
    assert report["suppressed"] == []
    assert "CDC-004" in {v["rule_id"] for v in report["violations"]}


def test_through_walk_seeds_the_crossings_bits_only(tmp_path: Path) -> None:
    """``-through`` the two clk_a bits covers the mix_in crossing: the
    walk starts at the crossing's own source bits."""
    sdc = (
        SDC_TEXT
        + "set_false_path -from [get_ports mix_in] "
        + "-through [get_nets {mix_in[0] mix_in[1]}]\n"
    )
    report = _cli(tmp_path, sdc)
    assert [v["rule_id"] for v in report["suppressed"]] == ["CDC-004"]
