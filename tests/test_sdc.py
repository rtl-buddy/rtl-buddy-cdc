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


def test_create_generated_clock_source_does_not_consume_target_port() -> None:
    spec = parse(
        """
        create_clock -name clk -period 10.0 [get_ports clk]
        create_generated_clock -name clk_fwd -master_clock clk \\
            -source [get_ports clk] [get_ports clk_fwd]
        """
    )

    assert spec.clocks["clk_fwd"].ports == ("clk_fwd",)
    assert spec.clock_for_port("clk_fwd") == "clk_fwd"
    assert spec.resolve("clk_fwd") == "clk"


# --- Issue #140 shape coverage ------------------------------------------------
#
# The tests above pin the port-target shape. The cases below extend
# the regression net to the other trailing-target forms that share
# the same parser path:
#
#   1. pin target (lands in ``pin_clocks`` rather than ``Clock.ports``)
#   2. pin source + pin target (chained-forwarded-clock idiom)
#   3. brace-form source ``{ck_a}`` (Tcl-legal, single-token collection)
#   4. ``-source`` not last (the ordering existing fixtures use today —
#      kept here as a non-regression sentinel)


def test_cgc_source_last_pin_target_retains_target() -> None:
    """Pin target lands in ``pin_clocks``. With the bug, ``pin_clocks``
    is empty and the analyzer's clock-network trace walks past the pin
    to the upstream port — flops downstream get the wrong clock root."""
    spec = parse(
        """
        create_clock -name ck_a -period 10.0 [get_ports ck_a]
        create_generated_clock -name ck_b0 -master_clock ck_a \\
            -source [get_ports ck_a] [get_pins u_buf/Y]
        """
    )
    assert spec.pin_clocks == {"u_buf/Y": "ck_b0"}
    assert spec.clocks["ck_b0"].ports == ()
    assert spec.resolve("ck_b0") == "ck_a"


def test_cgc_source_last_pin_source_with_pin_target() -> None:
    """Pin-source + pin-target — the chained-forwarded-clock shape
    (B0's pin source → C0). Both collections are pin-form, so neither
    one would be mistaken for a flag even before the fix; the test
    pins the parser's behaviour on the chained idiom end-to-end."""
    spec = parse(
        """
        create_clock -name ck_a -period 10.0 [get_ports ck_a]
        create_generated_clock -name ck_b0 -master_clock ck_a \\
            -source [get_ports ck_a] [get_pins u_a/clk_out_b0]
        create_generated_clock -name ck_c0 -master_clock ck_b0 \\
            -source [get_pins u_a/clk_out_b0] [get_pins u_b0/clk_out]
        """
    )
    assert spec.pin_clocks == {
        "u_a/clk_out_b0": "ck_b0",
        "u_b0/clk_out": "ck_c0",
    }
    assert spec.resolve("ck_c0") == "ck_a"


def test_cgc_source_last_brace_source_retains_target() -> None:
    """Tcl brace-form source ``{ck_a}`` — uncommon but legal. Same
    failure shape (and same one-line fix) applies to single-token
    bracket sources like ``[clk]``. Regression for issue #142: the
    helper used to pre-increment past the first token before checking
    for the closer, so a single-token collection got mis-counted and
    the trailing target was swallowed."""
    spec = parse(
        """
        create_clock -name ck_a -period 10.0 [get_ports ck_a]
        create_generated_clock -name ck_b0 -master_clock ck_a \\
            -source {ck_a} [get_ports ck_b0]
        """
    )
    assert spec.clocks["ck_b0"].ports == ("ck_b0",)


def test_cgc_source_last_single_token_bracket_source_retains_target() -> None:
    """Single-token bracket form ``[clk]`` — the other shape covered
    by the issue #142 fix. Pair to the brace test above."""
    spec = parse(
        """
        create_clock -name ck_a -period 10.0 [get_ports ck_a]
        create_generated_clock -name ck_b0 -master_clock ck_a \\
            -source [ck_a] [get_ports ck_b0]
        """
    )
    assert spec.clocks["ck_b0"].ports == ("ck_b0",)


def test_cgc_source_followed_by_other_flag_still_works() -> None:
    """Non-regression: when ``-source`` is followed by another flag
    (the ordering all existing fixtures use), the target must still
    be captured. This case passed on the buggy parser too — it is
    here so the fix doesn't accidentally trade one regression for
    another."""
    spec = parse(
        """
        create_clock -name clk -period 10.0 [get_ports clk]
        create_generated_clock -name clk_div2 -master_clock clk \\
            -source [get_ports clk] -divide_by 2 [get_pins div_q/Q]
        """
    )
    assert spec.pin_clocks == {"div_q/Q": "clk_div2"}
    assert spec.resolve("clk_div2") == "clk"


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


def test_set_clock_groups_brace_with_spaces_inside_single_clock() -> None:
    """Single-clock brace groups with internal whitespace
    (``-group { ck0 } -group { ck1 }``) tokenise into five tokens via
    ``shlex`` — the handler must slurp through them and reassemble
    the clock name."""
    spec = parse(
        """
        create_clock -name ck0 -period 10 [get_ports ck0]
        create_clock -name ck1 -period 7  [get_ports ck1]
        set_clock_groups -asynchronous -group { ck0 } -group { ck1 }
        """
    )
    assert spec.are_async("ck0", "ck1")


def test_set_clock_groups_brace_with_spaces_inside_multi_clock() -> None:
    """Multi-clock brace groups with internal whitespace
    (``-group { ck0 ck1 } -group { ck2 ck3 }``). Tokenises into ten
    tokens; the handler's slurp-until-next-flag loop must capture
    every clock in each group (issue #23).

    Pre-fix behaviour was: only the first clock per group survived,
    silently giving up on the rest. The reproducer from #23 lives
    here verbatim — all four cross-group pairs must be async."""
    spec = parse(
        """
        create_clock -name ck0 -period 10 [get_ports ck0]
        create_clock -name ck1 -period 10 [get_ports ck1]
        create_clock -name ck2 -period 10 [get_ports ck2]
        create_clock -name ck3 -period 10 [get_ports ck3]
        set_clock_groups -asynchronous -group { ck0 ck1 } -group { ck2 ck3 }
        """
    )
    # All four pairs across the two groups must be async.
    assert spec.are_async("ck0", "ck2")
    assert spec.are_async("ck1", "ck2")
    assert spec.are_async("ck0", "ck3")
    assert spec.are_async("ck1", "ck3")
    # And the groups themselves should each contain both clocks.
    assert len(spec.async_groups) == 1
    groups = spec.async_groups[0]
    assert any(g == {"ck0", "ck1"} for g in groups)
    assert any(g == {"ck2", "ck3"} for g in groups)


def test_set_clock_groups_brace_no_spaces_multi_clock() -> None:
    """No-space form (``-group {ck0 ck1}``) was already working before
    issue #23 — pinning it here so the rewrite of
    ``_handle_set_clock_groups`` doesn't silently regress the case
    most SDC files actually use."""
    spec = parse(
        """
        create_clock -name ck0 -period 10 [get_ports ck0]
        create_clock -name ck1 -period 10 [get_ports ck1]
        create_clock -name ck2 -period 10 [get_ports ck2]
        create_clock -name ck3 -period 10 [get_ports ck3]
        set_clock_groups -asynchronous -group {ck0 ck1} -group {ck2 ck3}
        """
    )
    assert spec.are_async("ck0", "ck2")
    assert spec.are_async("ck1", "ck3")
    groups = spec.async_groups[0]
    assert any(g == {"ck0", "ck1"} for g in groups)
    assert any(g == {"ck2", "ck3"} for g in groups)


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


def test_set_input_delay_without_clock_warns_and_leaves_port_untyped() -> None:
    """set_input_delay needs -clock to anchor the delay to a clock
    edge; without it the constraint has no STA semantics. Real timers
    reject it. The parser previously dropped this silently — now it
    surfaces a partial_warning and leaves the port untyped so CDC-011
    (issue #97) can pick the port up as unconstrained."""
    spec = parse(
        """
        create_clock -name clk -period 10 [get_ports clk]
        set_input_delay 1.5 [get_ports d_in]
        """
    )
    assert "d_in" not in spec.port_clock, (
        "Port without -clock anchor must stay untyped — adding it to "
        "port_clock would fabricate a clock relationship the user "
        "never declared."
    )
    assert any(
        "no -clock anchor" in w and "d_in" in w for w in spec.partial_warnings
    ), (
        f"Expected a partial_warning naming d_in and 'no -clock anchor'; "
        f"got {spec.partial_warnings!r}"
    )


def test_set_input_delay_without_clock_and_without_ports_stays_silent() -> None:
    """Bare delay with no port target is a delay-only / defaults-style
    usage. No port mapping to fabricate, nothing actionable for CDC,
    and not the misuse pattern the warning targets — stay silent."""
    spec = parse(
        """
        create_clock -name clk -period 10 [get_ports clk]
        set_input_delay 1.5
        """
    )
    assert spec.partial_warnings == []
