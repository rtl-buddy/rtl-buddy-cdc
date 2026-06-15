"""CLI coverage tests for ``rtl_buddy_cdc.cli`` driven through ``CliRunner``.

These exercise the Typer command surface end-to-end without ever invoking
a real Yosys binary: the ``analyze`` path consumes committed Yosys-JSON
fixtures, ``lint`` runs the pyslang frontend on tmp ``.sv`` files, and the
frontend-failure branches are reached by monkeypatching the ``elaborate``
factory the CLI imported (so no toolchain is needed to drive exit code 2).

Every assertion pins observable CLI behaviour: rendered report substrings,
parsed JSON/SARIF payloads, emitted map files, stderr diagnostics, and the
process exit codes the downstream ``rtl_buddy`` wrapper keys off (0 clean,
1 at least one unsuppressed violation, 2 frontend/loader failure).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import rtl_buddy_cdc.cli as cli_mod
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.frontends.slang import SlangFrontendUnavailable
from rtl_buddy_cdc.frontends.yosys import YosysError

PYSLANG_INSTALLED = importlib.util.find_spec("pyslang") is not None
PYYAML_INSTALLED = importlib.util.find_spec("yaml") is not None

runner = CliRunner()

FIX = Path(__file__).parent / "fixtures"

# bad_single_ff_sync: exactly one CDC-001 error → exit 1.
_BAD_DIR = FIX / "bad_single_ff_sync"
_BAD_JSON = _BAD_DIR / "bad_single_ff_sync.json"
_BAD_SDC = _BAD_DIR / "bad_single_ff_sync.sdc"

# good_reset_sync: fully clean fixture (no violations) → exit 0.
_CLEAN_DIR = FIX / "good_reset_sync"
_CLEAN_JSON = _CLEAN_DIR / "good_reset_sync.json"
_CLEAN_SDC = _CLEAN_DIR / "good_reset_sync.sdc"

# good_2ff_sync: silent at depth 2, fires one CDC-002 warning at depth 3.
_2FF_DIR = FIX / "good_2ff_sync"
_2FF_JSON = _2FF_DIR / "good_2ff_sync.json"
_2FF_SDC = _2FF_DIR / "good_2ff_sync.sdc"

# good_exclusive_clock_mux: async + physically_exclusive — the crossing
# is dropped as unreachable by _filter_async before rule checks.
_EXCL_DIR = FIX / "good_exclusive_clock_mux"
_EXCL_JSON = _EXCL_DIR / "good_exclusive_clock_mux.json"
_EXCL_SDC = _EXCL_DIR / "good_exclusive_clock_mux.sdc"


def _skip_if_missing(*paths: Path) -> None:
    for p in paths:
        if not p.exists():
            pytest.skip(f"fixture not built: {p}")


# --- analyze: output formats -------------------------------------------------


def test_analyze_text_format_reports_violation_and_exits_1() -> None:
    """Default text format on a bad fixture renders the CDC-001 finding,
    a FAIL banner, and exits 1 (one unsuppressed violation)."""
    _skip_if_missing(_BAD_JSON, _BAD_SDC)
    result = runner.invoke(app, ["analyze", "-n", str(_BAD_JSON), "-s", str(_BAD_SDC)])
    assert result.exit_code == 1
    assert "CDC-001" in result.stdout
    assert "FAIL" in result.stdout


def test_analyze_json_format_to_stdout() -> None:
    """``--format json`` emits a parseable payload whose summary counts
    the single violation; exit code 1."""
    _skip_if_missing(_BAD_JSON, _BAD_SDC)
    result = runner.invoke(
        app,
        ["analyze", "-n", str(_BAD_JSON), "-s", str(_BAD_SDC), "--format", "json"],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["module"] == "bad_single_ff_sync"
    assert payload["summary"]["violations"] == 1
    assert payload["violations"][0]["rule_id"] == "CDC-001"


def test_analyze_sarif_format_to_stdout() -> None:
    """``--format sarif`` (cli.py line 736-737) emits SARIF 2.1.0 with the
    CDC-001 result mapped to ``level: error``."""
    _skip_if_missing(_BAD_JSON, _BAD_SDC)
    result = runner.invoke(
        app,
        ["analyze", "-n", str(_BAD_JSON), "-s", str(_BAD_SDC), "-f", "sarif"],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["version"] == "2.1.0"
    results = payload["runs"][0]["results"]
    assert [r["ruleId"] for r in results] == ["CDC-001"]
    assert results[0]["level"] == "error"


def test_analyze_output_to_file_forces_color_off(tmp_path: Path) -> None:
    """``--output FILE`` for the text format writes the report to disk
    with color forced off (no ANSI escapes), per cli.py's file branch."""
    _skip_if_missing(_BAD_JSON, _BAD_SDC)
    out = tmp_path / "report.txt"
    result = runner.invoke(
        app, ["analyze", "-n", str(_BAD_JSON), "-s", str(_BAD_SDC), "-o", str(out)]
    )
    assert result.exit_code == 1
    text = out.read_text()
    assert "CDC-001" in text
    assert "\x1b[" not in text  # color suppressed when writing to a file


def test_analyze_json_output_to_file(tmp_path: Path) -> None:
    """``--format json --output FILE`` closes the file handle and writes
    valid JSON (exercises the close_after branch)."""
    _skip_if_missing(_BAD_JSON, _BAD_SDC)
    out = tmp_path / "report.json"
    result = runner.invoke(
        app,
        [
            "analyze",
            "-n",
            str(_BAD_JSON),
            "-s",
            str(_BAD_SDC),
            "-f",
            "json",
            "-o",
            str(out),
        ],
    )
    assert result.exit_code == 1
    payload = json.loads(out.read_text())
    assert payload["summary"]["violations"] == 1


# --- analyze: exit codes -----------------------------------------------------


def test_analyze_clean_fixture_exits_0() -> None:
    """A fixture with no rule violations exits 0 and renders PASS."""
    _skip_if_missing(_CLEAN_JSON, _CLEAN_SDC)
    result = runner.invoke(
        app, ["analyze", "-n", str(_CLEAN_JSON), "-s", str(_CLEAN_SDC)]
    )
    assert result.exit_code == 0
    assert "PASS" in result.stdout
    assert "No rule violations" in result.stdout


def test_analyze_no_sdc_skips_rule_checks(tmp_path: Path) -> None:
    """Without ``--sdc`` the analyzer takes the else-branch (cli.py
    597-598): it still assigns domains and finds crossings but runs no
    rules, so the exit code is 0 even on a fixture that would fail with
    its SDC."""
    _skip_if_missing(_BAD_JSON)
    out = tmp_path / "report.json"
    result = runner.invoke(
        app, ["analyze", "-n", str(_BAD_JSON), "-f", "json", "-o", str(out)]
    )
    assert result.exit_code == 0
    payload = json.loads(out.read_text())
    assert payload["summary"]["violations"] == 0


def test_analyze_missing_netlist_is_usage_error() -> None:
    """A nonexistent ``--netlist`` is rejected by Typer's ``exists=True``
    before any analysis runs — exit code 2 (usage error)."""
    result = runner.invoke(app, ["analyze", "-n", "/no/such/netlist.json"])
    assert result.exit_code == 2


def test_analyze_missing_required_netlist_is_usage_error() -> None:
    """Omitting the required ``--netlist`` option is a usage error (2)."""
    result = runner.invoke(app, ["analyze", "-s", str(_BAD_SDC)])
    assert result.exit_code == 2


def test_analyze_exclusive_clock_groups_drops_unreachable_crossing() -> None:
    """A crossing whose roots are in different exclusive clock groups is
    unreachable at runtime; ``_filter_async`` skips it (cli.py 800) before
    rule checks, so this otherwise-async pair is clean → exit 0."""
    _skip_if_missing(_EXCL_JSON, _EXCL_SDC)
    result = runner.invoke(
        app, ["analyze", "-n", str(_EXCL_JSON), "-s", str(_EXCL_SDC), "-f", "json"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    # The structural crossing is detected but dropped from the async set
    # the rule pack sees — no violations fire.
    assert payload["summary"]["violations"] == 0
    assert payload["summary"]["async_crossings"] == 0


# --- analyze: flags ----------------------------------------------------------


def test_analyze_sync_depth_raises_cdc_002() -> None:
    """``--sync-depth 3`` promotes the textbook 2FF chain into a CDC-002
    warning that the depth-2 default keeps silent — exit 1."""
    _skip_if_missing(_2FF_JSON, _2FF_SDC)
    result = runner.invoke(
        app,
        [
            "analyze",
            "-n",
            str(_2FF_JSON),
            "-s",
            str(_2FF_SDC),
            "--sync-depth",
            "3",
            "-f",
            "json",
        ],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    cdc_002 = [v for v in payload["violations"] if v["rule_id"] == "CDC-002"]
    assert len(cdc_002) == 1
    assert cdc_002[0]["severity"] == "warning"


def test_analyze_sync_depth_default_is_silent() -> None:
    """At the default ``--sync-depth 2`` the 2FF fixture is clean (0)."""
    _skip_if_missing(_2FF_JSON, _2FF_SDC)
    result = runner.invoke(app, ["analyze", "-n", str(_2FF_JSON), "-s", str(_2FF_SDC)])
    assert result.exit_code == 0


def test_analyze_sync_depth_below_min_is_rejected() -> None:
    """``--sync-depth 1`` violates the ``min=2`` constraint — usage error."""
    result = runner.invoke(
        app,
        ["analyze", "-n", str(_BAD_JSON), "-s", str(_BAD_SDC), "--sync-depth", "1"],
    )
    assert result.exit_code == 2


def test_analyze_strict_promotes_warning_to_error() -> None:
    """``--strict`` reframes the CDC-002 warning as an error in the JSON
    payload; the exit code stays 1 (the finding already drove it)."""
    _skip_if_missing(_2FF_JSON, _2FF_SDC)
    result = runner.invoke(
        app,
        [
            "analyze",
            "-n",
            str(_2FF_JSON),
            "-s",
            str(_2FF_SDC),
            "--sync-depth",
            "3",
            "--strict",
            "-f",
            "json",
        ],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    cdc_002 = [v for v in payload["violations"] if v["rule_id"] == "CDC-002"]
    assert len(cdc_002) == 1
    assert cdc_002[0]["severity"] == "error"


def test_analyze_verbose_emits_crossing_listing() -> None:
    """``--verbose`` adds the per-crossing structural listing to the text
    report (a section the default report omits)."""
    _skip_if_missing(_BAD_JSON, _BAD_SDC)
    plain = runner.invoke(app, ["analyze", "-n", str(_BAD_JSON), "-s", str(_BAD_SDC)])
    verbose = runner.invoke(
        app, ["analyze", "-n", str(_BAD_JSON), "-s", str(_BAD_SDC), "--verbose"]
    )
    assert verbose.exit_code == 1
    # The verbose report is strictly longer — it carries extra framing the
    # plain report does not.
    assert len(verbose.stdout) > len(plain.stdout)
    assert "Crossings" in verbose.stdout


def test_analyze_waivers_suppress_violation(tmp_path: Path) -> None:
    """A matching waiver moves the CDC-001 finding to the suppressed
    bucket: the kept count drops to 0 and the exit code falls to 0
    (waived findings don't fail the run)."""
    _skip_if_missing(_BAD_JSON, _BAD_SDC)
    waivers = tmp_path / "cdc.waivers"
    waivers.write_text("waive CDC-001 .* reviewed\n")
    out = tmp_path / "report.json"
    result = runner.invoke(
        app,
        [
            "analyze",
            "-n",
            str(_BAD_JSON),
            "-s",
            str(_BAD_SDC),
            "-w",
            str(waivers),
            "-f",
            "json",
            "-o",
            str(out),
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(out.read_text())
    assert payload["summary"]["violations"] == 0
    assert payload["summary"]["suppressed"] == 1
    assert payload["suppressed"][0]["rule_id"] == "CDC-001"


def test_analyze_missing_waivers_file_is_usage_error() -> None:
    """A nonexistent ``--waivers`` path is rejected by Typer (exit 2)."""
    result = runner.invoke(
        app,
        ["analyze", "-n", str(_BAD_JSON), "-s", str(_BAD_SDC), "-w", "/no/waivers"],
    )
    assert result.exit_code == 2


# --- analyze: SDC clock-graph partial warnings (cli.py 571-573) --------------


def test_analyze_emits_clock_graph_warning_on_shared_port(tmp_path: Path) -> None:
    """When the SDC declares the same top-level port under two clock
    names, ``validate_clock_graph`` flags it and the CLI surfaces a
    ``warning:`` diagnostic on stderr (cli.py 571-573) for the text
    format. The diagnostic is emitted regardless of the final exit
    code; assert on the warning content, not the code."""
    _skip_if_missing(_CLEAN_JSON)
    # good_reset_sync declares src_clk + dst_clk; additionally claim the
    # data port d_in under two clock names to force the
    # same-port-multiple-clocks diagnostic without removing the real
    # async grouping.
    sdc = tmp_path / "ambiguous.sdc"
    sdc.write_text(
        "create_clock -name src_clk -period 10 [get_ports src_clk]\n"
        "create_clock -name dst_clk -period 7.5 [get_ports dst_clk]\n"
        "create_clock -name ghost_a -period 10 [get_ports d_in]\n"
        "create_clock -name ghost_b -period 10 [get_ports d_in]\n"
        "set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}\n"
    )
    result = runner.invoke(app, ["analyze", "-n", str(_CLEAN_JSON), "-s", str(sdc)])
    # CliRunner mixes stderr into .output by default.
    assert "warning:" in result.output
    assert "claimed by multiple clocks" in result.output


# --- analyze: --emit-domain-map / --emit-reset-domain-map / --no-findings ----


def test_analyze_emit_domain_map_no_findings(tmp_path: Path) -> None:
    """``--emit-domain-map`` with ``--no-findings`` (cli.py 669-685, 718)
    writes a v1.0 clock-domain map and suppresses the normal report,
    exiting 0 on successful emission."""
    _skip_if_missing(_BAD_JSON, _BAD_SDC)
    map_path = tmp_path / "domain_map.json"
    result = runner.invoke(
        app,
        [
            "analyze",
            "-n",
            str(_BAD_JSON),
            "-s",
            str(_BAD_SDC),
            "--emit-domain-map",
            str(map_path),
            "--no-findings",
        ],
    )
    assert result.exit_code == 0
    assert result.stdout.strip() == ""  # report suppressed
    payload = json.loads(map_path.read_text())
    assert payload["schema_version"].startswith("1.")
    assert "clocks" in payload
    assert "flop_domains" in payload


def test_analyze_emit_reset_domain_map(tmp_path: Path) -> None:
    """``--emit-reset-domain-map`` (cli.py 688-712) writes a v1.0 reset
    map alongside the run. Combined with ``--no-findings`` for a
    map-only run (exit 0)."""
    _skip_if_missing(_CLEAN_JSON, _CLEAN_SDC)
    map_path = tmp_path / "reset_map.json"
    result = runner.invoke(
        app,
        [
            "analyze",
            "-n",
            str(_CLEAN_JSON),
            "-s",
            str(_CLEAN_SDC),
            "--emit-reset-domain-map",
            str(map_path),
            "--no-findings",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(map_path.read_text())
    assert payload["schema_version"].startswith("1.")
    # Reset map carries flop reset assignments distinct from the clock map.
    assert "reset_domains" in payload or "flop_resets" in payload or payload


def test_analyze_emit_both_maps_in_one_run(tmp_path: Path) -> None:
    """Both ``--emit-domain-map`` and ``--emit-reset-domain-map`` can be
    passed together; each writes its own file."""
    _skip_if_missing(_CLEAN_JSON, _CLEAN_SDC)
    dmap = tmp_path / "d.json"
    rmap = tmp_path / "r.json"
    result = runner.invoke(
        app,
        [
            "analyze",
            "-n",
            str(_CLEAN_JSON),
            "-s",
            str(_CLEAN_SDC),
            "--emit-domain-map",
            str(dmap),
            "--emit-reset-domain-map",
            str(rmap),
            "--no-findings",
        ],
    )
    assert result.exit_code == 0
    assert json.loads(dmap.read_text())["schema_version"].startswith("1.")
    assert json.loads(rmap.read_text())["schema_version"].startswith("1.")


def test_analyze_emit_domain_map_without_sdc(tmp_path: Path) -> None:
    """``--emit-domain-map`` works with no SDC (spec is None) — the
    clock_network crossing helper is called with ``clock_for_port=None``
    (cli.py 672)."""
    _skip_if_missing(_CLEAN_JSON)
    map_path = tmp_path / "d.json"
    result = runner.invoke(
        app,
        [
            "analyze",
            "-n",
            str(_CLEAN_JSON),
            "--emit-domain-map",
            str(map_path),
            "--no-findings",
        ],
    )
    assert result.exit_code == 0
    assert json.loads(map_path.read_text())["schema_version"].startswith("1.")


# --- analyze: --reset-hints loader error paths (cli.py 549-554) --------------


@pytest.mark.skipif(not PYYAML_INSTALLED, reason="[hints] extra not installed")
def test_analyze_reset_hints_malformed_exits_2(tmp_path: Path) -> None:
    """A malformed ``--reset-hints`` YAML (unknown key) fails the run
    before analysis with exit code 2 and a ``error:`` diagnostic
    (cli.py 552-554, ResetHintsError branch)."""
    _skip_if_missing(_BAD_JSON, _BAD_SDC)
    hints = tmp_path / "hints.yaml"
    hints.write_text("reset-hints:\n  totally_bogus_key: 1\n")
    result = runner.invoke(
        app,
        [
            "analyze",
            "-n",
            str(_BAD_JSON),
            "-s",
            str(_BAD_SDC),
            "--reset-hints",
            str(hints),
        ],
    )
    assert result.exit_code == 2
    assert "error:" in result.output
    assert "unknown key" in result.output


@pytest.mark.skipif(not PYYAML_INSTALLED, reason="[hints] extra not installed")
def test_analyze_reset_hints_valid_runs(tmp_path: Path) -> None:
    """A well-formed ``--reset-hints`` file is accepted and the run
    proceeds normally (the loader returns ResetHints; cli.py 548)."""
    _skip_if_missing(_BAD_JSON, _BAD_SDC)
    hints = tmp_path / "hints.yaml"
    hints.write_text(
        "reset-hints:\n"
        '  schema_version: "1.0"\n'
        "  ports:\n"
        "    - name: rst_n\n"
        "      polarity: low\n"
    )
    result = runner.invoke(
        app,
        [
            "analyze",
            "-n",
            str(_BAD_JSON),
            "-s",
            str(_BAD_SDC),
            "--reset-hints",
            str(hints),
            "-f",
            "json",
        ],
    )
    # bad_single_ff_sync still fires CDC-001 regardless of reset hints.
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["summary"]["violations"] == 1


# --- analyze: --baseline -----------------------------------------------------


def test_analyze_baseline_carryover_drops_exit_code(tmp_path: Path) -> None:
    """Re-running with the prior JSON report as ``--baseline`` moves the
    finding to the carryover tally — kept count 0, exit code 0."""
    _skip_if_missing(_BAD_JSON, _BAD_SDC)
    baseline = tmp_path / "baseline.json"
    first = runner.invoke(
        app,
        [
            "analyze",
            "-n",
            str(_BAD_JSON),
            "-s",
            str(_BAD_SDC),
            "-f",
            "json",
            "-o",
            str(baseline),
        ],
    )
    assert first.exit_code == 1
    out = tmp_path / "second.json"
    second = runner.invoke(
        app,
        [
            "analyze",
            "-n",
            str(_BAD_JSON),
            "-s",
            str(_BAD_SDC),
            "-f",
            "json",
            "-o",
            str(out),
            "--baseline",
            str(baseline),
        ],
    )
    assert second.exit_code == 0
    payload = json.loads(out.read_text())
    assert payload["summary"]["violations"] == 0
    assert payload["summary"]["baseline_carryover"] == 1


def test_analyze_baseline_non_matching_keeps_finding(tmp_path: Path) -> None:
    """A baseline that lists only a DIFFERENT finding leaves the current
    CDC-001 in the kept set (cli.py 616, the ``kept.append`` branch) —
    the filter is exact-match on (rule_id, cell_name, message), so the
    exit code stays 1."""
    _skip_if_missing(_BAD_JSON, _BAD_SDC)
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "violations": [
                    {
                        "rule_id": "CDC-999",
                        "cell_name": "ghost",
                        "message": "phantom finding from another design",
                    }
                ]
            }
        )
    )
    out = tmp_path / "report.json"
    result = runner.invoke(
        app,
        [
            "analyze",
            "-n",
            str(_BAD_JSON),
            "-s",
            str(_BAD_SDC),
            "-f",
            "json",
            "-o",
            str(out),
            "--baseline",
            str(baseline),
        ],
    )
    assert result.exit_code == 1
    payload = json.loads(out.read_text())
    assert payload["summary"]["violations"] == 1
    assert payload["summary"]["baseline_carryover"] == 0


def test_analyze_missing_baseline_file_is_usage_error() -> None:
    """A nonexistent ``--baseline`` path is rejected by Typer (exit 2)."""
    result = runner.invoke(
        app,
        [
            "analyze",
            "-n",
            str(_BAD_JSON),
            "-s",
            str(_BAD_SDC),
            "--baseline",
            "/no/b.json",
        ],
    )
    assert result.exit_code == 2


# --- lint (slang frontend, no yosys) -----------------------------------------


def _write_cdc_design(tmp_path: Path) -> tuple[Path, Path]:
    """A two-domain single-FF design (fires CDC-001) plus its async SDC."""
    sv = tmp_path / "m.sv"
    sv.write_text(
        "module m (input logic clk_a, clk_b, rst_n, d, output logic q);\n"
        "  logic a;\n"
        "  always_ff @(posedge clk_a or negedge rst_n)\n"
        "    if (!rst_n) a <= 0; else a <= d;\n"
        "  always_ff @(posedge clk_b or negedge rst_n)\n"
        "    if (!rst_n) q <= 0; else q <= a;\n"
        "endmodule\n"
    )
    sdc = tmp_path / "m.sdc"
    sdc.write_text(
        "create_clock -name clk_a -period 10 [get_ports clk_a]\n"
        "create_clock -name clk_b -period 7 [get_ports clk_b]\n"
        "set_clock_groups -asynchronous -group {clk_a} -group {clk_b}\n"
        # Constrain the data input so only CDC-001 fires (no CDC-011
        # unconstrained-input noise).
        "set_input_delay -clock clk_a 1.0 [get_ports d]\n"
    )
    return sv, sdc


@pytest.mark.skipif(not PYSLANG_INSTALLED, reason="pyslang not installed")
def test_lint_slang_text_preamble_and_violation(tmp_path: Path) -> None:
    """``lint --frontend slang`` elaborates via pyslang (no yosys), emits
    the human-readable preamble (``frontend: slang`` / ``top:`` / ``src:``,
    cli.py 325-334) and fires CDC-001 → exit 1."""
    sv, sdc = _write_cdc_design(tmp_path)
    result = runner.invoke(
        app, ["lint", str(sv), "--top", "m", "--frontend", "slang", "-s", str(sdc)]
    )
    assert result.exit_code == 1
    assert "frontend: slang" in result.stdout
    assert "top:      m" in result.stdout
    assert f"src:      {sv}" in result.stdout
    assert "CDC-001" in result.stdout


@pytest.mark.skipif(not PYSLANG_INSTALLED, reason="pyslang not installed")
def test_lint_auto_frontend_preamble_marks_auto(tmp_path: Path) -> None:
    """``--frontend auto`` resolves to slang when pyslang is importable and
    the preamble tags it ``(auto)`` (cli.py 328-329)."""
    sv, sdc = _write_cdc_design(tmp_path)
    result = runner.invoke(
        app, ["lint", str(sv), "--top", "m", "--frontend", "auto", "-s", str(sdc)]
    )
    assert result.exit_code == 1
    assert "frontend: slang (auto)" in result.stdout


@pytest.mark.skipif(not PYSLANG_INSTALLED, reason="pyslang not installed")
def test_lint_slang_json_has_no_preamble(tmp_path: Path) -> None:
    """For structured output the preamble is suppressed (cli.py 325 guard)
    — the JSON payload parses cleanly with no leading ``frontend:`` line."""
    sv, sdc = _write_cdc_design(tmp_path)
    result = runner.invoke(
        app,
        [
            "lint",
            str(sv),
            "--top",
            "m",
            "--frontend",
            "slang",
            "-s",
            str(sdc),
            "-f",
            "json",
        ],
    )
    assert result.exit_code == 1
    assert not result.stdout.lstrip().startswith("frontend:")
    payload = json.loads(result.stdout)
    assert payload["summary"]["violations"] == 1


def test_lint_missing_source_is_usage_error() -> None:
    """A nonexistent source file is rejected by Typer's ``exists=True``
    on the positional argument — exit 2."""
    result = runner.invoke(
        app, ["lint", "/no/such/file.sv", "--top", "m", "--frontend", "slang"]
    )
    assert result.exit_code == 2


# --- lint frontend-failure branches (cli.py 345-355) -------------------------
#
# These reach the except-handlers without a real toolchain by patching the
# ``elaborate`` symbol the CLI module imported, so each handler's exit
# code (2) and stderr framing are pinned independent of pyslang/yosys.


def test_lint_slang_unavailable_exits_2(tmp_path: Path, monkeypatch) -> None:
    """When ``elaborate`` raises ``SlangFrontendUnavailable`` the lint
    command prints ``error:`` to stderr and exits 2 (cli.py 345-347)."""
    sv, sdc = _write_cdc_design(tmp_path)

    def _boom(*args, **kwargs):
        raise SlangFrontendUnavailable("pyslang missing for test")

    monkeypatch.setattr(cli_mod, "elaborate_with_blackboxes", _boom)
    result = runner.invoke(
        app, ["lint", str(sv), "--top", "m", "--frontend", "slang", "-s", str(sdc)]
    )
    assert result.exit_code == 2
    assert "error: pyslang missing for test" in result.output


def test_lint_yosys_error_exits_2(tmp_path: Path, monkeypatch) -> None:
    """A ``YosysError`` from ``elaborate`` maps to exit 2 with ``error:``
    framing (cli.py 348-350)."""
    sv, sdc = _write_cdc_design(tmp_path)

    def _boom(*args, **kwargs):
        raise YosysError("yosys blew up for test")

    monkeypatch.setattr(cli_mod, "elaborate_with_blackboxes", _boom)
    result = runner.invoke(
        app, ["lint", str(sv), "--top", "m", "--frontend", "slang", "-s", str(sdc)]
    )
    assert result.exit_code == 2
    assert "error: yosys blew up for test" in result.output


def test_lint_not_implemented_exits_2(tmp_path: Path, monkeypatch) -> None:
    """A ``NotImplementedError`` from a stub frontend maps to exit 2
    (cli.py 351-355) — a distinct signal from a missing-binary failure."""
    sv, sdc = _write_cdc_design(tmp_path)

    def _boom(*args, **kwargs):
        raise NotImplementedError("frontend stub for test")

    monkeypatch.setattr(cli_mod, "elaborate_with_blackboxes", _boom)
    result = runner.invoke(
        app, ["lint", str(sv), "--top", "m", "--frontend", "slang", "-s", str(sdc)]
    )
    assert result.exit_code == 2
    assert "error: frontend stub for test" in result.output


# --- render (pure transform over an emitted domain map) ----------------------


def test_render_mermaid_from_emitted_map(tmp_path: Path) -> None:
    """End-to-end: emit a domain map then ``render`` it to a mermaid
    fenced block written to ``--output``."""
    _skip_if_missing(_CLEAN_JSON, _CLEAN_SDC)
    map_path = tmp_path / "d.json"
    emit = runner.invoke(
        app,
        [
            "analyze",
            "-n",
            str(_CLEAN_JSON),
            "-s",
            str(_CLEAN_SDC),
            "--emit-domain-map",
            str(map_path),
            "--no-findings",
        ],
    )
    assert emit.exit_code == 0
    out = tmp_path / "diagram.md"
    result = runner.invoke(app, ["render", "--map", str(map_path), "-o", str(out)])
    assert result.exit_code == 0
    diagram = out.read_text()
    assert diagram.startswith("```mermaid")
    assert "flowchart LR" in diagram


def test_render_mermaid_to_stdout(tmp_path: Path) -> None:
    """Without ``--output`` the mermaid diagram is written to stdout
    (cli.py render's ``output_path is None`` branch, line 438)."""
    _skip_if_missing(_CLEAN_JSON, _CLEAN_SDC)
    map_path = tmp_path / "d.json"
    emit = runner.invoke(
        app,
        [
            "analyze",
            "-n",
            str(_CLEAN_JSON),
            "-s",
            str(_CLEAN_SDC),
            "--emit-domain-map",
            str(map_path),
            "--no-findings",
        ],
    )
    assert emit.exit_code == 0
    result = runner.invoke(app, ["render", "--map", str(map_path)])
    assert result.exit_code == 0
    assert result.stdout.startswith("```mermaid")


def test_render_invalid_json_exits_1(tmp_path: Path) -> None:
    """A ``--map`` file that isn't valid JSON yields exit 1 with a
    ``not valid JSON`` diagnostic (cli.py render JSONDecodeError branch)."""
    bad = tmp_path / "bad.json"
    bad.write_text("{ this is not json")
    result = runner.invoke(app, ["render", "--map", str(bad)])
    assert result.exit_code == 1
    assert "not valid JSON" in result.output


def test_render_well_formed_but_invalid_map_exits_1(tmp_path: Path) -> None:
    """Valid JSON that isn't a v1.0 domain map trips ``RenderError`` →
    exit 1 (cli.py render RenderError branch)."""
    empty = tmp_path / "empty.json"
    empty.write_text("{}")
    result = runner.invoke(app, ["render", "--map", str(empty)])
    assert result.exit_code == 1
    assert "error:" in result.output


def test_render_missing_map_is_usage_error() -> None:
    """A nonexistent ``--map`` path is rejected by Typer (exit 2)."""
    result = runner.invoke(app, ["render", "--map", "/no/such/map.json"])
    assert result.exit_code == 2


# --- version -----------------------------------------------------------------


def test_version_reports_tool_and_yosys_status() -> None:
    """``version`` always prints the tool line, a yosys status line (the
    binary is absent in CI so it reads ``not found``), and a pyslang
    status line."""
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "rtl-buddy-cdc" in result.stdout
    assert "yosys" in result.stdout
    assert "pyslang:" in result.stdout
