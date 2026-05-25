"""Reset-domain analysis — parallel to :mod:`rtl_buddy_cdc.domain`.

Foundation for the broader RDC (Reset Domain Crossing) work tracked in
issue #107. This module exposes the structural facts the rule pack
needs to reason about resets without each rule re-walking the netlist:

* :class:`ResetSource` — describes the upstream origin of a flop's
  reset pin (top-level port vs. driven by another flop, polarity,
  sync vs. async).
* :class:`ResetDomain` — the per-flop assignment (flop name → reset
  source, or ``None`` when the flop has no reset pin at all).
* :func:`assign_reset_domains` — runs the per-flop walk once and
  returns the full map.

The classifier deliberately stays close to what the Yosys netlist
hands us. Polarity comes straight from the ``$adff*`` / ``$sdff*``
parameter (Yosys preserves it as a multi-bit binary string). Sync vs.
async is derived from the cell type: ``$adff*`` / ``$aldff*`` /
``$dffsr*`` are async, ``$sdff*`` are sync. The reset source is
classified by walking the reset bit back through the standard
backward-fanin: a top-level input port → ``"port"``; a flop's ``Q``
output → ``"inferred"`` (with the driving flop's cell name as the
identifier); a constant → ``"constant"``; anything else (combinational
expression, untracked driver) → ``"comb"``.

Out of scope here, by design:

* The :class:`ResetSource` ``clock`` field (the clock that samples a
  sync reset) is not populated yet — it requires the rule-side context
  to land cleanly. Stubbed for forward-compat; consumers should treat
  ``None`` as "not yet determined".
* Reset-synchronizer recognition lives in a follow-up PR alongside the
  first rule that needs it (RDC-001's recognizer wants the flop +
  clock-domain pair, not just the structural reset shape).
* No rules consume this module yet. The existing CDC-007 walk in
  :mod:`rtl_buddy_cdc.rules` is unchanged; rerooting it onto the new
  data model is a follow-up so the contract change (rule_id, message
  text) can be reviewed independently.
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import Literal

from rtl_buddy_cdc.flops import Flop, find_flops
from rtl_buddy_cdc.netlist import Bit, Module

# Cell types with an asynchronous reset pin. Each maps to the pin name
# carrying the reset and the parameter that documents its polarity.
# ``$dffsr`` / ``$dffsre`` are SR-latch flops with separate ``SET`` and
# ``CLR`` async pins; we surface ``CLR`` as the canonical reset to
# match the dominant industry idiom (active-low clear). ``SET`` semantics
# are different enough that we punt on them in this foundation pass —
# the rule pack can layer SET-specific handling on top later.
_ASYNC_RESET_PINS: dict[str, tuple[str, str]] = {
    "$adff": ("ARST", "ARST_POLARITY"),
    "$adffe": ("ARST", "ARST_POLARITY"),
    "$aldff": ("ALOAD", "ALOAD_POLARITY"),
    "$aldffe": ("ALOAD", "ALOAD_POLARITY"),
    "$dffsr": ("CLR", "CLR_POLARITY"),
    "$dffsre": ("CLR", "CLR_POLARITY"),
}

# Cell types with a synchronous reset pin.
_SYNC_RESET_PINS: dict[str, tuple[str, str]] = {
    "$sdff": ("SRST", "SRST_POLARITY"),
    "$sdffe": ("SRST", "SRST_POLARITY"),
    "$sdffce": ("SRST", "SRST_POLARITY"),
}


ResetType = Literal["sync", "async"]
ResetPolarity = Literal["high", "low"]
ResetSourceKind = Literal["port", "inferred", "constant", "comb"]


@dataclass(frozen=True)
class ResetSource:
    """The upstream origin of a flop's reset pin.

    ``name`` identifies the source: a top-level port name for
    ``source="port"``, the driver flop's cell name for
    ``source="inferred"``, the literal constant for
    ``source="constant"``, or an empty string for ``source="comb"``
    (the analyzer doesn't currently summarise combinational reset
    expressions).
    """

    name: str
    polarity: ResetPolarity
    type: ResetType
    source: ResetSourceKind
    # The clock that samples a sync reset, or ``None`` for async
    # resets and for sync resets whose context isn't available yet.
    # Stubbed; populated by the rule-side context in a later PR.
    clock: str | None = None


@dataclass(frozen=True)
class ResetDomain:
    """A flop's reset assignment.

    ``reset`` is ``None`` for plain ``$dff`` / ``$dffe`` cells (no
    reset pin); consumers should treat that as "power-on initialisation
    only".
    """

    flop: str
    reset: ResetSource | None


def assign_reset_domains(module: Module) -> dict[str, ResetDomain]:
    """For each flop in ``module``, classify its reset pin (if any).

    Returns ``{cell_name: ResetDomain}`` covering every flop. Plain
    ``$dff`` / ``$dffe`` cells get a ``ResetDomain`` whose ``reset`` is
    ``None``; reset-bearing cells get a populated :class:`ResetSource`
    derived from the cell type, its polarity parameter, and a one-hop
    walk back through the netlist's bit-drivers table.

    ``polarity`` on the returned source is the *flop pin*'s polarity as
    inferred by Yosys. A user-level override (e.g. ``(* reset_polarity
    = "low" *)`` on a top-level port) is **not** applied here — the
    consumer rule pack reconciles port-declared vs. flop-inferred
    polarity itself; see :func:`rtl_buddy_cdc.rules.user_reset_polarity_overrides`.
    """
    drivers = _bit_drivers(module)
    out: dict[str, ResetDomain] = {}
    for f in find_flops(module):
        ctype = f.cell.type
        if ctype in _ASYNC_RESET_PINS:
            pin, polarity_param = _ASYNC_RESET_PINS[ctype]
            reset_type: ResetType = "async"
        elif ctype in _SYNC_RESET_PINS:
            pin, polarity_param = _SYNC_RESET_PINS[ctype]
            reset_type = "sync"
        else:
            out[f.cell.name] = ResetDomain(flop=f.cell.name, reset=None)
            continue

        bits = f.cell.connections.get(pin, ())
        if not bits:
            # Defensive: a flop typed as reset-bearing but missing the
            # pin connection is malformed. Treat it as "no reset" rather
            # than raising — better to skip than crash on an exotic netlist.
            out[f.cell.name] = ResetDomain(flop=f.cell.name, reset=None)
            continue

        polarity = _polarity_from_param(f.cell.parameters.get(polarity_param, "1"))
        rst_bit = bits[0]
        name, kind = _classify_reset_source(module, rst_bit, drivers)
        out[f.cell.name] = ResetDomain(
            flop=f.cell.name,
            reset=ResetSource(
                name=name,
                polarity=polarity,
                type=reset_type,
                source=kind,
            ),
        )
    return out


# --- helpers ----------------------------------------------------------------


def _bit_drivers(module: Module) -> dict[Bit, tuple[str, str]]:
    """Mirror of :func:`rtl_buddy_cdc.domain._bit_drivers`.

    Built locally so the reset pass doesn't depend on importing from
    :mod:`rtl_buddy_cdc.domain` (which would create a layering tangle
    once the rule pack also wires this in). The map is small and the
    walk is O(cells); the duplication is cheap.
    """
    out: dict[Bit, tuple[str, str]] = {}
    for cell in module.cells.values():
        for port_name in ("Y", "Q"):
            for b in cell.connections.get(port_name, ()):
                if isinstance(b, int):
                    out[b] = (cell.name, port_name)
    return out


def _classify_reset_source(
    module: Module,
    bit: Bit,
    drivers: dict[Bit, tuple[str, str]],
) -> tuple[str, ResetSourceKind]:
    """One-hop classification of the bit driving a reset pin."""
    if not isinstance(bit, int):
        return (str(bit), "constant")
    port = module.port_of_bit(bit)
    if port is not None and port.direction == "input":
        return (port.name, "port")
    drv = drivers.get(bit)
    if drv is None:
        # Untracked driver (e.g. inout or a cell we don't recognise);
        # the safest classification is "comb" — the rule pack can refuse
        # to reason about such resets rather than misclassify them.
        return ("", "comb")
    cell_name, out_port = drv
    if out_port == "Q":
        return (cell_name, "inferred")
    return ("", "comb")


@dataclass(frozen=True)
class ResetSyncChain:
    """A recognised reset-synchroniser chain.

    Returned by :func:`iter_reset_sync_chains`. ``flops`` is the
    ordered list of cell names from tail (downstream-consumer-facing)
    to head (constant-fed). ``head_d_constant`` is the literal D-pin
    constant on the head flop — Yosys-encoded (``"0"`` / ``"1"`` /
    ``"x"`` / ``"z"`` or a multi-bit binary string). ``polarity`` is
    the chain's shared async-reset polarity.

    Consumers compare ``head_d_constant`` against the *deassertion*
    value implied by ``polarity`` (active-low → head D should be 1,
    active-high → head D should be 0). A mismatch is RDC-007's failure
    shape: the chain reloads the *asserted* value on the deassertion
    edge and never propagates "out of reset" to downstream consumers.
    """

    flops: tuple[str, ...]
    head_d_constant: str
    polarity: ResetPolarity


def find_reset_synchronizers(
    module: Module,
    clock_domains: dict[str, str | None],
    *,
    min_depth: int = 2,
    extra_synchronizers: AbstractSet[str] | None = None,
) -> set[str]:
    """Identify flop cells that participate in a reset-synchronizer chain.

    The canonical reset-synchronizer is the textbook async-assert /
    sync-deassert pattern: N≥2 flops in the destination clock domain
    sharing the same async reset, chained Q→D, with the chain's head
    flop's ``D`` tied to a constant (typically ``1'b1`` for an active-
    low reset, ``1'b0`` for an active-high reset). The synchronizer's
    last flop's ``Q`` is the synchronised reset that downstream
    consumers connect to their own ``ARST``.

    Returns the set of cell names of *every* flop participating in
    such a chain (head, tail, and anything in between) — RDC-002
    onward will skip these flops to avoid false-positive findings on
    legitimate synchronizers.

    A flop is recognised iff:

    * it is async-reset (has an ``ARST`` / ``ALOAD`` / ``CLR`` pin
      per :data:`_ASYNC_RESET_PINS`),
    * the chain walking backward through its ``D`` pin (following
      same-clock, same-reset-source flops only) reaches a flop whose
      ``D`` is a constant within ``min_depth - 1`` hops, and
    * the chain length (from head constant to tail) is at least
      ``min_depth`` flops.

    ``clock_domains`` is the per-flop clock-domain map that the rule
    pack already builds in ``_RuleContext.domains`` — passed in to
    avoid re-running :func:`rtl_buddy_cdc.domain.assign_domains`. The
    recogniser tolerates ``None`` entries (untraceable clock); those
    flops never match because their domain cannot be compared.

    ``extra_synchronizers`` (optional) is the set of flop cell names
    the caller wants treated as sync stages regardless of structural
    shape — typically the result of
    :func:`rtl_buddy_cdc.rules.user_reset_sync_flop_names` (flops the
    user has marked with ``(* reset_sync *)``). Useful when the chain
    head's D is fed by an upstream signal rather than a literal
    constant — the structural recogniser is deliberately conservative
    and would otherwise miss them.
    """
    if min_depth < 1:
        raise ValueError("min_depth must be >= 1")
    domains = assign_reset_domains(module)
    flop_by_name = {f.cell.name: f for f in find_flops(module)}
    bit_drivers = _bit_drivers(module)
    out: set[str] = set(extra_synchronizers or ())

    for name, rd in domains.items():
        if rd.reset is None or rd.reset.type != "async":
            continue
        chain, _head_d = _trace_reset_sync_chain(
            name,
            flop_by_name,
            domains,
            clock_domains,
            bit_drivers,
            max_depth=min_depth + 4,
        )
        if len(chain) >= min_depth:
            out.update(chain)
    return out


def iter_reset_sync_chains(
    module: Module,
    clock_domains: dict[str, str | None],
    *,
    min_depth: int = 2,
) -> list[ResetSyncChain]:
    """Enumerate structurally-recognised reset-synchroniser chains.

    Companion to :func:`find_reset_synchronizers`: instead of
    flattening every chain into a set of cell names, returns one
    :class:`ResetSyncChain` per *distinct* chain head — so a rule that
    needs the head's D constant (e.g. RDC-007) can read it without
    walking the netlist twice.

    Two chains are considered the same when their head flop is the
    same cell; the canonical chain is the one whose tail (downstream-
    consumer-facing end) is the longest walk from the head.

    User-marked ``(* reset_sync *)`` flops are deliberately *not*
    included here: the recogniser would have no head D-constant to
    return for them (their head may be fed by an upstream signal
    rather than a literal). RDC-007 already documents that as the
    known limitation.
    """
    flop_by_name = {f.cell.name: f for f in find_flops(module)}
    bit_drivers = _bit_drivers(module)
    domains = assign_reset_domains(module)

    best_by_head: dict[str, ResetSyncChain] = {}
    for name, rd in domains.items():
        if rd.reset is None or rd.reset.type != "async":
            continue
        chain, head_d = _trace_reset_sync_chain(
            name,
            flop_by_name,
            domains,
            clock_domains,
            bit_drivers,
            max_depth=min_depth + 4,
        )
        if len(chain) < min_depth or head_d is None:
            continue
        head_name = chain[-1]
        prev = best_by_head.get(head_name)
        if prev is None or len(chain) > len(prev.flops):
            best_by_head[head_name] = ResetSyncChain(
                flops=tuple(chain),
                head_d_constant=head_d,
                polarity=rd.reset.polarity,
            )
    return sorted(best_by_head.values(), key=lambda c: c.flops[0])


def _trace_reset_sync_chain(
    start: str,
    flop_by_name: dict[str, Flop],
    reset_domains: dict[str, ResetDomain],
    clock_domains: dict[str, str | None],
    bit_drivers: dict[Bit, tuple[str, str]],
    *,
    max_depth: int,
) -> tuple[list[str], str | None]:
    """Walk Q→D back from ``start``; return the chain iff its head is constant-fed.

    Returns ``(chain, head_d_constant)``. The chain includes ``start``
    (as the tail) and every intermediate flop up to the constant-fed
    head. ``head_d_constant`` is the Yosys-encoded D-pin constant on
    the head flop (``"0"`` / ``"1"`` / ``"x"`` / ``"z"`` or a
    multi-bit binary string).

    If the walk terminates on anything other than a constant — a
    foreign-domain flop, a port, a multi-bit ``D``, a combinational
    signal, depth exhaustion — the chain is **not** a reset
    synchroniser and ``([], None)`` is returned. This is the
    load-bearing distinction: without it, any 2-flop ARST-sharing
    chain in the same clock domain (including data-path register
    chains that happen to share a reset) would be mis-identified as a
    synchroniser."""
    chain = [start]
    cur_name = start
    cur_rd = reset_domains[cur_name]
    cur_clk = clock_domains.get(cur_name)
    if cur_clk is None or cur_rd.reset is None:
        return ([], None)
    reset_signature = (cur_rd.reset.name, cur_rd.reset.source)

    for _ in range(max_depth):
        cur_flop = flop_by_name[cur_name]
        d_bits = cur_flop.d
        if len(d_bits) != 1:
            return ([], None)  # Multi-bit chains aren't the reset-sync shape.
        d_bit = d_bits[0]
        if not isinstance(d_bit, int):
            # D is a constant — chain head found. Return the chain as
            # collected so far plus the literal constant; the constant
            # itself isn't a flop.
            return (chain, str(d_bit))
        drv = bit_drivers.get(d_bit)
        if drv is None:
            return (
                [],
                None,
            )  # Port- or constant-net-fed D isn't the recogniser's shape.
        drv_name, drv_port = drv
        if drv_port != "Q" or drv_name not in flop_by_name:
            return ([], None)
        drv_rd = reset_domains.get(drv_name)
        drv_clk = clock_domains.get(drv_name)
        if drv_rd is None or drv_rd.reset is None or drv_clk is None:
            return ([], None)
        if drv_clk != cur_clk:
            return ([], None)
        if (drv_rd.reset.name, drv_rd.reset.source) != reset_signature:
            return ([], None)
        chain.append(drv_name)
        cur_name = drv_name
    return ([], None)


def _polarity_from_param(raw: str) -> ResetPolarity:
    """Decode Yosys' parameter polarity string.

    Yosys emits parameters as binary strings (``'1'``, ``'0'``, or the
    32-bit padded form like ``'00000000000000000000000000000001'``).
    A trailing ``1`` bit means active-high; anything else (including
    the empty string) is treated as active-low — the conservative
    default for the active-low reset idiom that dominates the field.
    """
    if not raw:
        return "low"
    return "high" if raw[-1] == "1" else "low"


ResetCrossingKind = Literal[
    "async-deassert",
    "polarity-mismatch",
    "sync-crossing",
    "comb-driven",
]


@dataclass(frozen=True)
class ResetCrossing:
    """A single problematic reset arrival at a flop.

    Unified record consumed by external tooling (e.g. ``rtl-buddy-view``)
    that wants the reset-domain view without re-running each RDC rule
    individually. The rule pack still owns the *reporting* surface
    (rule-id, severity, grouped messages); :func:`find_reset_crossings`
    just emits the structural facts.

    Fields:

    * ``flop`` — destination flop cell name.
    * ``reset`` — the upstream reset source classification (from
      :func:`assign_reset_domains`).
    * ``flop_clock`` — the clock domain that samples ``flop``'s ``D``
      (i.e. the value passed in via ``clock_domains[flop]``). ``None``
      when the flop's clock isn't traceable.
    * ``kind`` — what about the crossing is worth flagging; see
      :data:`ResetCrossingKind`.
    """

    flop: str
    reset: ResetSource
    flop_clock: str | None
    kind: ResetCrossingKind


def find_reset_crossings(
    module: Module,
    clock_domains: dict[str, str | None],
    *,
    recognised_syncs: set[str] | None = None,
    polarity_overrides: dict[str, ResetPolarity] | None = None,
) -> list[ResetCrossing]:
    """Unified view of every reset arrival worth flagging in ``module``.

    Iterates :func:`assign_reset_domains` and emits one
    :class:`ResetCrossing` per flop whose reset would draw a rule
    finding. The kinds produced parallel the RDC rule family:

    * ``"async-deassert"`` — an async reset whose source domain differs
      from the consumer flop's clock domain and no reset-synchroniser
      stage is present (RDC-001's structural shape).
    * ``"polarity-mismatch"`` — a port-level ``(* reset_polarity *)``
      override declares a polarity that disagrees with the flop's
      inferred reset-pin polarity (RDC-002's port-declared variant).
      Only emitted when ``polarity_overrides`` is supplied.
    * ``"sync-crossing"`` — a sync reset whose source flop is in a
      different clock domain from the consumer (RDC-003's shape).
    * ``"comb-driven"`` — the reset is driven by combinational logic
      (RDC-004's shape).

    ``recognised_syncs`` lists flop cell names that the analyzer has
    already classified as reset-synchroniser members (the union of
    :func:`find_reset_synchronizers`'s output and any user-marked
    flops); those flops are skipped — by construction they bridge the
    crossing safely.

    ``polarity_overrides`` maps top-level reset *port* names to the
    declared polarity (``"high"`` / ``"low"``). When a flop's reset
    source is one of these ports and the flop's inferred polarity
    disagrees with the declaration, a ``"polarity-mismatch"`` crossing
    is emitted in addition to any crossing-kind that would also fire.

    This function is intentionally additive: the existing RDC-001..-005
    rule checks keep their own walks. ``find_reset_crossings`` is the
    public surface for *consumers* of the analyzer (downstream tooling
    and tests that want to enumerate the reset-domain facts directly).
    """
    out: list[ResetCrossing] = []
    syncs = recognised_syncs or set()
    domains = assign_reset_domains(module)

    for name, rd in domains.items():
        if name in syncs:
            continue
        if rd.reset is None:
            continue
        flop_clock = clock_domains.get(name)
        rsrc = rd.reset

        # comb-driven: structural reset shape is combinational; clock
        # domain comparison is moot here — the issue is upstream of
        # any crossing question.
        if rsrc.source == "comb":
            out.append(
                ResetCrossing(
                    flop=name,
                    reset=rsrc,
                    flop_clock=flop_clock,
                    kind="comb-driven",
                )
            )
            continue

        # Domain-crossing kinds. A source flop in a different clock
        # domain (inferred reset source) is the shape that drives
        # RDC-001 (async) and RDC-003 (sync).
        if rsrc.source == "inferred":
            src_clock = clock_domains.get(rsrc.name)
            if (
                flop_clock is not None
                and src_clock is not None
                and flop_clock != src_clock
            ):
                kind: ResetCrossingKind = (
                    "async-deassert" if rsrc.type == "async" else "sync-crossing"
                )
                out.append(
                    ResetCrossing(
                        flop=name,
                        reset=rsrc,
                        flop_clock=flop_clock,
                        kind=kind,
                    )
                )

        # Port-declared polarity override disagrees with the flop's
        # inferred polarity. Emitted independently of any
        # crossing-kind above; a single flop can be both an
        # async-deassert and a polarity-mismatch crossing.
        if (
            polarity_overrides
            and rsrc.source == "port"
            and rsrc.name in polarity_overrides
            and polarity_overrides[rsrc.name] != rsrc.polarity
        ):
            out.append(
                ResetCrossing(
                    flop=name,
                    reset=rsrc,
                    flop_clock=flop_clock,
                    kind="polarity-mismatch",
                )
            )

    return out
