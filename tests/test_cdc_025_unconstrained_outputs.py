"""CDC-025 — top-level output with no ``set_output_delay`` contract (#329).

The output-side mirror of CDC-011. An output with no
``set_output_delay -clock`` has no capture contract for CDC-024 to check;
CDC-025 reports it when clocked logic drives it:

* **error, always on** when some bit of it combinationally merges
  launches from two domains the SDC declares asynchronous;
* **warning, self-calibrating** otherwise — only once the SDC types at
  least one top-level output (``single_domain`` overrides the gate).

These tests drive the rule on hand-built netlists, under both SDC reader
backends, covering each shape, each exclusion and the interplay with
CDC-024. The fixture pairs ``bad_output_async_merge`` /
``good_output_async_merge_synced`` and ``bad_output_missing_contract`` /
``good_output_contracts_complete`` cover the same ground end to end.
"""

from __future__ import annotations

import pytest

from rtl_buddy_cdc import domain, rules, sdc
from rtl_buddy_cdc.netlist import Bit, Cell, Module, Port
from rtl_buddy_cdc.rules import _build_context, check_cdc_025


@pytest.fixture(autouse=True)
def _every_sdc_backend(sdc_backend):
    """Run every test here once per SDC reader backend (#298)."""
    return sdc_backend


# Bits 2..5 are the clock / data input ports every netlist here shares:
# d_in is typed clk_a and d_b clk_b, so no register here is a crossing.
CLK_A, CLK_B, D_IN, D_B = 2, 3, 4, 5

_BASE_SDC = """
create_clock -name clk_a -period 10 [get_ports clk_a]
create_clock -name clk_b -period 13 [get_ports clk_b]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b}
set_input_delay 0 -clock clk_a [get_ports d_in]
set_input_delay 0 -clock clk_b [get_ports d_b]
"""


def _dff(name: str, clk: Bit, d: tuple[Bit, ...], q: tuple[Bit, ...]) -> Cell:
    return Cell(
        name,
        "$dff",
        {"CLK": (clk,), "D": d, "Q": q},
        {"WIDTH": str(len(q)), "CLK_POLARITY": "1"},
    )


def _gate(name: str, ctype: str, a: tuple[Bit, ...], b, y: tuple[Bit, ...]) -> Cell:
    conns: dict[str, tuple[Bit, ...]] = {"A": a, "Y": y}
    if b is not None:
        conns["B"] = b
    return Cell(name, ctype, conns)


def _module(cells: list[Cell], outputs: dict[str, tuple[Bit, ...]], **inputs) -> Module:
    ports = {
        "clk_a": Port("clk_a", "input", (CLK_A,)),
        "clk_b": Port("clk_b", "input", (CLK_B,)),
        "d_in": Port("d_in", "input", (D_IN,)),
        "d_b": Port("d_b", "input", (D_B,)),
    }
    for name, bits in inputs.items():
        ports[name] = Port(name, "input", bits)
    for name, bits in outputs.items():
        ports[name] = Port(name, "output", bits)
    return Module("cdc_025_demo", ports, {c.name: c for c in cells}, {})


def _run(module: Module, sdc_text: str = _BASE_SDC):
    spec = sdc.parse(sdc_text)
    sdc.synthesize_unconstrained_inputs(spec, module)
    crossings = domain.find_crossings(
        module,
        port_clock=spec.port_clock,
        pin_clocks=spec.pin_clocks,
        clock_for_port=spec.clock_for_port,
    )
    return rules.run_all(module, domain.filter_async(crossings, spec), spec), spec


def _rule(violations, rule_id: str = "CDC-025"):
    return [v for v in violations if v.rule_id == rule_id]


def _port(v) -> str:
    return v.message.split("`", 2)[1]


def _merge(**extra_outputs) -> Module:
    """``irq = qa | qb`` — a clk_a flop ORed with a clk_b flop."""
    cells = [
        _dff("qa", CLK_A, (D_IN,), (10,)),
        _dff("qb", CLK_B, (D_B,), (11,)),
        _gate("or_irq", "$or", (10,), (11,), (12,)),
    ]
    return _module(cells, {"irq": (12,), **extra_outputs})


# --- the escalated shape: asynchronous merge --------------------------------


def test_async_merge_is_an_error_without_any_contract() -> None:
    violations, _ = _run(_merge())
    (v,) = _rule(violations)
    assert v.severity == "error"
    assert v.crossing is None
    assert v.cell_name == "or_irq"
    assert "top-level output `irq` combinationally merges launches from" in v.message
    assert "clk_a (flop `qa`) and clk_b (flop `qb`)" in v.message
    assert "mutually asynchronous" in v.message
    assert "waive CDC-025 for `irq`" in v.message
    # Not a crossing: the launch flops never reach each other.
    assert [v.rule_id for v in violations] == ["CDC-025"]


def test_async_merge_fires_even_when_single_domain_is_forced_off() -> None:
    module = _merge()
    spec = sdc.parse(_BASE_SDC)
    (v,) = check_cdc_025(module, [], spec, single_domain=False)
    assert v.severity == "error"


def test_merge_is_judged_per_bit() -> None:
    """A 4-bit output whose low two bits XOR a clk_b bus into a clk_a bus
    merges only those two bits; the message counts them and the finding
    anchors on the cell driving the lowest merged bit."""
    cells = [
        _dff("ra", CLK_A, (D_IN,) * 4, (10, 11, 12, 13)),
        _dff("rb", CLK_B, (D_B,) * 2, (20, 21)),
        _gate("x_lo", "$xor", (10, 11), (20, 21), (30, 31)),
    ]
    module = _module(cells, {"q_half": (30, 31, 12, 13)})
    (v,) = _rule(_run(module)[0])
    assert v.severity == "error"
    assert "`q_half` (2 of 4 bits)" in v.message
    assert v.cell_name == "x_lo"


def test_concatenated_domains_are_not_a_merge() -> None:
    """``{clk_a pair, clk_b pair}`` mixes domains across bits but merges
    none: not escalated, and silent while no output is contracted."""
    cells = [
        _dff("ra", CLK_A, (D_IN,) * 2, (10, 11)),
        _dff("rb", CLK_B, (D_B,) * 2, (20, 21)),
    ]
    module = _module(cells, {"mixed": (10, 11, 20, 21)})
    assert _rule(_run(module)[0]) == []

    # Once forced on, it is a single warning with the split-the-port hint.
    (v,) = check_cdc_025(module, [], sdc.parse(_BASE_SDC), single_domain=True)
    assert v.severity == "warning"
    assert "`mixed` (4 of 4 bits)" in v.message
    assert "clk_a (flop `ra`) and clk_b (flop `rb`)" in v.message
    assert "split it per domain" in v.message


def test_mux_select_from_another_domain_merges_every_bit() -> None:
    """A clk_b select steering a clk_a bus touches every output bit —
    the lane-aware replay spreads a select to the whole mux."""
    cells = [
        _dff("ra", CLK_A, (D_IN,) * 3, (10, 11, 12)),
        _dff("sel", CLK_B, (D_B,), (20,)),
        Cell(
            "mux",
            "$mux",
            {"A": ("0", "0", "0"), "B": (10, 11, 12), "S": (20,), "Y": (30, 31, 32)},
            {"WIDTH": "3"},
        ),
    ]
    (v,) = _rule(_run(_module(cells, {"status": (30, 31, 32)}))[0])
    assert v.severity == "error"
    assert "`status` (3 of 3 bits)" in v.message


def test_typed_input_merging_with_a_register_counts_as_a_launcher() -> None:
    cells = [
        _dff("qa", CLK_A, (D_IN,), (10,)),
        _gate("and_o", "$and", (10,), (D_B,), (12,)),
    ]
    (v,) = _rule(_run(_module(cells, {"o": (12,)}))[0])
    assert v.severity == "error"
    assert "clk_a (flop `qa`) and clk_b (input `d_b`)" in v.message


def test_three_domains_and_launcher_sampling() -> None:
    sdc_text = (
        _BASE_SDC
        + "create_clock -name clk_c -period 7 [get_ports clk_c]\n"
        + "set_clock_groups -asynchronous -group {clk_a} -group {clk_c}\n"
        + "set_input_delay 0 -clock clk_c [get_ports d_c]\n"
    )
    cells = [_dff(f"a{i}", CLK_A, (D_IN,), (10 + i,)) for i in range(5)]
    cells += [
        _dff("qb", CLK_B, (D_B,), (20,)),
        _dff("qc", 6, (8,), (21,)),
        Cell("red", "$reduce_xor", {"A": (10, 11, 12, 13, 14, 20, 21), "Y": (30,)}),
    ]
    module = _module(cells, {"o": (30,)}, clk_c=(6,), d_c=(8,))
    (v,) = _rule(_run(module, sdc_text)[0])
    assert v.severity == "error"
    assert "flop `a0`, flop `a1`, flop `a2`, +2 more" in v.message
    assert "clk_b (flop `qb`) and clk_c (flop `qc`)" in v.message


# --- related / unreachable domains are not escalated ------------------------


def test_generated_clock_of_the_launch_clock_is_not_a_merge() -> None:
    sdc_text = (
        _BASE_SDC + "create_generated_clock -name gclk -master_clock clk_a "
        "-source [get_ports clk_a] -divide_by 2 [get_ports clk_g]\n"
    )
    cells = [
        _dff("qa", CLK_A, (D_IN,), (10,)),
        _dff("qg", 6, (D_IN,), (11,)),
        _gate("x", "$xor", (10,), (11,), (12,)),
    ]
    module = _module(cells, {"o": (12,)}, clk_g=(6,))
    assert _rule(_run(module, sdc_text)[0]) == []
    (v,) = check_cdc_025(module, [], sdc.parse(sdc_text), single_domain=True)
    assert v.severity == "warning"
    assert "typically clk_a or gclk" in v.message


def test_exclusive_clocks_are_not_a_merge() -> None:
    sdc_text = (
        _BASE_SDC + "set_clock_groups -logically_exclusive -group {clk_a} "
        "-group {clk_b}\n"
    )
    assert _rule(_run(_merge(), sdc_text)[0]) == []


def test_sdc_without_async_relations_skips_the_rule() -> None:
    sdc_text = (
        "create_clock -name clk_a -period 10 [get_ports clk_a]\n"
        "create_clock -name clk_b -period 13 [get_ports clk_b]\n"
    )
    assert _rule(_run(_merge(), sdc_text)[0]) == []


def test_no_sdc_is_inert() -> None:
    assert check_cdc_025(_merge(), [], None) == []


# --- the self-calibrating single-domain shape -------------------------------


def _registered(**extra_outputs) -> Module:
    cells = [
        _dff("qa", CLK_A, (D_IN,), (10,)),
        _dff("qb", CLK_B, (D_B,), (11,)),
        _dff("qbus", CLK_A, (D_IN,) * 4, (20, 21, 22, 23)),
    ]
    return _module(
        cells, {"q_a": (10,), "q_b": (11,), "q_bus": (20, 21, 22, 23), **extra_outputs}
    )


def test_single_domain_outputs_are_silent_until_some_output_is_typed() -> None:
    violations, _ = _run(_registered())
    assert violations == []


def test_one_typed_output_turns_the_rest_on() -> None:
    sdc_text = _BASE_SDC + "set_output_delay 0 -clock clk_a [get_ports q_a]\n"
    violations, _ = _run(_registered(), sdc_text)
    by_port = {_port(v): v for v in _rule(violations)}
    # The contracted output is CDC-024's business, not CDC-025's.
    assert sorted(by_port) == ["q_b", "q_bus"]
    assert {v.severity for v in by_port.values()} == {"warning"}
    assert "`q_b` is launched from clk_b (flop `qb`)" in by_port["q_b"].message
    assert "Other outputs in this SDC do carry contracts" in by_port["q_b"].message
    assert "typically clk_b" in by_port["q_b"].message
    # A bus is ONE finding, with its bit count.
    assert "`q_bus` (4 of 4 bits) is launched from clk_a" in by_port["q_bus"].message
    assert by_port["q_bus"].cell_name == "qbus"
    assert _rule(violations, "CDC-024") == []


def test_forcing_single_domain_on_without_contracts() -> None:
    spec = sdc.parse(_BASE_SDC)
    found = check_cdc_025(_registered(), [], spec, single_domain=True)
    assert sorted(_port(v) for v in found) == ["q_a", "q_b", "q_bus"]
    # No other output is contracted, so the "likely an omission" lead is
    # dropped and the advice starts the sentence.
    assert all("Other outputs" not in v.message for v in found)
    assert all("cannot check it. Add set_output_delay" in v.message for v in found)


def test_single_domain_forced_off_with_contracts() -> None:
    sdc_text = _BASE_SDC + "set_output_delay 0 -clock clk_a [get_ports q_a]\n"
    spec = sdc.parse(sdc_text)
    assert check_cdc_025(_registered(), [], spec, single_domain=False) == []


def test_all_outputs_typed_is_silent() -> None:
    sdc_text = _BASE_SDC + (
        "set_output_delay 0 -clock clk_a [get_ports {q_a q_bus}]\n"
        "set_output_delay 0 -clock clk_b [get_ports q_b]\n"
    )
    assert _run(_registered(), sdc_text)[0] == []


# --- interplay with CDC-024 -------------------------------------------------


def test_contracted_merge_is_cdc_024_never_cdc_025() -> None:
    """An output WITH a contract never gets CDC-025, even when it merges
    asynchronous domains: CDC-024 reports the launch that is asynchronous
    to the contract."""
    sdc_text = _BASE_SDC + "set_output_delay 0 -clock clk_b [get_ports irq]\n"
    violations, _ = _run(_merge(), sdc_text)
    assert [v.rule_id for v in violations] == ["CDC-024"]
    assert "launched from clk_a (flop `qa`)" in violations[0].message


def test_cdc_024_and_cdc_025_share_one_cone_walk(monkeypatch) -> None:
    """Both output rules ask for the same cones; each output is walked at
    most once per run however many rules consult it."""
    calls: list[tuple[Bit, ...]] = []
    real = rules._output_cone

    def counting(module, start_bits, *args, **kwargs):
        calls.append(tuple(start_bits))
        return real(module, start_bits, *args, **kwargs)

    monkeypatch.setattr(rules, "_output_cone", counting)
    sdc_text = _BASE_SDC + "set_output_delay 0 -clock clk_b [get_ports q_a]\n"
    violations, _ = _run(_registered(), sdc_text)
    assert sorted({v.rule_id for v in violations}) == ["CDC-024", "CDC-025"]
    assert len(calls) == len(set(calls)) == 3


def test_standalone_calls_with_a_shared_context_reuse_the_memo() -> None:
    module = _merge()
    spec = sdc.parse(_BASE_SDC)
    ctx = _build_context(module, spec)
    first = check_cdc_025(module, [], spec, ctx=ctx)
    (idx,) = ctx.output_launch_memo.values()
    assert check_cdc_025(module, [], spec, ctx=ctx) == first
    assert list(ctx.output_launch_memo.values()) == [idx]
    # A different spec object gets its own index rather than a stale one.
    other = sdc.parse(_BASE_SDC)
    check_cdc_025(module, [], other, ctx=ctx)
    assert len(ctx.output_launch_memo) == 2


# --- exclusions -------------------------------------------------------------

_TYPED_SDC = _BASE_SDC + "set_output_delay 0 -clock clk_a [get_ports q_a]\n"


def _only_q_a(*cells: Cell, **outputs) -> list:
    """Run with q_a contracted, so the single-domain gate is open: any
    CDC-025 finding on the extra outputs would show up here."""
    base = [_dff("qa", CLK_A, (D_IN,), (10,)), *cells]
    module = _module(base, {"q_a": (10,), **outputs})
    return _rule(_run(module, _TYPED_SDC)[0])


def test_forwarded_clock_output_is_excluded() -> None:
    assert _only_q_a(clk_fwd=(CLK_A,)) == []


def test_gated_clock_output_is_excluded() -> None:
    """``clk_b & en_q``: the cone reaches a declared clock port."""
    en = _dff("en_q", CLK_B, (D_B,), (30,))
    gate = _gate("icg", "$and", (CLK_B,), (30,), (31,))
    assert _only_q_a(en, gate, clk_gated=(31,)) == []


def test_divider_output_on_the_clock_network_is_excluded() -> None:
    div = _dff("div", CLK_A, (32,), (30,))
    inv = _gate("inv", "$not", (30,), None, (32,))
    slow = _dff("slow", 30, (D_IN,), (33,))
    assert _only_q_a(div, inv, slow, div_o=(30,)) == []


def test_unconsumed_divider_is_still_reported() -> None:
    """Without an internal flop on it, a toggling register is just data —
    declare it a clock (create_generated_clock on the port) to exclude it."""
    div = _dff("div", CLK_A, (32,), (30,))
    inv = _gate("inv", "$not", (30,), None, (32,))
    (v,) = _only_q_a(div, inv, div_o=(30,))
    assert _port(v) == "div_o"

    sdc_text = _TYPED_SDC + (
        "create_generated_clock -name dclk -master_clock clk_a "
        "-source [get_ports clk_a] -divide_by 2 [get_ports div_o]\n"
    )
    module = _module(
        [_dff("qa", CLK_A, (D_IN,), (10,)), div, inv],
        {"q_a": (10,), "div_o": (30,)},
    )
    assert _rule(_run(module, sdc_text)[0]) == []


def test_constant_output_is_excluded() -> None:
    assert _only_q_a(tie_o=("0",), tie_bus=("1", "0", "x")) == []


def test_undriven_output_is_excluded() -> None:
    assert _only_q_a(floating=(40,)) == []


def test_input_feed_through_is_excluded() -> None:
    """A purely combinational path from an input port is launched outside
    this design; its domain belongs to the input's typing (CDC-011)."""
    inv = _gate("inv", "$not", (D_IN,), None, (30,))
    assert _only_q_a(inv, thru_o=(30,)) == []


def test_unresolved_launch_domain_is_excluded() -> None:
    """A flop whose clock traces nowhere is already counted in
    ``summary.domain_unknown``; CDC-025 does not guess its domain."""
    lost = _dff("lost", 50, (D_IN,), (30,))
    assert _only_q_a(lost, lost_o=(30,)) == []


def test_undeclared_clock_port_launcher_is_excluded() -> None:
    """A flop clocked from an input port no ``create_clock`` declares lands
    in the ``<unconstrained>`` sentinel domain — CDC-021's finding."""
    sdc_text = _BASE_SDC + "set_output_delay 0 -clock clk_a [get_ports q_a]\n"
    cells = [
        _dff("qa", CLK_A, (D_IN,), (10,)),
        _dff("qx", 7, (D_IN,), (30,)),
        _gate("x", "$xor", (10,), (30,), (31,)),
    ]
    module = _module(cells, {"q_a": (10,), "o": (31,), "q_x": (30,)}, clk_x=(7,))
    violations, _ = _run(module, sdc_text)
    assert "CDC-021" in {v.rule_id for v in violations}
    # `o` merges clk_a with the sentinel domain: only its clk_a launch is
    # judged, so it is a single-domain warning, never an error; `q_x` is
    # launched only from the sentinel domain and is not reported.
    found = {_port(v): v for v in _rule(violations)}
    assert sorted(found) == ["o"]
    assert found["o"].severity == "warning"
    assert "`<unconstrained>`" not in found["o"].message


def test_constant_loading_register_is_not_a_launcher() -> None:
    """A register whose ``D`` is a constant never launches data: merged
    with a real clk_a register it does not make a clk_b merge (yosys's
    ``opt`` folds such a flop away, slang keeps it — the two frontends
    must agree)."""
    sdc_text = _BASE_SDC
    cells = [
        _dff("qa", CLK_A, (D_IN,), (10,)),
        _dff("dummy", CLK_B, ("0",), (11,)),
        _gate("or_o", "$or", (10,), (11,), (12,)),
    ]
    assert _rule(_run(_module(cells, {"o": (12,)}), sdc_text)[0]) == []


def test_latch_launcher_is_not_a_register() -> None:
    latch = Cell("lat", "$dlatch", {"EN": (CLK_B,), "D": (D_IN,), "Q": (30,)})
    assert _only_q_a(latch, lat_o=(30,)) == []
