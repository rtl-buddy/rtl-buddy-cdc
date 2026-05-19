"""End-to-end: ``--reset-hints`` makes RDC-002 fire on the
``bad_hints_reset_polarity`` fixture identically to the
SV-attribute case (``bad_marked_reset_polarity``). Issue #129.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.reset_hints import load as load_reset_hints
from rtl_buddy_cdc.rules import run_all

PYYAML_INSTALLED = importlib.util.find_spec("yaml") is not None
pytestmark = pytest.mark.skipif(
    not PYYAML_INSTALLED, reason="[hints] extra not installed"
)

FIX = Path(__file__).parent / "fixtures" / "bad_hints_reset_polarity"
runner = CliRunner()


def _run_with_hints(use_hints: bool):
    module = netlist.load(FIX / "bad_hints_reset_polarity.json")
    spec = sdc_mod.parse_file(FIX / "bad_hints_reset_polarity.sdc")
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = find_crossings(
        module, port_clock=spec.port_clock, pin_clocks=spec.pin_clocks
    )
    async_cs = [
        c
        for c in crossings
        if spec.are_async(
            spec.clock_for_port(c.src_clock) or c.src_clock,
            spec.clock_for_port(c.dst_clock) or c.dst_clock,
        )
    ]
    hints = (
        load_reset_hints(FIX / "bad_hints_reset_polarity.hints.yaml")
        if use_hints
        else None
    )
    return run_all(module, async_cs, spec, reset_hints=hints)


def test_without_hints_rdc_002_silent() -> None:
    """No SV attribute, no hints — analyzer has no polarity reference,
    so RDC-002 stays silent. The fixture is engineered to need the
    external declaration."""
    violations = _run_with_hints(use_hints=False)
    rdc_002 = [v for v in violations if v.rule_id == "RDC-002"]
    assert rdc_002 == [], f"unexpected RDC-002: {rdc_002}"


def test_with_hints_rdc_002_fires_polarity_mismatch() -> None:
    """With the hints file declaring ``rst_n`` active-low, RDC-002
    fires on the flop wired ``posedge rst_n`` — identical to the
    SV-attribute path on ``bad_marked_reset_polarity``."""
    violations = _run_with_hints(use_hints=True)
    rdc_002 = [v for v in violations if v.rule_id == "RDC-002"]
    assert len(rdc_002) == 1, [v.message for v in violations]
    assert "rst_n" in rdc_002[0].message
    assert "polarity" in rdc_002[0].message.lower()


def test_cli_reset_hints_flag_end_to_end() -> None:
    """The ``--reset-hints`` flag should plumb through the CLI and
    drive RDC-002 to fire on the unannotated fixture."""
    result = runner.invoke(
        app,
        [
            "analyze",
            "--netlist",
            str(FIX / "bad_hints_reset_polarity.json"),
            "--sdc",
            str(FIX / "bad_hints_reset_polarity.sdc"),
            "--reset-hints",
            str(FIX / "bad_hints_reset_polarity.hints.yaml"),
            "--format",
            "json",
        ],
    )
    # Exit 1 because at least one violation fires; that's the success
    # signal for an analyzer run with findings, not an error.
    assert result.exit_code == 1, result.output
    assert "RDC-002" in result.output


def test_cli_without_hints_flag_is_silent() -> None:
    """Without ``--reset-hints``, the same fixture produces no
    findings — the analyzer has nothing to compare polarity against."""
    result = runner.invoke(
        app,
        [
            "analyze",
            "--netlist",
            str(FIX / "bad_hints_reset_polarity.json"),
            "--sdc",
            str(FIX / "bad_hints_reset_polarity.sdc"),
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
