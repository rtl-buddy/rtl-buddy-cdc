"""Instance-scoped waivers: ``waive <rule> inst:<path-regex>`` (#327).

Three layers:

- the parser — every pre-``inst:`` line parses exactly as before (pinned
  against a verbatim copy of the old parser), plus the new forms and
  their errors;
- ``waivers.apply`` matching semantics on hand-built violations — the
  ``/``-joined path, ``re.search`` anchoring, top-level findings, the
  parent-covers-children idiom, combination with an object regex;
- the CLI end to end on the ``ip_cdc_handshake`` hierarchy (Yosys
  ``$flatten\\`` names via ``analyze``, slang dotted names via ``lint``),
  the blackbox fixtures (``CDC-BBX`` and a lifted greybox finding) and
  the ``--baseline`` interplay, in text / JSON / SARIF.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import waivers as waivers_mod
from rtl_buddy_cdc.cli import _waiver_instance_scope, app  # noqa: PLC2701
from rtl_buddy_cdc.netlist import Cell, Module
from rtl_buddy_cdc.rules import Violation

runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures"
HS = FIXTURES / "ip_cdc_handshake"
HS_JSON = HS / "ip_cdc_handshake.json"
HS_SDC = HS / "ip_cdc_handshake.sdc"
HS_SOURCES = [HS / "ip_cdc_handshake.sv", HS / "ip_cdc_sync.sv"]
PYSLANG_INSTALLED = importlib.util.find_spec("pyslang") is not None


# --------------------------------------------------------------------------
# Parser: byte-for-byte compatibility with every pre-#327 line
# --------------------------------------------------------------------------


def _legacy_parse(text: str) -> list[waivers_mod.Waiver]:
    """The parser exactly as it shipped in 0.7.3, kept here as the oracle."""
    out: list[waivers_mod.Waiver] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split(maxsplit=3)
        if len(parts) < 3 or parts[0] != "waive":
            raise ValueError(
                f"line {lineno}: expected `waive <rule|*> <regex> [reason]`, "
                f"got: {raw!r}"
            )
        _, rule, pattern, *rest = parts
        try:
            compiled = re.compile(pattern)
        except re.error as e:
            raise ValueError(f"line {lineno}: invalid regex {pattern!r}: {e}") from e
        reason = rest[0].strip() if rest else ""
        out.append(
            waivers_mod.Waiver(
                rule_pattern=rule,
                regex=compiled,
                reason=reason,
                source_line=lineno,
            )
        )
    return out


LEGACY_CORPUS = """\
# header comment
waive CDC-001 .*procdff\\$9.*       hand-reviewed handshake
waive CDC-005 .*known_good_sync.*   library cell, see issue #42
waive *       .*generated_codegen.* auto-generated, ignore

   waive CDC-007 .* legacy alias for RDC-001
waive CDC-BBX u[01]$  clock mesh analysed standalone
waive CDC-002 dst_q
waive CDC-003 a->b   reason with   inner   spacing   and inst words
waive CDC-004 foo    per inst:u_x review (not a scope: not the 4th word)
\twaive\tCDC-006\tbar\ttab separated reason
"""


def test_legacy_lines_parse_identically() -> None:
    new = waivers_mod.parse(LEGACY_CORPUS)
    old = _legacy_parse(LEGACY_CORPUS)
    assert new == old
    assert len(new) == 9
    assert all(w.inst_regex is None for w in new)


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("notavalidline foo bar", "line 1: expected"),
        ("waive CDC-001", "line 1: expected"),
        ("waive CDC-001 *bogus reason", "line 1: invalid regex"),
    ],
)
def test_legacy_errors_unchanged(text: str, match: str) -> None:
    with pytest.raises(ValueError, match=match) as new:
        waivers_mod.parse(text)
    with pytest.raises(ValueError) as old:
        _legacy_parse(text)
    assert str(new.value) == str(old.value)


# --------------------------------------------------------------------------
# Parser: the new forms
# --------------------------------------------------------------------------


def test_parse_inst_only() -> None:
    (w,) = waivers_mod.parse("waive CDC-001 inst:^u_ip(/|$) signed-off IP\n")
    assert w.rule_pattern == "CDC-001"
    assert w.inst_regex is not None and w.inst_regex.pattern == "^u_ip(/|$)"
    # No object constraint: the object regex matches anything.
    assert w.regex.pattern == ".*"
    assert w.reason == "signed-off IP"
    assert w.source_line == 1
    assert w.origin is None


def test_parse_object_plus_inst() -> None:
    (w,) = waivers_mod.parse("waive * .*q_reg.*  inst:u_a/u_b   both must hold\n")
    assert w.rule_pattern == "*"
    assert w.regex.pattern == ".*q_reg.*"
    assert w.inst_regex is not None and w.inst_regex.pattern == "u_a/u_b"
    assert w.reason == "both must hold"


@pytest.mark.parametrize(
    "line",
    ["waive CDC-001 inst:^$", "waive CDC-001 dst_q inst:^$"],
)
def test_parse_inst_without_reason(line: str) -> None:
    (w,) = waivers_mod.parse(line)
    assert w.inst_regex is not None and w.inst_regex.pattern == "^$"
    assert w.reason == ""


def test_parse_legacy_alias_with_inst() -> None:
    (w,) = waivers_mod.parse("waive CDC-007 inst:^u_rst reset IP\n")
    v = _v("RDC-001", ("u_rst",))
    kept, sup = waivers_mod.apply([v], [w])
    assert kept == [] and sup[0].waiver is w


@pytest.mark.parametrize(
    ("line", "match"),
    [
        ("waive CDC-001 inst:(unclosed", "line 1: invalid inst regex '\\(unclosed'"),
        ("waive CDC-001 dst inst:*x", "line 1: invalid inst regex"),
        ("waive CDC-001 inst:", "line 1: empty `inst:` scope"),
        ("waive CDC-001 dst inst: why", "line 1: empty `inst:` scope"),
        ("waive CDC-001 inst:a inst:b", "line 1: more than one `inst:`"),
        ("waive CDC-001 x inst:a inst:b r", "line 1: more than one `inst:`"),
    ],
)
def test_parse_inst_errors(line: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        waivers_mod.parse(line)


# --------------------------------------------------------------------------
# Matching semantics on hand-built violations
# --------------------------------------------------------------------------


def _v(
    rule: str = "CDC-001",
    path: tuple[str, ...] = (),
    cell: str | None = "dst_q",
) -> Violation:
    return Violation(
        rule_id=rule,
        severity="error",
        message="unsynchronized control crossing",
        cell_name=cell,
        instance_path=path,
    )


def _waived(line: str, v: Violation) -> bool:
    kept, sup = waivers_mod.apply([v], waivers_mod.parse(line))
    assert len(kept) + len(sup) == 1
    return bool(sup)


def test_instance_path_text_is_slash_joined() -> None:
    assert waivers_mod.instance_path_text(()) == ""
    assert waivers_mod.instance_path_text(("u_a", "u_b")) == "u_a/u_b"


@pytest.mark.parametrize(
    ("scope", "path", "waived"),
    [
        # Top-level findings: the scope text is "".
        ("u_ip", (), False),
        ("^$", (), True),
        (".*", (), True),
        ("^$", ("u_ip",), False),
        # Parent-covers-children idiom.
        ("^u_ip(/|$)", ("u_ip",), True),
        ("^u_ip(/|$)", ("u_ip", "u_sync"), True),
        ("^u_ip(/|$)", ("u_ip", "u_sync", "u_ff"), True),
        ("^u_ip(/|$)", ("u_ip2",), False),
        ("^u_ip(/|$)", ("u_top", "u_ip"), False),
        # Unanchored search, exactly like the object regex.
        ("u_ip", ("u_top", "u_ip2"), True),
        # The issue's example: children only, not u_block_a itself.
        ("u_block_a/.*", ("u_block_a",), False),
        ("u_block_a/.*", ("u_block_a", "u_sync"), True),
        # Exact instance, children excluded.
        ("^u_a/u_b$", ("u_a", "u_b"), True),
        ("^u_a/u_b$", ("u_a", "u_b", "u_c"), False),
    ],
)
def test_inst_scope_semantics(scope: str, path: tuple[str, ...], waived: bool) -> None:
    assert _waived(f"waive CDC-001 inst:{scope} r", _v(path=path)) is waived


def test_inst_and_object_must_both_match() -> None:
    line = "waive CDC-001 dst_q inst:^u_ip(/|$) r"
    assert _waived(line, _v(path=("u_ip",), cell="dst_q"))
    assert not _waived(line, _v(path=("u_ip",), cell="other_q"))
    assert not _waived(line, _v(path=("u_x",), cell="dst_q"))


def test_inst_scope_still_honours_the_rule_id() -> None:
    assert not _waived("waive CDC-002 inst:.* r", _v(path=("u_ip",)))
    assert _waived("waive * inst:u_ip r", _v(rule="CDC-BBX", path=("u_ip",)))


def test_first_match_wins_across_scoped_and_unscoped_lines() -> None:
    ws = waivers_mod.parse(
        "waive CDC-001 inst:^u_a$ scoped\nwaive CDC-001 .* catch-all\n"
    )
    _, sup = waivers_mod.apply([_v(path=("u_a",)), _v(path=("u_b",))], ws)
    assert [s.waiver.reason for s in sup] == ["scoped", "catch-all"]


def test_instance_scope_resolver_overrides_the_default() -> None:
    ws = waivers_mod.parse("waive CDC-BBX inst:^u0$ r\n")
    v = _v(rule="CDC-BBX", path=(), cell="u0")
    assert waivers_mod.apply([v], ws)[1] == []
    _, sup = waivers_mod.apply([v], ws, instance_scope=lambda _v: "u0")
    assert len(sup) == 1


def test_pragma_waivers_ignore_the_instance_scope_resolver() -> None:
    """A pragma waiver never carries ``inst_regex``, so the resolver is
    never consulted for it — the source-location match is unchanged."""
    w = waivers_mod.Waiver(
        rule_pattern="CDC-001",
        regex=re.compile("dut\\.sv"),
        reason="pragma",
        source_line=3,
        origin="rtl/dut.sv",
    )

    def boom(_v: Violation) -> str:
        raise AssertionError("resolver consulted for a pragma waiver")

    _, sup = waivers_mod.apply(
        [_v(path=("u_x",))],
        [w],
        locate=lambda _v: waivers_mod.SourceRef("rtl/dut.sv"),
        instance_scope=boom,
    )
    assert len(sup) == 1


# --------------------------------------------------------------------------
# The CLI's scope resolver: blackbox-anchored findings
# --------------------------------------------------------------------------


def _bb_parent() -> tuple[Module, dict[str, Module]]:
    cells = {
        "u0": Cell(name="u0", type="ip", connections={}),
        "u_a.u_ip": Cell(name="u_a.u_ip", type="ip", connections={}),
        "$flatten\\u_a.\\u_ip2": Cell(
            name="$flatten\\u_a.\\u_ip2", type="ip", connections={}
        ),
        "u_a.q": Cell(name="u_a.q", type="$dff", connections={}),
        "FDRE_q": Cell(name="u_a.FDRE_q", type="FDRE", connections={}),
    }
    parent = Module(name="top", ports={}, cells=cells, netnames={})
    ip = Module(name="ip", ports={}, cells={}, netnames={}, is_blackbox=True)
    return parent, {"ip": ip}


@pytest.mark.parametrize(
    ("cell", "path", "expected"),
    [
        # A blackbox instance at top is scoped to itself.
        ("u0", (), "u0"),
        # A nested blackbox instance (dotted name) appends its own leaf.
        ("u_a.u_ip", ("u_a",), "u_a/u_ip"),
        # A ``$flatten\`` name already resolved the instance into the path.
        ("$flatten\\u_a.\\u_ip2", ("u_a", "u_ip2"), "u_a/u_ip2"),
        # Ordinary cells — primitive or not — keep their own instance path.
        ("u_a.q", ("u_a",), "u_a"),
        ("FDRE_q", ("u_a",), "u_a"),
        ("not_a_cell", ("u_z",), "u_z"),
    ],
)
def test_waiver_instance_scope(cell: str, path: tuple[str, ...], expected: str) -> None:
    parent, bbs = _bb_parent()
    v = _v(rule="CDC-BBX", path=path, cell=cell)
    assert _waiver_instance_scope(parent, bbs, v) == expected


def test_waiver_instance_scope_without_blackboxes_is_the_instance_path() -> None:
    parent, _ = _bb_parent()
    v = _v(path=(), cell="u0")
    assert _waiver_instance_scope(parent, None, v) == ""
    assert _waiver_instance_scope(parent, {}, v) == ""
    assert _waiver_instance_scope(parent, {"ip": parent}, _v(cell=None)) == ""


# --------------------------------------------------------------------------
# End to end through the CLI
# --------------------------------------------------------------------------


def _run(args: list[str]) -> tuple[int, str]:
    res = runner.invoke(app, args)
    return res.exit_code, res.output


def _analyze_hs(tmp_path: Path, waiver_text: str | None, fmt: str, *extra: str):
    args = ["analyze", "-n", str(HS_JSON), "-s", str(HS_SDC), "--sync-depth", "3"]
    if waiver_text is not None:
        wf = tmp_path / "cdc.waivers"
        wf.write_text(waiver_text)
        args += ["--waivers", str(wf)]
    return _run([*args, "-f", fmt, *extra])


WAIVE_ACK = (
    "# hierarchy waivers\nwaive CDC-002 inst:^u_sync_ack(/|$) ack sync signed off\n"
)


def test_cli_json_inst_waiver_on_yosys_flatten_names(tmp_path: Path) -> None:
    code, out = _analyze_hs(tmp_path, WAIVE_ACK, "json")
    rep = json.loads(out)
    assert code == 1  # u_sync_req is still live
    assert [v["instance_path"] for v in rep["violations"]] == [["u_sync_req"]]
    assert rep["violations"][0]["cell_name"].startswith("$flatten\\u_sync_req.")
    assert rep["summary"]["suppressed"] == 1
    (sup,) = rep["suppressed"]
    assert sup["instance_path"] == ["u_sync_ack"]
    assert sup["waiver"] == {
        "rule_pattern": "CDC-002",
        "regex": ".*",
        "reason": "ack sync signed off",
        "source_line": 2,
        "origin": None,
        "end_line": None,
        "inst_regex": "^u_sync_ack(/|$)",
    }
    # by_instance counts kept findings only.
    assert [b["instance_path"] for b in rep["by_instance"]] == [["u_sync_req"]]


def test_cli_json_unscoped_waiver_reports_null_inst_regex(tmp_path: Path) -> None:
    code, out = _analyze_hs(tmp_path, "waive CDC-002 .* all\n", "json")
    rep = json.loads(out)
    assert code == 0
    assert {s["waiver"]["inst_regex"] for s in rep["suppressed"]} == {None}


def test_cli_text_inst_waiver(tmp_path: Path) -> None:
    code, out = _analyze_hs(tmp_path, WAIVE_ACK, "text", "--no-color")
    assert code == 1
    assert "Suppressed by waivers (1)" in out
    assert "ack sync signed off" in out
    assert "(waiver line 2)" in out


def test_cli_sarif_inst_waiver(tmp_path: Path) -> None:
    code, out = _analyze_hs(tmp_path, WAIVE_ACK, "sarif")
    assert code == 1
    results = json.loads(out)["runs"][0]["results"]
    by_inst = {
        r["locations"][0]["logicalLocations"][0]["fullyQualifiedName"]: r
        for r in results
    }
    assert set(by_inst) == {"u_sync_ack", "u_sync_req"}
    assert by_inst["u_sync_ack"]["suppressions"][0]["justification"] == (
        "ack sync signed off"
    )
    assert "suppressions" not in by_inst["u_sync_req"]


def test_cli_inst_waiver_covering_every_instance_exits_zero(tmp_path: Path) -> None:
    code, out = _analyze_hs(tmp_path, "waive CDC-002 inst:^u_sync_ r\n", "json")
    assert code == 0
    assert json.loads(out)["summary"]["suppressed"] == 2


def test_cli_inst_waiver_runs_before_baseline(tmp_path: Path) -> None:
    """Waivers see the resolved ``instance_path`` and run before the
    ``--baseline`` partition; the carried-over entry still has its path."""
    _, base = _analyze_hs(tmp_path, None, "json")
    baseline = tmp_path / "baseline.json"
    baseline.write_text(base)
    code, out = _analyze_hs(tmp_path, WAIVE_ACK, "json", "--baseline", str(baseline))
    rep = json.loads(out)
    assert code == 0
    assert [s["instance_path"] for s in rep["suppressed"]] == [["u_sync_ack"]]
    assert [c["instance_path"] for c in rep["baseline_carryover"]] == [["u_sync_req"]]
    assert rep["violations"] == []


def test_cli_bad_inst_regex_fails_like_a_bad_object_regex(tmp_path: Path) -> None:
    results = []
    for line in ("waive CDC-002 *bad r\n", "waive CDC-002 inst:*bad r\n"):
        code, _ = _analyze_hs(tmp_path, line, "json")
        results.append(code)
    assert results[0] == results[1] != 0


@pytest.mark.skipif(not PYSLANG_INSTALLED, reason="pyslang not installed")
def test_cli_lint_slang_names_top_and_nested(tmp_path: Path) -> None:
    """The slang frontend names cells ``u_sync_ack.$slang$adff$N`` (no
    ``$flatten\\``) and also raises a top-level CDC-012 here: the same
    ``inst:`` lines select the same instances, and ``inst:^$`` takes
    only the top-level finding."""
    wf = tmp_path / "cdc.waivers"
    wf.write_text("waive * inst:^$ top only\nwaive CDC-002 inst:^u_sync_ack(/|$) ack\n")
    code, out = _run(
        [
            "lint",
            "--frontend",
            "slang",
            "--top",
            "ip_cdc_handshake",
            "--sdc",
            str(HS_SDC),
            "--sync-depth",
            "3",
            "--waivers",
            str(wf),
            "-f",
            "json",
            *map(str, HS_SOURCES),
        ]
    )
    rep = json.loads(out)
    assert code == 1
    kept = [(v["rule_id"], v["instance_path"]) for v in rep["violations"]]
    assert kept == [("CDC-002", ["u_sync_req"])]
    sup = sorted(
        (s["rule_id"], s["instance_path"], s["waiver"]["reason"])
        for s in rep["suppressed"]
    )
    assert sup == [
        ("CDC-002", ["u_sync_ack"], "ack"),
        ("CDC-012", [], "top only"),
    ]
    assert not any(s["cell_name"].startswith("$flatten") for s in rep["suppressed"])


def test_cli_bbx_is_scoped_to_its_own_instance(tmp_path: Path) -> None:
    fix = FIXTURES / "clock_output_blackbox"
    wf = tmp_path / "bbx.swl"
    wf.write_text("waive CDC-BBX inst:^u0(/|$) tile analysed standalone\n")
    code, out = _run(
        [
            "analyze",
            "-n",
            str(fix / "clock_output_blackbox.json"),
            "-s",
            str(fix / "clock_output_blackbox.sdc"),
            "--waivers",
            str(wf),
            "-f",
            "json",
        ]
    )
    rep = json.loads(out)
    assert code == 1
    assert [v["cell_name"] for v in rep["violations"]] == ["u1"]
    (sup,) = rep["suppressed"]
    assert (sup["rule_id"], sup["cell_name"]) == ("CDC-BBX", "u0")
    # The reported path is unchanged: the coverage gap lives in the parent.
    assert sup["instance_path"] == []


def test_cli_lifted_greybox_finding_is_scoped_to_its_instance(tmp_path: Path) -> None:
    fix = FIXTURES / "bbx_residual_declines"
    wf = tmp_path / "grey.swl"
    wf.write_text("waive * inst:^u_thru(/|$) feedthru signed off\n")
    code, out = _run(
        [
            "analyze",
            "-n",
            str(fix / "bbx_residual_declines.grey.json"),
            "-s",
            str(fix / "bbx_residual_declines.sdc"),
            "--waivers",
            str(wf),
            "-f",
            "json",
        ]
    )
    rep = json.loads(out)
    sup = [(s["rule_id"], s["cell_name"]) for s in rep["suppressed"]]
    assert sup == [("CDC-001", "u_thru")]
    assert rep["suppressed"][0]["message"].startswith("[inside `feedthru`")
    # The other blackboxes are untouched.
    assert sorted(v["cell_name"] for v in rep["violations"]) == ["u_odd", "u_two"]
    assert code == 1
