"""Unit layer for the SDC mutation operators (rtl-buddy-cdc#293).

Plain pytest — no marker, no Yosys, no xeno. These pin the *text
rewriting* contract of :mod:`tests.fuzz._sdc_mutator` on small inline
SDC strings: which commands are found, what exactly is removed or
rewritten, and when the CDC-021 claim is allowed to be positive. The
end-to-end "the analyzer really sees the mutated SDC" check lives in
:mod:`tests.fuzz.test_sdc_mutants`, which needs Yosys.
"""

from __future__ import annotations

from rtl_buddy_cdc import sdc as sdc_mod

from ._sdc_mutator import (
    CLOCK_PERIOD_SCALE,
    UNDECLARE_CLOCK_PORT,
    iter_clock_period_scale,
    iter_sdc_mutants,
    iter_undeclare_clock_port,
    split_commands,
)
from .templates.base import RenderedCase

_SV = """\
module top (input logic src_clk, input logic dst_clk, output logic q);
    logic a;
    always_ff @(posedge src_clk) a <= ~a;
    always_ff @(posedge dst_clk) q <= a;
endmodule
"""

_SDC_BRACKET = (
    "create_clock -name src_clk -period 10.0 [get_ports src_clk]\n"
    "create_clock -name dst_clk -period 7.5 [get_ports dst_clk]\n"
    "set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}\n"
)

_SDC_BRACE = (
    "create_clock -name src_clk -period 10.0 {src_clk}\n"
    "create_clock -name dst_clk -period 7.5 {dst_clk}\n"
)

_SDC_BARE = (
    "create_clock -name src_clk -period 10.0 src_clk\n"
    "create_clock -name dst_clk -period 7.5 dst_clk\n"
)

_SDC_CONTINUED = (
    "create_clock -name src_clk \\\n"
    "             -period 10.0 \\\n"
    "             [get_ports src_clk]\n"
    "create_clock -name dst_clk -period 7.5 [get_ports dst_clk]\n"
    "set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}\n"
)


def _parent(sdc: str, sv: str = _SV) -> RenderedCase:
    return RenderedCase(
        template_name="unit",
        case_id="unit_case",
        sv=sv,
        sdc=sdc,
        top="top",
        params={},
        expected=(),
    )


# ---- split_commands ---------------------------------------------------------


def test_split_commands_spans_round_trip() -> None:
    """Every command's span slices back out of the source text."""
    cmds = split_commands(_SDC_BRACKET)
    assert [c.name for c in cmds] == [
        "create_clock",
        "create_clock",
        "set_clock_groups",
    ]
    for cmd in cmds:
        assert _SDC_BRACKET[cmd.start : cmd.end].startswith(cmd.name)
    assert cmds[0].words == (
        "create_clock",
        "-name",
        "src_clk",
        "-period",
        "10.0",
        "[get_ports src_clk]",
    )


def test_split_commands_joins_continuation_lines() -> None:
    """A backslash-continued command is ONE command, not three."""
    cmds = split_commands(_SDC_CONTINUED)
    assert [c.name for c in cmds] == [
        "create_clock",
        "create_clock",
        "set_clock_groups",
    ]
    assert cmds[0].words == (
        "create_clock",
        "-name",
        "src_clk",
        "-period",
        "10.0",
        "[get_ports src_clk]",
    )
    assert _SDC_CONTINUED[cmds[0].start : cmds[0].end].count("\\\n") == 2


def test_split_commands_folds_trailing_comment_into_the_span() -> None:
    text = "create_clock -name c -period 2.0 [get_ports c]  # the fast one\nsecond\n"
    cmds = split_commands(text)
    assert cmds[0].name == "create_clock"
    assert text[cmds[0].start : cmds[0].end].endswith("# the fast one\n")
    assert cmds[1].name == "second"


def test_split_commands_skips_standalone_comments() -> None:
    text = "# header\ncreate_clock -name c -period 2.0 [get_ports c]\n"
    assert [c.name for c in split_commands(text)] == ["create_clock"]


# ---- UNDECLARE_CLOCK_PORT ---------------------------------------------------


def test_undeclare_removes_whole_command_and_nothing_else() -> None:
    mutants = list(iter_undeclare_clock_port(_SV, _SDC_BRACKET))
    assert [m.kind for m in mutants] == [UNDECLARE_CLOCK_PORT] * 2
    lines = _SDC_BRACKET.splitlines(keepends=True)
    # Byte-for-byte: the mutant is the parent minus exactly one line.
    assert mutants[0].sdc == lines[1] + lines[2]
    assert mutants[1].sdc == lines[0] + lines[2]


def test_undeclare_handles_brace_and_bare_forms() -> None:
    for text in (_SDC_BRACE, _SDC_BARE):
        mutants = list(iter_undeclare_clock_port(_SV, text))
        assert len(mutants) == 2
        assert "src_clk" not in sdc_mod.parse(mutants[0].sdc).clocks
        assert "dst_clk" in sdc_mod.parse(mutants[0].sdc).clocks
        assert mutants[0].cdc_rules_added == frozenset({"CDC-021"})


def test_undeclare_handles_continuation_lines() -> None:
    """The continued command is removed in full, leaving legal SDC."""
    mutants = list(iter_undeclare_clock_port(_SV, _SDC_CONTINUED))
    assert len(mutants) == 2
    assert "\\" not in mutants[0].sdc
    assert set(sdc_mod.parse(mutants[0].sdc).clocks) == {"dst_clk"}
    assert mutants[0].cdc_rules_added == frozenset({"CDC-021"})


def test_undeclare_claims_cdc_021_when_the_port_clocks_a_flop() -> None:
    mutants = list(iter_undeclare_clock_port(_SV, _SDC_BRACKET))
    assert mutants[0].cdc_rules_added == frozenset({"CDC-021"})
    assert "CDC-021" in mutants[0].rationale


def test_undeclare_stays_conservative_when_the_port_is_not_an_edge_signal() -> None:
    """A clock reaching CLK through a mux resolves to an internal net.

    The flop's traced domain is then ``muxed_clk``, not the port, so
    CDC-021 may legitimately stay silent — no positive claim.
    """
    sv = """\
module top (input logic src_clk, input logic dst_clk, input logic s, output logic q);
    logic muxed_clk;
    assign muxed_clk = s ? src_clk : dst_clk;
    always_ff @(posedge muxed_clk) q <= ~q;
endmodule
"""
    mutants = list(iter_undeclare_clock_port(sv, _SDC_BRACKET))
    assert [m.cdc_rules_added for m in mutants] == [frozenset(), frozenset()]
    assert all("CDC-021" in m.rationale for m in mutants)


def test_undeclare_stays_conservative_when_another_create_clock_covers_the_port() -> (
    None
):
    """Two declarations on one port: removing one leaves it declared."""
    text = (
        "create_clock -name src_a -period 10.0 [get_ports src_clk]\n"
        "create_clock -name src_b -period 20.0 [get_ports src_clk]\n"
    )
    mutants = list(iter_undeclare_clock_port(_SV, text))
    assert len(mutants) == 2
    assert [m.cdc_rules_added for m in mutants] == [frozenset(), frozenset()]
    assert all("CDC-021" in m.rationale for m in mutants)


def test_undeclare_stays_conservative_when_a_generated_clock_covers_the_port() -> None:
    text = (
        "create_clock -name src_clk -period 10.0 [get_ports src_clk]\n"
        "create_generated_clock -name gen -source [get_ports src_clk] "
        "-divide_by 2 [get_ports dst_clk]\n"
    )
    mutants = list(iter_undeclare_clock_port(_SV, text))
    assert mutants[0].cdc_rules_added == frozenset()


def test_undeclare_skips_commands_with_no_port_operand() -> None:
    text = "create_clock -name virt -period 10.0\n"
    assert list(iter_undeclare_clock_port(_SV, text)) == []


# ---- CLOCK_PERIOD_SCALE -----------------------------------------------------


def test_period_scale_emits_two_mutants_per_clock() -> None:
    mutants = list(iter_clock_period_scale(_SDC_BRACKET))
    assert [m.kind for m in mutants] == [CLOCK_PERIOD_SCALE] * 4
    periods = [sdc_mod.parse(m.sdc).clocks["src_clk"].period for m in mutants[:2]] + [
        sdc_mod.parse(m.sdc).clocks["dst_clk"].period for m in mutants[2:]
    ]
    assert periods == [1.0, 100.0, 0.75, 75.0]


def test_period_scale_changes_only_the_number() -> None:
    """Everything except the operand's bytes is preserved verbatim."""
    mutants = list(iter_clock_period_scale(_SDC_BRACKET))
    assert mutants[0].sdc == _SDC_BRACKET.replace(
        "-period 10.0 [get_ports src_clk]", "-period 1.0 [get_ports src_clk]", 1
    )
    assert mutants[1].sdc == _SDC_BRACKET.replace(
        "-period 10.0 [get_ports src_clk]", "-period 100.0 [get_ports src_clk]", 1
    )


def test_period_scale_formats_sub_unit_periods() -> None:
    text = "create_clock -name c -period 0.1 [get_ports c]\n"
    mutants = list(iter_clock_period_scale(text))
    assert [m.sdc for m in mutants] == [
        "create_clock -name c -period 0.01 [get_ports c]\n",
        "create_clock -name c -period 1.0 [get_ports c]\n",
    ]


def test_period_scale_handles_a_continued_command() -> None:
    mutants = list(iter_clock_period_scale(_SDC_CONTINUED))
    assert len(mutants) == 4
    assert sdc_mod.parse(mutants[0].sdc).clocks["src_clk"].period == 1.0
    # The continuation backslashes survive untouched.
    assert mutants[0].sdc.count("\\\n") == 2


def test_period_scale_prediction_is_conservative() -> None:
    for mutant in iter_clock_period_scale(_SDC_BRACKET):
        assert mutant.cdc_rules_added == frozenset()
        assert "CDC-009" in mutant.rationale


def test_period_scale_skips_a_command_without_a_period() -> None:
    text = "create_clock -name c [get_ports c]\n"
    assert list(iter_clock_period_scale(text)) == []


# ---- iter_sdc_mutants wrapping ---------------------------------------------


def test_iter_sdc_mutants_wraps_both_operators() -> None:
    parent = _parent(_SDC_BRACKET)
    cases = list(iter_sdc_mutants(parent))
    assert [mc.mutant.kind for mc in cases] == [UNDECLARE_CLOCK_PORT] * 2 + [
        CLOCK_PERIOD_SCALE
    ] * 4
    for mc in cases:
        assert mc.parent is parent
        assert mc.case.sdc == mc.mutant.sdc
        assert mc.case.template_name == "sdcmut_unit"
        assert mc.case.params["sdc_mutant_kind"] == mc.mutant.kind
        assert mc.case.extra_yosys_passes == parent.extra_yosys_passes


def test_iter_sdc_mutants_keeps_the_parent_sv_apart_from_the_top_rename() -> None:
    parent = _parent(_SDC_BRACKET)
    mc = next(iter(iter_sdc_mutants(parent)))
    assert mc.case.top == "top_sdcmut0"
    assert mc.case.sv == parent.sv.replace("top", "top_sdcmut0", 1)
    assert mc.case.sv.replace("top_sdcmut0", "top", 1) == parent.sv


def test_iter_sdc_mutants_gives_every_case_a_distinct_cache_key() -> None:
    """The rtl-buddy-cdc#293 cache-key hazard, pinned.

    ``runner._analyze`` writes ``<content_hash>.sdc`` only when the
    file is absent, so an SDC mutant that collided with its parent's
    digest would be silently analysed against the *parent's*
    constraints and the mutation would be a no-op.
    """
    parent = _parent(_SDC_BRACKET)
    cases = [mc.case for mc in iter_sdc_mutants(parent)]
    digests = [c.content_hash for c in cases]
    assert parent.content_hash not in digests
    assert len(set(digests)) == len(digests)
    assert len({c.case_id for c in cases}) == len(cases)
    assert len({c.top for c in cases}) == len(cases)


def test_iter_sdc_mutants_encodes_a_positive_claim_as_expected() -> None:
    parent = _parent(_SDC_BRACKET)
    undeclare = [
        mc for mc in iter_sdc_mutants(parent) if mc.mutant.kind == UNDECLARE_CLOCK_PORT
    ]
    assert [e.rule_id for e in undeclare[0].case.expected] == ["CDC-021"]
    assert undeclare[0].case.forbidden == ()


def test_iter_sdc_mutants_on_an_empty_sdc_yields_nothing() -> None:
    """``gap_g10`` ships an empty SDC by design — nothing to mutate."""
    assert list(iter_sdc_mutants(_parent(""))) == []
