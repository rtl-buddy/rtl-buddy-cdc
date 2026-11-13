"""End-to-end: object-scoped ``set_false_path`` on Yosys-flattened fixtures (#328).

``good_object_false_path`` — a config block's registers cross to dst_clk
with no synchroniser (CDC-004 on the bus, CDC-001 on the bit); the SDC
false-paths both sources by their synthesis-style names, so both
findings are *acknowledged*: listed as suppressed with the SDC statement
as the reason, in every report format, and the run exits 0.

``bad_object_false_path_reconvergent`` — the same declaration on a
source that fans into two recombined 2FF chains: CDC-005 stays live.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc.cli import app

FIX_ROOT = Path(__file__).parent / "fixtures"
GOOD = FIX_ROOT / "good_object_false_path"
BAD = FIX_ROOT / "bad_object_false_path_reconvergent"

runner = CliRunner()


@pytest.fixture(autouse=True)
def _both_backends(sdc_backend):
    return sdc_backend


def _run(fix: Path, *extra: str, sdc: Path | None = None):
    name = fix.name
    sdc = sdc or fix / f"{name}.sdc"
    return runner.invoke(
        app,
        ["analyze", "--netlist", str(fix / f"{name}.json"), "--sdc", str(sdc), *extra],
    )


def _without_false_paths(fix: Path, tmp_path: Path) -> Path:
    text = (fix / f"{fix.name}.sdc").read_text()
    out = tmp_path / "plain.sdc"
    out.write_text(
        "\n".join(ln for ln in text.splitlines() if "set_false_path" not in ln) + "\n"
    )
    return out


def test_without_the_false_paths_both_findings_fire(tmp_path: Path) -> None:
    res = _run(GOOD, "--format", "json", sdc=_without_false_paths(GOOD, tmp_path))
    assert res.exit_code == 1, res.output
    payload = json.loads(res.stdout)
    assert sorted(v["rule_id"] for v in payload["violations"]) == ["CDC-001", "CDC-004"]


def test_json_lists_acknowledged_findings_as_suppressed(sdc_backend) -> None:
    res = _run(GOOD, "--format", "json")
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload["violations"] == []
    assert payload["summary"]["suppressed"] == 2
    by_rule = {s["rule_id"]: s for s in payload["suppressed"]}
    assert set(by_rule) == {"CDC-001", "CDC-004"}
    sdc = by_rule["CDC-004"]["waiver"]["sdc"]
    assert sdc["statement"] == (
        "set_false_path -from [get_cells u_cfg/mode_q_reg*] -to [get_clocks dst_clk]"
    )
    assert sdc["file"].endswith("good_object_false_path.sdc")
    assert sdc["line"] == (7 if sdc_backend == "tcl" else None)
    assert by_rule["CDC-004"]["waiver"]["reason"].startswith("SDC false path: ")
    assert by_rule["CDC-001"]["waiver"]["sdc"]["statement"] == (
        "set_false_path -from [get_pins u_cfg/enable_q_reg/Q] "
        "-to [get_cells en_dst_reg]"
    )
    # The finding itself is unchanged: the source is still the u_cfg flop.
    assert by_rule["CDC-004"]["crossing"]["src_flop"].startswith("$flatten\\u_cfg.")


def test_text_report_shows_the_sdc_statement() -> None:
    res = _run(GOOD)
    assert res.exit_code == 0, res.output
    assert "Suppressed by waivers and SDC exceptions (2)" in res.stdout
    assert "SDC false path: set_false_path -from [get_cells u_cfg/mode_q_reg*]" in (
        res.stdout
    )
    assert "(sdc " in res.stdout


def test_sarif_carries_suppressions_with_the_statement() -> None:
    res = _run(GOOD, "--format", "sarif")
    assert res.exit_code == 0, res.output
    results = json.loads(res.stdout)["runs"][0]["results"]
    assert len(results) == 2
    for r in results:
        (sup,) = r["suppressions"]
        assert sup["status"] == "accepted"
        assert sup["justification"].startswith("SDC false path: set_false_path")


def test_waiver_wins_over_the_sdc_exception(tmp_path: Path) -> None:
    waivers = tmp_path / "w.txt"
    waivers.write_text("waive CDC-001 .* reviewed by hand\n")
    res = _run(GOOD, "--format", "json", "--waivers", str(waivers))
    payload = json.loads(res.stdout)
    by_rule = {s["rule_id"]: s["waiver"] for s in payload["suppressed"]}
    assert "sdc" not in by_rule["CDC-001"]
    assert "sdc" in by_rule["CDC-004"]


def test_domain_map_annotates_covered_crossings(tmp_path: Path) -> None:
    out = tmp_path / "map.json"
    res = _run(GOOD, "--emit-domain-map", str(out))
    assert res.exit_code == 0, res.output
    payload = json.loads(out.read_text())
    assert len(payload["false_path_exceptions"]) == 2
    covered = [c for c in payload["crossings"] if "sdc_exceptions" in c]
    assert len(covered) == 2
    for c in covered:
        assert c["sdc_acknowledged"] is True
        assert c["findings"] == []  # acknowledged, so not a raised finding
        (exc,) = c["sdc_exceptions"]
        assert exc["command"] == "set_false_path"


def test_unmatched_pattern_is_warned(tmp_path: Path) -> None:
    sdc = tmp_path / "typo.sdc"
    sdc.write_text(
        (GOOD / "good_object_false_path.sdc").read_text()
        + "set_false_path -from [get_cells u_cfg/mode_qq_reg*]\n"
    )
    res = _run(GOOD, sdc=sdc)
    assert res.exit_code == 0, res.output
    assert "'u_cfg/mode_qq_reg*' matches nothing usable in the netlist" in res.output


def test_reconvergence_is_not_acknowledged() -> None:
    res = _run(BAD, "--format", "json")
    assert res.exit_code == 1, res.output
    payload = json.loads(res.stdout)
    assert [v["rule_id"] for v in payload["violations"]] == ["CDC-005"]
    assert payload["suppressed"] == []


def test_reconvergent_source_is_still_covered_in_the_domain_map(tmp_path: Path) -> None:
    out = tmp_path / "map.json"
    res = _run(BAD, "--emit-domain-map", str(out))
    assert res.exit_code == 1, res.output
    crossings = json.loads(out.read_text())["crossings"]
    assert len(crossings) == 2
    for c in crossings:
        assert c["sdc_acknowledged"] is True
        assert c["findings"] == ["CDC-005"]


@pytest.mark.parametrize("frontend", ["yosys", "slang"])
def test_lint_matches_registers_on_both_frontends(frontend: str) -> None:
    """The register name lives on the ``Q`` net under both frontends
    (Yosys ``$flatten\\u_cfg.$procdff$N`` / slang ``u_cfg.$slang$dff$N``
    cells), so the same SDC acknowledges the same findings."""
    if frontend == "slang":
        pytest.importorskip("pyslang")
    else:
        import shutil

        if shutil.which("yosys") is None:
            pytest.skip("yosys not on PATH")
    res = runner.invoke(
        app,
        [
            "lint",
            "--frontend",
            frontend,
            "--top",
            "good_object_false_path",
            "--sdc",
            str(GOOD / "good_object_false_path.sdc"),
            "--format",
            "json",
            str(GOOD / "good_object_false_path.sv"),
        ],
    )
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload["violations"] == []
    assert sorted(s["rule_id"] for s in payload["suppressed"]) == ["CDC-001", "CDC-004"]
