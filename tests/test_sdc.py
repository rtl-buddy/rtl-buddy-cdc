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
