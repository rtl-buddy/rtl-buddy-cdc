"""``set_*_delay`` pattern targets resolve the same on the API path as on
the CLI path (rtl-buddy-cdc#386, item 3).

The parser keeps a bus index or glob target as written (``src_a[*]``);
only :func:`rtl_buddy_cdc.sdc_objects.resolve_port_delays` maps it onto
the netlist's ports. The CLI called it, but a direct API user —
``sdc.parse`` → ``sdc.synthesize_unconstrained_inputs`` →
``domain.find_crossings`` → ``rules.run_all`` / ``rules._build_context``
— never did, so ``[get_ports {src_a[*] src_b[*]}]`` left the literal keys
``src_a[*]`` / ``src_b[*]`` in ``spec.port_clock`` and the ports were
stamped ``<unconstrained>``. Both entry points that pair a spec with a
netlist now resolve it first (once per spec and netlist).
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import netlist, sdc as sdc_mod, sdc_objects
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.domain import filter_async, find_crossings
from rtl_buddy_cdc.rules import _build_context, run_all

NAME = "good_flop_variety_handshake"
FIX = Path(__file__).parent / "fixtures" / NAME
NETLIST = FIX / f"{NAME}.json"
_BASE_SDC = """\
create_clock -name src_clk -period 4.0  [get_ports src_clk]
create_clock -name dst_clk -period 10.0 [get_ports dst_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
set_input_delay -clock src_clk 0.5 [get_ports {src_rst_n src_aload src_ad src_req}]
set_input_delay -clock dst_clk 0.5 [get_ports {rst_n set_n aload ad srst}]
"""

# The payload buses, typed through a bus glob in each operand form.
FORMS = {
    "bracket": "set_input_delay -clock src_clk 0.5 [get_ports {src_a[*] src_b[*] src_c[*]}]\n",
    "brace": "set_input_delay -clock src_clk 0.5 {src_a[*] src_b[*] src_c[*]}\n",
    "bare": "".join(
        f"set_input_delay -clock src_clk 0.5 {p}[*]\n"
        for p in ("src_a", "src_b", "src_c")
    ),
    "glob": "set_input_delay -clock src_clk 0.5 [get_ports src_?]\n",
}
PAYLOAD = ("src_a", "src_b", "src_c")


@pytest.fixture(autouse=True)
def _both_backends(sdc_backend: str) -> str:
    return sdc_backend


def _spec(form: str) -> sdc_mod.ClockSpec:
    return sdc_mod.parse(_BASE_SDC + FORMS[form])


def _module() -> netlist.Module:
    return netlist.load(NETLIST)


@pytest.mark.parametrize("form", list(FORMS))
def test_synthesize_resolves_pattern_targets(form: str) -> None:
    spec = _spec(form)
    stamped = sdc_mod.synthesize_unconstrained_inputs(spec, _module())
    assert stamped == []
    for port in PAYLOAD:
        assert spec.port_clock[port] == "src_clk"
    assert not any("[" in key or "?" in key for key in spec.port_clock)


@pytest.mark.parametrize("form", list(FORMS))
def test_build_context_resolves_pattern_targets(form: str) -> None:
    spec = _spec(form)
    _build_context(_module(), spec)
    assert all(spec.port_clock[p] == "src_clk" for p in PAYLOAD)


@pytest.mark.parametrize("form", list(FORMS))
def test_api_path_matches_cli_path(tmp_path: Path, form: str) -> None:
    sdc_path = tmp_path / "x.sdc"
    sdc_path.write_text(_BASE_SDC + FORMS[form])
    result = CliRunner().invoke(
        app, ["analyze", "-n", str(NETLIST), "-s", str(sdc_path), "-f", "json"]
    )
    assert result.exit_code in (0, 1), result.output
    report = json.loads(result.stdout)
    cli = Counter(v["rule_id"] for v in report["violations"])

    module = _module()
    spec = _spec(form)
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = filter_async(
        find_crossings(module, port_clock=spec.port_clock, **spec.clock_root_inputs()),
        spec,
    )
    api = Counter(v.rule_id for v in run_all(module, crossings, spec))
    assert api == cli == Counter()
    assert len(crossings) == report["summary"]["crossings"]


def test_resolution_runs_once_per_spec_and_netlist(tmp_path: Path) -> None:
    """The CLI resolves explicitly (to surface the warnings) and then
    synthesizes; the second, implicit resolution is a no-op, so a warning
    is reported once."""
    module = _module()
    spec = sdc_mod.parse(_BASE_SDC + "set_input_delay -clock src_clk 0.5 {nope*}\n")
    warnings = sdc_objects.resolve_port_delays(module, spec)
    assert len(warnings) == 1 and "'nope*'" in warnings[0]
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    _build_context(module, spec)
    assert sdc_objects.resolve_port_delays(module, spec) == []
    assert not any("nope" in w for w in spec.partial_warnings)


def test_implicit_resolution_reports_its_warnings() -> None:
    """Without the explicit call, synthesize records the warnings on the
    spec, where the CLI would have put them."""
    spec = sdc_mod.parse(_BASE_SDC + "set_input_delay -clock src_clk 0.5 {nope*}\n")
    sdc_mod.synthesize_unconstrained_inputs(spec, _module())
    assert sum("'nope*'" in w for w in spec.partial_warnings) == 1


def test_a_new_delay_after_resolution_is_resolved() -> None:
    module = _module()
    spec = _spec("brace")
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    spec.port_delays.append(
        sdc_mod.PortDelay("set_output_delay", "dst_clk", ("cap_?",))
    )
    _build_context(module, spec)
    assert spec.port_clock["cap_a"] == "dst_clk"
