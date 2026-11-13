"""Negative-case fixture for CDC-024 (issue #323).

``bad_output_async_contract`` has four top-level outputs launched in
``clk_a`` whose ``set_output_delay -clock`` names a receiving clock the
SDC declares asynchronous to ``clk_a``: a directly registered output, a
combinational cone of two flops, a registered 4-bit vector, and a 4-bit
vector only half of which is launched in ``clk_a``. Nothing inside the
design captures them, so before #323 the run was clean. CDC-024 must
fire exactly once per output port — and nothing else may fire.

The end-to-end tests go through the CLI so the text / JSON / SARIF
reporters and the waiver path see the finding exactly as a user does.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.domain import filter_async, find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

NAME = "bad_output_async_contract"
FIX_DIR = Path(__file__).parent / "fixtures" / NAME
JSON = FIX_DIR / f"{NAME}.json"
SDC = FIX_DIR / f"{NAME}.sdc"
SV = FIX_DIR / f"{NAME}.sv"

runner = CliRunner()


@pytest.fixture(autouse=True)
def _every_sdc_backend(sdc_backend):
    """Run every test here once per SDC reader backend (#298)."""
    return sdc_backend


def _violations():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = find_crossings(
        module,
        port_clock=spec.port_clock,
        pin_clocks=spec.pin_clocks,
        clock_for_port=spec.clock_for_port,
    )
    async_crossings = filter_async(crossings, spec)
    return crossings, run_all_rules(module, async_crossings, spec)


def _by_port(violations) -> dict[str, str]:
    out: dict[str, str] = {}
    for v in violations:
        port = v.message.split("`", 2)[1]
        assert port not in out, f"two findings for {port}"
        out[port] = v.message
    return out


def test_only_cdc_024_fires_once_per_output() -> None:
    crossings, violations = _violations()
    # No internal crossing exists: the receiver is outside the design.
    assert crossings == []
    assert sorted({v.rule_id for v in violations}) == ["CDC-024"]
    assert sorted(_by_port(violations)) == ["q_bus", "q_comb", "q_mix", "q_reg"]
    assert all(v.severity == "warning" for v in violations)


def test_messages_name_port_launch_domain_and_receiving_clock() -> None:
    _, violations = _violations()
    msgs = _by_port(violations)
    # Direct registered output, contracted to a VIRTUAL clock.
    assert "launched from clk_a (flop `" in msgs["q_reg"]
    assert "names receiving clock vclk_b" in msgs["q_reg"]
    # Combinational cone of two clk_a flops: both launchers named.
    assert msgs["q_comb"].count("flop `") == 2
    assert "names receiving clock vclk_b" in msgs["q_comb"]
    # Vector, wholly clk_a.
    assert "`q_bus` (4 of 4 bits)" in msgs["q_bus"]
    assert "names receiving clock clk_b" in msgs["q_bus"]
    # Vector whose low half is launched in the contract's own clock:
    # only the clk_a half is reported.
    assert "`q_mix` (2 of 4 bits)" in msgs["q_mix"]
    assert "launched from clk_a" in msgs["q_mix"]
    # A 1-bit output carries no bit count.
    assert "bits)" not in msgs["q_reg"]


def test_findings_anchor_on_the_driving_cell() -> None:
    """``cell_name`` is the cell driving the output (the register itself
    for a registered output, the XOR for the comb cone) so the reported
    source location is where the output gets its value."""
    _, violations = _violations()
    module = netlist.load(JSON)
    by_port = {v.message.split("`", 2)[1]: v for v in violations}
    assert module.cells[by_port["q_reg"].cell_name].type == "$dff"
    assert module.cells[by_port["q_comb"].cell_name].type == "$xor"


# --- end to end through the CLI -------------------------------------------


def _analyze(*extra: str):
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    return runner.invoke(
        app, ["analyze", "--netlist", str(JSON), "--sdc", str(SDC), *extra]
    )


def test_cli_text_report_and_exit_code() -> None:
    result = _analyze()
    assert result.exit_code == 1, result.output
    assert "CDC-024" in result.output
    assert "Top-level output launched asynchronously" in result.output
    assert "4 warning" in result.output


def test_cli_json_report() -> None:
    result = _analyze("--format", "json")
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["summary"]["violations"] == 4
    assert payload["summary"]["crossings"] == 0
    rows = payload["violations"]
    assert {r["rule_id"] for r in rows} == {"CDC-024"}
    for r in rows:
        assert r["severity"] == "warning"
        assert r["cell_name"]
        assert r["location"]["file"].endswith(f"{NAME}.sv")
        # Not a crossing: no synthetic sink leaks into the report.
        assert "crossing" not in r


def test_cli_sarif_report() -> None:
    result = _analyze("--format", "sarif")
    assert result.exit_code == 1, result.output
    run = json.loads(result.output)["runs"][0]
    rules = {r["id"]: r for r in run["tool"]["driver"]["rules"]}
    assert rules["CDC-024"]["defaultConfiguration"]["level"] == "warning"
    assert "set_output_delay" in rules["CDC-024"]["shortDescription"]["text"]
    results = [r for r in run["results"] if r["ruleId"] == "CDC-024"]
    assert len(results) == 4
    assert all("suppressions" not in r for r in results)


def test_strict_promotes_to_error() -> None:
    result = _analyze("--format", "json", "--strict")
    payload = json.loads(result.output)
    assert {r["severity"] for r in payload["violations"]} == {"error"}


# --- intentional asynchronous output: the obligation stays visible --------

_WAIVER = (
    "waive CDC-024 `q_comb` receiver synchronises q_comb through a 2FF on vclk_b\n"
)


def test_waiver_keeps_the_obligation_in_every_report(tmp_path: Path) -> None:
    """An intentionally asynchronous output is accepted with an ordinary
    waiver. The finding leaves the exit-code-driving list but stays in
    the report — message and reason — so the receiver's synchronisation
    obligation is recorded rather than silently dropped."""
    waivers = tmp_path / "cdc.waivers"
    waivers.write_text(_WAIVER)

    result = _analyze("--format", "json", "--waivers", str(waivers))
    payload = json.loads(result.output)
    assert payload["summary"]["violations"] == 3
    assert payload["summary"]["suppressed"] == 1
    (sup,) = payload["suppressed"]
    assert sup["rule_id"] == "CDC-024"
    assert "`q_comb`" in sup["message"]
    assert "receiving design must synchronise" in sup["message"]
    assert sup["waiver"]["reason"].startswith("receiver synchronises q_comb")

    result = _analyze("--format", "sarif", "--waivers", str(waivers))
    results = json.loads(result.output)["runs"][0]["results"]
    waived = [r for r in results if "suppressions" in r]
    assert len(waived) == 1
    assert "`q_comb`" in waived[0]["message"]["text"]
    assert waived[0]["suppressions"][0]["justification"].startswith(
        "receiver synchronises"
    )

    result = _analyze("--waivers", str(waivers))
    assert "Suppressed by waivers (1)" in result.output
    assert "receiver synchronises q_comb" in result.output


def test_fully_waived_run_exits_zero(tmp_path: Path) -> None:
    waivers = tmp_path / "cdc.waivers"
    waivers.write_text("waive CDC-024 .* outputs re-synchronised by the receiver\n")
    result = _analyze("--waivers", str(waivers))
    assert result.exit_code == 0, result.output
    assert "Suppressed by waivers (4)" in result.output


# --- in-RTL pragma (lint path) --------------------------------------------

_HAVE_FRONTEND = (
    importlib.util.find_spec("pyslang") is not None or shutil.which("yosys") is not None
)


@pytest.mark.skipif(not _HAVE_FRONTEND, reason="needs pyslang or yosys on PATH")
def test_rbcdc_pragma_waives_the_output_it_wraps(tmp_path: Path) -> None:
    """``// rbcdc: disable-rule CDC-024`` around the logic that drives an
    output is the in-RTL form of the same waiver: the finding is
    anchored on the driving cell, so the pragma block scopes to it."""
    src = SV.read_text()
    marker = "    assign q_comb = r0 ^ r1;\n"
    assert marker in src
    sv = tmp_path / f"{NAME}.sv"
    sv.write_text(
        src.replace(
            marker,
            "    // rbcdc: disable-rule CDC-024 receiver 2FF-synchronises q_comb\n"
            + marker
            + "    // rbcdc: enable-rule CDC-024\n",
        )
    )
    frontend = "slang" if importlib.util.find_spec("pyslang") else "yosys"
    result = runner.invoke(
        app,
        [
            "lint",
            "--frontend",
            frontend,
            "--top",
            NAME,
            "--sdc",
            str(SDC),
            "--format",
            "json",
            str(sv),
        ],
    )
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    kept = sorted(v["message"].split("`", 2)[1] for v in payload["violations"])
    assert kept == ["q_bus", "q_mix", "q_reg"]
    (sup,) = payload["suppressed"]
    assert "`q_comb`" in sup["message"]
    assert sup["waiver"]["origin"].endswith(f"{NAME}.sv")
