"""Rules compare ``ctx.domains`` values as they are (rtl-buddy-cdc#378).

Since rtl-buddy-cdc#358 the rule context's domains are canonical clock
names: the clock-root walk renames a clock port through
``clock_for_port`` once. CDC-006, CDC-010, CDC-015, RDC-001 and RDC-003
renamed them again before comparing, which is a no-op unless a clock is
named after the port of a *different* clock: ``create_clock -name ck_d
[get_ports dst_clk]`` next to ``create_clock -name ck_far [get_ports
ck_d]`` turns domain ``ck_d`` into ``ck_far`` on the second rename. Each
case here pins the verdict the un-renamed comparison gives.
"""

from __future__ import annotations

from pathlib import Path

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all

FIX = Path(__file__).parent / "fixtures"


def _fired(name: str, sdc_text: str) -> list[str]:
    module = netlist.load(FIX / name / f"{name}.json")
    spec = sdc_mod.parse(sdc_text, backend="tokenizer")
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = [
        c
        for c in find_crossings(
            module, port_clock=spec.port_clock, **spec.clock_root_inputs()
        )
        if spec.are_async(c.src_clock, c.dst_clock)
    ]
    return [v.rule_id for v in run_all(module, crossings, spec)]


def test_cdc_006_port_typed_to_the_synchronisers_own_clock() -> None:
    """``a`` / ``b`` are typed to ``clk_x``, the synchroniser's own clock:
    same domain, no CDC-006. ``clk_y`` is declared on a port named
    ``clk_x``; renaming the domain ``clk_x`` again read it as ``clk_y``."""
    sdc = (
        "create_clock -name clk_x -period 7.5 [get_ports dst_clk]\n"
        "create_clock -name clk_y -period 5.0 [get_ports clk_x]\n"
        "set_clock_groups -asynchronous -group {clk_x} -group {clk_y}\n"
        "set_input_delay -clock clk_x 1.0 [get_ports {a b rst_n}]\n"
    )
    assert _fired("bad_comb_source", sdc) == []


def test_rdc_001_between_synchronous_clocks() -> None:
    """``ck_s`` and ``ck_d`` share a group, so the src → dst reset is not
    a crossing. ``ck_far`` (asynchronous to both) is declared on a port
    named ``ck_d``; renaming the domain ``ck_d`` again read it as
    ``ck_far`` and reported RDC-001."""
    sdc = (
        "create_clock -name ck_s -period 10.0 [get_ports src_clk]\n"
        "create_clock -name ck_d -period 10.0 [get_ports dst_clk]\n"
        "create_clock -name ck_far -period 3.0 [get_ports ck_d]\n"
        "set_clock_groups -asynchronous -group {ck_far} -group {ck_s ck_d}\n"
        "set_input_delay -clock ck_s 1.0 [get_ports kill_req]\n"
        "set_input_delay -clock ck_d 1.0 [get_ports d_in]\n"
    )
    assert "RDC-001" not in _fired("bad_reset_crossing", sdc)


def test_rdc_001_still_fires_between_asynchronous_clocks() -> None:
    """Control: the same renamed clocks in separate asynchronous groups."""
    sdc = (
        "create_clock -name ck_s -period 10.0 [get_ports src_clk]\n"
        "create_clock -name ck_d -period 10.0 [get_ports dst_clk]\n"
        "set_clock_groups -asynchronous -group {ck_s} -group {ck_d}\n"
        "set_input_delay -clock ck_s 1.0 [get_ports kill_req]\n"
        "set_input_delay -clock ck_d 1.0 [get_ports d_in]\n"
    )
    assert "RDC-001" in _fired("bad_reset_crossing", sdc)
