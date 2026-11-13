"""Negative-case fixture for CDC-025's asynchronous-merge shape (#329).

``bad_output_async_merge`` has two top-level outputs whose bits are a
combinational function of launches from ``clk_a`` and ``clk_b``, which the
SDC declares asynchronous: ``irq`` ORs a flag from each domain, and
``status`` gates a ``clk_a`` bus with a ``clk_b`` enable. No output carries
a ``set_output_delay`` contract. Two controls stay silent: ``mixed`` puts a
``clk_a`` pair beside a ``clk_b`` pair without merging them, and ``q_a`` is
a plain register — single-domain outputs are only reported once the SDC
types some output, and this one types none. CDC-025 must fire exactly
twice, as errors, and nothing else may fire.

The end-to-end tests go through the CLI so the text / JSON / SARIF
reporters and the waiver path see the findings exactly as a user does.
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

NAME = "bad_output_async_merge"
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
    return crossings, run_all_rules(module, filter_async(crossings, spec), spec)


def _by_port(violations) -> dict[str, object]:
    out: dict[str, object] = {}
    for v in violations:
        port = v.message.split("`", 2)[1]
        assert port not in out, f"two findings for {port}"
        out[port] = v
    return out


def test_only_cdc_025_fires_once_per_merging_output() -> None:
    crossings, violations = _violations()
    # The launches never meet inside the design: no crossing at all.
    assert crossings == []
    assert {v.rule_id for v in violations} == {"CDC-025"}
    assert sorted(_by_port(violations)) == ["irq", "status"]
    assert {v.severity for v in violations} == {"error"}


def test_messages_name_both_domains_and_the_merged_bits() -> None:
    _, violations = _violations()
    msgs = {p: v.message for p, v in _by_port(violations).items()}
    assert "`irq` combinationally merges launches from clk_a (flop `" in msgs["irq"]
    assert ") and clk_b (flop `" in msgs["irq"]
    assert "bits)" not in msgs["irq"]
    # A clk_b select steering a clk_a bus merges every bit of it.
    assert "`status` (4 of 4 bits)" in msgs["status"]
    assert "no set_output_delay -clock contract" in msgs["status"]


def test_findings_anchor_on_the_merging_cell() -> None:
    _, violations = _violations()
    module = netlist.load(JSON)
    by_port = _by_port(violations)
    assert module.cells[by_port["irq"].cell_name].type == "$or"
    assert module.cells[by_port["status"].cell_name].type == "$mux"


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
    assert "CDC-025" in result.output
    assert "no set_output_delay contract" in result.output
    assert "2 error" in result.output


def test_cli_json_report() -> None:
    result = _analyze("--format", "json")
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["summary"]["violations"] == 2
    assert payload["summary"]["crossings"] == 0
    rows = payload["violations"]
    assert {r["rule_id"] for r in rows} == {"CDC-025"}
    for r in rows:
        assert r["severity"] == "error"
        assert r["location"]["file"].endswith(f"{NAME}.sv")
        assert "crossing" not in r


def test_cli_sarif_report() -> None:
    result = _analyze("--format", "sarif")
    assert result.exit_code == 1, result.output
    run = json.loads(result.output)["runs"][0]
    rules = {r["id"]: r for r in run["tool"]["driver"]["rules"]}
    assert "set_output_delay" in rules["CDC-025"]["shortDescription"]["text"]
    results = [r for r in run["results"] if r["ruleId"] == "CDC-025"]
    assert len(results) == 2
    assert {r["level"] for r in results} == {"error"}


# --- intentional merge: the finding stays visible as suppressed -----------


def test_waiver_keeps_the_finding_in_every_report(tmp_path: Path) -> None:
    waivers = tmp_path / "cdc.waivers"
    waivers.write_text("waive CDC-025 `irq` receiver treats irq as a level, 2FF\n")

    result = _analyze("--format", "json", "--waivers", str(waivers))
    payload = json.loads(result.output)
    assert payload["summary"]["violations"] == 1
    assert payload["summary"]["suppressed"] == 1
    (sup,) = payload["suppressed"]
    assert sup["rule_id"] == "CDC-025"
    assert "`irq`" in sup["message"]
    assert sup["waiver"]["reason"].startswith("receiver treats irq")

    result = _analyze("--format", "sarif", "--waivers", str(waivers))
    results = json.loads(result.output)["runs"][0]["results"]
    waived = [r for r in results if "suppressions" in r]
    assert len(waived) == 1
    assert "`irq`" in waived[0]["message"]["text"]

    result = _analyze("--waivers", str(waivers))
    assert "Suppressed by waivers (1)" in result.output


def test_fully_waived_run_exits_zero(tmp_path: Path) -> None:
    waivers = tmp_path / "cdc.waivers"
    waivers.write_text("waive CDC-025 .* merged status outputs, receiver tolerates\n")
    result = _analyze("--waivers", str(waivers))
    assert result.exit_code == 0, result.output
    assert "Suppressed by waivers (2)" in result.output


_HAVE_FRONTEND = (
    importlib.util.find_spec("pyslang") is not None or shutil.which("yosys") is not None
)


@pytest.mark.skipif(not _HAVE_FRONTEND, reason="needs pyslang or yosys on PATH")
def test_rbcdc_pragma_waives_the_output_it_wraps(tmp_path: Path) -> None:
    """``// rbcdc: disable-rule CDC-025`` around the merging logic is the
    in-RTL form of the same waiver: the finding is anchored on the cell
    that merges, so the pragma block scopes to it."""
    src = SV.read_text()
    marker = "    assign irq    = flag_a | flag_b;\n"
    assert marker in src
    sv = tmp_path / f"{NAME}.sv"
    sv.write_text(
        src.replace(
            marker,
            "    // rbcdc: disable-rule CDC-025 receiver 2FF-synchronises irq\n"
            + marker
            + "    // rbcdc: enable-rule CDC-025\n",
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
    assert kept == ["status"]
    (sup,) = payload["suppressed"]
    assert "`irq`" in sup["message"]
    assert sup["waiver"]["origin"].endswith(f"{NAME}.sv")
