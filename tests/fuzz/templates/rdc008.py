"""RDC-008 — raw reset port used unsynchronised where a sync chain exists elsewhere.

``gap_g8`` pins the one shape the rule was written for (a 2FF reset
synchroniser in ``clk_a``, two raw-port consumers in ``clk_b``, SDC
periods swept). RDC-008 is deliberately *asymmetric*: it fires on a
``(port, consumer clock)`` group only when the port has a recognised
reset-sync chain in **some** domain and the group has **≥2** unsynced
consumer flops. This template sweeps both thresholds
(rtl-buddy-cdc#331):

- **chain present in another domain** × **unsynced consumer count**
  (1, 2, 4): fires only with the chain and ≥2 consumers. No chain
  anywhere is the "raw port everywhere" simplification the rule leaves
  alone.
- **domains** — a third clock with its own ≥2 raw consumers gives a
  second finding (one per consumer clock); consumers *in the chain's
  own domain* that bypass the chain fire too.
- **polarity** — an active-high reset port behaves the same.

Silent variants: the textbook fix (a second chain in ``clk_b``, its
consumers behind it) and ``(* reset_sync *)`` on the ``clk_b``
consumers (user-vouched).

**Pinned observation** (``gap_note`` on ``bus2``): the consumer count
is a count of *cells*, so one 2-bit register on the raw port — two
physical flops — reads as the single-flop shortcut and stays silent,
while the same two bits as separate registers fire.
"""

from __future__ import annotations

from collections.abc import Iterator

from .base import ExpectedFinding, Op, RenderedCase


def _sdc(clocks: list[str]) -> str:
    lines = [
        f"create_clock -name {c} -period {p} [get_ports {c}]"
        for c, p in zip(clocks, ("10.0", "7.0", "13.0"), strict=False)
    ]
    if len(clocks) > 1:
        groups = " ".join(f"-group {{{c}}}" for c in clocks)
        lines.append(f"set_clock_groups -asynchronous {groups}")
    lines += [f"set_input_delay -clock {c} 1.0 [get_ports d_{c}]" for c in clocks]
    return "\n".join(lines) + "\n"


class _Reset:
    def __init__(self, active_high: bool) -> None:
        self.port = "raw_rst" if active_high else "raw_rst_n"
        self.edge = f"posedge {self.port}" if active_high else f"negedge {self.port}"
        self.cond = self.port if active_high else f"!{self.port}"
        self.asserted = 1 if active_high else 0


def _chain(clk: str, r: _Reset) -> str:
    """2FF reset synchroniser in ``clk``; output ``rst_<clk>``."""
    return f"""\
    logic rst_{clk}_m, rst_{clk};
    always_ff @(posedge {clk} or {r.edge})
        if ({r.cond}) rst_{clk}_m <= 1'b{r.asserted};
        else rst_{clk}_m <= 1'b{1 - r.asserted};
    always_ff @(posedge {clk} or {r.edge})
        if ({r.cond}) rst_{clk} <= 1'b{r.asserted};
        else rst_{clk} <= rst_{clk}_m;
"""


def _consumers(
    clk: str,
    n: int,
    r: _Reset,
    *,
    synced: bool = False,
    attr: str = "",
    bus: bool = False,
    tag: str | None = None,
) -> str:
    """``n`` 1-bit shift-register flops in ``clk``, reset by the raw port
    (or, ``synced``, by that domain's chain output). Registers and the
    output ``o_<tag>`` are named after ``tag`` (default: the clock)."""
    if synced:
        sig = f"rst_{clk}"
        edge = f"posedge {sig}" if r.asserted else f"negedge {sig}"
        cond = sig if r.asserted else f"!{sig}"
    else:
        edge, cond = r.edge, r.cond
    t = tag or clk
    if bus:
        return f"""\
    {attr}logic [1:0] {t}_q;
    always_ff @(posedge {clk} or {edge})
        if ({cond}) {t}_q <= '0;
        else {t}_q <= {{{t}_q[0], d_{clk}}};
    assign o_{t} = {t}_q[1];
"""
    decls = "\n".join(f"    {attr}logic {t}_q{i};" for i in range(n))
    resets = " ".join(f"{t}_q{i} <= 1'b0;" for i in range(n))
    loads = " ".join(
        f"{t}_q{i} <= {f'd_{clk}' if i == 0 else f'{t}_q{i - 1}'};" for i in range(n)
    )
    return f"""\
{decls}
    always_ff @(posedge {clk} or {edge})
        if ({cond}) begin {resets} end
        else begin {loads} end
    assign o_{t} = {t}_q{n - 1};
"""


def _render(
    top: str, clocks: list[str], body: str, r: _Reset, extra_outs: tuple[str, ...] = ()
) -> str:
    ports = "".join(
        f"    input  logic {c},\n    input  logic d_{c},\n    output logic o_{c},\n"
        for c in clocks
    ) + "".join(f"    output logic o_{t},\n" for t in extra_outs)
    return f"""\
module {top} (
{ports}    input  logic {r.port}
);
{body}endmodule
"""


_QUIET = (ExpectedFinding("RDC-001", Op.ZERO), ExpectedFinding("RDC-007", Op.ZERO))
_SILENT = (ExpectedFinding("RDC-008", Op.ZERO),)


def _a_side(r: _Reset, chain: bool) -> str:
    """``clk_a``: a sync chain feeding one consumer, or (no chain) one
    raw-port consumer (a single raw consumer never fires on its own)."""
    if chain:
        return _chain("clk_a", r) + _consumers("clk_a", 1, r, synced=True)
    return _consumers("clk_a", 1, r)


class PrimaryResetAsymmetricSync:
    """Raw reset port: chain in one domain, raw consumers in another."""

    name = "rdc008_primary_reset_asymmetric"

    @classmethod
    def cases(cls) -> Iterator[RenderedCase]:
        n = cls.name
        low = _Reset(active_high=False)
        two = ["clk_a", "clk_b"]

        def case(
            tag: str,
            clocks: list[str],
            body: str,
            r: _Reset,
            findings: int,
            params: dict,
            gap_note: str | None = None,
            extra_outs: tuple[str, ...] = (),
        ) -> RenderedCase:
            top = f"fuzz_{n}_{tag}"
            return RenderedCase(
                template_name=n,
                case_id=top,
                sv=_render(top, clocks, body, r, extra_outs),
                sdc=_sdc(clocks),
                top=top,
                params=params,
                expected=(
                    (ExpectedFinding("RDC-008", Op.EQ, findings),) if findings else ()
                ),
                forbidden=_QUIET + (() if findings else _SILENT),
                gap_note=gap_note,
            )

        # Canonical (firing) first: chain in clk_a, 2 raw consumers in clk_b.
        sweep = [(True, 2)] + [
            (chain, cons)
            for chain in (True, False)
            for cons in (1, 2, 4)
            if (chain, cons) != (True, 2)
        ]
        for chain, cons in sweep:
            body = _a_side(low, chain) + _consumers("clk_b", cons, low)
            yield case(
                f"{'chain' if chain else 'nochain'}_b{cons}",
                two,
                body,
                low,
                int(chain and cons >= 2),
                {"chain_elsewhere": chain, "unsynced_consumers": cons},
            )

        high = _Reset(active_high=True)
        yield case(
            "chain_b2_activehigh",
            two,
            _a_side(high, True) + _consumers("clk_b", 2, high),
            high,
            1,
            {"chain_elsewhere": True, "unsynced_consumers": 2, "polarity": "high"},
        )

        three = ["clk_a", "clk_b", "clk_c"]
        yield case(
            "chain_b2_c2",
            three,
            _a_side(low, True)
            + _consumers("clk_b", 2, low)
            + _consumers("clk_c", 2, low),
            low,
            2,
            {"chain_elsewhere": True, "unsynced_consumers": 2, "domains": 3},
        )

        # The chain's own domain: two consumers that bypass it, next to
        # one that uses it.
        yield case(
            "chain_bypass_a2",
            ["clk_a"],
            _a_side(low, True) + _consumers("clk_a", 2, low, tag="raw"),
            low,
            1,
            {"chain_same_domain": True, "unsynced_consumers": 2, "bypass": True},
            extra_outs=("raw",),
        )

        yield case(
            "fixed_b2",
            two,
            _a_side(low, True)
            + _chain("clk_b", low)
            + _consumers("clk_b", 2, low, synced=True),
            low,
            0,
            {"chain_elsewhere": True, "unsynced_consumers": 0, "fix": "chain_in_b"},
        )

        yield case(
            "chain_b2_reset_sync_attr",
            two,
            _a_side(low, True) + _consumers("clk_b", 2, low, attr="(* reset_sync *) "),
            low,
            0,
            {
                "chain_elsewhere": True,
                "unsynced_consumers": 2,
                "suppression": "reset_sync",
            },
        )

        yield case(
            "chain_bus2",
            two,
            _a_side(low, True) + _consumers("clk_b", 1, low, bus=True),
            low,
            0,
            {"chain_elsewhere": True, "unsynced_consumers": "1 cell x 2 bits"},
            gap_note=(
                "rtl-buddy-cdc#331: RDC-008's >=2-consumer gate counts flop "
                "cells, so a 2-bit register on the raw port (two physical "
                "flops) is treated as the single-flop shortcut. Calibration "
                "question rather than a clear bug; pinned so a change is seen."
            ),
        )
