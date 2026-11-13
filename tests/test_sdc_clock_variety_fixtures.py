"""SDC-variety and three-to-five-clock fixtures (rtl-buddy-cdc#331).

Items 3 and 4 of rtl-buddy-cdc#331: before these fixtures nearly every
fixture had two clocks, one ``set_clock_groups -asynchronous`` and
period-only ``create_clock``. Each fixture below pins one SDC form or
one clock structure end to end, through ``analyze -f json`` (the CLI
path, so SDC object exceptions, multi-clock ports and the reporters run
as they do for users), on both SDC reader backends.

SDC forms (item 3):

- ``good_gen_clock_chain`` / ``bad_gen_clock_async_to_master`` — a
  generated clock of a generated clock (pin targets), root-group
  propagation, and a generated clock declared asynchronous to its own
  master (the override covers the named clocks only).
- ``good_gen_clock_edges_invert`` / ``bad_gen_clock_edges_invert`` —
  ``-edges``, ``-invert``, ``-combinational`` generated clocks.
- ``good_clock_groups_two_statements`` / ``bad_…`` — two
  ``set_clock_groups`` statements; a pair no single statement separates
  stays synchronous.
- ``good_false_path_one_direction`` / ``bad_…`` — a clock-pair
  ``set_false_path`` written one way covers the pair.
- ``good_false_path_through_bus`` / ``bad_false_path_through_partial``
  — ``-through`` on a bus crossing, and a second bus it does not cover.
- ``good_datapath_only_overrides`` / ``bad_…`` — ``set_max_delay
  -datapath_only`` beating a generated relation and an exclusive group.
- ``good_case_analysis_mode_sel0`` / ``bad_case_analysis_mode_sel1`` —
  one RTL, two ``set_case_analysis`` modes on a bus bit behind an
  inverter: the crossing disappears in one and appears in the other.
- ``good_virtual_clock_io`` / ``bad_virtual_clock_io`` — virtual clocks
  for ``set_input_delay`` / ``set_output_delay`` (CDC-001 port source,
  CDC-024 / CDC-025 contracts).
- ``tcl_generated_constraints`` — constraints built with variables,
  ``expr``, lists, ``dict`` and ``foreach``; the ``tokenizer`` fallback's
  verdict is pinned too.

Clock structure (item 4): ``*_three_domain_chain``,
``*_four_clock_mixed`` (two synchronous, two asynchronous, a crossing for
every pair), ``*_five_clock_exclusive`` (async + physically and
logically exclusive groups), ``good_reconvergence_two_source_domains`` /
``bad_reconvergence_three_domains``, and ``*_handshake_ack_via_third_domain``
(the acknowledge returns through a third domain).

Every expected verdict is reasoned from the rule definitions in the
architecture spec and stated in the fixture's leading comment. Where the
analyzer disagrees, the test is written against the reasoned verdict and
marked ``xfail(strict=True)`` with the cause (see :data:`_XFAIL`).
"""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import sdc as sdc_mod
from rtl_buddy_cdc.cli import app

FIX_ROOT = Path(__file__).parent / "fixtures"

runner = CliRunner()


@pytest.fixture(autouse=True)
def _every_sdc_backend(sdc_backend):
    """Run every test here once per SDC reader backend (#298)."""
    return sdc_backend


# --- helpers ----------------------------------------------------------------


@functools.cache
def _q_names(name: str) -> dict[str, str]:
    """Flop cell name → the register's own net name (its ``Q`` net).

    Cell names (``$procdff$41``) are build artefacts; the ``Q`` net name
    is the RTL register, which is what a reader of the fixture checks
    against. An output port aliasing the same net is skipped in favour
    of the internal name.
    """
    mod = json.loads((FIX_ROOT / name / f"{name}.json").read_text())["modules"][name]
    ports = set(mod["ports"])
    by_bits: dict[tuple, list[str]] = {}
    for net, info in mod["netnames"].items():
        if "$" in net:
            continue
        by_bits.setdefault(tuple(info["bits"]), []).append(net)
    out: dict[str, str] = {}
    for cell, info in mod["cells"].items():
        q = info["connections"].get("Q")
        if q is None:
            continue
        nets = sorted(by_bits.get(tuple(q), []), key=lambda n: (n in ports, n))
        if nets:
            out[cell] = nets[0]
    return out


Finding = tuple[str, str | None, str | None]


def _finding(name: str, v: dict) -> Finding:
    """``(rule, source, destination)`` of one reported finding.

    Source is the source register (or ``port <p>``), destination the
    capturing register. CDC-005 reports one representative crossing of a
    group, so only its source is kept. A finding with no crossing (CDC-006,
    CDC-024, …) keeps the register it names as its destination.
    """
    names = _q_names(name)
    c = v.get("crossing")
    if c is None:
        return (v["rule_id"], None, names.get(v["cell_name"], v["cell_name"]))
    src = names.get(c["src_flop"]) if c.get("src_flop") else f"port {c['src_port']}"
    dst = None if v["rule_id"] == "CDC-005" else names.get(c["dst_flop"])
    return (v["rule_id"], src, dst)


def _analyze(name: str):
    fix = FIX_ROOT / name
    res = runner.invoke(
        app,
        [
            "analyze",
            "--netlist",
            str(fix / f"{name}.json"),
            "--sdc",
            str(fix / f"{name}.sdc"),
            "--format",
            "json",
        ],
    )
    assert res.exit_code in (0, 1), res.output
    return res, json.loads(res.stdout)


@dataclass(frozen=True)
class Verdict:
    async_crossings: int
    findings: tuple[Finding, ...] = ()
    suppressed: tuple[Finding, ...] = ()


def _cdc001(src: str, dst: str) -> Finding:
    return ("CDC-001", src, dst)


# Expected verdicts, reasoned from the rule definitions (each fixture's
# leading comment says why). Order inside a tuple does not matter.
EXPECTED: dict[str, Verdict] = {
    # --- item 4: three to five clocks ---
    "good_three_domain_chain": Verdict(2),
    "bad_three_domain_chain": Verdict(2, (_cdc001("b_q", "c_s1"),)),
    "good_four_clock_mixed": Verdict(5),
    "bad_four_clock_mixed": Verdict(
        5, (_cdc001("x_q", "b_x_s1"), _cdc001("b_q", "c_b_s1"))
    ),
    "good_five_clock_exclusive": Verdict(5),
    "bad_five_clock_exclusive": Verdict(
        5, (_cdc001("io_q", "sys_io_s1"), _cdc001("m1_q", "dbg_m1_s1"))
    ),
    "good_reconvergence_two_source_domains": Verdict(2),
    "bad_reconvergence_three_domains": Verdict(3, (("CDC-005", "a_q", None),)),
    "good_handshake_ack_via_third_domain": Verdict(4),
    "bad_handshake_ack_via_third_domain": Verdict(4, (_cdc001("c_ack_q", "a_ack_s1"),)),
    # --- item 3: SDC forms ---
    "good_gen_clock_chain": Verdict(2),
    "bad_gen_clock_async_to_master": Verdict(2, (_cdc001("a_q", "d2_cap"),)),
    "good_gen_clock_edges_invert": Verdict(1),
    "bad_gen_clock_edges_invert": Verdict(2, (_cdc001("x_q", "gx_q"),)),
    "good_clock_groups_two_statements": Verdict(2),
    "bad_clock_groups_two_statements": Verdict(2, (_cdc001("b_q", "c_s1"),)),
    "good_false_path_one_direction": Verdict(2),
    "bad_false_path_one_direction": Verdict(2, (_cdc001("b_q", "a_cap"),)),
    "good_false_path_through_bus": Verdict(
        1, suppressed=(("CDC-004", "cfg_q", "cfg_cap"),)
    ),
    "bad_false_path_through_partial": Verdict(
        2,
        (("CDC-004", "stat_q", "stat_cap"),),
        (("CDC-004", "cfg_q", "cfg_cap"),),
    ),
    "good_datapath_only_overrides": Verdict(2),
    "bad_datapath_only_overrides": Verdict(
        2, (_cdc001("a_q", "h_s1"), _cdc001("m0_q", "m1_s1"))
    ),
    "good_case_analysis_mode_sel0": Verdict(2),
    "bad_case_analysis_mode_sel1": Verdict(2, (_cdc001("a_q", "m_cap"),)),
    "good_virtual_clock_io": Verdict(1),
    "bad_virtual_clock_io": Verdict(
        1, (_cdc001("port ext_req", "req_s1"), ("CDC-024", None, "ack_q"))
    ),
}

# Fixtures whose analyzer verdict disagrees with the reasoned one. Each is
# a finding for an issue; the test flips to XPASS (a failure, strict) the
# day the analyzer is fixed, and the entry must then be removed.
_XFAIL: dict[str, str] = {
    "good_handshake_ack_via_third_domain": (
        "false CDC-012: _has_dst_to_src_feedback walks only inside the "
        "source domain, so an acknowledge relayed clk_b → clk_c → clk_a "
        "(payload held until it arrives) is not seen as feedback"
    ),
    "bad_handshake_ack_via_third_domain": (
        "extra CDC-012 next to the expected CDC-001: same relayed-ack "
        "blind spot as good_handshake_ack_via_third_domain"
    ),
    "good_virtual_clock_io": (
        "false CDC-006 on a typed input port wired straight into a 2FF "
        "synchroniser: there is no combinational logic before the first "
        "stage, but check_cdc_006 fires on any input port in the D fanin "
        "whose clock differs from the synchroniser's"
    ),
}


def _params() -> list:
    out = []
    for name in EXPECTED:
        marks = []
        if name in _XFAIL:
            marks.append(pytest.mark.xfail(strict=True, reason=_XFAIL[name]))
        out.append(pytest.param(name, marks=marks, id=name))
    return out


# --- the verdict table ------------------------------------------------------


@pytest.mark.parametrize("name", _params())
def test_fixture_verdict(name: str) -> None:
    expected = EXPECTED[name]
    res, report = _analyze(name)
    assert report["summary"]["async_crossings"] == expected.async_crossings
    got = sorted(_finding(name, v) for v in report["violations"])
    assert got == sorted(expected.findings)
    got_supp = sorted(_finding(name, v) for v in report["suppressed"])
    assert got_supp == sorted(expected.suppressed)
    assert res.exit_code == (1 if expected.findings else 0)


def test_every_new_fixture_is_in_the_table() -> None:
    for name in EXPECTED:
        assert (FIX_ROOT / name / f"{name}.json").exists(), name
    assert set(_XFAIL) <= set(EXPECTED)


# --- parts of the xfail fixtures that are already right ----------------------


def test_relayed_ack_hop_is_reported_once() -> None:
    """The real bug in bad_handshake_ack_via_third_domain is found, and
    the gated payload is not a CDC-004 — only the extra CDC-012 is wrong."""
    _, report = _analyze("bad_handshake_ack_via_third_domain")
    got = [
        _finding("bad_handshake_ack_via_third_domain", v) for v in report["violations"]
    ]
    assert _cdc001("c_ack_q", "a_ack_s1") in got
    assert {f[0] for f in got} <= {"CDC-001", "CDC-012"}


def test_gen_clock_chain_raw_paths_are_synchronous() -> None:
    """good_gen_clock_chain: the raw clk → ck_div4 and ck_div2 → ck_div4
    captures are not async crossings (two-level master chain resolves to
    clk); only the clk_ext crossings are."""
    _, report = _analyze("good_gen_clock_chain")
    assert report["summary"]["crossings"] == 5
    assert report["summary"]["async_crossings"] == 2
    names = _q_names("good_gen_clock_chain")
    raw = {
        (names.get(c.get("src_flop")), names.get(c["dst_flop"]))
        for c in report["crossings"]
    }
    assert ("a_q", "d4_cap") in raw and ("d2_q", "d4_cap") in raw
    for v in report["violations"]:
        assert v["crossing"]["src_clock"] == "clk_ext"


def test_virtual_clock_io_contracts_are_clean() -> None:
    """good_virtual_clock_io: the output contracts (one virtual clock
    related to clk, one clk) and the related virtual-clock input raise
    nothing; only the CDC-006 noted in _XFAIL fires."""
    _, report = _analyze("good_virtual_clock_io")
    assert {v["rule_id"] for v in report["violations"]} <= {"CDC-006"}


# --- targeted checks --------------------------------------------------------


def test_through_statement_is_the_suppression_reason(sdc_backend: str) -> None:
    for name in ("good_false_path_through_bus", "bad_false_path_through_partial"):
        _, report = _analyze(name)
        (entry,) = report["suppressed"]
        sdc = entry["waiver"]["sdc"]
        assert sdc["statement"] == "set_false_path -through [get_pins u_bridge/cfg_o]"
        assert sdc["file"].endswith(f"{name}.sdc")
        assert sdc["line"] == (6 if sdc_backend == "tcl" else None)


def test_cdc_024_names_the_virtual_receiving_clock() -> None:
    _, report = _analyze("bad_virtual_clock_io")
    (v,) = [v for v in report["violations"] if v["rule_id"] == "CDC-024"]
    assert "`ext_ack`" in v["message"]
    assert "vclk_ext" in v["message"]


def test_reconvergence_names_the_clk_a_source_only() -> None:
    _, report = _analyze("bad_reconvergence_three_domains")
    (v,) = report["violations"]
    assert v["crossing"]["src_clock"] == "clk_a"
    assert v["crossing"]["dst_clock"] == "clk_c"
    assert "drives 2 independent sync chains" in v["message"]


def test_case_analysis_modes_move_the_mux_domain() -> None:
    """Same RTL: mode[1]=0 puts M on clk_a (a_q → m_cap disappears),
    mode[1]=1 on clk_b (it appears)."""
    for name, dst_clock in (
        ("good_case_analysis_mode_sel0", "clk_a"),
        ("bad_case_analysis_mode_sel1", "clk_b"),
    ):
        _, report = _analyze(name)
        m_flops = {
            c["dst_clock"] for c in report["crossings"] if c["dst_clock"] != "clk_c"
        }
        assert m_flops == {dst_clock}, (name, m_flops)


# --- the parsed SDC ---------------------------------------------------------


def _spec(name: str) -> sdc_mod.ClockSpec:
    return sdc_mod.parse_file(FIX_ROOT / name / f"{name}.sdc")


def test_generated_clock_forms_parse(sdc_backend: str) -> None:
    spec = _spec("good_gen_clock_edges_invert")
    for clk, pin in (("ck_div3", "div3_q"), ("ck_inv", "clk_n"), ("ck_gated", "clk_g")):
        assert spec.clocks[clk].master == "clk"
        assert spec.clocks[clk].is_generated
        assert spec.pin_clocks[pin] == clk
        assert spec.resolve(clk) == "clk"
    chain = _spec("good_gen_clock_chain")
    assert chain.clocks["ck_div4"].master == "ck_div2"
    assert chain.resolve("ck_div4") == "clk"
    assert chain.are_async("ck_div4", "clk_ext")
    four = _spec("good_four_clock_mixed")
    assert four.clocks["clk_a2x"].ports == ("clk_a2x",)
    assert four.clocks["clk_a2x"].period == pytest.approx(5.0)
    assert not four.are_async("clk_a", "clk_a2x")


def test_override_covers_named_clocks_only(sdc_backend: str) -> None:
    spec = _spec("bad_gen_clock_async_to_master")
    assert spec.are_async("clk", "ck_div2")
    # ck_div4 is not named by the group: it collapses to its root.
    assert not spec.are_async("clk", "ck_div4")
    assert not spec.are_async("ck_div2", "ck_div4")


def test_two_statements_relate_only_their_own_clocks(sdc_backend: str) -> None:
    spec = _spec("good_clock_groups_two_statements")
    assert len(spec.async_groups) == 2
    assert spec.are_async("clk_a", "clk_b")
    assert spec.are_async("clk_b", "clk_c")
    assert not spec.are_async("clk_a", "clk_c")


def test_five_clock_groups(sdc_backend: str) -> None:
    spec = _spec("good_five_clock_exclusive")
    assert [len(g) for g in spec.async_groups] == [5]
    assert len(spec.exclusive_groups) == 2
    assert spec.is_unreachable_crossing("clk_m0", "clk_m1")
    assert spec.is_unreachable_crossing("clk_io", "clk_dbg")
    assert not spec.is_unreachable_crossing("clk_m1", "clk_dbg")
    assert spec.are_async("clk_m1", "clk_dbg")


def test_one_way_false_path_is_a_pair(sdc_backend: str) -> None:
    spec = _spec("good_false_path_one_direction")
    assert spec.false_path_pairs == {frozenset({"ck_a", "ck_b"})}
    assert spec.are_async("ck_b", "ck_a")


def test_datapath_only_beats_generated_and_exclusive(sdc_backend: str) -> None:
    spec = _spec("good_datapath_only_overrides")
    assert spec.resolve("clk_half") == "clk"
    assert spec.are_async("clk", "clk_half")
    assert not spec.is_unreachable_crossing("ck_m0", "ck_m1")
    assert spec.are_async("ck_m0", "ck_m1")


def test_case_analysis_on_a_braced_bus_bit(sdc_backend: str) -> None:
    assert _spec("good_case_analysis_mode_sel0").case_analysis == {"mode[1]": "0"}
    assert _spec("bad_case_analysis_mode_sel1").case_analysis == {"mode[1]": "1"}


def test_virtual_clocks_have_no_ports(sdc_backend: str) -> None:
    spec = _spec("bad_virtual_clock_io")
    assert spec.clocks["vclk_ext"].ports == ()
    assert spec.clocks["vclk_sys"].ports == ()
    assert spec.port_clock["ext_req"] == "vclk_ext"
    assert spec.are_async("clk", "vclk_ext")
    assert not spec.are_async("clk", "vclk_sys")


# --- Tcl-generated constraints ----------------------------------------------


def test_tcl_generated_constraints(
    sdc_backend: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The ``tcl`` backend evaluates the clock table; the ``tokenizer``
    fallback cannot, and its verdict is pinned here: no clock is declared,
    so each clock port is an undeclared clock (CDC-021, one per port),
    no crossing is checked, and the file is reported as partly skipped."""
    name = "tcl_generated_constraints"
    res, report = _analyze(name)
    spec = _spec(name)
    if sdc_backend == "tcl":
        assert sorted(spec.clocks) == ["clk_a", "clk_b", "clk_c"]
        assert spec.clocks["clk_b"].period == pytest.approx(7.5)
        assert [len(g) for g in spec.async_groups] == [3]
        assert spec.port_clock == {"d_a": "clk_a", "d_b": "clk_b"}
        assert report["summary"]["async_crossings"] == 3
        assert report["violations"] == []
        assert res.exit_code == 0
    else:
        assert spec.clocks == {}
        assert report["summary"]["async_crossings"] == 0
        got = sorted(
            (v["rule_id"], v["message"].split("'")[1]) for v in report["violations"]
        )
        assert got == [("CDC-021", "clk_a"), ("CDC-021", "clk_b"), ("CDC-021", "clk_c")]
        assert any("sdc.tokenizer_skipped" in r.getMessage() for r in caplog.records)
        assert res.exit_code == 1
