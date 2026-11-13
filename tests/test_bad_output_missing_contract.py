"""Negative-case fixture for CDC-025's self-calibrating warning (#329).

``bad_output_missing_contract``'s SDC types ``q_a`` with
``set_output_delay -clock clk_a`` and leaves the other data outputs
uncontracted. Because the user has started typing outputs, a missing
contract is likely an omission: CDC-025 warns once each on ``q_b`` (a
``clk_b`` register), ``q_bus`` (a 4-bit ``clk_a`` register — one finding
for the bus) and ``q_pair`` (a ``clk_a`` bit beside a ``clk_b`` bit, which
no single contract can describe). Every exclusion is present and silent:
a forwarded clock, a gated clock, a divider that also clocks internal
logic, a constant and an input feed-through. Nothing else may fire.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.domain import filter_async, find_crossings
from rtl_buddy_cdc.rules import check_cdc_025, run_all as run_all_rules

NAME = "bad_output_missing_contract"
FIX_DIR = Path(__file__).parent / "fixtures" / NAME
JSON = FIX_DIR / f"{NAME}.json"
SDC = FIX_DIR / f"{NAME}.sdc"

runner = CliRunner()


@pytest.fixture(autouse=True)
def _every_sdc_backend(sdc_backend):
    """Run every test here once per SDC reader backend (#298)."""
    return sdc_backend


def _load():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    return module, spec


def _violations():
    module, spec = _load()
    crossings = find_crossings(
        module,
        port_clock=spec.port_clock,
        pin_clocks=spec.pin_clocks,
        clock_for_port=spec.clock_for_port,
    )
    return crossings, run_all_rules(module, filter_async(crossings, spec), spec)


def _by_port(violations) -> dict[str, str]:
    out: dict[str, str] = {}
    for v in violations:
        port = v.message.split("`", 2)[1]
        assert port not in out, f"two findings for {port}"
        out[port] = v.message
    return out


def test_only_cdc_025_warns_once_per_uncontracted_data_output() -> None:
    crossings, violations = _violations()
    assert crossings == []
    assert {v.rule_id for v in violations} == {"CDC-025"}
    assert {v.severity for v in violations} == {"warning"}
    # q_a is contracted; clk_fwd / clk_gated / div_o / tie_o / thru_o are
    # the exclusions.
    assert sorted(_by_port(violations)) == ["q_b", "q_bus", "q_pair"]


def test_messages_name_the_launch_domain() -> None:
    _, violations = _violations()
    msgs = _by_port(violations)
    assert "`q_b` is launched from clk_b (flop `" in msgs["q_b"]
    assert "Other outputs in this SDC do carry contracts" in msgs["q_b"]
    assert "set_output_delay -clock <receiving clock> [get_ports q_b]" in msgs["q_b"]
    assert "`q_bus` (4 of 4 bits) is launched from clk_a" in msgs["q_bus"]
    assert "clk_a (flop `" in msgs["q_pair"]
    assert ") and clk_b (flop `" in msgs["q_pair"]
    assert "split it per domain" in msgs["q_pair"]


def test_exclusions_hold_even_with_the_single_domain_shape_forced_on() -> None:
    module, spec = _load()
    found = check_cdc_025(module, [], spec, single_domain=True)
    assert sorted(_by_port(found)) == ["q_b", "q_bus", "q_pair"]


def test_dropping_the_only_contract_silences_the_warnings(tmp_path: Path) -> None:
    """The self-calibrating gate: with no output contracted at all, the
    single-domain shape is off — the state every pre-#329 design is in."""
    sdc = tmp_path / f"{NAME}.sdc"
    sdc.write_text(
        "\n".join(
            line
            for line in SDC.read_text().splitlines()
            if not line.startswith("set_output_delay")
        )
        + "\n"
    )
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    result = runner.invoke(app, ["analyze", "--netlist", str(JSON), "--sdc", str(sdc)])
    assert result.exit_code == 0, result.output


def test_cli_json_and_strict() -> None:
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    args = ["analyze", "--netlist", str(JSON), "--sdc", str(SDC), "--format", "json"]
    result = runner.invoke(app, args)
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["summary"]["violations"] == 3
    assert {r["severity"] for r in payload["violations"]} == {"warning"}
    strict = json.loads(runner.invoke(app, [*args, "--strict"]).output)
    assert {r["severity"] for r in strict["violations"]} == {"error"}


def test_cli_sarif_level_is_warning() -> None:
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    result = runner.invoke(
        app,
        ["analyze", "--netlist", str(JSON), "--sdc", str(SDC), "--format", "sarif"],
    )
    results = json.loads(result.output)["runs"][0]["results"]
    assert len(results) == 3
    assert {r["level"] for r in results} == {"warning"}
