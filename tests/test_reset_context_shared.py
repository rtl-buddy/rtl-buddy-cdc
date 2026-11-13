"""The rule pack's reset-side maps are built once per run (issue #324).

RDC-002 / -004 / -005 / -006 / -008 each used to call
``assign_reset_domains`` and ``find_reset_synchronizers`` themselves, and
RDC-007 called ``iter_reset_sync_chains``, each rebuilding the flop list
and the bit-driver map underneath. One ``analyze`` run on a 44k-cell design
classified every flop's reset 11 times. ``_RuleContext`` now holds those
views and every RDC rule reads them from there.

The netlist here is generated in-process (a few thousand cells: enough
blocks for every builder to matter, small enough to stay fast). The tests
count builder calls instead of timing them, so they are exact and cannot
flake; the counts must not grow with the design. Report identity is
pinned by the fixture suites and by the shared-vs-standalone comparison
below.
"""

from __future__ import annotations

import dataclasses
import json
from collections import Counter
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import cli, netlist, reset_domain, rules
from rtl_buddy_cdc.domain import filter_async, find_crossings
from rtl_buddy_cdc.netlist import Module, Port
from rtl_buddy_cdc.rules import RULES, _build_context, run_all
from rtl_buddy_cdc.sdc import parse_file

runner = CliRunner()

_DOMAINS = 4
_WIDTH = 4


def _write_design(tmp: Path, blocks: int) -> tuple[Path, Path]:
    """Write a flat Yosys-JSON netlist + SDC with ``blocks`` crossing blocks.

    Each clock domain has a textbook 2-flop reset synchroniser on its
    reset port. Each block is a registered datapath in one domain, a
    1-bit request synchronised into the next domain, and a gated bus
    capture there. Every few blocks add a reset shape the RDC rules
    fire on: a foreign-domain flop driving an async reset (RDC-001), a
    comb-driven reset (RDC-004), an AND of two reset ports (RDC-005) and
    raw-port reset consumers next to a synchronised domain (RDC-008).
    About 11 cells per block.
    """
    next_bit = [2]

    def bits(n: int = 1) -> list[int]:
        out = list(range(next_bit[0], next_bit[0] + n))
        next_bit[0] += n
        return out

    ports: dict[str, dict] = {}
    cells: dict[str, dict] = {}

    def port(name: str, direction: str, n: int = 1) -> list[int]:
        b = bits(n)
        ports[name] = {"direction": direction, "bits": b}
        return b

    def cell(name: str, typ: str, conn: dict, params: dict | None = None) -> None:
        cells[name] = {
            "type": typ,
            "parameters": params or {},
            "attributes": {"src": f"gen.sv:{len(cells) + 1}.1-{len(cells) + 1}.9"},
            "port_directions": {
                k: "output" if k in ("Q", "Y") else "input" for k in conn
            },
            "connections": conn,
        }

    observed: list[int] = []

    def adff(name: str, clk: int, d: list, q: list, rst: int) -> None:
        observed.extend(q)
        cell(
            name,
            "$adff",
            {"ARST": [rst], "CLK": [clk], "D": d, "Q": q},
            {"ARST_POLARITY": "0", "CLK_POLARITY": "1", "WIDTH": str(len(q))},
        )

    def gate(name: str, typ: str, a: list, b: list, y: list) -> None:
        cell(name, typ, {"A": a, "B": b, "Y": y})

    clk = [port(f"clk{i}", "input")[0] for i in range(_DOMAINS)]
    rst = [port(f"rst{i}_n", "input")[0] for i in range(_DOMAINS)]
    din = port("din", "input", _WIDTH)

    srst = []
    for i in range(_DOMAINS):
        a, b = bits(), bits()
        adff(f"rsync{i}_a", clk[i], ["1"], a, rst[i])
        adff(f"rsync{i}_b", clk[i], a, b, rst[i])
        srst.append(b[0])

    prev = {i: din for i in range(_DOMAINS)}
    for blk in range(blocks):
        i, j = blk % _DOMAINS, (blk + 1) % _DOMAINS
        p = f"b{blk}"
        x, r0 = bits(_WIDTH), bits(_WIDTH)
        gate(f"{p}.x", "$xor", prev[i], r0, x)
        adff(f"{p}.r0", clk[i], x, r0, srst[i])
        prev[i] = r0
        ro, req, s1, s2 = bits(), bits(), bits(), bits()
        cell(f"{p}.ro", "$reduce_or", {"A": r0, "Y": ro})
        adff(f"{p}.req", clk[i], ro, req, srst[i])
        adff(f"{p}.s1", clk[j], req, s1, srst[j])
        adff(f"{p}.s2", clk[j], s1, s2, srst[j])
        my, dq = bits(_WIDTH), bits(_WIDTH)
        cell(f"{p}.mx", "$mux", {"A": dq, "B": r0, "S": s2, "Y": my})
        adff(f"{p}.dst", clk[j], my, dq, srst[j])
        if blk % 10 == 3:  # RDC-001: foreign-domain flop drives ARST
            adff(f"{p}.rdc1", clk[j], s2, bits(), req[0])
        if blk % 10 == 5:  # RDC-004: comb of a flop drives ARST
            y = bits()
            gate(f"{p}.rand", "$and", [srst[i]], req, y)
            adff(f"{p}.rdc4", clk[i], s2, bits(), y[0])
        if blk % 10 == 7:  # RDC-005: two reset ports ANDed, no mux
            y = bits()
            gate(f"{p}.pand", "$and", [rst[i]], [rst[j]], y)
            adff(f"{p}.rdc5", clk[i], s2, bits(), y[0])
        if blk % 10 == 9:  # RDC-008: raw-port reset consumers
            adff(f"{p}.raw_a", clk[j], s2, bits(), rst[j])
            adff(f"{p}.raw_b", clk[j], s2, bits(), rst[j])

    ports["dout"] = {"direction": "output", "bits": prev[0]}
    # Every register is observable, so the liveness pass
    # (rtl-buddy-cdc#338) keeps the whole design.
    ports["obs"] = {"direction": "output", "bits": observed}
    design = {
        "creator": "test_reset_context_shared",
        "modules": {
            "gen": {
                "attributes": {"top": "1"},
                "ports": ports,
                "cells": cells,
                "netnames": {},
            }
        },
    }
    net = tmp / f"gen{blocks}.json"
    sdc = tmp / f"gen{blocks}.sdc"
    net.write_text(json.dumps(design))
    clocks = [
        f"create_clock -name clk{i} -period {4 + i} [get_ports clk{i}]"
        for i in range(_DOMAINS)
    ]
    groups = " ".join(f"-group {{clk{i}}}" for i in range(_DOMAINS))
    sdc.write_text(
        "\n".join(
            [
                *clocks,
                f"set_clock_groups -asynchronous {groups}",
                "set_input_delay -clock clk0 0.5 [get_ports din[*]]",
            ]
        )
        + "\n"
    )
    return net, sdc


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Counter[str]:
    """Count the per-module builders wherever the analyzer reaches them.

    ``rules._clock_network_cells`` (CDC-008 and CDC-010 each built it)
    is counted alongside the reset-side builders. ``rules`` imports those from :mod:`rtl_buddy_cdc.reset_domain` at
    call time and the reset helpers call each other through that
    module's globals, so patching the module counts both; ``cli`` binds
    its own names at import and is patched separately."""
    counts: Counter[str] = Counter()

    def wrap(mod: object, name: str) -> None:
        real = getattr(mod, name)

        def counting(*args, **kwargs):
            counts[name] += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(mod, name, counting)

    for name in (
        "assign_reset_domains",
        "find_reset_synchronizers",
        "iter_reset_sync_chains",
        "find_reset_crossings",
        "_bit_drivers",
    ):
        wrap(reset_domain, name)
    for name in (
        "assign_reset_domains",
        "find_reset_synchronizers",
        "find_reset_crossings",
    ):
        wrap(cli, name)
    wrap(rules, "_clock_network_cells")
    return counts


def _run_all(net: Path, sdc: Path) -> list:
    module = netlist.load(net)
    spec = parse_file(sdc)
    crossings = filter_async(
        find_crossings(module, clock_for_port=spec.clock_for_port), spec
    )
    return run_all(module, crossings, spec)


@pytest.mark.parametrize("blocks", [100, 300])
def test_run_all_builds_reset_views_once(
    tmp_path: Path, calls: Counter[str], blocks: int
) -> None:
    net, sdc = _write_design(tmp_path, blocks)
    calls.clear()
    violations = _run_all(net, sdc)

    fired = {v.rule_id for v in violations}
    # The RDC rules really ran on reset structure, not on an empty map.
    assert {"RDC-001", "RDC-004", "RDC-005", "RDC-008"} <= fired, fired
    assert calls == Counter(
        assign_reset_domains=1,
        find_reset_synchronizers=1,
        iter_reset_sync_chains=1,
        _clock_network_cells=1,
    ), calls


def test_analyze_builds_reset_views_once(tmp_path: Path, calls: Counter[str]) -> None:
    net, sdc = _write_design(tmp_path, 300)
    calls.clear()
    res = runner.invoke(
        cli.app, ["analyze", "-n", str(net), "-s", str(sdc), "-f", "json"]
    )
    assert res.exit_code == 1, res.output
    assert calls == Counter(
        assign_reset_domains=1,
        find_reset_synchronizers=1,
        iter_reset_sync_chains=1,
        _clock_network_cells=1,
    ), calls

    # --emit-reset-domain-map classifies once more (it uses the CLI's own
    # flop clocks, not the rule context's) and shares that one map with
    # the synchroniser and crossing passes.
    calls.clear()
    rmap = tmp_path / "rmap.json"
    res = runner.invoke(
        cli.app,
        [
            "analyze",
            "-n",
            str(net),
            "-s",
            str(sdc),
            "-f",
            "json",
            "--emit-reset-domain-map",
            str(rmap),
        ],
    )
    assert res.exit_code == 1, res.output
    assert calls["assign_reset_domains"] == 2, calls
    assert calls["find_reset_synchronizers"] == 2, calls
    assert calls["find_reset_crossings"] == 1, calls


def test_shared_reset_views_match_standalone(tmp_path: Path) -> None:
    """Each RDC rule reports the same findings from the shared context as
    when called standalone, where it builds its own maps from scratch."""
    net, sdc = _write_design(tmp_path, 100)
    module = netlist.load(net)
    spec = parse_file(sdc)
    crossings = filter_async(
        find_crossings(module, clock_for_port=spec.clock_for_port), spec
    )
    ctx = _build_context(module, spec)
    for rule_id, rule in RULES.items():
        if not rule_id.startswith("RDC-"):
            continue
        assert rule(module, crossings, spec, ctx=ctx) == rule(
            module, crossings, spec
        ), rule_id
    assert ctx.reset_bit_drivers == reset_domain._bit_drivers(module)
    assert ctx.reset_domains == reset_domain.assign_reset_domains(module)
    assert ctx.reset_synchronizers == reset_domain.find_reset_synchronizers(
        module, ctx.domains, extra_synchronizers=ctx.reset_sync_flop_names
    )
    assert list(ctx.reset_sync_chains) == reset_domain.iter_reset_sync_chains(
        module, ctx.domains
    )


def _module(ports: dict[str, tuple]) -> Module:
    return Module(
        name="m",
        ports={n: Port(name=n, direction="input", bits=b) for n, b in ports.items()},
        cells={},
        netnames={},
    )


def test_port_of_bit_index_keeps_first_port_and_dataclass_semantics() -> None:
    m = _module({"a": (2, 3), "b": (3, 4), "c": ("1", 5)})
    twin = _module({"a": (2, 3), "b": (3, 4), "c": ("1", 5)})
    before = repr(m)

    # A bit carried by two ports resolves to the first in ``ports`` order,
    # as the old linear scan did.
    assert m.port_of_bit(3) is m.ports["a"]
    assert m.port_of_bit(4) is m.ports["b"]
    assert m.port_of_bit(5) is m.ports["c"]
    assert m.port_of_bit(99) is None
    assert m.port_of_bit("1") is None  # constants are never port bits

    # The lazily built index is not a field: equality and repr see only
    # the declared fields, and replace() starts with a fresh index.
    assert "_port_by_bit" not in {f.name for f in dataclasses.fields(Module)}
    assert repr(m) == before
    assert m == twin
    other = dataclasses.replace(m, ports={"z": Port("z", "input", (3,))})
    assert other.port_of_bit(3) is other.ports["z"]
    assert other.port_of_bit(2) is None
