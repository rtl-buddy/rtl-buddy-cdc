"""SDC parser unit tests — focus on the CDC-relevant subset."""

from __future__ import annotations

from rtl_buddy_cdc.sdc import parse


def test_create_clock_basic() -> None:
    spec = parse("create_clock -name src_clk -period 10.0 [get_ports src_clk]")
    assert "src_clk" in spec.clocks
    clk = spec.clocks["src_clk"]
    assert clk.period == 10.0
    assert clk.ports == ("src_clk",)


def test_create_clock_without_get_ports() -> None:
    spec = parse("create_clock -name foo -period 4.5")
    assert spec.clocks["foo"].period == 4.5
    assert spec.clocks["foo"].ports == ()


def test_set_clock_groups_async() -> None:
    spec = parse(
        """
        create_clock -name src_clk -period 10.0 [get_ports src_clk]
        create_clock -name dst_clk -period 7.5  [get_ports dst_clk]
        set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
        """
    )
    assert spec.are_async("src_clk", "dst_clk")
    assert spec.are_async("dst_clk", "src_clk")
    assert not spec.are_async("src_clk", "src_clk")


def test_unknown_clock_pair_is_not_async() -> None:
    """Conservative default: clocks not mentioned in any group are sync."""
    spec = parse("create_clock -name only_clk -period 10.0 [get_ports clk]")
    assert not spec.are_async("only_clk", "other_clk")


def test_continuation_and_comments() -> None:
    spec = parse(
        """
        # a comment
        create_clock -name a -period 8 \\
            [get_ports a_pin]
        create_clock -name b -period 5 [get_ports b_pin]
        # another comment
        set_clock_groups -asynchronous \\
            -group {a} \\
            -group {b}
        """
    )
    assert spec.clocks["a"].ports == ("a_pin",)
    assert spec.clocks["b"].ports == ("b_pin",)
    assert spec.are_async("a", "b")


def test_unsupported_command_ignored() -> None:
    spec = parse(
        """
        create_clock -name clk -period 10 [get_ports clk]
        set_input_delay -clock clk 1.0 [get_ports d_in]
        set_max_delay -from [get_ports a] 5.0
        """
    )
    assert "clk" in spec.clocks


def test_clock_for_port_lookup() -> None:
    spec = parse("create_clock -name src_clk -period 10 [get_ports src_clk_pin]")
    assert spec.clock_for_port("src_clk_pin") == "src_clk"
    assert spec.clock_for_port("missing") is None


# ---- create_generated_clock -------------------------------------------------


def test_create_generated_clock_div2_resolves_to_master() -> None:
    spec = parse(
        """
        create_clock -name clk -period 10.0 [get_ports clk]
        create_generated_clock -name clk_div2 -source [get_ports clk] \\
            -divide_by 2 [get_pins div_q/Q]
        """
    )
    assert spec.clocks["clk_div2"].is_generated
    # Without -master_clock, master is left None and resolve() returns
    # the clock's own name. Adding -master_clock would chain.
    spec2 = parse(
        """
        create_clock -name clk -period 10.0 [get_ports clk]
        create_generated_clock -name clk_div2 -master_clock clk \\
            -source [get_ports clk] -divide_by 2 [get_pins div_q/Q]
        """
    )
    assert spec2.resolve("clk_div2") == "clk"
    # Master and divided clock are *not* async by default.
    assert not spec2.are_async("clk", "clk_div2")


def test_generated_clock_async_override_wins() -> None:
    spec = parse(
        """
        create_clock -name clk -period 10.0 [get_ports clk]
        create_generated_clock -name clk_div2 -master_clock clk \\
            -source [get_ports clk] -divide_by 2 [get_pins div_q/Q]
        set_clock_groups -asynchronous -group {clk} -group {clk_div2}
        """
    )
    # Explicit override: SDC says these are async; CDC respects that
    # even though they share a master.
    assert spec.are_async("clk", "clk_div2")


def test_generated_clock_chain_resolves_transitively() -> None:
    spec = parse(
        """
        create_clock -name clk -period 10.0 [get_ports clk]
        create_generated_clock -name clk_d2 -master_clock clk \\
            -source [get_ports clk] -divide_by 2 [get_pins x]
        create_generated_clock -name clk_d4 -master_clock clk_d2 \\
            -source [get_pins x] -divide_by 2 [get_pins y]
        """
    )
    assert spec.resolve("clk_d4") == "clk"


# ---- set_false_path ---------------------------------------------------------


def test_false_path_clock_pair_is_treated_as_async() -> None:
    spec = parse(
        """
        create_clock -name a -period 10 [get_ports a]
        create_clock -name b -period 7  [get_ports b]
        set_false_path -from [get_clocks a] -to [get_clocks b]
        """
    )
    assert spec.are_async("a", "b")
    assert spec.are_async("b", "a")


def test_false_path_through_is_partial_warning() -> None:
    spec = parse(
        """
        create_clock -name a -period 10 [get_ports a]
        create_clock -name b -period 7  [get_ports b]
        set_false_path -from [get_clocks a] -through [get_pins x] -to [get_clocks b]
        """
    )
    assert not spec.are_async("a", "b")
    assert any("through" in w for w in spec.partial_warnings)


def test_false_path_pin_endpoint_is_partial_warning() -> None:
    spec = parse(
        """
        create_clock -name a -period 10 [get_ports a]
        set_false_path -from [get_pins inst/Q] -to [get_clocks a]
        """
    )
    assert any("non-clock endpoints" in w for w in spec.partial_warnings)


def test_false_path_rise_from_fall_to_recognized() -> None:
    """``-rise_from`` and ``-fall_to`` are recognised endpoint flags
    (sdc.py ``_ENDPOINT_FLAGS``). A path declared via the edge-specific
    variants must produce the same async pair as plain ``-from``/``-to``
    — the rule pack doesn't model edge phase, so the edge qualifier is
    informational only (issue #14, gap 4)."""
    spec = parse(
        """
        create_clock -name a -period 10 [get_ports a]
        create_clock -name b -period 7  [get_ports b]
        set_false_path -rise_from [get_clocks a] -fall_to [get_clocks b]
        """
    )
    assert spec.are_async("a", "b")
    assert spec.are_async("b", "a")


# ---- exclusive groups -------------------------------------------------------


def test_logically_exclusive_groups() -> None:
    spec = parse(
        """
        create_clock -name ck0 -period 10 [get_ports ck0]
        create_clock -name ck1 -period 7  [get_ports ck1]
        set_clock_groups -logically_exclusive -group {ck0} -group {ck1}
        """
    )
    assert spec.is_unreachable_crossing("ck0", "ck1")
    # Exclusive ≠ async — they don't coexist, so the rule pack
    # shouldn't even be asked the question, but the predicate is
    # nonetheless false.
    assert not spec.are_async("ck0", "ck1")


def test_physically_exclusive_groups() -> None:
    spec = parse(
        """
        create_clock -name ck0 -period 10 [get_ports ck0]
        create_clock -name ck1 -period 7  [get_ports ck1]
        set_clock_groups -physically_exclusive -group {ck0} -group {ck1}
        """
    )
    assert spec.is_unreachable_crossing("ck0", "ck1")


def test_set_clock_groups_brace_with_spaces_inside() -> None:
    """When braces have spaces inside (``{ ck0 } -group { ck1 }``)
    shlex splits ``{`` and ``ck0`` into separate tokens. The handler's
    brace-split re-glob fallback (sdc.py ``_extract_clock_list(args[i+1]
    + " " + args[i+2])``) recovers the clock name from the next token
    instead of dropping the group (issue #14, gap 4).

    Note: today the fallback recovers only the *first* clock when a
    multi-clock group has internal spaces (``{ ck0 ck1 }``); that
    quirk is a parser limitation, not a target of this regression
    sentinel. This test pins the single-clock case the fallback was
    written for."""
    spec = parse(
        """
        create_clock -name ck0 -period 10 [get_ports ck0]
        create_clock -name ck1 -period 7  [get_ports ck1]
        set_clock_groups -asynchronous -group { ck0 } -group { ck1 }
        """
    )
    # Both clocks declared and async-grouped despite the brace-padding
    # that splits the tokens. Without the re-glob fallback, both groups
    # would collapse to empty and `are_async` would return False.
    assert spec.are_async("ck0", "ck1")


def test_set_clock_groups_without_kind_warns() -> None:
    spec = parse(
        """
        create_clock -name a -period 10 [get_ports a]
        create_clock -name b -period 7  [get_ports b]
        set_clock_groups -group {a} -group {b}
        """
    )
    assert not spec.are_async("a", "b")
    assert any("missing -asynchronous" in w for w in spec.partial_warnings)


# ---- set_input_delay / set_output_delay ------------------------------------


def test_set_input_delay_records_port_clock() -> None:
    spec = parse(
        """
        create_clock -name clk -period 10 [get_ports clk]
        set_input_delay -clock clk 1.5 [get_ports d_in]
        set_output_delay -clock clk 2.0 [get_ports d_out]
        """
    )
    assert spec.clock_for_port("d_in") == "clk"
    assert spec.clock_for_port("d_out") == "clk"


def test_set_input_delay_overrides_create_clock_port_lookup() -> None:
    """If a port is named on a create_clock AND a set_input_delay,
    the set_input_delay mapping wins (it's the more specific user
    statement of intent)."""
    spec = parse(
        """
        create_clock -name ext -period 10 [get_ports d_in]
        create_clock -name clk -period 5  [get_ports clk]
        set_input_delay -clock clk 1.0 [get_ports d_in]
        """
    )
    assert spec.clock_for_port("d_in") == "clk"


# ---- diagnostics ------------------------------------------------------------


def test_filter_clause_is_partial_warning() -> None:
    spec = parse(
        """
        create_clock -name clk -period 10 \\
            [get_ports -filter {NAME =~ \\"clk*\\"}]
        """
    )
    # Without filter evaluation the parser may still pick up the
    # name from -name, but the filter-presence flag should surface.
    assert any("filter" in w for w in spec.partial_warnings)
