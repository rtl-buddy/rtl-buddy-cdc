"""The rule context and the crossing walk name a flop's domain alike
(rtl-buddy-cdc#358).

``rules._build_context`` used to trace every flop's clock without the
SDC's generated-clock pins and without the ``clock_for_port``
normalisation, while ``find_crossings`` (on the CLI path) had both. Rules
that compare ``ctx.domains[f]`` with a crossing's ``dst_clock`` — the
sync-chain walk above all — then compared a raw port name with a clock
name, and a correct 2FF synchroniser read as depth 1:

- on a pin-declared generated clock (``good_gen_clock_chain``);
- on *any* clock whose ``-name`` differs from its port
  (``create_clock -name core [get_ports clk]``) — every fixture verdict
  changed under a pure clock rename.

Both views are now built from ``ClockSpec.clock_root_inputs()``. The leg
choice at a clock gate counts only ``create_clock`` ports as clocks in
every view, so an ICG whose enable is ``set_input_delay``-typed to a
foreign clock is not a two-clock combine (no false CDC-023).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import netlist, sdc
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.compositional import analyse_module
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.netlist import Cell, Module, Port
from rtl_buddy_cdc.rules import _build_context, run_all

FIX = Path(__file__).parent / "fixtures"
runner = CliRunner()


def _analyze(netlist_path: Path, sdc_path: Path) -> dict:
    res = runner.invoke(
        app,
        [
            "analyze",
            "--netlist",
            str(netlist_path),
            "--sdc",
            str(sdc_path),
            "-f",
            "json",
        ],
    )
    assert res.exit_code in (0, 1), res.output
    return json.loads(res.stdout)


def _verdict(report: dict) -> tuple[int, list[tuple[str, str]]]:
    return (
        report["summary"]["async_crossings"],
        sorted((v["rule_id"], v["cell_name"]) for v in report["violations"]),
    )


def _renamed(text: str, names: list[str]) -> str:
    """Rename every clock in ``names`` to ``CK_<name>`` — the clock name
    only, never the port or pin it sits on."""
    for n in sorted(names, key=len, reverse=True):
        word = r"(?<![\w/])" + re.escape(n) + r"(?![\w/])"
        text = re.sub(r"(-name\s+)" + re.escape(n) + r"\b", r"\g<1>CK_" + n, text)
        text = re.sub(
            r"(-(?:clock|master_clock)\s+)" + re.escape(n) + r"\b",
            r"\g<1>CK_" + n,
            text,
        )
        text = re.sub(
            r"(-group\s+\{[^}]*\})",
            lambda m, w=word, n=n: re.sub(w, "CK_" + n, m.group(1)),
            text,
        )
    return text


# Fixtures with port-named clocks, generated pin clocks, a three-clock
# handshake and a greybox: renaming the clocks must change no verdict.
_RENAME_FIXTURES = [
    ("good_2ff_sync", "good_2ff_sync.json"),
    ("good_2ff_sync", "good_2ff_sync.gates.json"),
    ("good_gen_clock_chain", "good_gen_clock_chain.json"),
    ("good_dffe_gated_bus_crossing", "good_dffe_gated_bus_crossing.json"),
    ("bad_handshake_ack_missing", "bad_handshake_ack_missing.json"),
    ("good_four_clock_mixed", "good_four_clock_mixed.json"),
    ("bbx_residual_declines", "bbx_residual_declines.grey.json"),
    ("multi_clock_blackbox", "multi_clock_blackbox.grey.json"),
]


@pytest.mark.parametrize(("fixture", "netlist_name"), _RENAME_FIXTURES)
def test_renaming_the_clocks_changes_no_verdict(
    tmp_path: Path, fixture: str, netlist_name: str
) -> None:
    src = (FIX / fixture / f"{fixture}.sdc").read_text()
    names = list(sdc.parse(src, backend="tokenizer").clocks)
    renamed = tmp_path / f"{fixture}.sdc"
    renamed.write_text(_renamed(src, names))
    assert set(sdc.parse(renamed.read_text(), backend="tokenizer").clocks) == {
        f"CK_{n}" for n in names
    }
    netlist_path = FIX / fixture / netlist_name
    assert _verdict(_analyze(netlist_path, renamed)) == _verdict(
        _analyze(netlist_path, FIX / fixture / f"{fixture}.sdc")
    )


@pytest.mark.parametrize(
    "fixture",
    [
        "good_gen_clock_chain",
        "good_case_analysis_mode_sel0",
        "bad_case_analysis_mode_sel1",
        "good_multi_clock_port",
        "good_exclusive_clock_mux",
    ],
)
def test_context_domains_equal_the_crossing_walk(fixture: str) -> None:
    """Every crossing's endpoint clocks are the context's domains of its
    endpoint flops — the invariant the sync-chain walk relies on."""
    module = netlist.load(FIX / fixture / f"{fixture}.json")
    spec = sdc.parse_file(FIX / fixture / f"{fixture}.sdc", backend="tokenizer")
    sdc.synthesize_unconstrained_inputs(spec, module)
    crossings = find_crossings(
        module, port_clock=spec.port_clock, **spec.clock_root_inputs()
    )
    assert crossings
    ctx = _build_context(module, spec)
    for c in crossings:
        assert ctx.domains[c.dst_flop.cell.name] == c.dst_clock
        if c.src_flop is not None:
            assert ctx.domains[c.src_flop.cell.name] == c.src_clock


def test_generated_pin_clock_is_the_context_domain() -> None:
    module = netlist.load(FIX / "good_gen_clock_chain" / "good_gen_clock_chain.json")
    spec = sdc.parse_file(
        FIX / "good_gen_clock_chain" / "good_gen_clock_chain.sdc", backend="tokenizer"
    )
    ctx = _build_context(module, spec)
    assert {d for d in ctx.domains.values()} >= {"clk", "ck_div2", "ck_div4"}
    # The raw root of a pin-clocked flop is the generated clock, which
    # names no port: CDC-021 stays out of it.
    assert "ck_div4" in set(ctx.clock_roots.values())


_ICG_SDC = """
create_clock -name clk -period 10.0 [get_ports clk]
create_clock -name vclk -period 13.0
set_clock_groups -asynchronous -group {clk} -group {vclk}
set_input_delay -clock vclk 1.0 [get_ports en]
set_input_delay -clock clk 1.0 [get_ports d]
"""


def _icg(gate_type: str, a: int, b: int) -> Module:
    # Bits: clk=2, en=3, d=4, q=5, gated clock=6.
    return Module(
        name="top",
        ports={
            "clk": Port("clk", "input", (2,)),
            "en": Port("en", "input", (3,)),
            "d": Port("d", "input", (4,)),
            "q": Port("q", "output", (5,)),
        },
        cells={
            "g": Cell("g", gate_type, {"A": (a,), "B": (b,), "Y": (6,)}),
            "f": Cell("f", "$_DFF_P_", {"C": (6,), "D": (4,), "Q": (5,)}),
        },
        netnames={},
    )


@pytest.mark.parametrize(("a", "b"), [(2, 3), (3, 2)], ids=["clk-first", "en-first"])
def test_enable_typed_to_a_foreign_clock_is_not_a_clock_combine(a: int, b: int) -> None:
    """``en`` is launched by ``vclk`` but is not a clock: the gate is an
    ordinary ICG on ``clk`` in the crossing walk and the rule context
    alike, and CDC-023 reports no combine of ``{clk, vclk}``."""
    spec = sdc.parse(_ICG_SDC, backend="tokenizer")
    module = _icg("$_AND_", a, b)
    ctx = _build_context(module, spec)
    assert ctx.domains["f"] == "clk"
    assert ctx.clock_roots["f"] == "clk"
    from rtl_buddy_cdc.domain import assign_domains

    (fd,) = assign_domains(module, **spec.clock_root_inputs())
    assert fd.clock == "clk"
    assert not [v for v in run_all(module, [], spec) if v.rule_id == "CDC-023"]


def test_greybox_internal_pass_names_domains_like_its_crossings() -> None:
    """The per-module pass (rtl-buddy-cdc#261) ran ``run_all`` with a
    context built without the derived spec's normaliser, so inside a
    greybox a flop was ``clk`` (its pin) while its crossings said the
    parent's clock name."""
    sub = netlist.load(
        FIX / "good_2ff_sync" / "good_2ff_sync.json"
    )  # a 2FF synchroniser module with clock pins src_clk / dst_clk
    spec = sdc.parse(
        "create_clock -name A -period 10 [get_ports pa]\n"
        "create_clock -name B -period 7 [get_ports pb]\n"
        "set_clock_groups -asynchronous -group {A} -group {B}\n",
        backend="tokenizer",
    )
    analysis = analyse_module(sub, {"src_clk": "A", "dst_clk": "B"}, spec)
    assert analysis is not None
    assert analysis.resolved
    assert [v.rule_id for v in analysis.violations] == []
