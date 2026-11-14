"""The structural gray-encoder match is operand-order free
(rtl-buddy-cdc#365).

``_is_gray_encoded_source`` used to accept a gray encoder only as
``b ^ (b >> 1)``: an ``$xor`` whose **B** input is the right-shifted
**A**. ``(b >> 1) ^ b`` is the same function with the operands swapped,
and an async FIFO written that way reported CDC-004 on every gray
pointer. Synthetic cells pin both orders and the shapes that must stay
rejected; ``good_gray_xor_swapped`` pins the end-to-end verdict.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.flops import Flop
from rtl_buddy_cdc.netlist import Bit, Cell, Module
from rtl_buddy_cdc.rules import (
    _bit_drivers,
    _is_gray_encoded_source,
    _is_gray_xor_pair,
    run_all,
)

FIX = Path(__file__).parent / "fixtures"

# Binary value b = bits 10..13; gray register D = 20..23, Q = 30..33.
B: tuple[Bit, ...] = (10, 11, 12, 13)
SHR1: tuple[Bit, ...] = (11, 12, 13, "0")  # b >> 1
SHR2: tuple[Bit, ...] = (12, 13, "0", "0")  # b >> 2
SHL1: tuple[Bit, ...] = ("0", 10, 11, 12)  # b << 1
ROR1: tuple[Bit, ...] = (11, 12, 13, 10)  # rotate, no constant fill


def _gray_source(a: tuple[Bit, ...], b: tuple[Bit, ...]) -> tuple[Module, Flop]:
    d = (20, 21, 22, 23)
    q = (30, 31, 32, 33)
    xor = Cell(name="enc", type="$xor", connections={"A": a, "B": b, "Y": d})
    reg = Cell(name="gray", type="$dff", connections={"CLK": (1,), "D": d, "Q": q})
    module = Module(name="m", ports={}, cells={"enc": xor, "gray": reg}, netnames={})
    return module, Flop(cell=reg, clk=1, d=d, q=q)


@pytest.mark.parametrize(
    ("a", "b"),
    [(B, SHR1), (SHR1, B)],
    ids=["b_xor_shifted", "shifted_xor_b"],
)
def test_gray_encoder_matches_in_either_operand_order(
    a: tuple[Bit, ...], b: tuple[Bit, ...]
) -> None:
    module, src = _gray_source(a, b)
    assert _is_gray_encoded_source(module, src, _bit_drivers(module))


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (B, SHR2),
        (SHR2, B),
        (B, SHL1),
        (SHL1, B),
        (B, ROR1),
        (ROR1, B),
        (B, B),
    ],
    ids=[
        "shr2",
        "shr2_swapped",
        "shl1",
        "shl1_swapped",
        "rotate",
        "rotate_swapped",
        "self",
    ],
)
def test_non_gray_xor_is_rejected_in_either_order(
    a: tuple[Bit, ...], b: tuple[Bit, ...]
) -> None:
    module, src = _gray_source(a, b)
    assert not _is_gray_encoded_source(module, src, _bit_drivers(module))


def test_pair_predicate_is_directional() -> None:
    """The helper itself tests one direction; the caller tries both."""
    assert _is_gray_xor_pair(B, SHR1)
    assert not _is_gray_xor_pair(SHR1, B)
    assert not _is_gray_xor_pair(B[:1], SHR1[:1])  # 1-bit: not a code


def _verdict(name: str, json_name: str | None = None) -> list[str]:
    d = FIX / name
    module = netlist.load(d / (json_name or f"{name}.json"))
    spec = sdc_mod.parse_file(d / f"{name}.sdc")
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = [
        c
        for c in find_crossings(module, port_clock=spec.port_clock)
        if spec.are_async(c.src_clock, c.dst_clock)
    ]
    assert crossings
    return [v.rule_id for v in run_all(module, crossings, spec)]


def test_swapped_fixture_is_clean_like_the_canonical_one() -> None:
    assert _verdict("good_gray_xor_swapped") == []
    assert _verdict("good_gray_bus_sync_chain") == []


def test_binary_bus_into_the_same_sync_still_fires() -> None:
    """Control: the multi-bit sync alone is not enough — a plain binary
    source into the same chain is still CDC-004."""
    d = FIX / "good_gray_xor_swapped"
    module = netlist.load(d / "good_gray_xor_swapped.json")
    enc = next(c for c in module.cells.values() if c.type == "$xor")
    # Replace the encoder by a plain buffer of its unshifted operand
    # (the one without a constant fill): the gray register now carries
    # the binary value.
    plain = next(
        bits
        for bits in (enc.connections["A"], enc.connections["B"])
        if isinstance(bits[-1], int)
    )
    rewired = Cell(
        name=enc.name, type="$pos", connections={"A": plain, "Y": enc.connections["Y"]}
    )
    cells = dict(module.cells)
    cells[enc.name] = rewired
    module = Module(
        name=module.name, ports=module.ports, cells=cells, netnames=module.netnames
    )
    spec = sdc_mod.parse_file(d / "good_gray_xor_swapped.sdc")
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = [
        c
        for c in find_crossings(module, port_clock=spec.port_clock)
        if spec.are_async(c.src_clock, c.dst_clock)
    ]
    assert [v.rule_id for v in run_all(module, crossings, spec)] == ["CDC-004"]
