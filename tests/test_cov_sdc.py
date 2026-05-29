"""Coverage-raising tests for :mod:`rtl_buddy_cdc.sdc` parser corners.

These exercise the less-trodden branches of the SDC parser through the
public ``parse`` / ``parse_file`` entry points (plus a few targeted
direct calls into the documented Layer-2 helpers ``_slice`` / ``Arity``
/ ``ArgSpec`` / ``_extract_names`` / ``_safe_int``):

  * ``ClockSpec`` resolve/are_async edge cases — master cycles, the
    ``-master_clock`` chain dead-end, the unconstrained sentinel, and
    the unresolved-name false-path branch.
  * The ``_slice`` arg-spec slicer's malformed-input corners — a
    one-operand flag that runs off the end of the command, and the
    unknown-flag skip heuristic (flag-then-flag vs flag-then-operand).
  * Per-command handler bail-outs that append ``partial_warnings``:
    missing/non-numeric ``-period``, missing ``-name``, the
    ``create_generated_clock`` filter clause, duplicate generated-clock
    names, fewer-than-two ``-group`` clauses, and an incomplete
    ``set_false_path`` ``-from``/``-to``.
  * The collection-peeling helpers — ``-filter`` cut, the
    ``-include_generated_clocks`` drop, and bare vendor-flag skipping.

No Yosys binary, no frontend: pure text-in / ``ClockSpec``-out.
"""

from __future__ import annotations

from pathlib import Path

from rtl_buddy_cdc.sdc import (
    UNCONSTRAINED_SENTINEL,
    ArgSpec,
    Arity,
    Clock,
    ClockSpec,
    _extract_names,
    _safe_int,
    _slice,
    parse,
    parse_file,
)

# ---- ClockSpec.resolve corners ---------------------------------------------


def test_resolve_master_cycle_guard_terminates() -> None:
    """A self-referential master chain must not infinite-loop.

    ``resolve`` keeps a ``seen`` set; the first clock revisited
    terminates the walk and that name is returned (sdc.py:144). Build
    a two-clock A<->B cycle directly and confirm ``resolve`` returns
    within the cycle rather than hanging.
    """
    spec = ClockSpec()
    spec.clocks["A"] = Clock(
        name="A", period=0.0, ports=(), master="B", is_generated=True
    )
    spec.clocks["B"] = Clock(
        name="B", period=0.0, ports=(), master="A", is_generated=True
    )
    # First revisited node terminates; result is one of the cycle members.
    assert spec.resolve("A") in {"A", "B"}
    assert spec.resolve("B") in {"A", "B"}


def test_resolve_unknown_master_returns_name_unchanged() -> None:
    """A ``-master_clock`` naming a clock that was never declared:
    the chain dead-ends and ``resolve`` returns the unknown name."""
    spec = parse(
        """
        create_generated_clock -name g -master_clock ck_missing \\
            -divide_by 2 [get_pins u/clk_out]
        """
    )
    # ck_missing is not in spec.clocks, so resolve stops at it.
    assert spec.resolve("g") == "ck_missing"


# ---- ClockSpec.are_async corners -------------------------------------------


def test_are_async_unconstrained_sentinel_is_async_to_everything() -> None:
    """The synthesised ``<unconstrained>`` sentinel is async to every
    real clock (sdc.py:177-178)."""
    spec = parse("create_clock -name clk -period 10 [get_ports clk]")
    assert spec.are_async(UNCONSTRAINED_SENTINEL, "clk")
    assert spec.are_async("clk", UNCONSTRAINED_SENTINEL)
    # Identity short-circuits before the sentinel check.
    assert not spec.are_async(UNCONSTRAINED_SENTINEL, UNCONSTRAINED_SENTINEL)


def test_are_async_unresolved_name_false_path_branch() -> None:
    """A false-path declared on *generated* clock names whose resolved
    roots differ is honoured via the raw-name ``frozenset({a, b})``
    branch (sdc.py:192-193).

    ``g0``/``g1`` resolve to distinct masters ``m0``/``m1``, so the
    resolved-root false-path check (``frozenset({m0, m1})``, sdc.py:190)
    misses — that pair was never declared. The raw-name set still
    carries ``{g0, g1}``, so are_async returns True only via line 193.
    """
    spec = parse(
        """
        create_clock -name m0 -period 10 [get_ports m0]
        create_clock -name m1 -period 7  [get_ports m1]
        create_generated_clock -name g0 -master_clock m0 \\
            -divide_by 2 [get_pins u0/clk_out]
        create_generated_clock -name g1 -master_clock m1 \\
            -divide_by 2 [get_pins u1/clk_out]
        set_false_path -from [get_clocks g0] -to [get_clocks g1]
        """
    )
    # Roots differ (m0 vs m1) so the ra==rb short-circuit is skipped;
    # {m0, m1} was never declared a false-path, but {g0, g1} was.
    assert spec.resolve("g0") == "m0"
    assert spec.resolve("g1") == "m1"
    assert frozenset({"g0", "g1"}) in spec.false_path_pairs
    assert frozenset({"m0", "m1"}) not in spec.false_path_pairs
    assert spec.are_async("g0", "g1")


def test_are_async_resolved_root_async_group_branch() -> None:
    """Two generated clocks on distinct masters, with an async group
    declared on the *masters*: are_async must collapse the generated
    clocks to their roots and find the cross-group relation
    (sdc.py:194-198)."""
    spec = parse(
        """
        create_clock -name m0 -period 10 [get_ports m0]
        create_clock -name m1 -period 7  [get_ports m1]
        create_generated_clock -name g0 -master_clock m0 \\
            -divide_by 2 [get_pins u0/clk_out]
        create_generated_clock -name g1 -master_clock m1 \\
            -divide_by 2 [get_pins u1/clk_out]
        set_clock_groups -asynchronous -group {m0} -group {m1}
        """
    )
    # Neither g0 nor g1 is named in any group; resolution to m0 / m1
    # is what makes them async.
    assert spec.resolve("g0") == "m0"
    assert spec.resolve("g1") == "m1"
    assert spec.are_async("g0", "g1")
    assert spec.are_async("g1", "g0")


# ---- parse() top-level: blank command skip ---------------------------------


def test_parse_skips_blank_lines_between_commands() -> None:
    """``parse`` continues past empty word-lists (sdc.py:231); a file
    that is mostly blank still yields the one real clock."""
    spec = parse("\n\n   \n\ncreate_clock -name clk -period 10 [get_ports clk]\n\n")
    assert set(spec.clocks) == {"clk"}
    assert spec.clocks["clk"].period == 10.0


def test_parse_file_reads_from_disk(tmp_path: Path) -> None:
    """``parse_file`` reads the path then delegates to ``parse``."""
    p = tmp_path / "design.sdc"
    p.write_text("create_clock -name clk -period 12.5 [get_ports clk]\n")
    spec = parse_file(p)
    assert spec.clocks["clk"].period == 12.5
    # And the str-path overload.
    spec2 = parse_file(str(p))
    assert spec2.clocks["clk"].period == 12.5


# ---- _slice arg-spec slicer corners ----------------------------------------


def test_slice_arity_one_flag_at_end_of_command() -> None:
    """An ``Arity.ONE`` flag with no following operand (it is the last
    word) is consumed without recording a value (sdc.py:609-610)."""
    spec = ArgSpec(flags={"-name": Arity.ONE})
    parsed = _slice(["-name"], spec)
    assert parsed.flags["-name"] == []
    assert not parsed.present("-name")
    assert parsed.first("-name") is None
    assert parsed.tail == []


def test_slice_unknown_flag_then_operand_skips_both() -> None:
    """Unknown flag followed by a non-flag operand: the heuristic skips
    both, assuming one-operand arity (sdc.py:621-622). The real flag
    after them must still parse."""
    spec = ArgSpec(flags={"-name": Arity.ONE})
    parsed = _slice(["-weird", "value", "-name", "clk"], spec)
    assert parsed.first("-name") == "clk"
    # The unknown flag and its operand left no tail and no spurious flag.
    assert parsed.tail == []


def test_slice_unknown_flag_then_flag_skips_only_flag() -> None:
    """Unknown flag immediately followed by another flag: only the
    unknown flag is skipped (sdc.py:623-624); the following known flag
    parses normally."""
    spec = ArgSpec(flags={"-name": Arity.ONE})
    parsed = _slice(["-weird", "-name", "clk"], spec)
    assert parsed.first("-name") == "clk"
    assert parsed.tail == []


def test_slice_unknown_flag_at_end_skips_only_flag() -> None:
    """Unknown flag as the final word: nothing follows, so just the
    flag is skipped (sdc.py:623-624 else-branch via i+1 == n)."""
    spec = ArgSpec(flags={"-name": Arity.ONE})
    parsed = _slice(["-name", "clk", "-weird"], spec)
    assert parsed.first("-name") == "clk"
    assert parsed.tail == []


# ---- _handle_create_clock bail-outs ----------------------------------------


def test_create_clock_missing_period_is_dropped() -> None:
    """No ``-period`` operand → handler returns early, no clock recorded
    (sdc.py:715-716)."""
    spec = parse("create_clock -name clk [get_ports clk]")
    assert "clk" not in spec.clocks
    assert spec.clocks == {}


def test_create_clock_non_numeric_period_is_dropped() -> None:
    """A ``-period`` that doesn't ``float()`` → ValueError → early
    return, no clock (sdc.py:719-720)."""
    spec = parse("create_clock -name clk -period not_a_number [get_ports clk]")
    assert "clk" not in spec.clocks


def test_create_clock_name_falls_back_to_first_port() -> None:
    """With no ``-name`` but a port target, the first port becomes the
    clock name (sdc.py:729-730)."""
    spec = parse("create_clock -period 10 [get_ports my_clk]")
    assert "my_clk" in spec.clocks
    assert spec.clocks["my_clk"].period == 10.0
    assert spec.clocks["my_clk"].ports == ("my_clk",)


def test_create_clock_no_name_no_ports_is_dropped() -> None:
    """Neither ``-name`` nor any port target → no name to key on → the
    handler returns without recording a clock (sdc.py:731-732)."""
    spec = parse("create_clock -period 10")
    assert spec.clocks == {}
    assert spec.partial_warnings == []


# ---- _handle_create_generated_clock bail-outs ------------------------------


def test_generated_clock_name_falls_back_to_first_target() -> None:
    """No ``-name`` on create_generated_clock, but a (non-pin) target —
    the first target supplies the name (sdc.py:776-777)."""
    spec = parse(
        """
        create_clock -name ck_a -period 10 [get_ports ck_a]
        create_generated_clock -master_clock ck_a -divide_by 2 [get_ports ck_b]
        """
    )
    assert "ck_b" in spec.clocks
    assert spec.clocks["ck_b"].is_generated
    assert spec.clocks["ck_b"].master == "ck_a"


def test_generated_clock_missing_name_and_target_warns() -> None:
    """No ``-name`` and no fallback target → a partial warning, no clock
    (sdc.py:778-782)."""
    spec = parse("create_generated_clock -master_clock ck_a -divide_by 2")
    assert spec.clocks == {}
    assert any(
        "create_generated_clock: missing -name" in w for w in spec.partial_warnings
    ), spec.partial_warnings


def test_generated_clock_filter_clause_warns() -> None:
    """A ``-filter`` clause inside the target collection surfaces a
    partial warning (sdc.py:792-793) but the clock is still recorded."""
    spec = parse(
        """
        create_clock -name ck_a -period 10 [get_ports ck_a]
        create_generated_clock -name g -master_clock ck_a -divide_by 2 \\
            [get_pins -filter {NAME =~ \\"u/*\\"} u/clk_out]
        """
    )
    assert "g" in spec.clocks
    assert any(
        "create_generated_clock g" in w and "filter" in w for w in spec.partial_warnings
    ), spec.partial_warnings


def test_generated_clock_duplicate_name_warns() -> None:
    """Two ``create_generated_clock -name g`` → the second silently
    overrides and a duplicate-name partial warning fires (sdc.py:809-811)."""
    spec = parse(
        """
        create_clock -name ck_a -period 10 [get_ports ck_a]
        create_generated_clock -name g -master_clock ck_a -divide_by 2 \\
            [get_pins u0/clk_out]
        create_generated_clock -name g -master_clock ck_a -divide_by 4 \\
            [get_pins u1/clk_out]
        """
    )
    assert any(
        "duplicate clock name 'g'" in w and "create_generated_clock" in w
        for w in spec.partial_warnings
    ), spec.partial_warnings
    # The second declaration's pin target is the one retained.
    assert spec.pin_clocks == {"u0/clk_out": "g", "u1/clk_out": "g"}


# ---- _handle_set_clock_groups: fewer than 2 groups -------------------------


def test_set_clock_groups_single_group_warns() -> None:
    """A ``set_clock_groups`` kind with only one ``-group`` clause is
    meaningless (you need at least two groups to declare a relation):
    a partial warning fires and nothing is recorded (sdc.py:848-852)."""
    spec = parse(
        """
        create_clock -name ck0 -period 10 [get_ports ck0]
        set_clock_groups -asynchronous -group {ck0}
        """
    )
    assert spec.async_groups == []
    assert spec.exclusive_groups == []
    assert any("fewer than 2 -group" in w for w in spec.partial_warnings), (
        spec.partial_warnings
    )


# ---- _handle_set_false_path: incomplete from/to ----------------------------


def test_set_false_path_missing_to_warns() -> None:
    """``set_false_path -from <clk>`` with no ``-to`` clock list is an
    incomplete pair: a partial warning fires and no false-path pair is
    recorded (sdc.py:908-912)."""
    spec = parse(
        """
        create_clock -name a -period 10 [get_ports a]
        create_clock -name b -period 7  [get_ports b]
        set_false_path -from [get_clocks a]
        """
    )
    assert spec.false_path_pairs == set()
    assert not spec.are_async("a", "b")
    assert any("incomplete -from/-to clock list" in w for w in spec.partial_warnings), (
        spec.partial_warnings
    )


# ---- _handle_set_delay: filter clause --------------------------------------


def test_set_input_delay_filter_clause_warns() -> None:
    """A ``-filter`` inside the ``[get_ports ...]`` target of a
    ``set_input_delay`` surfaces the generic delay filter warning
    (sdc.py:939-940)."""
    spec = parse(
        """
        create_clock -name clk -period 10 [get_ports clk]
        set_input_delay -clock clk 1.5 [get_ports -filter {DIRECTION == in} d_in]
        """
    )
    assert any(
        "set_*_delay: ignored unsupported -filter" in w for w in spec.partial_warnings
    ), spec.partial_warnings


# ---- _extract_names collection-peeling corners -----------------------------


def test_extract_names_filter_cut_drops_trailing_tokens() -> None:
    """``-filter`` cuts the name list at its position; tokens after
    ``-filter`` are dropped and ``saw_filter`` is True (sdc.py:996-1005).
    Here a name precedes the filter, so it survives the cut."""
    names, saw_filter = _extract_names("[get_ports keep -filter {NAME =~ x} drop]")
    assert saw_filter is True
    assert names == ["keep"]


def test_extract_names_filter_substring_without_standalone_token() -> None:
    """``saw_filter`` is True whenever ``-filter`` is a substring, but
    the cut only happens if ``-filter`` is a standalone token. When it
    is glued to other text (``-filterish``) the ``.index`` lookup raises
    ValueError and is swallowed (sdc.py:1004-1005) — names are kept and
    the glued ``-``-prefixed token is dropped by the vendor-flag skip."""
    names, saw_filter = _extract_names("[get_ports -filterish clk]")
    assert saw_filter is True
    # No standalone "-filter" token → no cut → clk survives; the
    # "-filterish" pseudo-flag is skipped as an unknown vendor flag.
    assert names == ["clk"]


def test_extract_names_include_generated_clocks_dropped() -> None:
    """The ``-include_generated_clocks`` vendor flag is silently dropped
    from the name list (sdc.py:1008-1009) while real names survive."""
    names, saw_filter = _extract_names(
        "[get_clocks -include_generated_clocks ck_a ck_b]"
    )
    assert saw_filter is False
    assert names == ["ck_a", "ck_b"]


def test_extract_names_unknown_vendor_flag_skipped() -> None:
    """Any other ``-``-prefixed token inside a ``get_*`` expression is
    conservatively skipped (sdc.py:1010-1012)."""
    names, _ = _extract_names("[get_ports -hierarchical clk]")
    assert names == ["clk"]


# ---- _safe_int fallback ----------------------------------------------------


def test_safe_int_none_returns_default() -> None:
    """``_safe_int(None, ...)`` short-circuits to the default
    (sdc.py:1031-1032)."""
    assert _safe_int(None, default=1) == 1
    assert _safe_int(None, default=7) == 7


def test_safe_int_non_numeric_returns_default() -> None:
    """A word that doesn't ``int()`` falls back to the default via the
    ValueError branch (sdc.py:1035-1036)."""
    assert _safe_int("not_int", default=3) == 3
    # And a parseable value passes straight through.
    assert _safe_int("12", default=3) == 12


def test_generated_clock_non_numeric_divide_by_uses_default_period() -> None:
    """End-to-end exercise of the ``_safe_int`` ValueError fallback:
    a non-numeric ``-divide_by`` defaults to 1, so the generated clock
    inherits the master's period unscaled."""
    spec = parse(
        """
        create_clock -name ck_a -period 10 [get_ports ck_a]
        create_generated_clock -name g -master_clock ck_a \\
            -divide_by bogus [get_pins u/clk_out]
        """
    )
    # default divide_by == 1, multiply_by == 1 → period == master period.
    assert spec.clocks["g"].period == 10.0
    assert spec.resolve("g") == "ck_a"
