"""DFT scan-mode crossing suppression — ``--ignore-scan-mode`` (#45).

Issue #44 taught the analyzer to *recognise* the scan-mode attributes;
this is the half that acts on them. The contract has four parts and
each is pinned below:

1. **Detection rides the existing walk.** ``find_scan_mode_flops``
   attaches a :data:`~rtl_buddy_cdc.domain.ClockControlSink` recorder
   to the same ``trace_clock_root`` call that assigns the flop's
   domain — the #269 ``on_combine`` pattern — so "this flop is clocked
   through a scan mux" and "the tracer walked a mux to get here" are
   the same event, not two predicates that agree today.
2. **Tag, don't drop.** The crossing is always emitted and always
   counted; ``Crossing.scan_mode`` is a label on it.
3. **Suppression is opt-in.** Without ``--ignore-scan-mode`` the report
   is byte-for-byte what it was before the flag existed.
4. **Nothing is silent.** With the flag, the skipped crossings surface
   as a tally line and as ``summary.scan_mode_suppressed``.

The paired fixtures make the boundary of (3) concrete:
``good_scan_mode_ignored`` is *entirely* scan-path and goes clean under
the flag, while ``bad_scan_mode_functional_crossing`` carries the same
scan mux plus an ordinary functional crossing that must survive it — a
flag that cleared that design would be hiding a real bug behind a DFT
annotation.
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import click
import pytest
import typer.main
from typer.testing import CliRunner

from rtl_buddy_cdc import netlist, reporter, sdc as sdc_mod
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.domain import (
    filter_async,
    find_crossings,
    find_scan_mode_flops,
)
from rtl_buddy_cdc.netlist import Cell, Module, Netname, Port
from rtl_buddy_cdc.rules import (
    run_all as run_all_rules,
    scan_mode_clock_select_flops,
)

FIX = Path(__file__).parent / "fixtures"
runner = CliRunner()

GOOD = "good_scan_mode_ignored"
CONTROL = "bad_scan_mode_functional_crossing"


def _paths(name: str) -> tuple[Path, Path]:
    d = FIX / name
    return d / f"{name}.json", d / f"{name}.sdc"


def _analyse(name: str, *, ignore_scan_mode: bool):
    """Run the fixture through the same sequence the CLI uses."""
    json_path, sdc_path = _paths(name)
    module = netlist.load(json_path)
    spec = sdc_mod.parse_file(sdc_path)
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    scan_flops = frozenset(
        scan_mode_clock_select_flops(
            module, pin_clocks=spec.pin_clocks, clock_for_port=spec.clock_for_port
        )
    )
    crossings = find_crossings(
        module,
        port_clock=spec.port_clock,
        pin_clocks=spec.pin_clocks,
        clock_for_port=spec.clock_for_port,
        scan_mode_flops=scan_flops,
    )
    async_crossings = filter_async(crossings, spec)
    violations = run_all_rules(
        module, async_crossings, spec, ignore_scan_mode=ignore_scan_mode
    )
    return async_crossings, violations


# --- fixture behaviour ------------------------------------------------------


def test_good_fixture_fires_without_the_flag() -> None:
    """Default behaviour is today's behaviour: the scan mux resolves to
    its first leg (``func_clk``), the source sits in ``scan_clk``, and
    the resulting single-stage crossing trips CDC-001. Pinning the
    exact rule id matters — the flag's value is that this specific,
    reproducible finding goes away."""
    crossings, violations = _analyse(GOOD, ignore_scan_mode=False)
    assert len(crossings) == 1
    assert [v.rule_id for v in violations] == ["CDC-001"]


def test_good_fixture_is_clean_with_the_flag() -> None:
    """The whole design is scan-path, so the flag clears it."""
    _crossings, violations = _analyse(GOOD, ignore_scan_mode=True)
    assert violations == []


def test_scan_crossing_is_tagged_not_dropped() -> None:
    """The crossing survives into the report either way — the flag
    changes what the *rules* look at, never what the walk found."""
    for ignore in (False, True):
        crossings, _violations = _analyse(GOOD, ignore_scan_mode=ignore)
        assert len(crossings) == 1
        assert crossings[0].scan_mode is True


def test_control_fixture_functional_crossing_survives_the_flag() -> None:
    """The flag is not a blanket "stop checking this design": the
    functional ``other_clk -> func_clk`` crossing has no mux in its
    destination's clock network, is never tagged, and still fires."""
    crossings, violations = _analyse(CONTROL, ignore_scan_mode=True)
    assert sorted(c.scan_mode for c in crossings) == [False, True]
    assert [v.rule_id for v in violations] == ["CDC-001"]
    survivor = next(c for c in crossings if not c.scan_mode)
    assert violations[0].crossing is not None
    assert violations[0].crossing.dst_flop.name == survivor.dst_flop.name


def test_control_fixture_fires_twice_without_the_flag() -> None:
    """Both crossings are real findings by default; only one of them is
    the flag's business."""
    _crossings, violations = _analyse(CONTROL, ignore_scan_mode=False)
    assert [v.rule_id for v in violations] == ["CDC-001", "CDC-001"]


def test_only_the_scan_clocked_flop_is_detected() -> None:
    """``scan_mode_clock_select_flops`` names the flop behind the mux
    and nothing else — in particular not the scan-domain source flop,
    whose CLK is wired straight to ``scan_clk``."""
    module = netlist.load(_paths(CONTROL)[0])
    detected = scan_mode_clock_select_flops(module)
    crossings, _v = _analyse(CONTROL, ignore_scan_mode=False)
    tagged = {c.dst_flop.cell.name for c in crossings if c.scan_mode}
    assert detected == tagged
    assert len(detected) == 1


# --- helper unit behaviour --------------------------------------------------


def _clock_gate_module(*, attrs: dict[str, str]) -> Module:
    """``gclk = func_clk & test_en`` feeding one flop, with ``attrs`` on
    the ``test_en`` input port. The gate variant of the scan structure:
    a test-mode pin forcing an ICG's enable rather than steering a mux."""
    func_clk, test_en, gclk, d, q = 1, 2, 3, 4, 5
    return Module(
        name="m",
        ports={
            "func_clk": Port(name="func_clk", direction="input", bits=(func_clk,)),
            "test_en": Port(name="test_en", direction="input", bits=(test_en,)),
        },
        cells={
            "icg": Cell(
                name="icg",
                type="$and",
                connections={"A": (func_clk,), "B": (test_en,), "Y": (gclk,)},
            ),
            "f": Cell(
                name="f",
                type="$dff",
                connections={"CLK": (gclk,), "D": (d,), "Q": (q,)},
            ),
        },
        netnames={
            "func_clk": Netname(name="func_clk", bits=(func_clk,), attributes={}),
            "test_en": Netname(name="test_en", bits=(test_en,), attributes=attrs),
        },
    )


def test_clock_gate_enable_from_a_scan_port_is_detected() -> None:
    """Not only muxes: the non-clock leg of a clock gate is a control
    input too, and a ``test_mode`` pin forcing it is the same DFT
    structure by another shape."""
    module = _clock_gate_module(attrs={"test_mode": "1"})
    assert scan_mode_clock_select_flops(module) == {"f"}


def test_clock_gate_enable_from_an_ordinary_port_is_not_detected() -> None:
    """The same gate with an untagged enable is an ordinary ICG."""
    module = _clock_gate_module(attrs={})
    assert scan_mode_clock_select_flops(module) == set()


def test_no_scan_ports_short_circuits_before_any_walk() -> None:
    """The common case — a design with no DFT annotation at all —
    returns immediately, which is what keeps this free for the other
    140 fixtures."""
    module = netlist.load(_paths("bad_single_ff_sync")[0])
    assert scan_mode_clock_select_flops(module) == set()


def test_find_scan_mode_flops_predicate_is_injected() -> None:
    """``domain`` owns the walk, not the attribute vocabulary: the
    predicate decides. A never-true one detects nothing; an always-true
    one detects exactly the flops whose clock path has a control input
    at all — here the one behind the mux, not the scan-domain source
    flop, whose CLK is wired straight to the port."""
    module = netlist.load(_paths(GOOD)[0])
    assert find_scan_mode_flops(module, is_scan_control=lambda _bits: False) == set()
    assert find_scan_mode_flops(
        module, is_scan_control=lambda _bits: True
    ) == scan_mode_clock_select_flops(module)


# --- reporter surface -------------------------------------------------------


def _report(name: str, *, ignore_scan_mode: bool, fmt: str) -> str:
    json_path, sdc_path = _paths(name)
    args = [
        "analyze",
        "--netlist",
        str(json_path),
        "--sdc",
        str(sdc_path),
        "--format",
        fmt,
    ]
    if ignore_scan_mode:
        args.append("--ignore-scan-mode")
    result = runner.invoke(app, args)
    return result.stdout


def test_text_report_tallies_the_suppression() -> None:
    """A suppression the reader cannot see is indistinguishable from a
    clean design, so the flag always prints what it dropped."""
    out = _report(GOOD, ignore_scan_mode=True, fmt="text")
    assert "1 async crossing suppressed by --ignore-scan-mode" in out
    assert "No rule violations." in out


def test_text_report_says_nothing_without_the_flag() -> None:
    out = _report(GOOD, ignore_scan_mode=False, fmt="text")
    assert "--ignore-scan-mode" not in out


def test_json_summary_carries_the_tally_and_the_tag() -> None:
    """``summary.scan_mode_suppressed`` is additive; the per-crossing
    ``scan_mode`` tag is emitted flag or no flag so a run that
    suppresses nothing is still auditable."""
    off = json.loads(_report(GOOD, ignore_scan_mode=False, fmt="json"))
    on = json.loads(_report(GOOD, ignore_scan_mode=True, fmt="json"))
    assert off["summary"]["scan_mode_suppressed"] == 0
    assert on["summary"]["scan_mode_suppressed"] == 1
    assert off["crossings"][0]["scan_mode"] is True
    assert on["crossings"][0]["scan_mode"] is True
    assert off["summary"]["violations"] == 1
    assert on["summary"]["violations"] == 0
    # The contract keys downstream rtl_buddy reads are untouched.
    for key in ("violations", "suppressed", "crossings"):
        assert isinstance(on["summary"][key], int)


def test_untagged_crossing_omits_the_json_key() -> None:
    """Absent, not ``false`` — the same shape every other optional
    crossing field uses."""
    payload = json.loads(_report(CONTROL, ignore_scan_mode=True, fmt="json"))
    tags = [c.get("scan_mode") for c in payload["crossings"]]
    assert sorted(tags, key=lambda t: t is None) == [True, None]


def test_verbose_crossing_listing_marks_the_scan_path() -> None:
    result = runner.invoke(
        app,
        [
            "analyze",
            "--netlist",
            str(_paths(CONTROL)[0]),
            "--sdc",
            str(_paths(CONTROL)[1]),
            "--verbose",
        ],
    )
    lines = [
        ln for ln in result.stdout.splitlines() if "→ func_clk" in ln and "width=" in ln
    ]
    assert len(lines) == 2
    assert sum("(scan-mode)" in ln for ln in lines) == 1


def test_reporter_tally_defaults_to_zero() -> None:
    """``AnalysisResult`` gains the field additively — every existing
    construction site keeps working and prints nothing new."""
    module = netlist.load(_paths(GOOD)[0])
    result = reporter.AnalysisResult(
        module=module,
        domains=[],
        crossings=[],
        async_crossings=[],
        spec=None,
        violations=[],
    )
    assert result.scan_mode_suppressed == 0
    buf = io.StringIO()
    reporter.render_text(result, buf, color=False)
    assert "--ignore-scan-mode" not in buf.getvalue()


# --- CLI surface ------------------------------------------------------------


def test_exit_codes_flip_with_the_flag() -> None:
    """The downstream contract: 1 with an unsuppressed violation, 0 when
    the only finding was suppressed."""
    json_path, sdc_path = _paths(GOOD)
    base = ["analyze", "--netlist", str(json_path), "--sdc", str(sdc_path)]
    assert runner.invoke(app, base).exit_code == 1
    assert runner.invoke(app, [*base, "--ignore-scan-mode"]).exit_code == 0


def test_flag_is_exposed_on_both_commands() -> None:
    """Read off the command objects rather than the rendered ``--help``:
    at 80 columns rich elides the option name itself."""
    root = typer.main.get_command(app)
    assert isinstance(root, click.Group)
    for name in ("analyze", "lint"):
        command = root.get_command(click.Context(root), name)
        assert command is not None, name
        decls = {decl for p in command.params for decl in p.opts}
        assert "--ignore-scan-mode" in decls, name


# --- slang-frontend parity (issue #289) ------------------------------------
#
# This fixture is the sharpest end-to-end probe for the ``$mux`` A/B pin
# convention. Its whole point is a clock mux — ``scan_en ? scan_clk :
# func_clk`` — and ``trace_clock_root`` resolves such a mux by returning
# the *first* leg (``A``, then ``B``) that resolves to a clock. Emit the
# legs swapped and the destination flop lands in ``scan_clk``, the same
# domain as its source: no crossing, no CDC-001, a silent false negative
# with a PASS report. Before #289 that is exactly what
# ``lint --frontend slang`` produced here while the Yosys build reported
# 1× CDC-001.

PYSLANG_INSTALLED = importlib.util.find_spec("pyslang") is not None

pyslang_only = pytest.mark.skipif(
    not PYSLANG_INSTALLED,
    reason="pyslang not installed — slang-frontend parity is gated on it",
)


def _cli_json(
    tmp_path: Path, argv: list[str], name: str = "report"
) -> tuple[int, dict]:
    """Run the CLI with JSON output into ``tmp_path`` and return
    ``(exit_code, report)``."""
    out = tmp_path / f"{name}.json"
    result = runner.invoke(app, [*argv, "--format", "json", "--output", str(out)])
    assert out.exists(), result.output
    return result.exit_code, json.loads(out.read_text())


def _rule_ids(report: dict) -> list[str]:
    return sorted(v["rule_id"] for v in report["violations"])


@pyslang_only
def test_slang_lint_matches_the_committed_yosys_netlist(tmp_path: Path) -> None:
    """``lint --frontend slang`` on the ``.sv`` must reach the same
    verdict as ``analyze`` on the committed Yosys ``.json`` — 1×
    CDC-001, exit 1 — and the same one crossing."""
    json_path, sdc_path = _paths(GOOD)
    sv_path = json_path.with_suffix(".sv")

    yosys_code, yosys_report = _cli_json(
        tmp_path,
        ["analyze", "--netlist", str(json_path), "--sdc", str(sdc_path)],
        name="yosys",
    )
    slang_code, slang_report = _cli_json(
        tmp_path,
        [
            "lint",
            "--frontend",
            "slang",
            "--top",
            GOOD,
            "--sdc",
            str(sdc_path),
            str(sv_path),
        ],
        name="slang",
    )

    assert _rule_ids(yosys_report) == ["CDC-001"], yosys_report
    assert _rule_ids(slang_report) == _rule_ids(yosys_report), (
        "slang frontend disagrees with the committed Yosys netlist; "
        f"slang={_rule_ids(slang_report)} yosys={_rule_ids(yosys_report)}"
    )
    assert (
        slang_report["summary"]["crossings"] == (yosys_report["summary"]["crossings"])
    ), (slang_report["summary"], yosys_report["summary"])
    assert slang_code == yosys_code == 1


@pyslang_only
def test_slang_lint_is_clean_with_the_flag(tmp_path: Path) -> None:
    """The other half of the fixture's contract on the slang path: the
    crossing is found, tagged, and then suppressed by the flag — not
    absent. A frontend that never found it would also exit 0 here, which
    is why the previous test pins the without-flag finding."""
    json_path, sdc_path = _paths(GOOD)
    sv_path = json_path.with_suffix(".sv")
    code, report = _cli_json(
        tmp_path,
        [
            "lint",
            "--frontend",
            "slang",
            "--ignore-scan-mode",
            "--top",
            GOOD,
            "--sdc",
            str(sdc_path),
            str(sv_path),
        ],
    )
    assert code == 0, report
    assert report["violations"] == []
    assert report["summary"]["crossings"] == 1
    assert report["summary"]["scan_mode_suppressed"] == 1
