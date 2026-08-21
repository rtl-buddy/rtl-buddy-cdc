"""CDC rule checks.

Each rule is a small function ``check_<rule>(...) -> list[Violation]``
operating on the netlist + the list of crossings (already filtered to
asynchronous pairs by the caller). Adding a rule is intentionally a
self-contained edit: register the function in :data:`RULES` and you're
done.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Literal

from rtl_buddy_cdc.domain import Crossing, assign_domains, find_clock_combines
from rtl_buddy_cdc.flops import Flop, find_flops, is_ff_cell, is_latch_cell
from rtl_buddy_cdc.netlist import Bit, Cell, Module
from rtl_buddy_cdc.primitives import (
    is_sync_primitive,
    is_xpm_primitive,
    normalise_type as normalise_primitive_type,
    sync_depths as primitive_sync_depths,
)
from rtl_buddy_cdc.pulse import classify_d_pin_shape, classify_toggle_d_pin
from rtl_buddy_cdc.reset_hints import ResetHints
from rtl_buddy_cdc.sdc import UNCONSTRAINED_SENTINEL, ClockSpec


@dataclass(frozen=True)
class Violation:
    rule_id: str
    severity: str  # "error" | "warning" | "info"
    message: str
    # Most rules attach the data crossing they fired on; rules that
    # operate on a different shape (e.g. reset crossings) leave it None.
    crossing: Crossing | None = None
    # The single cell most directly responsible — used by structured
    # reporters (JSON/SARIF) to surface a source location via the
    # cell's ``attributes["src"]`` field. Falls back to the crossing's
    # dst flop if not set.
    cell_name: str | None = None
    # Hierarchical instance path the offending cell lives in, derived
    # from ``cell_name`` by ``reporter._instance_path`` at the CLI
    # boundary. ``()`` means the cell is at the top instance (the
    # common case on flat IP-block fixtures) and is also the safe
    # default for rules that construct ``Violation`` directly — the
    # resolver only runs in ``cli._analyze_and_report``, not in the
    # rule pack. Reporters consume this in phase 2/3/4 of #46 to
    # group findings by instance.
    instance_path: tuple[str, ...] = ()


# ``ctx`` is keyword-only on every rule; using ``Callable[..., ...]`` because
# typing.Callable can't express a kw-only argument without a Protocol.
RuleFn = Callable[..., list[Violation]]


# --- shared rule context ----------------------------------------------------


@dataclass(frozen=True)
class _RuleContext:
    """Structural views every rule wants. Computed once in :func:`run_all`
    and threaded into each ``check_cdc_NNN`` via the keyword-only
    ``ctx=`` argument; rules called standalone (a test invoking
    ``check_cdc_002`` directly) lazy-build their own context.

    The cache exists because the rule pack used to recompute these
    views per-rule. ``assign_domains`` ran 7×, ``find_flops`` ran
    transitively many more times, and ``_sync_chain_depth``'s inner
    loop called ``find_flops`` every chain-extension step (O(N) per
    step). On a 90-flop block the overhead is invisible; the moment
    rule fan-out crosses a few hundred flops it dominates.
    """

    module: Module
    clock_spec: ClockSpec | None
    flops: tuple[Flop, ...]
    domains: dict[str, str | None]
    bit_drivers: dict[Bit, tuple[str, str, int]]
    # Forward index: maps each bit to every (cell_name, input_port_name,
    # index) consuming it. Symmetric to ``bit_drivers``. Used by
    # :func:`_forward_reachable_flops` for downstream-cone walks (e.g.
    # the CDC-005 reconvergence filter). Built once per ``run_all``.
    bit_consumers: dict[Bit, tuple[tuple[str, str, int], ...]]
    reader_counts: dict[Bit, int]
    user_syncs: frozenset[str]
    user_grays: frozenset[str]
    user_statics: frozenset[str]
    # Flops tagged ``(* cdc_handshake *)`` — participants in a sanctioned
    # four-phase req/ack vector-CDC primitive. Suppresses CDC-001 / 013 /
    # 014 / 020 on the crossing keyed at the tagged flop (issue #247).
    user_handshakes: frozenset[str]
    # Bits belonging to nets tagged with
    # :data:`USER_GLITCHLESS_CLOCK_MUX_ATTRS`. Consulted by CDC-010
    # to suppress on a hand-built glitchless-clock-mux select wire.
    user_glitchless_mux_bits: frozenset[Bit]
    # Reverse index used by ``_sync_chain_depth``: for every flop whose
    # ``D`` is exactly one bit wide, map that bit to the flop. The
    # chain walker previously did ``for f in find_flops(module):`` per
    # step; this turns each step into an O(1) lookup.
    d_bit_to_single_bit_flop: dict[Bit, Flop]
    # Reset-side user input (SV attributes + optional ``--reset-hints``
    # YAML overlay), merged once at context-build time so the RDC rules
    # consult ``ctx.reset_sync_flop_names`` / ``ctx.reset_polarity_overrides``
    # instead of re-walking ``module.netnames`` per rule. Hints win on
    # disagreement with SV attributes — see :mod:`rtl_buddy_cdc.reset_hints`.
    reset_sync_flop_names: frozenset[str]
    reset_polarity_overrides: dict[str, Literal["high", "low"]]
    # Module names of auto-abstracted single-clock boundary cells
    # (#256). A boundary instance is an opaque summarised subtree — its
    # internals aren't in the flattened netlist, so structural rules
    # that walk cell pins (notably CDC-008's clock-used-as-data) must
    # treat the instance as exempt: a clock entering its clock pin is
    # legitimate distribution into the subtree, not a wiring bug, and
    # firing on it would diverge from the un-abstracted (flattened)
    # result the abstraction must preserve.
    boundary_modules: frozenset[str] = frozenset()
    # Module names of *all* first-class blackbox boundary cells (#255),
    # the superset of ``boundary_modules``. A blackbox the summariser
    # *declined* to auto-abstract (foreign-domain data input, not
    # provably single-clock) is still an opaque boundary instance: its
    # internals are absent from the flattened netlist, so a clock on its
    # clock pin is distribution into the subtree, not a wiring bug. We
    # exempt these from CDC-008 too — otherwise a user who legitimately
    # blackboxes a clocked subtree gets a spurious clock-as-data finding
    # on the boundary instance even though abstraction was declined (a
    # finding the un-blackboxed flattened design never reports). See
    # rtl-buddy-cdc#257 review.
    blackbox_modules: frozenset[str] = frozenset()
    # Per-instance clock-pin port names for blackbox boundary cells
    # (FIX 4, soundness audit of #259). Maps instance (cell) name to the
    # set of its port names determined to be CLOCK pins (traced, not just
    # name-allow-listed). CDC-008 exempts only those pins on a blackbox
    # instance — a clock wired into a genuine DATA input of the blackbox
    # still fires clock-as-data. When an instance is absent / empty here,
    # CDC-008 falls back to the per-type ``boundary_modules`` /
    # ``blackbox_modules`` whole-instance exemption (e.g. legacy callers
    # that don't supply the map).
    boundary_clock_pins: dict[str, frozenset[str]] = field(default_factory=dict)
    # Clock-trace hop budget this context was built with (``--clock-trace-depth``).
    # Rules that need to re-walk the clock network themselves — CDC-023's
    # combine report — must use the SAME budget as ``domains`` above, or the
    # two views of the clock network could disagree at a non-default depth.
    max_depth: int = 16


def _build_context(
    module: Module,
    clock_spec: ClockSpec | None,
    *,
    reset_hints: ResetHints | None = None,
    boundary_modules: frozenset[str] = frozenset(),
    blackbox_modules: frozenset[str] = frozenset(),
    boundary_clock_pins: dict[str, frozenset[str]] | None = None,
    max_depth: int = 16,
) -> _RuleContext:
    """Compute the per-``run_all`` cached views in one pass.

    Pure function of ``(module, clock_spec)``; safe to call multiple
    times but pointless — the whole point is amortising the work
    across rules.

    ``max_depth`` is the clock-trace hop budget forwarded to
    :func:`~rtl_buddy_cdc.domain.assign_domains` (default 16, surfaced
    as ``--clock-trace-depth``). It must match the budget the crossing
    walk uses so ``ctx.domains`` — the rule-context per-flop domain
    view, consulted by CDC-010 and the domain-map path — agrees with
    the crossings list at any depth. See issue #263.
    """
    flops = tuple(find_flops(module))
    flop_domains = assign_domains(module, max_depth=max_depth)
    domains = {fd.flop.cell.name: fd.clock for fd in flop_domains}

    bit_drivers: dict[Bit, tuple[str, str, int]] = {}
    for cell in module.cells.values():
        for port_name in ("Y", "Q"):
            for idx, b in enumerate(cell.connections.get(port_name, ())):
                if isinstance(b, int):
                    bit_drivers[b] = (cell.name, port_name, idx)

    # Forward index: same shape as ``bit_drivers`` but for *inputs*.
    # CLK is excluded (clock pins are walked separately by the clock-
    # network tracer); other pin names — D, A, B, S, SEL, EN, ARST, … —
    # all count as data consumers.
    consumers_builder: dict[Bit, list[tuple[str, str, int]]] = {}
    for cell in module.cells.values():
        for port_name, bits in cell.connections.items():
            if port_name in _OUTPUT_PINS or port_name == "CLK":
                continue
            for idx, b in enumerate(bits):
                if isinstance(b, int):
                    consumers_builder.setdefault(b, []).append(
                        (cell.name, port_name, idx)
                    )
    bit_consumers: dict[Bit, tuple[tuple[str, str, int], ...]] = {
        bit: tuple(entries) for bit, entries in consumers_builder.items()
    }

    reader_counts: dict[Bit, int] = {}
    for cell in module.cells.values():
        for port_name, bits in cell.connections.items():
            if port_name in {"Y", "Q", "CLK"}:
                continue
            for b in bits:
                if isinstance(b, int):
                    reader_counts[b] = reader_counts.get(b, 0) + 1

    d_bit_to_single_bit_flop: dict[Bit, Flop] = {}
    for f in flops:
        if len(f.d) == 1 and isinstance(f.d[0], int):
            d_bit_to_single_bit_flop[f.d[0]] = f

    return _RuleContext(
        module=module,
        clock_spec=clock_spec,
        flops=flops,
        domains=domains,
        bit_drivers=bit_drivers,
        bit_consumers=bit_consumers,
        reader_counts=reader_counts,
        user_syncs=frozenset(user_sync_flop_names(module)),
        user_grays=frozenset(user_gray_flop_names(module)),
        user_statics=frozenset(user_static_flop_names(module)),
        user_handshakes=frozenset(user_handshake_flop_names(module)),
        user_glitchless_mux_bits=frozenset(user_glitchless_clock_mux_bits(module)),
        d_bit_to_single_bit_flop=d_bit_to_single_bit_flop,
        reset_sync_flop_names=frozenset(
            user_reset_sync_flop_names(module, hints=reset_hints)
        ),
        reset_polarity_overrides=user_reset_polarity_overrides(
            module, hints=reset_hints
        ),
        boundary_modules=boundary_modules,
        blackbox_modules=blackbox_modules,
        boundary_clock_pins=boundary_clock_pins or {},
        max_depth=max_depth,
    )


# --- helpers ----------------------------------------------------------------


# SV attributes that mark a flop as a user-vetted synchronizer first
# stage. Attach to the wire/reg declaration the flop drives, e.g.::
#
#     (* cdc_sync *) logic dst_q;
#
# Yosys preserves the attribute on the *netname* (not the cell), so
# we map back from a tagged netname's bits to the flop whose Q
# produces them. Multiple aliases are accepted so projects using
# other common synthesis-attribute tags don't have to rename.
USER_SYNC_ATTRS: frozenset[str] = frozenset({"cdc_sync", "synchronizer", "async_reg"})


def user_sync_flop_names(module: Module) -> set[str]:
    """Return cell names of flops whose Q is named via a wire annotated
    with one of the :data:`USER_SYNC_ATTRS`. These are treated by
    CDC-001 / CDC-002 / CDC-003 / CDC-006 as "trust-me, this is a
    correctly-engineered synchronizer" — the structural rule passes
    skip them.

    Attribute names are matched **case-insensitively** (#275). The
    ``async_reg`` alias exists to honour the Xilinx synthesis attribute,
    but Xilinx (and the XPM CDC macro sources) spell it
    ``(* ASYNC_REG = "TRUE" *)`` and Yosys preserves the attribute name
    verbatim — so a case-sensitive match never fired on the very idiom
    the alias was added for."""
    sync_bits: set[Bit] = set()
    for nn in module.netnames.values():
        if USER_SYNC_ATTRS & {a.lower() for a in nn.attributes}:
            for b in nn.bits:
                if isinstance(b, int):
                    sync_bits.add(b)
    if not sync_bits:
        return set()
    out: set[str] = set()
    for f in find_flops(module):
        if any(isinstance(b, int) and b in sync_bits for b in f.q):
            out.add(f.cell.name)
    return out


# Companion to USER_SYNC_ATTRS: an explicit gray-coded promise. Attach
# to the source-side bus wire (the gray counter's output) to suppress
# CDC-004 false positives when the structural detector can't see the
# multi-bit sync chain (e.g. across a module boundary that hasn't been
# flattened, or when the chain is implemented in a non-canonical way).
USER_GRAY_ATTRS: frozenset[str] = frozenset({"cdc_gray", "gray_code"})


# Reset-side counterpart of USER_SYNC_ATTRS — mark a flop as a
# user-vetted reset-synchroniser stage. Consumed by
# :func:`rtl_buddy_cdc.reset_domain.find_reset_synchronizers` as an
# alternate match path so RDC-002 / RDC-004 / RDC-005 skip the marked
# flops without needing to match the constant-fed-D structural shape.
# Useful for sync chains whose head's D is fed by an upstream signal
# (rather than a literal constant) — the structural recogniser is
# deliberately conservative and would otherwise miss them.
USER_RESET_SYNC_ATTRS: frozenset[str] = frozenset({"reset_sync", "reset_synchronizer"})


def user_reset_sync_flop_names(
    module: Module, *, hints: ResetHints | None = None
) -> set[str]:
    """Cell names of flops whose Q is named via a wire annotated with
    ``(* reset_sync *)`` (or :data:`USER_RESET_SYNC_ATTRS` aliases),
    optionally unioned with synchroniser entries from a YAML hints
    file (issue #129). Treated by the RDC rule pack as a vetted
    reset-synchroniser stage regardless of the flop's structural
    shape.

    SV attribute and hints set-union with no precedence question:
    both sides mark the same kind of fact (this cell is a sync
    stage). Disagreement isn't possible — neither side has a "not
    a sync stage" assertion."""
    sync_bits: set[Bit] = set()
    for nn in module.netnames.values():
        if USER_RESET_SYNC_ATTRS & set(nn.attributes):
            for b in nn.bits:
                if isinstance(b, int):
                    sync_bits.add(b)
    out: set[str] = set()
    if sync_bits:
        for f in find_flops(module):
            if any(isinstance(b, int) and b in sync_bits for b in f.q):
                out.add(f.cell.name)
    if hints is not None:
        out |= hints.synchronizer_cell_names(module)
    return out


def user_gray_flop_names(module: Module) -> set[str]:
    """Cell names of source flops whose Q is named via a wire annotated
    with ``(* cdc_gray *)``. CDC-004 treats those bus crossings as safe
    by fiat (the user is asserting only one bit changes per cycle)."""
    gray_bits: set[Bit] = set()
    for nn in module.netnames.values():
        if USER_GRAY_ATTRS & set(nn.attributes):
            for b in nn.bits:
                if isinstance(b, int):
                    gray_bits.add(b)
    if not gray_bits:
        return set()
    out: set[str] = set()
    for f in find_flops(module):
        if any(isinstance(b, int) and b in gray_bits for b in f.q):
            out.add(f.cell.name)
    return out


# Quasi-static signals: configuration / mode bits programmed once at
# boot and held constant during the operating window. Structurally
# they're cross-domain crossings without a synchroniser, but the
# metastability failure mode the CDC rules target (transitioning
# value sampled mid-flight) cannot occur — the value is not
# transitioning. Attach to the source-side flop's wire/reg
# declaration::
#
#     (* cdc_static *) logic [7:0] cfg_mode_q;
#
# Suppresses CDC-001 / CDC-002 / CDC-003 / CDC-004 on crossings that
# *originate* at the tagged flop. Same Yosys attribute-on-netname
# placement convention as ``cdc_sync`` / ``cdc_gray``. CDC-005
# (reconvergence) deliberately stays live — reconvergent fanout of a
# static signal merged with a non-static signal is still a coherent
# hazard worth surfacing.
USER_STATIC_ATTRS: frozenset[str] = frozenset({"cdc_static", "quasi_static"})


def user_static_flop_names(module: Module) -> set[str]:
    """Cell names of source flops whose Q is named via a wire annotated
    with ``(* cdc_static *)`` (or :data:`USER_STATIC_ATTRS` aliases).
    CDC-001 / CDC-002 / CDC-003 / CDC-004 skip crossings whose source
    flop is in this set — the user is asserting the value is
    runtime-constant during operation, so cross-domain sampling is
    coherent by construction."""
    static_bits: set[Bit] = set()
    for nn in module.netnames.values():
        if USER_STATIC_ATTRS & set(nn.attributes):
            for b in nn.bits:
                if isinstance(b, int):
                    static_bits.add(b)
    if not static_bits:
        return set()
    out: set[str] = set()
    for f in find_flops(module):
        if any(isinstance(b, int) and b in static_bits for b in f.q):
            out.add(f.cell.name)
    return out


# Four-phase req/ack handshake primitive (issue #247). Attach to the
# registers that *participate* in a sanctioned vector-CDC handshake (the
# canonical `ip_cdc_handshake` shape): the source-side req toggle, the
# held payload register, and the destination-side capture register::
#
#     (* cdc_handshake *) logic              src_req;     // toggle
#     (* cdc_handshake *) logic [WIDTH-1:0]  src_payload; // held stable
#     (* cdc_handshake *) logic [WIDTH-1:0]  dst_data;    // single capture
#
# The protocol makes the otherwise-flagged paths safe by construction:
#   - the payload is latched and held stable for the whole req→ack→done
#     window, so the sliced-bus reconvergence CDC-020 targets cannot
#     misresolve (no bit is mid-flight when the destination samples);
#   - the source is backpressured (``src_ready = (src_req == ack_in_src)``)
#     and cannot launch a new transfer until the destination acks, so the
#     fast→slow toggle event-loss CDC-013 targets cannot drop an event;
#   - the payload is stable under ``dst_valid``, so a *single* destination
#     register is the intended capture (not a missing CDC-001 second
#     stage), and feeding that capture through decode comb is ordinary
#     datapath, not a gate wedged between sync stages (CDC-014).
#
# Suppresses CDC-001 / CDC-013 / CDC-014 / CDC-020 on the crossing keyed
# at the tagged flop. Same attribute-on-netname placement convention as
# ``cdc_sync`` / ``cdc_gray`` / ``cdc_static``. This is the opt-in escape
# hatch from issue #247 — mark the blessed primitive once and every
# instance is recognised, retiring the per-instance waivers.
USER_HANDSHAKE_ATTRS: frozenset[str] = frozenset({"cdc_handshake", "req_ack_handshake"})


def user_handshake_flop_names(module: Module) -> set[str]:
    """Cell names of flops whose Q is named via a wire annotated with
    ``(* cdc_handshake *)`` (or :data:`USER_HANDSHAKE_ATTRS` aliases).
    CDC-001 / CDC-013 / CDC-014 / CDC-020 skip the crossing keyed at a
    flop in this set — the user is asserting the flop participates in a
    sanctioned four-phase req/ack handshake whose protocol makes the
    structural pattern safe (see :data:`USER_HANDSHAKE_ATTRS`)."""
    handshake_bits: set[Bit] = set()
    for nn in module.netnames.values():
        if USER_HANDSHAKE_ATTRS & set(nn.attributes):
            for b in nn.bits:
                if isinstance(b, int):
                    handshake_bits.add(b)
    if not handshake_bits:
        return set()
    out: set[str] = set()
    for f in find_flops(module):
        if any(isinstance(b, int) and b in handshake_bits for b in f.q):
            out.add(f.cell.name)
    return out


# Glitchless clock-mux promise (issue #208 / G-9). Attach to the
# *select* wire of a hand-built glitchless 2-input clock mux (the
# textbook cross-coupled-latch shape: two ``$dlatch``es per leg
# gated by the inverted clocks, ANDed with their respective clocks,
# ORed for the output). The cross-coupled latching makes an
# asynchronous select safe — synchronising the select onto one of
# the gated clocks would actually break the glitchless property,
# so CDC-010's standard fix advice does not apply.
#
# Suppression is by *net* (not by source flop): the attribute is on
# the select wire itself; CDC-010 skips when any control-pin bit it
# walks belongs to a tagged netname.
USER_GLITCHLESS_CLOCK_MUX_ATTRS: frozenset[str] = frozenset(
    {"glitchless_clock_mux", "glitchless_mux", "glitchfree_clock_mux"}
)


def user_glitchless_clock_mux_bits(module: Module) -> set[Bit]:
    """Return the set of net bits annotated with
    :data:`USER_GLITCHLESS_CLOCK_MUX_ATTRS`.

    CDC-010 consults this set when walking each clock-network cell's
    control pin: if any control bit is a member, the rule treats the
    cell as a user-vetted glitchless mux and stays silent on that
    pin. The attribute is the user's explicit "I built a glitchless
    mux around this select, trust me" promise, parallel to
    ``(* cdc_sync *)`` for synchronisers and ``(* cdc_gray *)`` for
    gray-coded buses.
    """
    out: set[Bit] = set()
    for nn in module.netnames.values():
        if USER_GLITCHLESS_CLOCK_MUX_ATTRS & set(nn.attributes):
            for b in nn.bits:
                if isinstance(b, int):
                    out.add(b)
    return out


# Reset-port polarity declaration (issue #107). Attach to a top-level
# reset port to assert "this signal is active-<low|high>" regardless of
# what Yosys infers from a downstream flop's edge sensitivity. Consumed
# by RDC-002, which fires when a flop's inferred reset-pin polarity
# disagrees with the user's port-level declaration — the classic
# "designer added a posedge flop on a port the rest of the design
# treats as active-low" wiring bug.
#
# Value syntax: ``(* reset_polarity = "low" *)`` or ``"high"``. Anything
# else is ignored (warned-quietly). The bare form
# ``(* reset_polarity *)`` (no value) is also ignored — without a
# polarity string the attribute conveys nothing useful.
USER_RESET_POLARITY_ATTRS: frozenset[str] = frozenset({"reset_polarity"})


def user_reset_polarity_overrides(
    module: Module, *, hints: ResetHints | None = None
) -> dict[str, Literal["high", "low"]]:
    """Map top-level reset *port* name → user-declared polarity.

    Walks ``module.netnames`` looking for the
    :data:`USER_RESET_POLARITY_ATTRS` attribute. When the netname
    coincides with a top-level input port and the attribute value is
    one of ``"high"`` / ``"low"`` (case-insensitive), the port is
    recorded in the returned map.

    The attribute is interpreted as a *port-level* declaration only;
    internal nets with the attribute are silently ignored. Rationale:
    the consumer rule (RDC-002 port-override variant) reconciles flop
    polarity against an external source-of-truth, which only the
    top-level port boundary represents.

    Values that don't decode (numeric strings, typos like ``"lo"``)
    are skipped rather than raised — the analyzer's tolerance for
    malformed-but-non-fatal user input matches the rest of the SV
    attribute handling.

    When ``hints`` is supplied, port hints from the YAML file (#129)
    overlay the SV-attribute map: hints win on disagreement (the
    file is the explicit override; the attribute is the in-RTL
    default). Hints are *only* applied to ports that actually exist
    on the module — a hint for an unknown port name is silently
    ignored here, but the loader's strict validation catches
    anything earlier in the pipeline that's obviously wrong.
    """
    out: dict[str, Literal["high", "low"]] = {}
    port_names = {p.name for p in module.ports.values() if p.direction == "input"}
    for nn_name, nn in module.netnames.items():
        if nn_name not in port_names:
            continue
        for attr in USER_RESET_POLARITY_ATTRS:
            raw = nn.attributes.get(attr)
            if raw is None:
                continue
            value = raw.strip().lower()
            if value == "high":
                out[nn_name] = "high"
                break
            if value == "low":
                out[nn_name] = "low"
                break
    if hints is not None:
        for name, pol in hints.port_polarity_overrides().items():
            if name in port_names:
                out[name] = pol
    return out


# Pin names whose connections are *outputs* of a cell (so we never walk
# backward through them). All other pins are treated as inputs for the
# purpose of fanin walks.
_OUTPUT_PINS: frozenset[str] = frozenset({"Y", "Q"})


def _backward_fanin(
    module: Module,
    start_bits: tuple[Bit, ...],
    drivers: dict[Bit, tuple[str, str, int]],
    max_depth: int = 12,
) -> tuple[set[str], set[str]]:
    """Reverse-BFS through combinational cells.

    Returns ``(flop_cell_names, input_port_names)`` — every flop ``Q``
    and every top-level *input* port reached. Useful when a rule needs
    to distinguish "the upstream is a registered flop" from "the
    upstream is an external pin".
    """
    flops: set[str] = set()
    ports: set[str] = set()
    seen: set[Bit] = set()
    frontier: list[tuple[Bit, int]] = [(b, 0) for b in start_bits if isinstance(b, int)]
    while frontier:
        nxt: list[tuple[Bit, int]] = []
        for bit, depth in frontier:
            if bit in seen:
                continue
            seen.add(bit)
            drv = drivers.get(bit)
            if drv is None:
                port = module.port_of_bit(bit)
                if port is not None and port.direction == "input":
                    ports.add(port.name)
                continue
            cell_name, port_name, _idx = drv
            cell = module.cells[cell_name]
            if port_name == "Q":
                flops.add(cell_name)
                continue
            if depth >= max_depth:
                continue
            for in_port, in_bits in cell.connections.items():
                if in_port in _OUTPUT_PINS:
                    continue
                for b in in_bits:
                    if isinstance(b, int):
                        nxt.append((b, depth + 1))
        frontier = nxt
    return flops, ports


def _forward_reachable_flops(
    module: Module,
    start_bits: tuple[Bit, ...],
    consumers: dict[Bit, tuple[tuple[str, str, int], ...]],
    max_depth: int = 12,
) -> set[str]:
    """Forward-BFS through combinational cells, mirror of
    :func:`_backward_fanin`.

    Starts at ``start_bits`` (typically a flop's ``Q`` bits), walks
    each consumer cell forward through its output bits, and returns
    the set of flop cell names whose ``D`` pin was reached. Stops at
    every flop boundary — flops aren't crossed, they're the answer.
    Stops at ``max_depth`` hops to bound the walk on huge designs;
    the ``seen`` set keeps the walk linear regardless.

    See also :func:`_forward_reachable_cells` for a broader variant
    that also records comb-cell consumers on the path — used by
    CDC-005's reconvergence filter, which must catch unregistered
    recombination too (e.g. a top-level output port driven directly
    by a comb cell combining two synchronized values).
    """
    reached: set[str] = set()
    seen: set[Bit] = set()
    frontier: list[tuple[Bit, int]] = [(b, 0) for b in start_bits if isinstance(b, int)]
    while frontier:
        nxt: list[tuple[Bit, int]] = []
        for bit, depth in frontier:
            if bit in seen:
                continue
            seen.add(bit)
            for cell_name, port_name, _idx in consumers.get(bit, ()):
                cell = module.cells[cell_name]
                if is_ff_cell(cell.type):
                    # Reached a flop — recorded if it's the D input, but
                    # never crossed regardless of which pin we hit.
                    if port_name == "D":
                        reached.add(cell_name)
                    continue
                if depth >= max_depth:
                    continue
                # Combinational cell — enqueue every output bit.
                for out_port in _OUTPUT_PINS:
                    for b in cell.connections.get(out_port, ()):
                        if isinstance(b, int):
                            nxt.append((b, depth + 1))
        frontier = nxt
    return reached


def _forward_reachable_cells(
    module: Module,
    start_bits: tuple[Bit, ...],
    consumers: dict[Bit, tuple[tuple[str, str, int], ...]],
    max_depth: int = 12,
) -> set[str]:
    """Like :func:`_forward_reachable_flops` but records EVERY cell
    whose input is touched on the way — flops (via their D pin) and
    intermediate combinational cells alike.

    Recommended over the flop-only variant when the reconvergence
    point isn't necessarily a flop: e.g. CDC-005 must fire on a
    fixture whose two synchronized values recombine into a
    combinational reduction driving an unregistered output port.
    Two chains "reconverge" the moment any cell observes both
    values — registered or not.
    """
    reached: set[str] = set()
    seen: set[Bit] = set()
    frontier: list[tuple[Bit, int]] = [(b, 0) for b in start_bits if isinstance(b, int)]
    while frontier:
        nxt: list[tuple[Bit, int]] = []
        for bit, depth in frontier:
            if bit in seen:
                continue
            seen.add(bit)
            for cell_name, _port_name, _idx in consumers.get(bit, ()):
                cell = module.cells[cell_name]
                reached.add(cell_name)
                if is_ff_cell(cell.type):
                    # Flop boundary — recorded above, but don't cross.
                    continue
                if depth >= max_depth:
                    continue
                for out_port in _OUTPUT_PINS:
                    for b in cell.connections.get(out_port, ()):
                        if isinstance(b, int):
                            nxt.append((b, depth + 1))
        frontier = nxt
    return reached


def _backward_flop_fanin(
    module: Module,
    start_bits: tuple[Bit, ...],
    drivers: dict[Bit, tuple[str, str, int]],
    max_depth: int = 12,
) -> set[str]:
    """Backward BFS through combinational cells; return the set of flop
    cell names whose ``Q`` is reached.

    Traversal stops at flop ``Q`` outputs, top-level ports, or constants.
    The depth bound exists only to keep pathological fanins bounded; for
    well-structured CDC patterns the answer converges quickly.
    """
    flops: set[str] = set()
    seen: set[Bit] = set()
    frontier: list[tuple[Bit, int]] = [(b, 0) for b in start_bits if isinstance(b, int)]
    while frontier:
        nxt: list[tuple[Bit, int]] = []
        for bit, depth in frontier:
            if bit in seen:
                continue
            seen.add(bit)
            drv = drivers.get(bit)
            if drv is None:
                # bit comes from a top-level port or is a constant; the
                # caller will treat it as "untracked source" and decide
                continue
            cell_name, port, _idx = drv
            cell = module.cells[cell_name]
            if port == "Q":
                flops.add(cell_name)
                continue
            # combinational cell — walk back through every input pin
            if depth >= max_depth:
                continue
            for in_port, in_bits in cell.connections.items():
                if in_port in _OUTPUT_PINS:
                    continue
                for b in in_bits:
                    if isinstance(b, int):
                        nxt.append((b, depth + 1))
        frontier = nxt
    return flops


def _bit_reader_count(module: Module) -> dict[Bit, int]:
    """Count every (cell, port-bit) site that reads each net bit.

    Both flop D and combinational inputs count; flop CLK is excluded
    because clock connections aren't part of the data fanout. Two reads
    of the same bit by the same cell on different ports count
    independently — both are real loads.
    """
    counts: dict[Bit, int] = {}
    for cell in module.cells.values():
        # Outputs of $-prefixed primitives use the ``Y`` port; flop Q is
        # also an output. Both are intentionally ignored: we want input
        # readers, not drivers.
        for port_name, bits in cell.connections.items():
            if port_name in {"Y", "Q", "CLK"}:
                continue
            for b in bits:
                if isinstance(b, int):
                    counts[b] = counts.get(b, 0) + 1
    return counts


def _packed_shift_register_depth(
    head: Flop,
    reader_counts: dict[Bit, int],
) -> int | None:
    """Effective synchroniser depth when ``head`` is a *packed* shift
    register — a single multi-bit flop coded as ``q <= {q[N-2:0], d}``.

    A textbook N-FF synchroniser can be written either as N separate
    1-bit flops (``s0 <= d; s1 <= s0; …``) or as one ``reg [N-1:0]``
    that shifts (``s <= {s[N-2:0], d}``). After ``proc; flatten`` the
    second form lowers to a single multi-bit ``$dff`` whose stage-to-
    stage movement is *intra-cell* (``D[i] = Q[i-1]``). The plain
    flop→flop walk in :func:`_sync_chain_depth` sees no inter-cell
    D→Q hops, stops at the head, and returns depth 1 — a false CDC-001
    on every such instance (issue #264).

    This recognises the idiom structurally: a flop whose ``D`` vector
    is, lane for lane, either one of the flop's *own* ``Q`` bits (an
    internal shift tap, ``D[i] == Q[j]``) or — for exactly one lane —
    an external bit (the freshly sampled crossing signal). Following
    the per-lane shift from that single external input lane to the
    terminal tap yields the effective depth.

    Returns ``None`` when ``head`` is not this shape, so the caller
    falls back to its depth-1 verdict. Non-matches include a genuine
    multi-bit **bus** crossing (≥ 2 external lanes, no self-feedback),
    a gray-coded counter (``D`` bits are comb-cell outputs, not raw
    ``Q`` bits), and an enabled register whose ``D`` is a mux output.

    The walk mirrors :func:`_sync_chain_depth`'s "exactly one reader"
    rule: an intermediate lane whose ``Q`` is read by anything other
    than the single follow-on shift lane ends the chain (the
    synchronised value is already in use).
    """
    n = len(head.q)
    if n < 2 or len(head.d) != n:
        return None
    if not all(isinstance(b, int) for b in head.q):
        return None
    if not all(isinstance(b, int) for b in head.d):
        return None
    # Lane index of each of the flop's own Q bits. Distinct bits only;
    # a repeated Q bit isn't a well-formed register and breaks the
    # per-lane shift assumption.
    q_index: dict[Bit, int] = {}
    for i, qb in enumerate(head.q):
        if qb in q_index:
            return None
        q_index[qb] = i
    # Per-lane successor: lane ``j`` shifts its old value into lane
    # ``i`` iff ``D[i] == Q[j]``. Lanes whose ``D`` is not one of the
    # flop's own ``Q`` bits are external inputs.
    succ: dict[int, int] = {}
    external_lanes: list[int] = []
    for i, db in enumerate(head.d):
        j = q_index.get(db)
        if j is None:
            external_lanes.append(i)
            continue
        # Q[j] driving two different D lanes is a fanout, not a clean
        # linear shift register — bail rather than guess a depth.
        if j in succ:
            return None
        succ[j] = i
    # A packed shift-register synchroniser has exactly one freshly
    # sampled lane (the crossing bit). Zero means pure feedback; more
    # than one means an unrelated multi-input register (e.g. a bus).
    if len(external_lanes) != 1:
        return None
    depth = 1
    cur = external_lanes[0]
    visited = {cur}
    while True:
        # Intra-cell hop only extends the chain if this lane's output
        # feeds exactly the next shift lane and nothing else.
        if reader_counts.get(head.q[cur], 0) != 1:
            break
        nxt = succ.get(cur)
        if nxt is None or nxt in visited:
            break
        depth += 1
        visited.add(nxt)
        cur = nxt
    return depth if depth >= 2 else None


def _sync_chain_depth(
    module: Module,
    head: Flop,
    head_clock: str,
    domains: dict[str, str | None],
    reader_counts: dict[Bit, int] | None = None,
    *,
    d_bit_to_single_bit_flop: dict[Bit, Flop] | None = None,
) -> int:
    """Length of the synchronizer chain that starts at ``head``.

    A chain extends to the next flop iff:

    - the head's Q bit has *exactly one* reader anywhere in the
      module (any extra reader — combinational or otherwise — means
      the synchronized value is already in use; the chain ends),
    - that reader is a 1-bit flop on its D pin in the same clock
      domain.

    Returns the count of dst-domain flops *including* ``head``. Callers
    typically check ``depth >= 2``.

    When ``d_bit_to_single_bit_flop`` is supplied (from
    :class:`_RuleContext`), the chain extension step is an O(1) dict
    lookup instead of an O(N) ``find_flops`` scan. The argument is
    optional so callers that haven't built a context still work
    (the lazy-build path in each rule when ``ctx=None``).
    """
    if reader_counts is None:
        reader_counts = _bit_reader_count(module)
    if d_bit_to_single_bit_flop is None:
        d_bit_to_single_bit_flop = {
            f.d[0]: f
            for f in find_flops(module)
            if len(f.d) == 1 and isinstance(f.d[0], int)
        }

    depth = 1
    current = head
    visited = {current.cell.name}
    while True:
        # 1-bit chains only at this stage — multi-bit syncs are bus
        # crossings, handled by a separate rule.
        if len(current.q) != 1 or len(current.q) != len(current.d):
            # …unless the multi-bit head is a *packed* shift-register
            # synchroniser (``q <= {q[N-2:0], d}``), where the whole
            # chain lives intra-cell. Recognise that idiom and return
            # its effective depth instead of the false depth-1 (#264).
            if current is head:
                packed = _packed_shift_register_depth(current, reader_counts)
                if packed is not None:
                    return packed
            break
        next_q = current.q[0]
        if not isinstance(next_q, int):
            break
        # If the bit is consumed by anything other than a single follow-
        # on flop D pin, the chain ends here (the value is "in use").
        if reader_counts.get(next_q, 0) != 1:
            break
        nxt = d_bit_to_single_bit_flop.get(next_q)
        if nxt is None or nxt.cell.name in visited:
            break
        if domains.get(nxt.cell.name) != head_clock:
            break
        depth += 1
        visited.add(nxt.cell.name)
        current = nxt
    return depth


def _sync_chain_flops(
    module: Module,
    head: Flop,
    head_clock: str,
    domains: dict[str, str | None],
    reader_counts: dict[Bit, int],
    d_bit_to_single_bit_flop: dict[Bit, Flop],
) -> tuple[Flop, ...]:
    """Same walk as :func:`_sync_chain_depth` but returns the ordered
    list of chain flops (starting at ``head``).

    Phase-2 of CDC-005 needs the terminal flop's Q to start the
    forward-cone walk for the reconvergence filter; this helper is
    the shared truth between "how long is the chain" and "what's the
    chain's tail".
    """
    out: list[Flop] = [head]
    current = head
    visited = {current.cell.name}
    while True:
        if len(current.q) != 1 or len(current.q) != len(current.d):
            break
        next_q = current.q[0]
        if not isinstance(next_q, int):
            break
        if reader_counts.get(next_q, 0) != 1:
            break
        nxt = d_bit_to_single_bit_flop.get(next_q)
        if nxt is None or nxt.cell.name in visited:
            break
        if domains.get(nxt.cell.name) != head_clock:
            break
        out.append(nxt)
        visited.add(nxt.cell.name)
        current = nxt
    return tuple(out)


def _chain_has_inter_stage_comb(head: Flop, ctx: _RuleContext) -> Flop | None:
    """Detect a comb cell *between* sync stages — i.e. ``head.Q`` feeds
    a combinational cell whose output drives a flop in ``head``'s own
    clock domain.

    This is the structural shape CDC-014 fires on and the trigger
    condition for CDC-001's deferral: when ``_sync_chain_depth`` says
    "depth = 1" but a follow-on flop *does* exist behind a gate, the
    "no second-stage synchronizer" message would mislead the user.

    Bounded to a single comb hop. Walking deeper would entangle with
    CDC-005's reconvergent-fanout filter; this is the load-bearing
    case (gate immediately following the first stage) and the only
    one whose framing is unambiguously sync-chain-related.

    Returns the downstream flop if the pattern matches, else ``None``.
    """
    if len(head.q) != 1:
        return None
    head_q = head.q[0]
    if not isinstance(head_q, int):
        return None
    head_clock = ctx.domains.get(head.cell.name)
    if head_clock is None:
        return None
    for cell_name, _port_name, _idx in ctx.bit_consumers.get(head_q, ()):
        cell = ctx.module.cells.get(cell_name)
        if cell is None:
            continue
        # Skip flop cells — that's a direct chain extension, handled
        # by _sync_chain_depth, not the inter-stage-comb pattern.
        if is_ff_cell(cell.type):
            continue
        # Walk the comb cell's Y output forward one hop.
        for yb in cell.connections.get("Y", ()):
            if not isinstance(yb, int):
                continue
            nxt = ctx.d_bit_to_single_bit_flop.get(yb)
            if nxt is None:
                continue
            if nxt.cell.name == head.cell.name:
                continue
            if ctx.domains.get(nxt.cell.name) != head_clock:
                continue
            return nxt
    return None


# --- rules ------------------------------------------------------------------


def check_cdc_001(
    module: Module,
    crossings: list[Crossing],
    clock_spec: ClockSpec | None = None,  # noqa: ARG001
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """CDC-001 — Unsynchronized control crossing.

    A single-bit register-to-register CDC where the destination flop has
    no follow-on synchronizer flop in the same domain. This is the
    classic "metastability bug": the source-domain signal lands on a
    single flop and is then used directly, with no second stage to
    filter metastable values.

    Multi-bit (bus) crossings deliberately bypass this check — their
    correctness pattern is gating or gray-coding, handled by CDC-004.
    """
    if ctx is None:
        ctx = _build_context(module, clock_spec)
    violations: list[Violation] = []

    for c in crossings:
        if c.width != 1:
            continue
        if c.src_clock == UNCONSTRAINED_SENTINEL:
            continue  # CDC-011 owns crossings from unconstrained ports
        if c.dst_flop.cell.name in ctx.user_syncs:
            continue  # user vouches for the synchronizer shape
        if c.dst_flop.cell.name in ctx.user_handshakes:
            continue  # (* cdc_handshake *) — single capture under dst_valid is intended (#247)
        if c.src_flop is not None and c.src_flop.cell.name in ctx.user_statics:
            continue  # source is (* cdc_static *) — held constant, no metastability
        depth = _sync_chain_depth(
            module,
            c.dst_flop,
            c.dst_clock,
            ctx.domains,
            ctx.reader_counts,
            d_bit_to_single_bit_flop=ctx.d_bit_to_single_bit_flop,
        )
        if depth < 2:
            # Defer to CDC-014 when a follow-on flop *is* present but
            # behind a comb cell — the message "no second-stage" would
            # mislead the user into adding a stage they already have.
            if _chain_has_inter_stage_comb(c.dst_flop, ctx) is not None:
                continue
            src_desc = (
                f"flop {c.src_flop.name}" if c.src_flop is not None else c.src_name
            )
            violations.append(
                Violation(
                    rule_id="CDC-001",
                    severity="error",
                    message=(
                        f"unsynchronized control crossing "
                        f"{c.src_clock} → {c.dst_clock} "
                        f"(src: {src_desc}): destination flop "
                        f"{c.dst_flop.name} has no second-stage "
                        f"synchronizer (chain depth = {depth})"
                    ),
                    crossing=c,
                    cell_name=c.dst_flop.cell.name,
                )
            )
    return violations


def check_cdc_002(
    module: Module,
    crossings: list[Crossing],
    clock_spec: ClockSpec | None = None,  # noqa: ARG001
    required_depth: int = 2,
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """CDC-002 — Insufficient synchronizer depth.

    Fires when a control crossing has a synchronizer chain present
    (depth >= 2, so CDC-001 is satisfied) but shorter than the
    site-required minimum. The default ``required_depth`` of 2 keeps
    this rule silent unless a project explicitly raises the bar (e.g.
    3 stages for high-speed/low-MTBF designs).
    """
    if required_depth <= 2:
        return []
    if ctx is None:
        ctx = _build_context(module, clock_spec)
    violations: list[Violation] = []

    for c in crossings:
        if c.width != 1:
            continue
        if c.src_clock == UNCONSTRAINED_SENTINEL:
            continue  # CDC-011 owns crossings from unconstrained ports
        if c.dst_flop.cell.name in ctx.user_syncs:
            continue
        if c.src_flop is not None and c.src_flop.cell.name in ctx.user_statics:
            continue  # source is (* cdc_static *) — held constant, no metastability
        depth = _sync_chain_depth(
            module,
            c.dst_flop,
            c.dst_clock,
            ctx.domains,
            ctx.reader_counts,
            d_bit_to_single_bit_flop=ctx.d_bit_to_single_bit_flop,
        )
        if 2 <= depth < required_depth:
            violations.append(
                Violation(
                    rule_id="CDC-002",
                    severity="warning",
                    message=(
                        f"insufficient synchronizer depth on "
                        f"{c.src_clock} → {c.dst_clock} crossing: "
                        f"found {depth} flop(s), required >= {required_depth} "
                        f"(dst flop: {c.dst_flop.name})"
                    ),
                    crossing=c,
                    cell_name=c.dst_flop.cell.name,
                )
            )
    return violations


def check_cdc_003(
    module: Module,
    crossings: list[Crossing],
    clock_spec: ClockSpec | None = None,  # noqa: ARG001
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """CDC-003 — Combinational logic on the way to a synchronizer.

    Fires when a single-bit crossing reaches a destination synchronizer
    (chain depth >= 2, so CDC-001/-002 don't already cover it) but the
    path from the source flop to the synchronizer's first stage passes
    through one or more combinational cells (``min_hops >= 1``).

    The classic failure mode: two source-domain flops are combined by a
    gate (AND/OR/MUX) and the gate output is sampled by the
    synchronizer. The two source flops can transition on different src
    clock cycles, producing a glitch on the gate output that a 2FF
    synchronizer cannot reliably filter — the destination may sample
    a transient value that never existed as a real source state.
    """
    if ctx is None:
        ctx = _build_context(module, clock_spec)
    violations: list[Violation] = []

    for c in crossings:
        if c.width != 1:
            continue
        if c.min_hops < 1:
            continue
        if c.is_port_sourced:
            # CDC-006 already covers comb logic from a top-level port
            # to a synchronizer; don't double-fire.
            continue
        if c.dst_flop.cell.name in ctx.user_syncs:
            continue
        if c.src_flop is not None and c.src_flop.cell.name in ctx.user_statics:
            continue  # source is (* cdc_static *) — held constant, no metastability
        depth = _sync_chain_depth(
            module,
            c.dst_flop,
            c.dst_clock,
            ctx.domains,
            ctx.reader_counts,
            d_bit_to_single_bit_flop=ctx.d_bit_to_single_bit_flop,
        )
        if depth < 2:
            # CDC-001 covers this crossing already; don't double-fire.
            continue
        violations.append(
            Violation(
                rule_id="CDC-003",
                severity="error",
                message=(
                    f"combinational logic between source flop and "
                    f"synchronizer on {c.src_clock} → {c.dst_clock} "
                    f"crossing: {c.min_hops} cell(s) on path "
                    f"(src: {c.src_name}, "
                    f"sync first stage: {c.dst_flop.name})"
                ),
                crossing=c,
                cell_name=c.dst_flop.cell.name,
            )
        )
    return violations


# Single-input cells that pass each input bit through to the
# corresponding output bit (modulo polarity). Used by the CDC-004
# gating-shape detector to walk back from the destination flop's
# ``D`` pin to the originating mux through Yosys-inserted fanout
# buffers. Polarity is irrelevant because we're only locating the
# origin cell, not preserving signal value. Kept as a small local
# constant rather than reusing ``domain._BUFFER_TYPES`` because that
# set also includes reduce-style cells (``$reduce_bool``,
# ``$logic_not``) that aren't bit-aligned passthroughs on buses.
_BUS_BUFFER_TYPES: frozenset[str] = frozenset(
    {"$not", "$buf", "$pos", "$_BUF_", "$_NOT_"}
)
# Maximum number of transparent buffers tolerated between the gating
# mux and the destination flop's ``D`` pin. Two covers the realistic
# Yosys-inserted buffer count (typically zero or one); deeper chains
# are unusual and stay flagged so users get a review nudge.
_GATING_BUF_BUDGET = 2


def _trace_through_bus_buffers(
    module: Module,
    bit: Bit,
    drivers: dict[Bit, tuple[str, str, int]],
    max_hops: int = _GATING_BUF_BUDGET,
) -> tuple[str, str, int] | None:
    """Return the driver of ``bit`` after stepping through up to
    ``max_hops`` transparent single-input buffers.

    A "transparent" cell here is one of :data:`_BUS_BUFFER_TYPES`
    whose ``A`` input is bit-aligned with its ``Y`` output (same
    width, bit *i* of A produces bit *i* of Y). When the chain
    exceeds the budget, the function returns the last buffer's
    driver so the caller's cell-type check still runs and rejects
    cleanly — the originating mux is then one more hop away than
    we accept.
    """
    cur_bit: Bit = bit
    hops = 0
    while True:
        drv = drivers.get(cur_bit) if isinstance(cur_bit, int) else None
        if drv is None:
            return None
        cell_name, _port, idx = drv
        cell = module.cells[cell_name]
        if cell.type not in _BUS_BUFFER_TYPES:
            return drv
        if hops >= max_hops:
            return drv
        a_bits = cell.connections.get("A", ())
        y_bits = cell.connections.get("Y", ())
        if len(a_bits) != len(y_bits) or idx >= len(a_bits):
            return drv
        nxt = a_bits[idx]
        if not isinstance(nxt, int):
            return drv
        cur_bit = nxt
        hops += 1


def _is_gated_bus_crossing(
    module: Module,
    crossing: Crossing,
    domains: dict[str, str | None],
    drivers: dict[Bit, tuple[str, str, int]],
) -> bool:
    """Heuristic: the bus crossing is properly gated by a dst-domain
    handshake (so CDC-004 should not fire).

    Two shapes are accepted; both reduce to the same correctness
    argument — the destination only latches the bus when a dst-domain
    control signal allows it, so mid-flight values aren't captured:

    - **Mux-on-D**: the cell driving the destination flop's ``D`` is
      a ``$mux`` whose ``S`` (select) fanin sits entirely in the dst
      clock domain (the golden ``ip_cdc_handshake`` shape). Up to
      :data:`_GATING_BUF_BUDGET` transparent fanout buffers between
      the mux and ``D`` are tolerated — Yosys routinely inserts them
      after the flatten/opt passes.
    - **Dffe-EN**: the destination cell is itself a flop-with-enable
      from :data:`flops.FF_CELL_TYPES` (``$dffe`` / ``$sdffe`` /
      ``$adffe`` / etc.) whose ``EN`` pin fans in only from dst-domain
      flops. That's the "load on synced enable" idiom.

    A more rigorous check would also verify that the mux's
    hold-input is the destination flop's own ``Q`` (proper feedback
    hold), but we leave that as a follow-up — the current shape
    catches the bus crossing patterns we care about without
    false-positives on the golden fixture.
    """
    dst_clock = crossing.dst_clock
    dst_cell = crossing.dst_flop.cell

    # Shape 1: $dffe-style EN gating. Cheap to test (one cell-type
    # lookup plus one pin walk); try first so EN-gated designs skip
    # the more expensive driver-cell aggregation below.
    if is_ff_cell(dst_cell.type):
        en_bits = dst_cell.connections.get("EN", ())
        en_int_bits = tuple(b for b in en_bits if isinstance(b, int))
        if en_int_bits:
            en_fanin_flops = _backward_flop_fanin(module, en_int_bits, drivers)
            if en_fanin_flops and all(
                domains.get(name) == dst_clock for name in en_fanin_flops
            ):
                return True

    # Shape 2: mux-on-D, optionally through a short chain of
    # transparent buffers. All D bits must trace back to the same
    # origin cell — a bus split across multiple drivers can't be a
    # single gating mux.
    driver_cells: set[str] = set()
    for d_bit in crossing.dst_flop.d:
        if not isinstance(d_bit, int):
            continue
        drv = _trace_through_bus_buffers(module, d_bit, drivers)
        if drv is None:
            return False
        driver_cells.add(drv[0])
    if len(driver_cells) != 1:
        return False
    drv_cell = module.cells[next(iter(driver_cells))]
    if drv_cell.type != "$mux":
        return False
    s_bits = drv_cell.connections.get("S", ())
    if not s_bits:
        return False
    s_fanin_flops = _backward_flop_fanin(module, s_bits, drivers)
    if not s_fanin_flops:
        return False
    # Every flop reached in the select-fanin must be in the dst clock
    # domain. A single src-domain flop in there means the "gate" is
    # itself a cross-domain signal — not a valid handshake.
    return all(domains.get(name) == dst_clock for name in s_fanin_flops)


def _is_gray_encoded_source(
    module: Module,
    src_flop: Flop,
    drivers: dict[Bit, tuple[str, str, int]],
    max_back_hops: int = 8,
) -> bool:
    """Detect the canonical gray-encoding pattern in the source flop's
    fanin.

    A gray counter computes ``g = b ^ (b >> 1)`` where ``b`` is the
    binary value. ``b >> 1`` is a logical right-shift of one
    position, so the shifted operand's bit ``i`` is the unshifted
    operand's bit ``i+1``. After Yosys flattening the right-shift
    becomes pure wire-routing — the ``$xor`` cell sees::

        A = (b[0], b[1], ..., b[N-1])
        B = (b[1], b[2], ..., b[N-1], '0')   # MSB padded with constant

    We walk the source flop's D fanin backward through combinational
    cells (bounded by ``max_back_hops``) looking for any ``$xor``
    whose A/B pair satisfies ``A[i+1] == B[i]`` for ``i < N-1`` and
    ``B[N-1]`` is a constant. That signature is essentially unique to
    gray encoding and is what async-FIFO pointers produce.
    """
    seen: set[Bit] = set()
    frontier: list[tuple[Bit, int]] = [(b, 0) for b in src_flop.d if isinstance(b, int)]
    while frontier:
        nxt: list[tuple[Bit, int]] = []
        for bit, depth in frontier:
            if bit in seen or depth > max_back_hops:
                continue
            seen.add(bit)
            drv = drivers.get(bit)
            if drv is None:
                continue
            cell_name, port_name, _idx = drv
            if port_name == "Q":
                # Reached a flop — that's another register's output.
                # Don't traverse into a different domain's logic.
                continue
            cell = module.cells[cell_name]
            if cell.type == "$xor":
                a_bits = cell.connections.get("A", ())
                b_bits = cell.connections.get("B", ())
                n = len(a_bits)
                if (
                    n >= 2
                    and len(b_bits) == n
                    and all(
                        isinstance(a_bits[i + 1], int)
                        and isinstance(b_bits[i], int)
                        and a_bits[i + 1] == b_bits[i]
                        for i in range(n - 1)
                    )
                    and isinstance(b_bits[n - 1], str)
                ):
                    return True
            for in_port, in_bits in cell.connections.items():
                if in_port in _OUTPUT_PINS:
                    continue
                for b in in_bits:
                    if isinstance(b, int):
                        nxt.append((b, depth + 1))
        frontier = nxt
    return False


def _is_multibit_sync_first_stage(
    module: Module,
    dst_flop: Flop,
    dst_clock: str,
    domains: dict[str, str | None],
) -> bool:
    """Test whether ``dst_flop`` is the first stage of a multi-bit
    synchronizer chain.

    Canonical shape: a width-N flop whose ``Q`` drives the ``D`` of
    another width-N flop in the same clock domain, lane-for-lane (so
    ``Q[i] == nextflop.D[i]`` for every ``i``). This is exactly what
    ``ip_cdc_sync`` produces for ``WIDTH > 1`` after Yosys flatten —
    one multi-bit cell per stage, lane-aligned. Async-FIFO pointer
    crossings use this exact pattern with gray-coded data, which is
    safe because at most one bit changes per source cycle.

    Lane-wise alignment is what makes this *gray-friendly*: a generic
    multi-bit sync chain doesn't promise gray-coding in the source,
    but in practice the only well-known reason to wire one up is for
    a gray-coded crossing. We therefore accept it as the gray-code
    structural case; users who want stricter behaviour can keep
    asserting via ``set_clock_groups`` and the rule pass.
    """
    if len(dst_flop.q) < 2:
        return False
    width = len(dst_flop.q)
    if len(dst_flop.d) != width:
        return False
    q_bits = tuple(dst_flop.q)
    if not all(isinstance(b, int) for b in q_bits):
        return False
    for f in find_flops(module):
        if f.cell.name == dst_flop.cell.name:
            continue
        if domains.get(f.cell.name) != dst_clock:
            continue
        if len(f.d) != width:
            continue
        if tuple(f.d) == q_bits:
            return True
    return False


def check_cdc_004(
    module: Module,
    crossings: list[Crossing],
    clock_spec: ClockSpec | None = None,  # noqa: ARG001
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """CDC-004 — Multi-bit bus crossing without gating or gray-coding.

    A multi-bit data path that crosses clock domains needs an extra
    coherence mechanism on top of per-bit synchronization, because
    individual bits can settle on different destination cycles.
    Three patterns are accepted:

    - **Handshake / load-enable gating** — destination flops only
      sample the bus when a synchronized control signal allows it
      (the golden ``ip_cdc_handshake`` shape).
    - **Gray-coded crossing into a multi-bit sync chain** — the
      destination is itself a multi-bit synchronizer (e.g. async-FIFO
      pointer sync), so each lane is independently filtered for
      metastability. This is correct iff the source actually toggles
      one bit at a time; gray-coded counters guarantee that.
    - **User-asserted gray-coding** — the source wire is annotated
      ``(* cdc_gray *)`` (or ``(* gray_code *)``), telling the
      analyzer to trust the gray-counter promise even when the
      structural detector can't see the sync chain.

    Port-sourced bus crossings (``src_flop is None``) can be cleared
    only by the gating pattern. Both gray-coding paths key off a
    source register — the structural detector pattern-matches the
    canonical ``g = b ^ (b >> 1)`` shape in the source flop's fanin,
    and the user-assertion path consults the source flop's attribute
    set — so neither has a port-side equivalent. Typed top-level
    buses crossing an async boundary must either be gated by a
    synchronized load enable or be waived.
    """
    if ctx is None:
        ctx = _build_context(module, clock_spec)
    violations: list[Violation] = []

    for c in crossings:
        if c.width <= 1:
            continue
        if c.src_flop is not None and c.src_flop.cell.name in ctx.user_grays:
            # Explicit user assertion of gray-coding.
            continue
        if c.src_flop is not None and c.src_flop.cell.name in ctx.user_statics:
            # Explicit user assertion of quasi-static source: the bus
            # is held constant during the operating window, so dst-side
            # sampling is coherent regardless of width.
            continue
        if (
            c.src_flop is not None
            and _is_multibit_sync_first_stage(
                module, c.dst_flop, c.dst_clock, ctx.domains
            )
            and _is_gray_encoded_source(module, c.src_flop, ctx.bit_drivers)
        ):
            # Structural gray-coded crossing: source has the canonical
            # gray-encode XOR pattern AND the destination is a
            # multi-bit synchronizer chain. This is the async-FIFO
            # pointer shape and is correct by construction.
            continue
        if _is_gated_bus_crossing(module, c, ctx.domains, ctx.bit_drivers):
            continue
        violations.append(
            Violation(
                rule_id="CDC-004",
                severity="error",
                message=(
                    f"unprotected bus crossing on {c.src_clock} → "
                    f"{c.dst_clock}: {c.width}-bit path with no "
                    f"recognized gating or gray-coding "
                    f"(src: {c.src_name}, "
                    f"dst flop: {c.dst_flop.name})"
                ),
                crossing=c,
                cell_name=c.dst_flop.cell.name,
            )
        )
    return violations


def check_cdc_005(
    module: Module,
    crossings: list[Crossing],
    clock_spec: ClockSpec | None = None,  # noqa: ARG001
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """CDC-005 — Reconvergent synchronizers.

    Fires when a single source-domain flop fans out to two or more
    *independent* synchronizer chains in the same destination domain
    AND the synchronized outputs reconverge downstream. Each chain
    individually filters metastability, but their resolution times
    can differ by a destination cycle, so any downstream logic that
    recombines the synchronized outputs may observe a value pair
    that was never simultaneously true at the source.

    Phase 2 (issue #33) filters out groups whose forward cones don't
    intersect — having redundant synchronizers without a
    recombination point isn't itself a bug, just a code smell, and
    pure-structural reporting on it was the rule's biggest
    false-positive source.
    """
    if ctx is None:
        ctx = _build_context(module, clock_spec)
    violations: list[Violation] = []

    # Group single-bit, properly synchronized crossings by source flop
    # + destination domain. We require depth>=2 so we don't double-fire
    # against CDC-001 (the depth==1 cases will be reported there).
    grouped: dict[tuple[str, str], list[Crossing]] = defaultdict(list)
    for c in crossings:
        if c.width != 1:
            continue
        if c.src_flop is None:
            # Reconvergence is defined by a single source flop fanning
            # out to multiple sync chains; port sources don't have the
            # metastability-resolution behaviour the rule is testing.
            continue
        depth = _sync_chain_depth(
            module,
            c.dst_flop,
            c.dst_clock,
            ctx.domains,
            ctx.reader_counts,
            d_bit_to_single_bit_flop=ctx.d_bit_to_single_bit_flop,
        )
        if depth < 2:
            continue
        grouped[(c.src_flop.cell.name, c.dst_clock)].append(c)

    for (src_name, dst_clk), group in grouped.items():
        if len(group) < 2:
            continue
        # Reconvergence filter (issue #33): walk forward from each
        # chain's *terminal* flop's Q and look for any downstream cell
        # reached by ≥2 chains. The walk uses
        # :func:`_forward_reachable_cells` so a recombination point
        # that isn't itself a flop (e.g. a comb reduction driving an
        # unregistered top-level port) still counts. Chain-internal
        # flops are excluded so chains never "reconverge on
        # themselves" — only genuinely downstream cells count. If no
        # downstream cell is reached by 2+ chains, the group's
        # redundancy is harmless and we don't fire.
        appears_in: dict[str, int] = defaultdict(int)
        for c in group:
            chain = _sync_chain_flops(
                module,
                c.dst_flop,
                c.dst_clock,
                ctx.domains,
                ctx.reader_counts,
                ctx.d_bit_to_single_bit_flop,
            )
            terminal = chain[-1]
            chain_internal = {f.cell.name for f in chain}
            reached = _forward_reachable_cells(
                module,
                start_bits=terminal.q,
                consumers=ctx.bit_consumers,
            )
            for cell_name in reached - chain_internal:
                appears_in[cell_name] += 1
        if not any(count >= 2 for count in appears_in.values()):
            continue

        dst_names = sorted(c.dst_flop.cell.name for c in group)
        violations.append(
            Violation(
                rule_id="CDC-005",
                severity="warning",
                message=(
                    f"reconvergent synchronizers: source flop "
                    f"{src_name} drives {len(group)} independent sync "
                    f"chains in domain {dst_clk} (dst flops: "
                    f"{', '.join(dst_names)}); independent metastability "
                    f"resolution can produce mismatched synchronized "
                    f"values when these outputs recombine"
                ),
                crossing=group[0],
                cell_name=src_name,
            )
        )
    return violations


def check_cdc_006(
    module: Module,
    crossings: list[Crossing],  # noqa: ARG001
    clock_spec: ClockSpec | None = None,
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """CDC-006 — Glitchy combinational source on a control crossing.

    Fires when a synchronizer first stage's ``D`` pin traces back —
    through combinational logic only — to one or more top-level input
    ports without an intervening source-domain flop. The synchronizer
    can therefore sample a transient comb-output value that never
    represented a stable input state.

    A "synchronizer first stage" here is a flop with chain depth >= 2
    in its own clock domain (so CDC-001 doesn't already cover the
    case). Top-level ports declared as clocks in the SDC are
    intentionally ignored — they aren't logic data, just a clock
    signal whose level is irrelevant to data correctness.
    """
    if ctx is None:
        ctx = _build_context(module, clock_spec)
    violations: list[Violation] = []

    clock_ports: set[str] = set()
    if clock_spec is not None:
        for clk in clock_spec.clocks.values():
            clock_ports.update(clk.ports)

    for f in ctx.flops:
        my_clk = ctx.domains.get(f.cell.name)
        if my_clk is None:
            continue
        # User-marked synchronizers are explicitly trusted regardless
        # of the input shape.
        if f.cell.name in ctx.user_syncs:
            continue
        # We only fire on flops that are bona fide synchronizer first
        # stages — i.e. the chain on the destination side is >= 2.
        depth = _sync_chain_depth(
            module,
            f,
            my_clk,
            ctx.domains,
            ctx.reader_counts,
            d_bit_to_single_bit_flop=ctx.d_bit_to_single_bit_flop,
        )
        if depth < 2:
            continue
        fanin_flops, fanin_ports = _backward_fanin(module, f.d, ctx.bit_drivers)
        # If a real source-domain flop exists in the fanin, this
        # crossing is already covered by CDC-001/-002/-003 logic.
        if fanin_flops:
            continue
        # Drop ports that are top-level clocks (clock leakage into
        # data paths is a CDC-008 shape, not CDC-006) and ports that
        # the SDC has explicitly typed into the *same* domain as the
        # destination flop via `set_input_delay -clock <my_clk>`.
        my_clock_name: str | None = None
        if clock_spec is not None:
            my_clock_name = clock_spec.clock_for_port(my_clk) or my_clk
        flagged_ports: list[tuple[str, str | None]] = []
        for p in sorted(fanin_ports):
            if p in clock_ports:
                continue
            port_clk: str | None = None
            if clock_spec is not None:
                port_clk = clock_spec.port_clock.get(p)
            # Unconstrained ports belong to CDC-011, not CDC-006. The
            # right fix for them is SDC typing, not "register the
            # source"; firing CDC-006 here would push the wrong
            # remediation.
            if port_clk == UNCONSTRAINED_SENTINEL:
                continue
            if (
                port_clk is not None
                and my_clock_name is not None
                and clock_spec is not None
            ):
                # Both ends typed: only fire when the port's clock
                # resolves to a different root than the sync's clock.
                # If they match, the user has asserted same-domain
                # timing — no CDC issue.
                if clock_spec.resolve(port_clk) == clock_spec.resolve(my_clock_name):
                    continue
            flagged_ports.append((p, port_clk))
        if not flagged_ports:
            continue
        port_descs = ", ".join(
            f"{p}" if pc is None else f"{p} (clock={pc})" for p, pc in flagged_ports
        )
        violations.append(
            Violation(
                rule_id="CDC-006",
                severity="error",
                message=(
                    f"glitchy combinational source on synchronizer "
                    f"{f.cell.name} (clk={my_clk}): D pin is driven "
                    f"by combinational logic with no registering flop, "
                    f"reaching unregistered top-level port(s): "
                    f"{port_descs}"
                ),
                cell_name=f.cell.name,
            )
        )
    return violations


def _clock_network_cells(
    module: Module,
    drivers: dict[Bit, tuple[str, str, int]],
) -> set[str]:
    """Cells whose output transitively drives some flop's ``CLK`` pin.

    These cells form the legitimate clock-distribution network
    (buffers, ICGs, clock muxes, dividers, etc.); reading a clock
    signal on one of their input pins is *expected*, not a bug.
    Used by CDC-008 to suppress false positives on intentional clock
    gating / muxing structures.
    """
    cells: set[str] = set()
    seen_bits: set[Bit] = set()
    frontier: list[Bit] = []
    for f in find_flops(module):
        if isinstance(f.clk, int):
            frontier.append(f.clk)
    while frontier:
        nxt: list[Bit] = []
        for bit in frontier:
            if bit in seen_bits:
                continue
            seen_bits.add(bit)
            drv = drivers.get(bit)
            if drv is None:
                continue
            cell_name, _out_port, _idx = drv
            if cell_name in cells:
                continue
            cells.add(cell_name)
            cell = module.cells[cell_name]
            for in_port, in_bits in cell.connections.items():
                if in_port in _OUTPUT_PINS:
                    continue
                for b in in_bits:
                    if isinstance(b, int):
                        nxt.append(b)
        frontier = nxt
    return cells


# Fallback clock-pin name allow-list for CDC-008's per-pin blackbox
# exemption (FIX 4) when the caller did not supply a traced
# ``boundary_clock_pins`` map. Mirrors ``abstract._CLOCK_PIN_NAMES``;
# kept local so the rule pack stays import-light.
_BLACKBOX_CLOCK_PIN_NAMES: frozenset[str] = frozenset(
    {"CLK", "clk", "C", "clock", "clk_i"}
)


def check_cdc_008(
    module: Module,
    crossings: list[Crossing],  # noqa: ARG001
    clock_spec: ClockSpec | None = None,
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """CDC-008 — Clock signal used as data.

    Fires when a bit that drives any flop's ``CLK`` pin (or that's
    declared as a clock in the SDC) is also wired into a non-clock
    pin on a cell that isn't part of the clock-distribution network.

    Clocks ride on dedicated low-skew networks; sampling them as data
    delivers the high-frequency edge toggle, breaks STA, and almost
    always indicates a wiring mistake. The CLK pin itself, cells on
    the clock network (buffers, ICGs, clock muxes, dividers — anything
    whose output transitively drives a flop CLK), and top-level
    *output* ports (forwarding a clock off-chip) are intentionally
    not flagged.
    """
    if ctx is None:
        ctx = _build_context(module, clock_spec)
    violations: list[Violation] = []

    # Build the set of net bits that act as clocks: union of every
    # flop's CLK pin and every bit covered by an SDC create_clock
    # port.
    clock_bits: set[Bit] = set()
    for f in ctx.flops:
        if isinstance(f.clk, int):
            clock_bits.add(f.clk)
    if clock_spec is not None:
        for clk in clock_spec.clocks.values():
            for port_name in clk.ports:
                port = module.ports.get(port_name)
                if port is None:
                    continue
                for b in port.bits:
                    if isinstance(b, int):
                        clock_bits.add(b)
    if not clock_bits:
        return violations

    # Map each clock bit to a human-readable label, preferring the
    # top-level port name where applicable.
    def _label(bit: Bit) -> str:
        port = module.port_of_bit(bit)
        return port.name if port is not None else f"net@{bit}"

    # Compute the clock-distribution network once: every cell whose
    # output eventually feeds some flop CLK is exempt.
    clock_net_cells = _clock_network_cells(module, ctx.bit_drivers)

    # Walk every cell connection and report each (clock_bit, cell, pin)
    # triple where the bit appears on a non-CLK input pin. We dedupe
    # at the (bit, cell, pin) granularity so a multi-bit pin reporting
    # the same clock once doesn't blow up to N violations.
    # FIX 4 (soundness audit of #259): the CDC-008 exemption for a
    # blackbox boundary instance is per-CLOCK-PIN, not per-instance. A
    # clock on the instance's clock pin is legitimate distribution into
    # the opaque subtree, but a clock wired into a genuine DATA input of
    # the blackbox is still a clock-as-data bug the flattened design
    # would report — so only the traced clock pins are exempt.
    is_blackbox_inst = {
        cell.name: (
            cell.type in ctx.boundary_modules or cell.type in ctx.blackbox_modules
        )
        for cell in module.cells.values()
    }
    seen: set[tuple[Bit, str, str]] = set()
    for cell in module.cells.values():
        if cell.name in clock_net_cells:
            continue
        # Per-instance clock-pin exemption set for a blackbox boundary
        # cell. When the caller supplied a traced map (``boundary_clock_pins``)
        # we use the instance's entry; otherwise we fall back to the name
        # allow-list so legacy callers that exempt by module type still
        # behave (whole-instance exemption is *retired* — a data pin now
        # fires regardless of how the instance was abstracted).
        bb_clock_pins: frozenset[str] | None = None
        if is_blackbox_inst.get(cell.name):
            bb_clock_pins = ctx.boundary_clock_pins.get(cell.name)
            if bb_clock_pins is None:
                bb_clock_pins = _BLACKBOX_CLOCK_PIN_NAMES
        # Gate-level FF cells ($_DFF_*, $_DFFE_*, etc.) carry the
        # clock on pin ``C`` instead of ``CLK``; exempt that pin
        # too when the cell is a flop. Without this exemption,
        # CDC-008 false-fires on every gate-level flop in the
        # netlist (see rtl-buddy-cdc#194).
        ff_cell = is_ff_cell(cell.type)
        for port_name, bits in cell.connections.items():
            if port_name == "CLK" or port_name in _OUTPUT_PINS:
                continue
            if ff_cell and port_name == "C":
                continue
            if bb_clock_pins is not None and port_name in bb_clock_pins:
                # This blackbox pin is a clock pin — distribution, not
                # data. A non-clock (data) pin falls through and fires.
                continue
            for idx, b in enumerate(bits):
                if not isinstance(b, int) or b not in clock_bits:
                    continue
                key = (b, cell.name, port_name)
                if key in seen:
                    continue
                seen.add(key)
                violations.append(
                    Violation(
                        rule_id="CDC-008",
                        severity="error",
                        message=(
                            f"clock signal {_label(b)} used as data: "
                            f"connected to {cell.name}.{port_name}[{idx}] "
                            f"(cell type {cell.type})"
                        ),
                        cell_name=cell.name,
                    )
                )
    return violations


# Explicit control-pin map for CDC-010. Covers the cell types whose
# control-pin name is fixed by Yosys's own definitions:
#
# - Higher-level parametric cells (phase 1): ``$mux``, ``$dffe``,
#   ``$dlatch``.
# - Yosys gate-level primitives emitted by ``simplemap`` / ``abc``
#   (phase 3): ``$_DLATCH_*``, ``$_MUX_`` family, ``$_DFFE_*`` /
#   ``$_SDFFE_*`` families. The ``$_DFFE`` / ``$_SDFFE`` / ``$_DLATCH``
#   prefixes are handled by the prefix paths below so we don't have
#   to enumerate every polarity / reset-shape variant.
#
# Vendor library cells (``ICG_*``, ``CKMUX2_*``) are intentionally
# *not* enumerated here — the namespace is too large and varies per
# library. The heuristic fallback below catches the common
# enable-style names; vendor-specific outliers should be added here
# in a follow-up rather than expanding the heuristic.
_CDC_010_CONTROL_PINS: dict[str, frozenset[str]] = {
    # Higher-level Yosys cells (phase 1).
    "$mux": frozenset({"S"}),
    "$dffe": frozenset({"EN"}),
    "$dlatch": frozenset({"EN"}),
    # Gate-level mux family. Yosys's ``$_MUX{4,8,16}_`` carry their
    # selects on consecutive letters starting at ``S``.
    "$_MUX_": frozenset({"S"}),
    "$_MUX4_": frozenset({"S", "T"}),
    "$_MUX8_": frozenset({"S", "T", "U"}),
    "$_MUX16_": frozenset({"S", "T", "U", "V"}),
}

# Case-insensitive input-pin names treated as control pins for cells
# that aren't in the explicit map. Conservative — these are the
# enable-style names that almost always indicate an ICG / clock-gate
# control input on tech-mapped library cells. Mux-style select names
# (``S`` / ``SEL`` / numbered variants) are *not* heuristic targets:
# they collide with too many non-control pins on unrelated cells, so
# mux shapes have to be in the explicit map above.
#
# Rationale for each:
#   ``E``     — Yosys gate-level latch / enable-flop convention
#               (``$_DLATCH_P_.E``, ``$_DFFE_*.E``).
#   ``EN``    — most-common SV / library enable-pin name.
#   ``CE``    — clock-enable (``CE`` = "Clock Enable"), classic
#               flip-flop and ICG nomenclature.
#   ``GATE``  — ICG library cells (e.g. ``GATE`` on TSMC-style ICGs).
#   ``SE``    — scan-enable, often wired into a clock gate's enable
#               path on tech-mapped designs (DFT bypass).
_CDC_010_HEURISTIC_PINS: frozenset[str] = frozenset({"E", "EN", "CE", "GATE", "SE"})


def _control_pins_for(cell: Cell, *, use_heuristic: bool = True) -> frozenset[str]:
    """Control-pin names for a clock-network cell.

    A *control pin* is a non-clock input whose transition can chop
    the cell's output clock — the select on a clock mux (``$mux.S``,
    ``$_MUX_.S``), the enable on an integrated clock gate
    (``$dffe.EN``, ``$dlatch.EN``, ``$_DLATCH_P_.E``,
    ``$_DFFE_*.E``), or the analogous pin on a tech-mapped library
    cell.

    Resolution order:

    1. **Explicit map** (:data:`_CDC_010_CONTROL_PINS`) for
       higher-level Yosys cells (phase 1) and the gate-level mux
       family (phase 3).
    2. **Prefix path** for the Yosys gate-level latch and
       enable-flop families: any cell type starting with
       ``$_DLATCH``, ``$_DFFE_``, or ``$_SDFFE_`` reports ``E`` as
       the control pin. Avoids enumerating the polarity-/reset-/
       set-shape variant explosion (``$_DFFE_PP0P_`` and friends).
    3. **Heuristic fallback** (when ``use_heuristic`` is True, the
       default): scan the cell's input pins for names matching
       :data:`_CDC_010_HEURISTIC_PINS` case-insensitively. Designed
       to catch the enable-style pin names common to standard-cell
       ICG library cells (``ICG_*``, vendor-specific names) without
       needing a per-library cell map.

    Buffers / inverters / AND-tree gates ($buf / $not / $_AND_ /
    …) fall through every step and return the empty set — their
    output is a function of all inputs, none of which gate the
    clock-network signal in a way that produces a glitch when
    transitioned mid-cycle.
    """
    explicit = _CDC_010_CONTROL_PINS.get(cell.type)
    if explicit is not None:
        return explicit
    # Yosys gate-level latch / enable-flop families. Cover the
    # prefix once instead of enumerating $_DFFE_PP0P_, $_DFFE_NP1N_,
    # etc. — every variant carries the enable on ``E``.
    if (
        cell.type.startswith("$_DLATCH")
        or cell.type.startswith("$_DFFE_")
        or cell.type.startswith("$_SDFFE_")
    ):
        return frozenset({"E"})
    if not use_heuristic:
        return frozenset()
    matches: set[str] = set()
    for port_name, bits in cell.connections.items():
        if port_name in _OUTPUT_PINS:
            continue
        if not bits:
            continue
        if port_name.upper() in _CDC_010_HEURISTIC_PINS:
            matches.add(port_name)
    return frozenset(matches)


def _clock_input_domains_for(
    module: Module,
    cell: Cell,
    ctx: _RuleContext,
    clock_spec: ClockSpec | None,
    control_ports: frozenset[str],
) -> set[str]:
    """Set of clock-domain names that drive ``cell``'s non-control inputs.

    Walks backward through combinational cells from every non-control,
    non-output input pin and collects:

    * the clock domain of any flop whose ``Q`` is reached (via
      ``ctx.domains``); and
    * the SDC clock name of any top-level clock port reached directly.

    Empty set means none of the non-control inputs trace back to a
    classifiable clock source — the rule treats that as "can't prove
    async-ness" and stays silent, matching the false-negative-biased
    posture of CDC-009 / CDC-011.
    """
    domains: set[str] = set()
    seen: set[Bit] = set()
    frontier: list[tuple[Bit, int]] = []
    for port_name, bits in cell.connections.items():
        if port_name in _OUTPUT_PINS or port_name in control_ports:
            continue
        for b in bits:
            if isinstance(b, int):
                frontier.append((b, 0))

    max_depth = 12
    while frontier:
        nxt: list[tuple[Bit, int]] = []
        for bit, depth in frontier:
            if bit in seen:
                continue
            seen.add(bit)
            drv = ctx.bit_drivers.get(bit)
            if drv is None:
                if clock_spec is None:
                    continue
                port = module.port_of_bit(bit)
                if port is None:
                    continue
                clk = clock_spec.clock_for_port(port.name)
                if clk is not None:
                    domains.add(clk)
                continue
            src_cell_name, out_port, _idx = drv
            if out_port == "Q":
                src_clk = ctx.domains.get(src_cell_name)
                if src_clk is not None:
                    domains.add(src_clk)
                continue
            if depth >= max_depth:
                continue
            src_cell = module.cells[src_cell_name]
            for in_port, in_bits in src_cell.connections.items():
                if in_port in _OUTPUT_PINS:
                    continue
                for b in in_bits:
                    if isinstance(b, int):
                        nxt.append((b, depth + 1))
        frontier = nxt
    return domains


def check_cdc_010(
    module: Module,
    crossings: list[Crossing],  # noqa: ARG001 — flop-D-pin enumeration doesn't apply
    clock_spec: ClockSpec | None = None,
    *,
    ctx: _RuleContext | None = None,
    use_heuristic: bool = True,
) -> list[Violation]:
    """CDC-010 — Glitch on the clock network from a wrong-domain control signal.

    Dual of :func:`check_cdc_008`. CDC-008 fires when a clock arrives
    on a data pin; CDC-010 fires when the *control* pin of a clock-
    network cell (a clock mux's ``S``, an ICG's ``EN``) is driven by
    a flop in a clock domain asynchronous to every one of the cell's
    own clock inputs. The async control transition chops the output
    clock — every downstream flop sees a runt edge and the damage is
    not recoverable by any synchronizer at the sink.

    Detection is structural: walk every cell in
    :func:`_clock_network_cells`, take each control pin from
    :func:`_control_pins_for`, backward-walk the control bits via
    :func:`_backward_flop_fanin`, and compare each source flop's
    domain against the cell's own clock-input domains from
    :func:`_clock_input_domains_for`. Fires when the source domain
    is async to *every* clock-input domain — a control flop sharing
    one of the gated clocks is fine, and an SDC ``set_clock_groups``
    declaring the pair synchronous suppresses naturally via
    :meth:`ClockSpec.are_async`.

    Coverage:

    - Yosys higher-level cells (``$mux.S``, ``$dffe.EN``,
      ``$dlatch.EN``) and the gate-level cells emitted by
      ``simplemap`` / ``abc`` (``$_MUX_`` / ``$_MUX{4,8,16}_``,
      ``$_DLATCH_*``, ``$_DFFE_*``, ``$_SDFFE_*``) — see the
      explicit map and prefix paths in :func:`_control_pins_for`.
    - Tech-mapped library cells via a conservative pin-name
      heuristic (``E`` / ``EN`` / ``CE`` / ``GATE`` / ``SE``).
      Pass ``use_heuristic=False`` (CLI:
      ``--cdc-010-no-heuristic``) to disable when the heuristic
      false-positives on a library with conflicting pin naming.

    Severity is ``error``: the glitch propagates to every downstream
    flop and cannot be recovered at the sink.
    """
    if ctx is None:
        ctx = _build_context(module, clock_spec)
    violations: list[Violation] = []

    def _async(a: str, b: str) -> bool:
        if clock_spec is None:
            return a != b
        ca = clock_spec.clock_for_port(a) or a
        cb = clock_spec.clock_for_port(b) or b
        return clock_spec.are_async(ca, cb)

    clock_net_cells = _clock_network_cells(module, ctx.bit_drivers)
    seen: set[tuple[str, str, str]] = set()

    for cell_name in sorted(clock_net_cells):
        cell = module.cells[cell_name]
        control_ports = _control_pins_for(cell, use_heuristic=use_heuristic)
        if not control_ports:
            continue
        cell_clock_domains: set[str] | None = None
        for ctrl_port in sorted(control_ports):
            ctrl_bits = cell.connections.get(ctrl_port, ())
            if not ctrl_bits:
                continue
            if ctx.user_glitchless_mux_bits and any(
                isinstance(b, int) and b in ctx.user_glitchless_mux_bits
                for b in ctrl_bits
            ):
                # User-vouched glitchless mux: the cross-coupled-latch
                # shape around this select makes an async select safe;
                # the rule's standard "synchronise the select" fix
                # advice would actually break the glitchless property.
                continue
            ctrl_fanin = _backward_flop_fanin(module, ctrl_bits, ctx.bit_drivers)
            if not ctrl_fanin:
                # Control driven by a top-level port or a constant —
                # same posture RDC-001 takes for ARST sourced from ports.
                continue
            if cell_clock_domains is None:
                cell_clock_domains = _clock_input_domains_for(
                    module, cell, ctx, clock_spec, control_ports
                )
            if not cell_clock_domains:
                # Cell's own clock-input domains aren't classifiable
                # (e.g. inputs sit behind unmapped library cells). Stay
                # silent rather than fire blind.
                continue
            for src_flop in sorted(ctrl_fanin):
                src_clk = ctx.domains.get(src_flop)
                if src_clk is None:
                    continue
                if src_clk in cell_clock_domains:
                    continue
                if not all(_async(src_clk, d) for d in cell_clock_domains):
                    continue
                key = (cell_name, ctrl_port, src_flop)
                if key in seen:
                    continue
                seen.add(key)
                violations.append(
                    Violation(
                        rule_id="CDC-010",
                        severity="error",
                        message=(
                            f"clock-network cell {cell_name} "
                            f"({cell.type}) control pin {ctrl_port} is "
                            f"driven by flop {src_flop} in domain "
                            f"{src_clk}, asynchronous to the cell's "
                            f"clock-input domain(s) "
                            f"({sorted(cell_clock_domains)}); an async "
                            f"control transition can chop the output "
                            f"clock into runt pulses on every "
                            f"downstream flop. Synchronize {src_flop} "
                            f"into one of the gated-clock domains, or "
                            f"use a glitch-free clock-mux library cell."
                        ),
                        cell_name=cell_name,
                    )
                )
    return violations


def check_rdc_001(
    module: Module,
    crossings: list[Crossing],  # noqa: ARG001
    clock_spec: ClockSpec | None = None,
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """RDC-001 — Reset crossing without a reset synchronizer.

    Renamed from CDC-007 in #107 as the rule joins the RDC (Reset
    Domain Crossing) family. Existing waivers written against
    ``CDC-007`` continue to suppress via the alias map in
    :mod:`rtl_buddy_cdc.waivers`.

    Fires when a flop's asynchronous reset pin (``ARST``) is driven by
    another flop sitting in a *different* asynchronous clock domain.
    The classic safe pattern is "async-assert, sync-deassert": the
    reset is asserted immediately but its deassertion is filtered
    through a 2FF chain on the destination clock — and crucially, the
    reset signal itself originates from a top-level pin or from a
    reset synchronizer in the *destination* domain, not from a flop in
    a foreign domain.

    Reset signals coming from top-level ports are intentionally
    accepted (they're the user's responsibility to drive correctly,
    e.g. the reset synchronizer's ARST input).
    """
    if ctx is None:
        ctx = _build_context(module, clock_spec)
    violations: list[Violation] = []

    def _async(a: str, b: str) -> bool:
        # If the user supplied an SDC, defer to its clock-groups
        # statement. Without an SDC every distinct domain is treated
        # as asynchronous (conservative — surfaces possible issues).
        if clock_spec is None:
            return a != b
        ca = clock_spec.clock_for_port(a) or a
        cb = clock_spec.clock_for_port(b) or b
        return clock_spec.are_async(ca, cb)

    # Collect ARST → (foreign source flop) edges. Group by
    # (src_flop_name, dst_clk) so a single async source feeding many
    # destination flops in the same domain becomes ONE violation that
    # lists every destination — matches how a real reset distribution
    # tree is reviewed.
    edges: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for f in ctx.flops:
        arst_bits = f.cell.connections.get("ARST", ())
        if not arst_bits:
            continue
        my_clk = ctx.domains.get(f.cell.name)
        if my_clk is None:
            continue
        fanin_flops = _backward_flop_fanin(module, arst_bits, ctx.bit_drivers)
        for src_name in fanin_flops:
            src_clk = ctx.domains.get(src_name)
            if src_clk is None or src_clk == my_clk:
                continue
            if not _async(my_clk, src_clk):
                continue
            edges[(src_name, src_clk, my_clk)].append(f.cell.name)

    for (src_name, src_clk, dst_clk), dst_flops in edges.items():
        dsts = sorted(set(dst_flops))
        # Choose representative cell for source-location reporting:
        # the first destination (alphabetically) — the source flop's
        # location would also work, but the destination is what the
        # user has to fix.
        repr_cell = dsts[0]
        if len(dsts) == 1:
            dst_desc = f"destination flop: {dsts[0]}"
        else:
            dst_desc = (
                f"{len(dsts)} destination flops share this source "
                f"(reset distribution tree): "
                f"{', '.join(dsts[:3])}" + (", ..." if len(dsts) > 3 else "")
            )
        violations.append(
            Violation(
                rule_id="RDC-001",
                severity="error",
                message=(
                    f"async reset crossing: flop(s) in clk={dst_clk} "
                    f"have ARST driven by flop {src_name} from a "
                    f"different domain (clk={src_clk}); add a reset "
                    f"synchronizer in the {dst_clk} domain. {dst_desc}"
                ),
                cell_name=repr_cell,
            )
        )
    return violations


def check_rdc_003(
    module: Module,
    crossings: list[Crossing],  # noqa: ARG001
    clock_spec: ClockSpec | None = None,
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """RDC-003 — Sync reset crossing without a reset synchroniser.

    Fires when a flop's synchronous reset pin (``SRST``) is driven —
    directly or through combinational logic — by a flop sitting in a
    different asynchronous clock domain. A sync reset is sampled on
    the destination clock's rising edge; if the upstream signal lives
    in a foreign async clock domain the sample can be metastable on
    the cycle the source flop changes.

    Detection is the SRST analogue of RDC-001's ARST walk: backward
    fanin from the ``SRST`` pin through combinational cells; any
    reached flop in a different async clock domain produces an edge.
    Edges are grouped by ``(src_flop, src_clk, dst_clk)`` so a single
    foreign-domain source feeding many sync-reset consumers becomes
    one finding (mirroring RDC-001's reset-tree grouping).

    The classic fix is a 2FF reset synchroniser in the destination
    clock domain between the foreign source and the consumer; that
    breaks the cross-domain path so the immediate ``SRST`` driver is
    a same-domain flop.
    """
    if ctx is None:
        ctx = _build_context(module, clock_spec)

    def _async(a: str, b: str) -> bool:
        if clock_spec is None:
            return a != b
        ca = clock_spec.clock_for_port(a) or a
        cb = clock_spec.clock_for_port(b) or b
        return clock_spec.are_async(ca, cb)

    edges: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for f in ctx.flops:
        srst_bits = f.cell.connections.get("SRST", ())
        if not srst_bits:
            continue
        my_clk = ctx.domains.get(f.cell.name)
        if my_clk is None:
            continue
        fanin_flops = _backward_flop_fanin(module, srst_bits, ctx.bit_drivers)
        for src_name in fanin_flops:
            src_clk = ctx.domains.get(src_name)
            if src_clk is None or src_clk == my_clk:
                continue
            if not _async(my_clk, src_clk):
                continue
            edges[(src_name, src_clk, my_clk)].append(f.cell.name)

    violations: list[Violation] = []
    for (src_name, src_clk, dst_clk), dst_flops in edges.items():
        dsts = sorted(set(dst_flops))
        repr_cell = dsts[0]
        if len(dsts) == 1:
            dst_desc = f"destination flop: {dsts[0]}"
        else:
            dst_desc = (
                f"{len(dsts)} destination flops share this source "
                f"(reset distribution tree): "
                f"{', '.join(dsts[:3])}" + (", ..." if len(dsts) > 3 else "")
            )
        violations.append(
            Violation(
                rule_id="RDC-003",
                severity="error",
                message=(
                    f"sync reset crossing: flop(s) in clk={dst_clk} have "
                    f"SRST driven by flop {src_name} from a different "
                    f"async domain (clk={src_clk}); the sync reset is "
                    f"sampled on the destination clock and the cross-"
                    f"domain source may produce metastability. Add a 2FF "
                    f"reset synchroniser in the {dst_clk} domain. "
                    f"{dst_desc}"
                ),
                cell_name=repr_cell,
            )
        )
    return violations


def check_rdc_004(
    module: Module,
    crossings: list[Crossing],  # noqa: ARG001
    clock_spec: ClockSpec | None = None,
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """RDC-004 — Reset driven by combinational logic with no synchroniser.

    Fires when a flop's async reset pin is driven by combinational
    logic (an ``$and`` / ``$or`` / ``$mux`` / etc. output, not a
    flop's ``Q`` and not a top-level port), and the comb's backward
    fanin reaches one or more flops. Comb-gate outputs can glitch
    when the inputs transition near-simultaneously; on an async
    reset pin the transient looks like a real reset assertion.

    Detection:

    * Consumer must be async-reset (``$adff*`` / ``$aldff*`` /
      ``$dffsr*``) — sync resets sample the gate on the clock edge,
      so glitches shorter than a cycle are filtered.
    * :func:`rtl_buddy_cdc.reset_domain.assign_reset_domains` must
      classify the reset source as ``"comb"`` (the immediate driver
      is a non-``Q`` cell output).
    * Backward fanin from the reset pin must reach at least one flop.
      Pure-port comb (e.g. ``rst_a_n & test_mode_n``) is accepted —
      those signals are the user's responsibility to drive cleanly.

    Suppressed when the consumer is recognised as a reset-synchroniser
    chain member by :func:`find_reset_synchronizers` (intentional
    comb-on-reset patterns inside a vetted sync stage stay silent).
    """
    from rtl_buddy_cdc.reset_domain import (
        assign_reset_domains,
        find_reset_synchronizers,
    )

    if ctx is None:
        ctx = _build_context(module, clock_spec)
    reset_domains = assign_reset_domains(module)
    recognised_syncs = find_reset_synchronizers(
        module, ctx.domains, extra_synchronizers=ctx.reset_sync_flop_names
    )

    violations: list[Violation] = []
    for f in ctx.flops:
        if f.cell.name in recognised_syncs:
            continue
        rd = reset_domains.get(f.cell.name)
        if rd is None or rd.reset is None:
            continue
        if rd.reset.type != "async" or rd.reset.source != "comb":
            continue
        # Locate the actual reset pin on this cell type.
        rst_bits: tuple[Bit, ...] = ()
        for pin in ("ARST", "ALOAD", "CLR"):
            bits = f.cell.connections.get(pin, ())
            if bits:
                rst_bits = bits
                break
        if not rst_bits:
            continue
        fanin_flops = _backward_flop_fanin(module, rst_bits, ctx.bit_drivers)
        if not fanin_flops:
            # Pure comb-of-ports — the user's responsibility, not a
            # CDC concern. Keeps the noise floor low on designs that
            # legitimately AND two external reset ports.
            continue
        flop_list = sorted(fanin_flops)[:3]
        flop_desc = ", ".join(flop_list) + ("..." if len(fanin_flops) > 3 else "")
        violations.append(
            Violation(
                rule_id="RDC-004",
                severity="error",
                message=(
                    f"reset driven by combinational logic: flop "
                    f"{f.cell.name}'s reset pin is the output of "
                    f"combinational gate(s) fed by flop(s) "
                    f"{flop_desc}. Comb-gate outputs can glitch when "
                    f"inputs transition asynchronously, causing "
                    f"spurious reset assertions. Register the comb "
                    f"output on the consumer's clock before using as "
                    f"a reset."
                ),
                cell_name=f.cell.name,
            )
        )
    return violations


def check_rdc_005(
    module: Module,
    crossings: list[Crossing],  # noqa: ARG001
    clock_spec: ClockSpec | None = None,
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """RDC-005 — Multiple reset sources converging on a flop without muxing.

    Fires when a flop's async reset pin is the output of comb logic
    whose backward fanin reaches two or more distinct **top-level
    reset ports**, the immediate driver cell is not a ``$mux`` /
    ``$pmux`` (the explicit-muxing exemption), and the fanin
    contains no flops (those go through RDC-004).

    The canonical anti-pattern is ``assign rst_for_flop =
    global_rst_n & block_rst_n;`` — both reset sources are active
    simultaneously, the user can't disable one to isolate the
    other, and the AND has no selection signal making the intent
    unambiguous. The textbook fix is either to mux the two
    sources on a control signal (``selected_rst = sel ? a : b``)
    or to register the combined signal on the local clock.

    Severity ``warning`` — the AND-of-resets pattern is common
    enough in SoC designs that calling it an unambiguous bug
    would be too strong; ``warning`` invites review.

    Suppressed when the consumer is recognised as a reset-
    synchroniser chain member.
    """
    from rtl_buddy_cdc.reset_domain import (
        assign_reset_domains,
        find_reset_synchronizers,
    )

    if ctx is None:
        ctx = _build_context(module, clock_spec)
    reset_domains = assign_reset_domains(module)
    recognised_syncs = find_reset_synchronizers(
        module, ctx.domains, extra_synchronizers=ctx.reset_sync_flop_names
    )

    violations: list[Violation] = []
    for f in ctx.flops:
        if f.cell.name in recognised_syncs:
            continue
        rd = reset_domains.get(f.cell.name)
        if rd is None or rd.reset is None:
            continue
        if rd.reset.type != "async" or rd.reset.source != "comb":
            continue
        rst_bits: tuple[Bit, ...] = ()
        for pin in ("ARST", "ALOAD", "CLR"):
            bits = f.cell.connections.get(pin, ())
            if bits:
                rst_bits = bits
                break
        if not rst_bits:
            continue
        # Immediate-driver exemption: explicit-mux selection of the
        # reset source is the user telling us they intended the
        # multi-source pattern. The exemption only checks the cell
        # whose output IS the reset bit — nested muxes deeper in the
        # fanin are out of scope for v1.
        rst_bit = rst_bits[0]
        if isinstance(rst_bit, int):
            drv = ctx.bit_drivers.get(rst_bit)
            if drv is not None:
                drv_cell_name, _, _ = drv
                drv_cell = module.cells.get(drv_cell_name)
                if drv_cell is not None and drv_cell.type in {"$mux", "$pmux"}:
                    continue
        # RDC-004 already owns the "fanin reaches flops" case.
        fanin_flops = _backward_flop_fanin(module, rst_bits, ctx.bit_drivers)
        if fanin_flops:
            continue
        fanin_ports = _backward_port_fanin(module, rst_bits, ctx.bit_drivers)
        # Need 2+ distinct ports for this to be a multi-source
        # convergence. A single port through a comb cell is just a
        # polarity flip or a constant fold; not the target shape.
        if len(fanin_ports) < 2:
            continue
        port_list = sorted(fanin_ports)
        violations.append(
            Violation(
                rule_id="RDC-005",
                severity="warning",
                message=(
                    f"multiple reset sources converging without muxing: "
                    f"flop {f.cell.name}'s reset pin is the comb-AND/OR "
                    f"of {len(fanin_ports)} top-level reset ports "
                    f"({', '.join(port_list[:3])}"
                    + ("..." if len(port_list) > 3 else "")
                    + "). "
                    "Both sources are active simultaneously and the "
                    "user has no control over which dominates. Add a "
                    "$mux selecting between them on a control signal, "
                    "or register the combined signal on the local "
                    "clock so the reset edge is at least glitch-free."
                ),
                cell_name=f.cell.name,
            )
        )
    return violations


def _backward_port_fanin(
    module: Module,
    start_bits: tuple[Bit, ...],
    drivers: dict[Bit, tuple[str, str, int]],
    max_depth: int = 12,
) -> set[str]:
    """Backward BFS through combinational cells; return the set of
    top-level **input port names** reached.

    Mirrors :func:`_backward_flop_fanin` but stops on ports rather
    than ``Q`` outputs. Used by RDC-005 to count distinct external
    reset sources behind a comb cell.
    """
    ports: set[str] = set()
    seen: set[Bit] = set()
    frontier: list[tuple[Bit, int]] = [(b, 0) for b in start_bits if isinstance(b, int)]
    while frontier:
        nxt: list[tuple[Bit, int]] = []
        for bit, depth in frontier:
            if bit in seen:
                continue
            seen.add(bit)
            port = module.port_of_bit(bit)
            if port is not None and port.direction == "input":
                ports.add(port.name)
                continue
            drv = drivers.get(bit)
            if drv is None:
                continue
            cell_name, port_name, _idx = drv
            cell = module.cells[cell_name]
            if port_name == "Q":
                # Reached a flop — RDC-004's domain, not ours.
                continue
            if depth >= max_depth:
                continue
            for in_port, in_bits in cell.connections.items():
                if in_port in _OUTPUT_PINS:
                    continue
                for b in in_bits:
                    if isinstance(b, int):
                        nxt.append((b, depth + 1))
        frontier = nxt
    return ports


def check_rdc_006(
    module: Module,
    crossings: list[Crossing],  # noqa: ARG001
    clock_spec: ClockSpec | None = None,
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """RDC-006 — Muxed/derived async reset without a local synchroniser.

    Fires when a flop's async reset pin is driven *directly* by a
    ``$mux`` / ``$pmux`` output and the flop is not part of a
    recognised reset-synchroniser chain. RDC-005 deliberately
    exempts the ``$mux`` shape — explicit selection is the user's
    declared intent for which reset source dominates. RDC-006 fills
    the resulting gap: making source selection explicit does not
    make the deassertion edge synchronous to the consumer clock.
    Whichever leg the mux currently selects will deassert
    asynchronously to ``CLK``; the textbook fix is a 2FF reset
    synchroniser between the mux and the downstream consumers.

    Severity ``warning`` — the muxed-reset pattern is common in SoC
    designs that route a chip-level reset through a control
    register, and the local block may legitimately consume an
    upstream-synchronised reset. ``warning`` invites review rather
    than declaring an unambiguous bug.

    The rule is suppressed on:

    * flops recognised as reset-synchroniser stages (their own
      ARST may legitimately be the muxed source — that's the
      sync chain's job),
    * flops marked with ``(* reset_sync *)`` (the user-annotation
      escape hatch, threaded through
      :attr:`_RuleContext.reset_sync_flop_names`).
    """
    from rtl_buddy_cdc.reset_domain import (
        assign_reset_domains,
        find_reset_synchronizers,
    )

    if ctx is None:
        ctx = _build_context(module, clock_spec)
    reset_domains = assign_reset_domains(module)
    recognised_syncs = find_reset_synchronizers(
        module, ctx.domains, extra_synchronizers=ctx.reset_sync_flop_names
    )

    violations: list[Violation] = []
    for f in ctx.flops:
        if f.cell.name in recognised_syncs:
            continue
        rd = reset_domains.get(f.cell.name)
        if rd is None or rd.reset is None:
            continue
        if rd.reset.type != "async" or rd.reset.source != "comb":
            continue
        rst_bits: tuple[Bit, ...] = ()
        for pin in ("ARST", "ALOAD", "CLR"):
            bits = f.cell.connections.get(pin, ())
            if bits:
                rst_bits = bits
                break
        if not rst_bits:
            continue
        rst_bit = rst_bits[0]
        if not isinstance(rst_bit, int):
            continue
        drv = ctx.bit_drivers.get(rst_bit)
        if drv is None:
            continue
        drv_cell_name, _, _ = drv
        drv_cell = module.cells.get(drv_cell_name)
        if drv_cell is None or drv_cell.type not in {"$mux", "$pmux"}:
            continue
        # Walk only the mux's *data* legs for the message — the
        # select pin is a control signal, not a reset source, and
        # surfacing it as one is confusing. ``$mux`` has data pins
        # A / B and select S; ``$pmux`` has A (default) / B (cases)
        # / S (one-hot select).
        data_bits: list[Bit] = []
        for pin in ("A", "B"):
            data_bits.extend(drv_cell.connections.get(pin, ()))
        fanin_ports = _backward_port_fanin(module, tuple(data_bits), ctx.bit_drivers)
        port_list = sorted(fanin_ports)
        if port_list:
            sources_phrase = (
                f"{len(port_list)} reset sources "
                f"({', '.join(port_list[:3])}"
                + ("..." if len(port_list) > 3 else "")
                + ")"
            )
        else:
            sources_phrase = "multiple reset sources"
        violations.append(
            Violation(
                rule_id="RDC-006",
                severity="warning",
                message=(
                    f"muxed async reset without local synchroniser: "
                    f"flop {f.cell.name}'s reset pin is driven by a "
                    f"{drv_cell.type} selecting between {sources_phrase}. "
                    f"The mux makes source selection explicit (why "
                    f"RDC-005 stays silent), but the selected reset's "
                    f"deassertion edge is still asynchronous to this "
                    f"flop's clock. Add a 2FF reset synchroniser in "
                    f"the consumer clock domain between the mux and "
                    f"this flop's reset pin."
                ),
                cell_name=f.cell.name,
            )
        )
    return violations


def check_rdc_002(
    module: Module,
    crossings: list[Crossing],  # noqa: ARG001
    clock_spec: ClockSpec | None = None,  # noqa: ARG001
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """RDC-002 — Reset polarity mismatch.

    Two variants fire under this rule:

    *Flop→flop variant.* A flop's reset pin is driven *directly* (no
    inverter, no comb between) by another flop's ``Q``, and the
    consumer's polarity expectation doesn't match the producer's
    reset-value output. Concretely:

    * Let *P* be the producer flop and *C* the consumer flop.
    * When *P* enters reset, ``P.Q = P.ARST_VALUE``.
    * *C* enters reset when its reset pin equals ``C.ARST_POLARITY``.
    * If ``P.ARST_VALUE != C.ARST_POLARITY``, *C* does **not** enter
      reset when *P* does — a polarity wiring bug.

    *Port-declared variant* (issue #107). When a top-level reset port
    carries a ``(* reset_polarity = "<low|high>" *)`` attribute, treat
    that declaration as authoritative. Any flop whose async reset
    traces back to that port (``ResetSource.source == "port"``) and
    whose inferred polarity disagrees with the declaration is reported.
    This catches the "designer added a ``posedge rst_n`` flop on a port
    the rest of the design treats as active-low" wiring bug — the
    structural fanin walk hides it because the connection is just a
    plain wire, but the user's declared intent gives us the reference
    to compare against.

    Both variants are suppressed when the consumer flop is a recognised
    reset-synchroniser stage (a polarity-inverting sync may be the
    deliberate fix, marked by ``(* reset_sync *)`` or matched by the
    constant-fed-head structural recogniser).

    This is a structural rule — it does not depend on the SDC clock
    declarations. It runs whether or not ``clock_spec`` is supplied.
    """
    from rtl_buddy_cdc.reset_domain import (
        assign_reset_domains,
        find_reset_synchronizers,
    )

    if ctx is None:
        ctx = _build_context(module, clock_spec)
    reset_domains = assign_reset_domains(module)
    recognised_syncs = find_reset_synchronizers(
        module, ctx.domains, extra_synchronizers=ctx.reset_sync_flop_names
    )
    polarity_overrides = ctx.reset_polarity_overrides

    # Group by (producer, producer_arst_value, consumer_polarity_bit)
    # so the typical "one upstream polarity wiring bug, N downstream
    # consumers" shape becomes one finding listing every affected
    # consumer — matches RDC-001's reset-tree grouping convention and
    # the fix-shape the user has to take.
    groups: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    # Port-declared variant uses a parallel grouping key: (port_name,
    # declared_polarity_bit, consumer_polarity_bit). Keeping the two
    # buckets separate avoids cross-contaminating their messages.
    port_groups: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for f in ctx.flops:
        if f.cell.name in recognised_syncs:
            continue
        rd = reset_domains.get(f.cell.name)
        if rd is None or rd.reset is None:
            continue
        # Only fire on async-reset consumers (ARST). Sync-reset (SRST)
        # signals are intentional gating — e.g. a "kill" signal that
        # synchronously clears a pipeline — and the producer's own
        # ARST_VALUE has no semantic relationship with the consumer's
        # SRST_POLARITY. RDC-003 owns the sync-reset crossing concern;
        # RDC-002 stays scoped to the async-reset distribution tree
        # where the "consumer must enter reset when producer does"
        # invariant actually holds.
        if rd.reset.type != "async":
            continue
        consumer_polarity_bit = "1" if rd.reset.polarity == "high" else "0"
        if rd.reset.source == "inferred":
            producer_name = rd.reset.name
            producer = module.cells.get(producer_name)
            if producer is None:
                continue
            # If the producer is itself a recognised reset-synchroniser
            # stage, the user has vetted that the reset arrives cleanly —
            # any polarity inversion in the chain is intentional. Catches
            # the ``(* reset_sync *)``-marked-tail shape where the user
            # explicitly declared the chain trustworthy.
            if producer_name in recognised_syncs:
                continue
            producer_arst_value = _trailing_bit(
                producer.parameters.get("ARST_VALUE", "0")
            )
            if producer_arst_value == consumer_polarity_bit:
                continue  # polarities match — wiring is correct
            groups[(producer_name, producer_arst_value, consumer_polarity_bit)].append(
                f.cell.name
            )
        elif rd.reset.source == "port" and rd.reset.name in polarity_overrides:
            declared = polarity_overrides[rd.reset.name]
            declared_bit = "1" if declared == "high" else "0"
            if declared_bit == consumer_polarity_bit:
                continue
            port_groups[(rd.reset.name, declared_bit, consumer_polarity_bit)].append(
                f.cell.name
            )

    violations: list[Violation] = []
    for (producer_name, prod_val, cons_pol), consumers in groups.items():
        dsts = sorted(set(consumers))
        repr_cell = dsts[0]
        if len(dsts) == 1:
            dst_desc = f"destination flop: {dsts[0]}"
        else:
            dst_desc = (
                f"{len(dsts)} destination flops share this polarity "
                f"mismatch from the same source "
                f"(reset distribution tree): "
                f"{', '.join(dsts[:3])}" + (", ..." if len(dsts) > 3 else "")
            )
        violations.append(
            Violation(
                rule_id="RDC-002",
                severity="error",
                message=(
                    f"reset polarity mismatch: consumer flop(s) expect "
                    f"reset asserted on '{cons_pol}' "
                    f"(ARST_POLARITY={cons_pol}), but the producer flop "
                    f"{producer_name} drives its reset to '{prod_val}' "
                    f"on assert (ARST_VALUE={prod_val}); consumer(s) "
                    f"will never enter reset when the producer does. Add "
                    f"an inverter between them, or flip the consumer's "
                    f"reset edge sensitivity to match. {dst_desc}"
                ),
                cell_name=repr_cell,
            )
        )
    for (port_name, decl_bit, cons_pol), consumers in port_groups.items():
        dsts = sorted(set(consumers))
        repr_cell = dsts[0]
        declared = "high" if decl_bit == "1" else "low"
        if len(dsts) == 1:
            dst_desc = f"destination flop: {dsts[0]}"
        else:
            dst_desc = (
                f"{len(dsts)} destination flops disagree with the "
                f"port-declared polarity: "
                f"{', '.join(dsts[:3])}" + (", ..." if len(dsts) > 3 else "")
            )
        violations.append(
            Violation(
                rule_id="RDC-002",
                severity="error",
                message=(
                    f"reset polarity mismatch with port-level declaration: "
                    f"port '{port_name}' is annotated "
                    f'(* reset_polarity = "{declared}" *) (active-{declared}, '
                    f"asserts on '{decl_bit}'), but the consumer flop(s) "
                    f"are wired active-{'high' if cons_pol == '1' else 'low'} "
                    f"(ARST_POLARITY={cons_pol}); the declared port "
                    f"polarity disagrees with the flop's reset edge "
                    f"sensitivity. Fix the flop's edge to match the port "
                    f"declaration, or remove the (* reset_polarity *) "
                    f"attribute if the declaration is wrong. {dst_desc}"
                ),
                cell_name=repr_cell,
            )
        )
    return violations


def _trailing_bit(raw: str) -> str:
    """Yosys polarity / value parameters are binary strings. Return
    the trailing bit (``'1'`` or ``'0'``); empty string defaults to
    ``'0'`` — the conservative active-low / reset-value convention."""
    if not raw:
        return "0"
    return raw[-1] if raw[-1] in "01" else "0"


def check_rdc_007(
    module: Module,
    crossings: list[Crossing],  # noqa: ARG001
    clock_spec: ClockSpec | None = None,  # noqa: ARG001
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """RDC-007 — Reset-synchroniser chain with deassertion-polarity backwards.

    For every structurally-recognised reset-synchroniser chain (the
    canonical async-assert / sync-deassert 2FF idiom), checks that
    the chain head's ``D`` constant matches the *deassertion* value
    of the chain's reset polarity:

    * active-low reset (``ARST_POLARITY=0``) → head D must be ``1``
      so the chain's Q rises after the reset deasserts.
    * active-high reset (``ARST_POLARITY=1``) → head D must be ``0``.

    If the chain head loads the *asserted* value instead, the
    synchroniser is a one-shot: on the deassertion edge it reloads
    the asserted value and the chain's tail keeps driving the reset
    "asserted" forever. Worse, every downstream consumer using that
    chain's output as their ``ARST`` is silently exempted from
    RDC-001..-006 because the analyzer treats the chain as a valid
    synchroniser.

    Severity ``error`` — a chain that never deasserts is a hard
    functional bug; downstream consumers stay held in reset
    permanently.

    Limitations (documented; do not extend RDC-007 to cover):

    * User-marked ``(* reset_sync *)`` flops whose chain head isn't
      constant-fed are not checkable structurally — the structural
      recogniser has no head D-constant to compare against.
    * Sync-reset (``SRST``) chains are out of scope; RDC-007 stays
      scoped to the async-assert / sync-deassert async-reset idiom.
    """
    from rtl_buddy_cdc.reset_domain import iter_reset_sync_chains

    if ctx is None:
        ctx = _build_context(module, clock_spec)
    violations: list[Violation] = []
    for chain in iter_reset_sync_chains(module, ctx.domains):
        head_d = _trailing_bit(chain.head_d_constant)
        # Deassertion bit is the inverse of the assertion polarity.
        deassert_bit = "0" if chain.polarity == "high" else "1"
        if head_d == deassert_bit:
            continue
        tail = chain.flops[0]
        head = chain.flops[-1]
        polarity_word = "active-high" if chain.polarity == "high" else "active-low"
        violations.append(
            Violation(
                rule_id="RDC-007",
                severity="error",
                message=(
                    f"reset-synchroniser chain {tail} ← ... ← {head} has its "
                    f"head D tied to '{head_d}', but the chain's reset is "
                    f"{polarity_word} (deasserts on '{deassert_bit}'). The "
                    f"chain reloads the *asserted* value on the deassertion "
                    f"edge — its output is a one-shot stuck driving the "
                    f"asserted value forever, and every downstream consumer "
                    f"using {tail}.Q as their ARST will never leave reset. "
                    f"Tie the head's D to 1'b{deassert_bit} — the deasserted "
                    f"value for an {polarity_word} reset."
                ),
                cell_name=tail,
            )
        )
    return violations


def check_rdc_008(
    module: Module,
    crossings: list[Crossing],  # noqa: ARG001
    clock_spec: ClockSpec | None = None,
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """RDC-008 — Unsynced primary-reset-port deassertion.

    Fires when a flop's async reset is driven *directly* by a top-level
    input port (``ResetDomain.reset.source == "port"``) and the flop
    is not part of a recognised reset-synchroniser chain in its own
    clock domain. RDC-001 is the symmetric rule for foreign-domain
    flop-sourced resets (``source == "inferred"``); RDC-008 fills the
    port-source gap.

    Reset assertion is fine (combinational propagation from the port),
    but the deassertion edge is unsynchronised to the consumer clock —
    recovery/removal timing violations can leave random subsets of
    flops in different reset states. Textbook fix: a 2FF reset-
    synchroniser chain in the consumer clock domain whose ``ARST`` is
    the raw port; downstream consumers then use the chain's tail Q.

    Severity ``error`` — methodology bug; the chain's absence is the
    root cause of a class of intermittent silicon issues.

    Detection groups by ``(port_name, consumer_clock)``: one finding
    per port per clock domain listing every affected consumer flop.

    **Asymmetric detection.** RDC-008 only fires when the user has
    clearly *built* reset-sync infrastructure for the port in one
    clock domain but *missed* it in another. That asymmetry signals
    intent (the user knows the port needs synchronisation) and the
    miss is a methodology bug rather than a simplification. Designs
    that use the raw reset port directly in every consumer domain
    (the common pattern in small unit-test RTL) stay silent — they're
    a different concern, not what RDC-008 is calibrated for.

    Suppression:

    * Consumer flop is in :func:`find_reset_synchronizers` set — the
      chain's *own* flops have their ARST as the port by design (that's
      the chain's input). They're recognised members, so they don't
      fire.
    * Consumer flop is marked ``(* reset_sync *)`` (user-vouched).
    * Reset source is ``"inferred"`` (RDC-001), ``"comb"`` (RDC-004),
      or ``"constant"`` (no failure mode). RDC-008 stays narrowly on
      ``"port"``.
    * Sync-reset (``SRST``) consumers are skipped — RDC-008 is scoped
      to async-reset distribution only.
    * Port has no sync chain anywhere in the design (the
      "every-consumer-uses-the-raw-port" simplification).
    """
    from rtl_buddy_cdc.reset_domain import (
        assign_reset_domains,
        find_reset_synchronizers,
    )

    if ctx is None:
        ctx = _build_context(module, clock_spec)
    reset_domains = assign_reset_domains(module)
    recognised_syncs = find_reset_synchronizers(
        module, ctx.domains, extra_synchronizers=ctx.reset_sync_flop_names
    )

    # First pass: collect every unsynced port-sourced ARST consumer,
    # grouped by (port, consumer_clock).
    unsynced_groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for f in ctx.flops:
        if f.cell.name in recognised_syncs:
            continue
        rd = reset_domains.get(f.cell.name)
        if rd is None or rd.reset is None:
            continue
        if rd.reset.type != "async" or rd.reset.source != "port":
            continue
        consumer_clk = ctx.domains.get(f.cell.name)
        if consumer_clk is None:
            continue
        unsynced_groups[(rd.reset.name, consumer_clk)].append(f.cell.name)

    # Per-port: which clock domains DO have a recognised reset-sync
    # chain consuming this port? Determined by walking recognised sync
    # flops and consulting their reset source. A chain's own flops have
    # rd.reset.source == "port" with rd.reset.name == the port driving
    # them; their domain is the sync chain's clock domain.
    synced_clocks_by_port: dict[str, set[str]] = defaultdict(set)
    for sync_name in recognised_syncs:
        rd = reset_domains.get(sync_name)
        if rd is None or rd.reset is None:
            continue
        if rd.reset.source != "port":
            continue
        sync_clk = ctx.domains.get(sync_name)
        if sync_clk is None:
            continue
        synced_clocks_by_port[rd.reset.name].add(sync_clk)

    violations: list[Violation] = []
    for (port_name, consumer_clk), consumers in sorted(unsynced_groups.items()):
        # The asymmetric-intent gate: only fire when the user has built
        # a chain for this port in *some* domain. Without that signal
        # the design likely just uses the raw port as-is in every
        # domain (a simplification we don't flag here).
        if not synced_clocks_by_port[port_name]:
            continue
        consumer_names = sorted(set(consumers))
        # Single-flop shortcut: one unsynced consumer in a domain that
        # otherwise has no need for a sync chain often reflects a
        # designer judgement call (the lone flop's startup timing
        # happens to align). RDC-008 stays narrowly on the clearer
        # methodology bug: ≥2 unsynced consumers in the same (port,
        # clk) group, indicating the user has built a distribution
        # pattern that's missing the sync stage.
        if len(consumer_names) < 2:
            continue
        consumer_desc = (
            f"{len(consumer_names)} flops ({', '.join(consumer_names[:3])}"
            + ("..." if len(consumer_names) > 3 else "")
            + ")"
        )
        clk_phrase = (
            f"in {consumer_clk}" if consumer_clk else "in an untraceable clock domain"
        )
        violations.append(
            Violation(
                rule_id="RDC-008",
                severity="error",
                message=(
                    f"unsynced primary-reset port: top-level reset "
                    f"port '{port_name}' drives {consumer_desc} {clk_phrase} "
                    f"directly, with no reset-synchroniser chain in that "
                    f"clock domain. Reset assertion is fine, but the "
                    f"deassertion edge is unsynchronised to the consumer "
                    f"clock — recovery/removal timing violations can "
                    f"leave random subsets of flops in different reset "
                    f"states. Add a 2FF reset-synchroniser chain in "
                    f"{consumer_clk or 'the consumer clock domain'} whose "
                    f"ARST is '{port_name}', and route consumers through "
                    f"the chain's tail."
                ),
                cell_name=consumer_names[0],
            )
        )
    return violations


def _unconstrained_srst_captures(
    module: Module,
    clock_spec: ClockSpec,
    ctx: _RuleContext,
) -> dict[str, set[tuple[str, str]]]:
    """Untyped input ports that reach a flop's **synchronous reset** pin.

    The crossing model is D-pin-scoped: :func:`find_crossings` seeds
    and terminates its walk on flop ``D`` pins only, so a port that
    lands on a dedicated ``SRST`` pin produces no ``Crossing`` at all.
    Whether a sync reset lowers into a ``$dff`` D-cone (a reset mux,
    reached by the port walk) or into a ``$sdff`` ``SRST`` pin (not
    reached) is a synthesis-pass / coding-style detail — see issue
    rtl-buddy-cdc#272. This helper closes that gap by walking the
    ``SRST`` pins directly so CDC-011 reports the same finding either
    way.

    Returns ``{port_name: {(dst_clock, dst_flop_cell_name), ...}}`` for
    every top-level input port carrying :data:`UNCONSTRAINED_SENTINEL`
    that reaches an ``SRST`` pin, directly or through combinational
    logic. Empty when
    :func:`~rtl_buddy_cdc.sdc.synthesize_unconstrained_inputs` hasn't
    stamped the sentinel. The caller skips this walk entirely when no
    SDC was supplied — there is no "untyped" verdict to make without
    one.

    Scoped to **synchronous** resets on purpose. An async reset pin
    (``ARST`` / ``CLR`` / ``ALOAD``) is legitimately untimed — a
    ``set_input_delay -clock`` on it would be meaningless advice, and
    the RDC family (RDC-001 / RDC-006 / RDC-008) already owns the
    async-reset failure modes. A sync reset, by contrast, is sampled
    on the destination clock edge exactly like any data input, so the
    missing SDC typing is the same methodology gap CDC-011 exists to
    report.
    """
    unconstrained = {
        port
        for port, clk in clock_spec.port_clock.items()
        if clk == UNCONSTRAINED_SENTINEL
    }
    if not unconstrained:
        return {}

    out: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for f in ctx.flops:
        srst_bits = f.cell.connections.get("SRST", ())
        if not srst_bits:
            continue
        dst_clk = ctx.domains.get(f.cell.name)
        if dst_clk is None:
            continue
        for port in _backward_port_fanin(module, srst_bits, ctx.bit_drivers):
            if port in unconstrained:
                out[port].add((dst_clk, f.cell.name))
    return out


def check_cdc_011(
    module: Module,
    crossings: list[Crossing],
    clock_spec: ClockSpec | None = None,
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """CDC-011 — Unconstrained primary input captured by clocked logic.

    Fires on top-level input ports that the SDC didn't type via
    ``set_input_delay -clock <name>`` but that physically reach a
    flop's ``D`` pin — or its synchronous-reset (``SRST``) pin. For
    the ``D``-pin case, ``sdc.synthesize_unconstrained_inputs``
    assigns :data:`UNCONSTRAINED_SENTINEL` as ``port_clock`` for any
    such port; :func:`find_crossings` then walks them and emits
    port-sourced crossings whose ``src_clock`` carries the sentinel.
    The ``SRST`` case is picked up by
    :func:`_unconstrained_srst_captures`, because crossings only ever
    sink on ``D`` pins (issue rtl-buddy-cdc#272); both sets of
    destinations are merged per port before severity is decided, so a
    port captured on a ``D`` pin in one domain and an ``SRST`` pin in
    another still collapses to one finding.

    Severity escalation by shape:

    * **error** when one port lands on flops in ``>=2`` distinct
      destination clock domains. A single port cannot be synchronous
      to two clocks at once, so this is intrinsically wrong
      regardless of SDC opinion — typing it on either side won't
      silence the rule, only fixing the RTL (or adding a synchronizer
      on the foreign side) will.
    * **warning** when the port lands in a single destination domain.
      The usual fix is adding ``set_input_delay -clock <name>`` to
      assert which domain the port belongs to (and adding a 2FF
      synchronizer if it's a different domain than declared). This is
      a methodology gap, not a hard bug.

    One :class:`Violation` per source port — the message lists every
    distinct destination clock, so a port fanning out across many
    flops in two domains collapses to a single error rather than a
    fixture-per-flop flood.
    """
    # {port: {(dst_clock, dst_flop_cell_name), ...}} — the union of the
    # D-pin destinations the crossing walk found and the SRST-pin
    # destinations it structurally cannot see (#272).
    by_port: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for c in crossings:
        if c.src_port is None:
            continue
        if c.src_clock != UNCONSTRAINED_SENTINEL:
            continue
        by_port[c.src_port].add((c.dst_clock, c.dst_flop.cell.name))

    if clock_spec is not None:
        if ctx is None:
            ctx = _build_context(module, clock_spec)
        for port, dsts in _unconstrained_srst_captures(module, clock_spec, ctx).items():
            by_port[port] |= dsts

    violations: list[Violation] = []
    for port in sorted(by_port):
        dsts = by_port[port]
        dst_clocks = sorted({clk for clk, _cell in dsts})
        # Representative cell for source-location reporting: pick the
        # alphabetically-first destination flop so the reporter has
        # something concrete to anchor on.
        repr_cell = sorted(cell for _clk, cell in dsts)[0]
        if len(dst_clocks) > 1:
            violations.append(
                Violation(
                    rule_id="CDC-011",
                    severity="error",
                    message=(
                        f"unconstrained primary input {port!r} is "
                        f"captured in multiple clock domains "
                        f"({dst_clocks}); a single port cannot be "
                        f"synchronous to more than one clock — add "
                        f"set_input_delay -clock <name> and a "
                        f"synchronizer on at least one destination"
                    ),
                    cell_name=repr_cell,
                )
            )
        else:
            (dst_clk,) = dst_clocks
            violations.append(
                Violation(
                    rule_id="CDC-011",
                    severity="warning",
                    message=(
                        f"unconstrained primary input {port!r} has no "
                        f"set_input_delay -clock typing in the SDC "
                        f"but is captured in domain {dst_clk!r}; add "
                        f"set_input_delay -clock {dst_clk} "
                        f"[get_ports {port}] (or declare the port's "
                        f"true source clock and add a synchronizer)"
                    ),
                    cell_name=repr_cell,
                )
            )
    return violations


# Cummings's rule of thumb: the src pulse must be ≥ PULSE_FACTOR × dst
# period for the dst flop to reliably capture it. 1.5× absorbs a
# typical dst-clock phase + metastability resolution budget. Hard-coded
# for v1 — a CLI flag is deferred (see #47 §7).
PULSE_FACTOR = 1.5


def check_cdc_009(
    module: Module,
    crossings: list[Crossing],
    clock_spec: ClockSpec | None = None,
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """CDC-009: pulse-width risk on fast-to-slow data crossings.

    Fires when a single-bit, flop-sourced crossing has:
    - both clocks declared with periods in the SDC,
    - ``src_period * PULSE_FACTOR < dst_period`` (src enough faster
      that a 1-cycle pulse may slip between dst rising edges), and
    - the src flop's D pin matches the edge-detector pattern
      ``A & ~A_d`` (per :func:`rtl_buddy_cdc.pulse.classify_d_pin_shape`).

    Severity is ``warning`` — single-bit pulse loss is a methodology
    smell, not unconditional breakage. Pairs with CDC-001/002 (missing
    sync). See issue #47 for the full design.
    """
    if ctx is None:
        ctx = _build_context(module, clock_spec)
    if clock_spec is None:
        return []

    out: list[Violation] = []
    for c in crossings:
        if c.src_clock == UNCONSTRAINED_SENTINEL:
            continue
        if c.src_flop is None or c.width != 1:
            continue
        src_clk = clock_spec.clocks.get(c.src_clock)
        dst_clk = clock_spec.clocks.get(c.dst_clock)
        if src_clk is None or dst_clk is None:
            continue
        if src_clk.period * PULSE_FACTOR >= dst_clk.period:
            continue
        d_bits = c.src_flop.cell.connections.get("D", ())
        if len(d_bits) != 1:
            continue
        shape = classify_d_pin_shape(
            d_bits[0], c.src_clock, module, ctx.bit_drivers, ctx.domains
        )
        if shape != "pulse":
            continue
        out.append(
            Violation(
                rule_id="CDC-009",
                severity="warning",
                message=(
                    f"pulse-width risk on {c.src_flop.name!r} → "
                    f"{c.dst_flop.name!r}: src clock {c.src_clock!r} "
                    f"(period {src_clk.period}) is faster than dst "
                    f"clock {c.dst_clock!r} (period {dst_clk.period}); "
                    f"a 1-cycle src pulse may not be captured. "
                    f"Consider a pulse-stretcher, toggle synchronizer, "
                    f"or req/ack handshake."
                ),
                crossing=c,
                cell_name=c.src_flop.cell.name,
            )
        )
    return out


def _has_xor_tail_pulse_recovery(
    head: Flop,
    head_clock: str,
    ctx: _RuleContext,
    module: Module,
) -> bool:
    """True iff the dst-side chain starting at ``head`` ends in the
    canonical pulse-sync XOR-tail shape.

    The canonical pulse-synchroniser idiom that closes the
    fast-to-slow toggle event-loss class:

        toggle_q (src_clk) → sync_meta (dst_clk) → sync_q (dst_clk)
                                                       │
                                            ┌──────────┴──────────┐
                                            ▼                     ▼
                                        sync_q_d              ┌───XOR───┐ → pulse_out
                                        (dst_clk)             │  inputs:│
                                            │                 │  sync_q,│
                                            └─────────────────► sync_q_d
                                                              └─────────┘

    Walks the dst-domain chain from ``head``. Confirms the chain
    tail's Q is consumed by:

    1. A follow-on flop in the same dst_clock domain whose ``D``
       is exactly tail.Q (the ``sync_q_d`` of the diagram).
    2. An XOR cell (``$xor`` / ``$_XOR_``) whose two inputs are
       tail.Q and that follow-on flop's Q.

    Returns ``True`` if both are present. The follow-on flop's Q
    fanout beyond the XOR is irrelevant — it can drive any number
    of consumers.

    Used by CDC-013 as a positive-recognition suppression: a chain
    whose toggle source is matched by an XOR-tail destination is
    the correct fast-to-slow idiom, not the bug CDC-013 was
    designed to flag.
    """
    chain = _sync_chain_flops(
        module,
        head,
        head_clock,
        ctx.domains,
        ctx.reader_counts,
        d_bit_to_single_bit_flop=ctx.d_bit_to_single_bit_flop,
    )
    if len(chain) < 2:
        # The XOR-tail breaks _sync_chain_depth's exactly-one-reader
        # invariant on the chain tail, so the chain returned here is
        # the prefix up to (but not including) the tail. Pick the
        # last flop in this prefix as the chain head's downstream
        # 1-stage neighbour and look for the tail flop manually
        # downstream.
        return False
    tail = chain[-1]
    if len(tail.q) != 1:
        return False
    tail_q = tail.q[0]
    if not isinstance(tail_q, int):
        return False

    follow_flop_q: int | None = None
    xor_cells: list[Cell] = []
    for cell in module.cells.values():
        ctype = cell.type
        is_xor = ctype in {"$xor", "$_XOR_"}
        is_flop = is_ff_cell(ctype)
        if not is_xor and not is_flop:
            continue
        for port_name, bits in cell.connections.items():
            if port_name in _OUTPUT_PINS or port_name in {"CLK", "C"}:
                continue
            if any(b == tail_q for b in bits if isinstance(b, int)):
                if is_xor:
                    xor_cells.append(cell)
                elif is_flop and port_name == "D":
                    # Candidate sync_q_d. Must be a 1-bit follow-on
                    # in the same dst_clock domain.
                    if ctx.domains.get(cell.name) != head_clock:
                        continue
                    q_bits = cell.connections.get("Q", ())
                    d_bits = cell.connections.get("D", ())
                    if len(q_bits) != 1 or len(d_bits) != 1:
                        continue
                    if not isinstance(q_bits[0], int):
                        continue
                    # Reject if D is wider than 1 bit or if the
                    # follow-on flop is multi-driver.
                    follow_flop_q = q_bits[0]
                break

    if follow_flop_q is None or not xor_cells:
        return False

    # Confirm at least one XOR cell has both tail_q and follow_q as inputs.
    for xor in xor_cells:
        inputs: set[int] = set()
        for port_name, bits in xor.connections.items():
            if port_name in _OUTPUT_PINS:
                continue
            for b in bits:
                if isinstance(b, int):
                    inputs.add(b)
        if tail_q in inputs and follow_flop_q in inputs:
            return True

    return False


def check_cdc_013(
    module: Module,
    crossings: list[Crossing],
    clock_spec: ClockSpec | None = None,
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """CDC-013 — Fast-to-slow control-event loss on a toggle synchroniser.

    A toggle synchroniser ( ``always_ff @(posedge src_clk) if (en)
    src_q <= ~src_q;`` ) is the textbook fix for CDC-009's pulse-
    width problem when consecutive events are guaranteed to be far
    enough apart at the application level. The destination
    typically synchronises ``src_q`` through a 2FF chain and runs an
    XOR edge detector ``toggle_sync ^ toggle_sync_d`` to recover one
    pulse per event.

    The pattern is *structurally* safe for metastability — the 2FF
    sync handles that — but it does not guarantee event accounting.
    If two events occur close enough together that ``src_q`` toggles
    twice between two destination samples, the destination observes
    zero edges and both events are silently lost. CDC-013 flags the
    pattern for review.

    Fires when:

    - Both clocks declared with periods in the SDC.
    - ``src_period * PULSE_FACTOR < dst_period`` (parallel to
      CDC-009's threshold).
    - The src flop's ``D`` pin matches the toggle pattern
      ``D = en ? ~Q : Q`` per
      :func:`rtl_buddy_cdc.pulse.classify_toggle_d_pin`.

    Severity ``warning`` — many designs use this pattern correctly
    by rate-limiting events at the application level (or by
    guaranteeing one-in-flight via downstream backpressure). The
    rule invites review rather than declaring an unambiguous bug.
    Handshake / counter-with-backpressure patterns naturally fall
    outside the classifier (their ``D`` is a priority-encoded mux
    nest or an adder, not the ``Q``/``~Q`` mux shape) and stay
    silent.

    Complement to CDC-009 — both flag fast-to-slow data-loss
    classes but on different ``D``-pin shapes; CDC-009 owns the
    raw-pulse case, CDC-013 owns the toggle case.
    """
    if ctx is None:
        ctx = _build_context(module, clock_spec)
    if clock_spec is None:
        return []

    out: list[Violation] = []
    for c in crossings:
        if c.src_clock == UNCONSTRAINED_SENTINEL:
            continue
        if c.src_flop is None or c.width != 1:
            continue
        src_clk = clock_spec.clocks.get(c.src_clock)
        dst_clk = clock_spec.clocks.get(c.dst_clock)
        if src_clk is None or dst_clk is None:
            continue
        if src_clk.period * PULSE_FACTOR >= dst_clk.period:
            continue
        d_bits = c.src_flop.cell.connections.get("D", ())
        q_bits = c.src_flop.cell.connections.get("Q", ())
        if len(d_bits) != 1 or len(q_bits) != 1:
            continue
        if c.src_flop.cell.name in ctx.user_handshakes:
            continue  # (* cdc_handshake *) — req toggle is backpressured until ack (#247)
        shape = classify_toggle_d_pin(d_bits[0], q_bits[0], module, ctx.bit_drivers)
        if shape != "toggle":
            continue
        # Suppress on the canonical pulse-synchroniser shape
        # (toggle + 2FF + XOR tail in dst domain) — that's the
        # *correct* idiom, not the bug CDC-013 targets. See
        # _has_xor_tail_pulse_recovery for the structural match.
        if _has_xor_tail_pulse_recovery(c.dst_flop, c.dst_clock, ctx, module):
            continue
        out.append(
            Violation(
                rule_id="CDC-013",
                severity="warning",
                message=(
                    f"toggle-synchroniser event-loss risk on "
                    f"{c.src_flop.name!r} → {c.dst_flop.name!r}: src "
                    f"clock {c.src_clock!r} (period {src_clk.period}) "
                    f"is faster than dst clock {c.dst_clock!r} (period "
                    f"{dst_clk.period}); the src flop toggles on an "
                    f"enable (D = en ? ~Q : Q) and two events between "
                    f"dst samples cancel to zero edges at the destination. "
                    f"Use a req/ack handshake (source holds value until "
                    f"ack returns) or an event counter with backpressure "
                    f"to prevent a second event before the first is "
                    f"observed."
                ),
                crossing=c,
                cell_name=c.src_flop.cell.name,
            )
        )
    return out


def _flop_input_fanin_bits(flop: Flop) -> tuple[Bit, ...]:
    """Every data/control input bit of a flop cell — D, EN/E, set/reset,
    everything except the clock and the Q/Y outputs.

    The handshake feedback can gate the payload register through either
    its ``D`` (mux-on-D hold) or its enable (``$dffe`` ``EN`` / gate-level
    ``E``), so a feedback walk that looked at ``D`` alone would miss the
    enable-gated form. Walking the full input surface catches both.
    """
    out: list[Bit] = []
    is_ff = is_ff_cell(flop.cell.type)
    for port_name, bits in flop.cell.connections.items():
        if port_name == "CLK" or port_name in _OUTPUT_PINS:
            continue
        if is_ff and port_name == "C":
            continue
        out.extend(b for b in bits if isinstance(b, int))
    return tuple(out)


def _has_dst_to_src_feedback(
    module: Module,
    crossing: Crossing,
    domains: dict[str, str | None],
    bit_drivers: dict[Bit, tuple[str, str, int]],
    flops_by_name: dict[str, Flop],
    *,
    max_flop_hops: int = 6,
) -> bool:
    """Heuristic: does *this crossing's* source flop carry the
    structural marker of a synced-back req/ack handshake?

    In a handshake design the source conditions its payload / request
    updates on an ``ack_sync`` flop in ``src_clock`` whose ``D`` fanin
    reaches the destination's ack (a ``dst_clock`` flop's ``Q``). We
    walk the register-neighbourhood of ``crossing.src_flop`` — hopping
    flop → its full input fanin (:func:`_flop_input_fanin_bits`) →
    flop, staying inside ``src_clock`` — and report feedback the moment
    that walk reaches a ``dst_clock`` flop. Absence of any such path
    means the source has no signalling channel from the destination
    for *this* crossing — the payload can change independently of the
    enable's in-flight progress, which is the failure mode CDC-012
    flags.

    Feedback presence is a **crossing-level** property, not a
    domain-level one: scoping the walk to ``crossing.src_flop`` is what
    stops an unrelated handshake (or a FIFO's pointer sync) between the
    same domain pair from silencing a genuinely broken crossing — the
    domain-wide bleed fixed in rtl-buddy-cdc#239. The walk is bounded to
    ``max_flop_hops`` flop hops, enough to clear a 2–3FF ack
    synchroniser plus the enable flop(s) gating the payload load.
    """
    src_flop = crossing.src_flop
    if src_flop is None:
        return False
    src_clock = crossing.src_clock
    dst_clock = crossing.dst_clock

    seen: set[str] = {src_flop.cell.name}
    frontier: list[Flop] = [src_flop]
    for _hop in range(max_flop_hops):
        nxt: list[Flop] = []
        for f in frontier:
            in_bits = _flop_input_fanin_bits(f)
            if not in_bits:
                continue
            for ff_name in _backward_flop_fanin(module, in_bits, bit_drivers):
                if ff_name in seen:
                    continue
                seen.add(ff_name)
                ff_domain = domains.get(ff_name)
                if ff_domain == dst_clock:
                    return True
                if ff_domain == src_clock and ff_name in flops_by_name:
                    nxt.append(flops_by_name[ff_name])
        if not nxt:
            break
        frontier = nxt
    return False


def check_cdc_012(
    module: Module,
    crossings: list[Crossing],
    clock_spec: ClockSpec | None = None,
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """CDC-012 — Functional data-hold on a gated multi-bit crossing.

    CDC-004's gated-bus exemption accepts a multi-bit crossing whose
    destination only latches data when a destination-domain enable
    allows it (mux-on-D with sync'd select, or ``$dffe`` with sync'd
    ``EN``). That structural shape is necessary but not sufficient
    for a signoff-clean crossing: it ensures the destination samples
    on a clean enable, but does not ensure that the *source payload*
    is stable across the enable's sync-chain latency.

    The bug pattern: source registers a new payload every src_clk
    cycle, asserts a request, the request propagates through a 2FF
    synchroniser into the destination, and the destination latches
    the payload it sees ``N`` dst_clk cycles after the original
    request. By that time the source payload may have advanced one
    or more times. The destination captures an incoherent value —
    not the payload that motivated the original request.

    The textbook fix is a req/ack handshake: the source holds the
    payload (and the request) until a synced-back ack proves the
    destination has sampled. The handshake's structural marker is a
    src-clock flop in the source payload register's fanin whose own
    input fanin reaches a dst-clock flop's ``Q`` — the ``ack_sync``
    register. CDC-012 stays silent whenever that feedback path is
    reachable from *this crossing's* source flop.

    Detection (v1):

    * Multi-bit (`width > 1`) async crossing with a non-trivial src
      flop.
    * Crossing passes :func:`_is_gated_bus_crossing` (so CDC-004 is
      silent — without that the crossing is already flagged
      elsewhere; CDC-012 does not duplicate CDC-004's territory).
    * The source flop's register-neighbourhood has no path back to a
      ``dst_clock`` flop (no handshake feedback) — see
      :func:`_has_dst_to_src_feedback`.

    Suppressed when either endpoint of the crossing is tagged
    ``(* cdc_handshake *)`` (issue #247): the attribute vouches a
    sanctioned req/ack primitive whose synced-back ack holds the
    payload, which is precisely CDC-012's hold guarantee — but the
    structural feedback walk can't always see the in-primitive ack, so
    the annotation is honoured directly (as CDC-001/013/014/020 do).

    Severity ``warning`` — the rule's structural heuristic for
    "handshake present" can't see application-level guarantees (e.g. a
    slow-write config-register bus where the host writes once and waits
    many src cycles), which correctly trip the rule. ``warning`` invites
    review rather than declaring an unambiguous bug.
    """
    if ctx is None:
        ctx = _build_context(module, clock_spec)

    out: list[Violation] = []
    flops_by_name = {f.cell.name: f for f in ctx.flops}
    # Feedback presence is a crossing-level property: cache per source
    # flop, not per domain pair. A per-domain-pair cache would let one
    # crossing's handshake silence an unrelated broken crossing between
    # the same clocks (rtl-buddy-cdc#239).
    feedback_cache: dict[str, bool] = {}

    for c in crossings:
        if c.src_clock == UNCONSTRAINED_SENTINEL:
            continue
        if c.src_flop is None or c.width <= 1:
            continue
        if not _is_gated_bus_crossing(module, c, ctx.domains, ctx.bit_drivers):
            continue
        # Gray-encoded sources are exempt: at most one bit changes
        # per src cycle, so any dst-side sample mid-transition lands
        # on either the previous gray code or the next — never an
        # incoherent mix. CDC-012's failure mode doesn't apply.
        # Honour both the structural detector (canonical
        # ``g = b ^ (b >> 1)`` shape in src fanin) and the
        # ``(* cdc_gray *)`` user annotation.
        if c.src_flop.cell.name in ctx.user_grays:
            continue
        if _is_gray_encoded_source(module, c.src_flop, ctx.bit_drivers):
            continue
        # A (* cdc_handshake *)-vouched req/ack primitive holds the
        # source payload via its synced-back ack/backpressure until the
        # destination captures — exactly the guarantee CDC-012 checks
        # for. The structural feedback walk can't always see the
        # in-primitive ack (it depends on how the toggle/ack flops lower,
        # and differs across frontends), so honour the annotation the
        # same way CDC-001/013/014/020 do (#247). Either endpoint of the
        # crossing being a tagged participant marks the whole crossing as
        # part of the sanctioned primitive.
        if (
            c.src_flop.cell.name in ctx.user_handshakes
            or c.dst_flop.cell.name in ctx.user_handshakes
        ):
            continue
        key = c.src_flop.cell.name
        if key not in feedback_cache:
            feedback_cache[key] = _has_dst_to_src_feedback(
                module, c, ctx.domains, ctx.bit_drivers, flops_by_name
            )
        if feedback_cache[key]:
            continue
        out.append(
            Violation(
                rule_id="CDC-012",
                severity="warning",
                message=(
                    f"functional data-hold risk on {c.src_flop.name!r} "
                    f"→ {c.dst_flop.name!r}: {c.width}-bit gated bus "
                    f"crossing from {c.src_clock!r} to {c.dst_clock!r} "
                    f"has no synced-back handshake between the two "
                    f"domains. The destination's enable is "
                    f"synchronised, but nothing keeps the source "
                    f"payload stable while the enable is in flight — "
                    f"a payload change between request and capture "
                    f"silently corrupts the latched value. Add a "
                    f"req/ack handshake: hold the source payload "
                    f"until an ack from the destination "
                    f"(synchronised back through a 2FF chain) "
                    f"confirms the sample."
                ),
                crossing=c,
                cell_name=c.src_flop.cell.name,
            )
        )
    return out


def check_cdc_014(
    module: Module,
    crossings: list[Crossing],
    clock_spec: ClockSpec | None = None,  # noqa: ARG001
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """CDC-014 — Combinational logic *between* synchroniser stages.

    Distinct hazard from CDC-003 (comb feeding the chain's first
    stage). Here the offending shape is::

        async_signal → stage_1 → gate → stage_2.D

    The gate is the bug: stage_1's Q may still be metastable when the
    gate output is sampled by stage_2 on the next clock edge, so
    the gate output can be a transient mix. The full clock period
    of resolution time the user thinks they're buying is destroyed
    by the gate's propagation delay budget.

    Detection reuses :func:`_chain_has_inter_stage_comb` — the same
    helper CDC-001 consults for its deferral when ``_sync_chain_depth``
    says "depth = 1" but a follow-on flop *is* present behind a gate.
    Severity ``error`` — same physics class as CDC-001 (silently
    broken synchroniser).

    Suppression follows the existing user-vetted hatch:
    ``(* cdc_sync *)`` on the chain head asserts user intent, and the
    rule skips the chain.

    Single-bit only — multi-bit syncs collapse into per-bit chains in
    the netlist and the inter-stage helper keys off a 1-bit head;
    multi-bit support is a follow-up if/when the case shows up.
    """
    if ctx is None:
        ctx = _build_context(module, clock_spec)
    violations: list[Violation] = []

    for c in crossings:
        if c.width != 1:
            continue
        head = c.dst_flop
        if head.cell.name in ctx.user_syncs:
            continue
        if head.cell.name in ctx.user_handshakes:
            continue  # (* cdc_handshake *) — post-capture decode comb is datapath (#247)
        nxt = _chain_has_inter_stage_comb(head, ctx)
        if nxt is None:
            continue
        violations.append(
            Violation(
                rule_id="CDC-014",
                severity="error",
                message=(
                    f"combinational logic between synchroniser stages on "
                    f"{c.src_clock} → {c.dst_clock}: head {head.name} "
                    f"feeds a comb cell whose output reaches {nxt.name} "
                    f"(same clock domain). The gate's propagation delay "
                    f"can sample a metastable head output, destroying the "
                    f"full-cycle resolution time the chain was meant to "
                    f"provide. Remove the gate and place it after the "
                    f"chain's final stage."
                ),
                crossing=c,
                cell_name=head.cell.name,
            )
        )
    return violations


def check_cdc_015(
    module: Module,
    crossings: list[Crossing],
    clock_spec: ClockSpec | None = None,
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """CDC-015 — Sync chain asynchronously reset from a foreign clock domain.

    A synchroniser whose resolving flops are released by an async
    reset from a *different* clock domain cannot reach steady state
    on its own clock — every foreign-domain reset deassertion races
    the dst-domain clock edge, restoring the flops at an arbitrary
    point in pointer-value space.

    CDC-shaped framing for the same underlying structural shape
    RDC-001 detects on the reset path: the offended structure is a
    synchroniser, the fix is "use dst_clk's reset for the chain", not
    "add a reset synchroniser to the reset path". The two findings
    are independent and can coexist — users can act on either one.

    Walks each crossing's chain via :func:`_sync_chain_flops` and
    checks every chain flop's ``ARST`` pin. Fires (one finding per
    chain) when any chain flop's reset reaches back to a flop in a
    clock domain async to ``c.dst_clock``. Suppressed when the chain
    head is ``(* cdc_sync *)``-marked — user vouches for the chain
    shape including its reset routing.

    Single-bit only — multi-bit chain support is deferred (multi-bit
    syncs typically use the same reset on every lane, so the framing
    should generalise without much code).
    """
    if ctx is None:
        ctx = _build_context(module, clock_spec)
    violations: list[Violation] = []
    reported_heads: set[str] = set()

    def _async(a: str, b: str) -> bool:
        if clock_spec is None:
            return a != b
        ca = clock_spec.clock_for_port(a) or a
        cb = clock_spec.clock_for_port(b) or b
        return clock_spec.are_async(ca, cb)

    for c in crossings:
        if c.width != 1:
            continue
        head = c.dst_flop
        if head.cell.name in reported_heads:
            continue
        if head.cell.name in ctx.user_syncs:
            continue
        chain = _sync_chain_flops(
            module,
            head,
            c.dst_clock,
            ctx.domains,
            ctx.reader_counts,
            ctx.d_bit_to_single_bit_flop,
        )
        if len(chain) < 2:
            continue

        offending: set[tuple[str, str]] = set()
        for chain_flop in chain:
            arst_bits = chain_flop.cell.connections.get("ARST", ())
            if not arst_bits:
                continue
            fanin = _backward_flop_fanin(module, arst_bits, ctx.bit_drivers)
            for src_name in fanin:
                src_clk = ctx.domains.get(src_name)
                if src_clk is None or src_clk == c.dst_clock:
                    continue
                if not _async(src_clk, c.dst_clock):
                    continue
                offending.add((src_name, src_clk))

        if not offending:
            continue
        reported_heads.add(head.cell.name)
        srcs_desc = ", ".join(f"{name} ({clk})" for name, clk in sorted(offending))
        violations.append(
            Violation(
                rule_id="CDC-015",
                severity="error",
                message=(
                    f"synchroniser chain in {c.dst_clock} (head "
                    f"{head.name}) has ARST driven by foreign-domain "
                    f"source(s): {srcs_desc}. The chain cannot reach "
                    f"steady state because its resolving flops are "
                    f"asynchronously released by a reset that is not "
                    f"in {c.dst_clock}'s reset tree. Use {c.dst_clock}'s "
                    f"reset (or a reset synchronised into "
                    f"{c.dst_clock}) for the sync chain."
                ),
                crossing=c,
                cell_name=head.cell.name,
            )
        )
    return violations


def _clk_polarity(flop: Flop) -> int:
    """Return 1 for posedge / 0 for negedge.

    Resolution order:

    1. Parametric Yosys FF cells (``$dff`` / ``$adff`` / ``$sdff`` /
       ``$dffe`` / etc.) — read the ``CLK_POLARITY`` parameter.
       Yosys encodes scalar params as ``"0"`` or ``"1"``.
    2. Gate-level cells (``$_DFF_N_``, ``$_DFF_P_``, ``$_DFFE_PP0P_``,
       ``$_SDFFE_PP0P_``, ``$_ALDFF_*_``, …) — the first letter after
       the family prefix encodes the clock-edge polarity (``P`` /
       ``N``). Covered by walking the underscore-separated segments
       and picking the first ``P`` / ``N`` token.
    3. Fall back to positive — unknown cell types are treated as
       posedge, which keeps the rule false-negative-biased on
       library cells the heuristic can't classify.
    """
    params = flop.cell.parameters or {}
    p = params.get("CLK_POLARITY")
    if p is not None:
        return 1 if p == "1" else 0
    for seg in flop.cell.type.split("_"):
        if seg == "P":
            return 1
        if seg == "N":
            return 0
    return 1


def _clk_polarity_str(flop: Flop) -> str:
    return "posedge" if _clk_polarity(flop) == 1 else "negedge"


def check_cdc_016(
    module: Module,
    crossings: list[Crossing],
    clock_spec: ClockSpec | None = None,  # noqa: ARG001
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """CDC-016 — Opposite-edge synchroniser halves MTBF.

    A two-stage synchroniser whose adjacent stages sample on opposite
    edges of the same clock gives each metastable value only half a
    clock period to resolve. The mean-time-between-failure is roughly
    halved versus a same-edge chain; the failure is silent because
    the RTL looks syntactically symmetric.

    Walks each recognised sync chain starting from every crossing's
    destination flop (via :func:`_sync_chain_flops`). Fires when two
    *adjacent* flops in the chain disagree on clock-pin polarity.
    Severity ``error`` — same physics class as CDC-001 (silently
    broken synchroniser).

    Suppression composes with the existing ``(* cdc_sync *)`` escape
    hatch: a marked head asserts user intent for the whole chain and
    the rule skips it. Same dst-flop-keyed convention as CDC-001..003.
    """
    if ctx is None:
        ctx = _build_context(module, clock_spec)
    violations: list[Violation] = []
    reported_heads: set[str] = set()

    for c in crossings:
        if c.width != 1:
            continue
        head = c.dst_flop
        if head.cell.name in reported_heads:
            continue
        if head.cell.name in ctx.user_syncs:
            continue
        chain = _sync_chain_flops(
            module,
            head,
            c.dst_clock,
            ctx.domains,
            ctx.reader_counts,
            ctx.d_bit_to_single_bit_flop,
        )
        if len(chain) < 2:
            continue
        for i in range(len(chain) - 1):
            a, b = chain[i], chain[i + 1]
            if _clk_polarity(a) != _clk_polarity(b):
                reported_heads.add(head.cell.name)
                violations.append(
                    Violation(
                        rule_id="CDC-016",
                        severity="error",
                        message=(
                            f"opposite-edge synchroniser on "
                            f"{c.src_clock} → {c.dst_clock}: chain "
                            f"stage {a.name} ({_clk_polarity_str(a)}) "
                            f"and {b.name} ({_clk_polarity_str(b)}) "
                            f"sample on different edges of "
                            f"{c.dst_clock} — halves MTBF"
                        ),
                        crossing=c,
                        cell_name=head.cell.name,
                    )
                )
                break
    return violations


def check_cdc_017(
    module: Module,
    crossings: list[Crossing],  # noqa: ARG001
    clock_spec: ClockSpec | None = None,
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """CDC-017 — transparent latch in a CDC path.

    A ``$dlatch`` (or gate-level ``$_DLATCH_*``) sitting between a
    source flop in clock domain A and a destination flop in clock
    domain B != A defeats CDC synchronisation:

    - During the latch's enable-active phase, a metastable source
      signal propagates *transparently* through to the destination
      flop's D pin. The latch provides no resolution time at all.
    - During the enable-inactive phase, the destination samples the
      held value — fine, but only the cycles you happen to land
      there.

    ``find_crossings`` keys off flop-to-flop fanin and stops at the
    latch (the latch's Q is not a flop output the chain walker
    follows). Without this rule the entire CDC bug is silent —
    zero crossings, zero findings — even though the destination
    flop is sampling a foreign-domain signal every cycle.

    Detection: walk every ``$dlatch`` / ``$_DLATCH_*`` cell. For
    each, identify the dst flop reading its Q (direct D-pin fanin
    on a single-bit follow-on flop) and the src flop driving its
    D (via :func:`_backward_flop_fanin`). If src_clock !=
    dst_clock — fire.

    Severity ``error`` — same physics class as CDC-001 (silently
    broken synchroniser). Suppressed when the destination flop is
    marked ``(* cdc_sync *)`` (user vouches for the shape — e.g. a
    latch-based glitchless mux that's safe by other arguments).
    """
    if ctx is None:
        ctx = _build_context(module, clock_spec)

    violations: list[Violation] = []
    # Dedup at (latch_cell, src_clock, dst_clock) so a multi-bit
    # latch reaches the user as one finding, not N.
    seen: set[tuple[str, str, str]] = set()

    for cell in module.cells.values():
        if not is_latch_cell(cell.type):
            continue
        q_bits = cell.connections.get("Q", ())
        d_bits = cell.connections.get("D", ())
        if not q_bits or not d_bits:
            continue

        # Identify the dst flop(s) reading this latch's Q.
        dst_clocks: dict[str, Flop] = {}
        for q_bit in q_bits:
            if not isinstance(q_bit, int):
                continue
            dst = ctx.d_bit_to_single_bit_flop.get(q_bit)
            if dst is None:
                continue
            if dst.cell.name in ctx.user_syncs:
                continue  # user-vouched destination
            dst_clk = ctx.domains.get(dst.cell.name)
            if dst_clk is None:
                continue
            dst_clocks.setdefault(dst_clk, dst)
        if not dst_clocks:
            continue

        # Identify the src flop(s) driving this latch's D, walking
        # back through any intermediate comb.
        d_int_bits = tuple(b for b in d_bits if isinstance(b, int))
        if not d_int_bits:
            continue
        src_flop_names = _backward_flop_fanin(module, d_int_bits, ctx.bit_drivers)
        if not src_flop_names:
            continue
        src_clocks: dict[str, str] = {}
        for src_name in src_flop_names:
            src_clk = ctx.domains.get(src_name)
            if src_clk is None:
                continue
            src_clocks.setdefault(src_clk, src_name)
        if not src_clocks:
            continue

        for dst_clk, dst_flop in dst_clocks.items():
            for src_clk, src_name in src_clocks.items():
                if src_clk == dst_clk:
                    continue
                key = (cell.name, src_clk, dst_clk)
                if key in seen:
                    continue
                seen.add(key)
                violations.append(
                    Violation(
                        rule_id="CDC-017",
                        severity="error",
                        message=(
                            f"transparent latch in CDC path "
                            f"{src_clk} → {dst_clk}: latch "
                            f"{cell.name} feeds destination flop "
                            f"{dst_flop.name} (in {dst_clk}) from "
                            f"source flop {src_name} (in {src_clk}). "
                            f"During the latch's enable-active phase "
                            f"the foreign-domain signal propagates "
                            f"transparently to the destination — no "
                            f"metastability resolution. Replace the "
                            f"latch with a 2FF synchroniser flop chain "
                            f"on {dst_clk}."
                        ),
                        cell_name=cell.name,
                    )
                )

    return violations


def check_cdc_019(
    module: Module,
    crossings: list[Crossing],
    clock_spec: ClockSpec | None = None,
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """CDC-019 — Independently-synced one-hot decode across CDC.

    Fires when N≥2 single-bit source-domain flops sharing a common
    combinational driver (a one-hot decoder, priority arbiter, case-
    statement output, etc.) each have an async crossing to flops in
    the same destination clock domain. CDC-004 misses this shape
    because each source flop is structurally 1-bit — the lanes are
    *related* in the comb logic upstream, but the multi-bit-bus
    detector only sees N independent 1-bit crossings.

    The destination side sees a vector of independently-resolved
    bits. Every transition where ≥2 source bits change simultaneously
    can be sampled as an *intermediate* value at the destination
    (e.g. a 4-bit one-hot transitioning ``0001 → 0100`` can be
    sampled as ``0101`` or ``0000`` when the per-lane sync chains
    resolve out of phase).

    Severity ``warning`` — the pattern is sometimes intentional
    (e.g. one-hot encodings where the destination only reads one bit
    at a time, or where a separate handshake gates the dst sample).
    The textbook fixes are (a) gray-code the source (`(* cdc_gray *)`
    on the registering flops, or refactor to encode the value rather
    than the decoded one-hot) or (b) add a req/ack handshake so the
    destination only samples after the source guarantees the lanes
    are stable together.

    Detection groups by ``(driver_comb_cell, dst_clock)``: one
    finding per shared decoder per destination domain, listing every
    affected lane.

    Suppressed when:

    * any source flop is marked ``(* cdc_gray *)`` (user vouches the
      multi-bit coherence is handled),
    * any source flop is marked ``(* cdc_static *)`` (quasi-static
      doesn't transition),
    * any source flop is marked ``(* cdc_sync *)`` (the flop is itself
      a vetted sync stage and the rule's framing doesn't apply), or
    * the immediate driver is itself a flop (chained registers are
      CDC-001 / CDC-002's territory, not a shared decoder).
    """
    if ctx is None:
        ctx = _build_context(module, clock_spec)

    # Group: (driver_cell_name, dst_clock) → ordered list of (src_flop_name, dst_flop_name)
    # Use dict-of-lists keyed by src_name to dedupe lanes coming through
    # multiple crossings to the same dst clock.
    groups: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
    suppressed_groups: set[tuple[str, str]] = set()

    for crossing in crossings:
        if crossing.src_flop is None:
            continue
        src_flop = crossing.src_flop
        src_name = src_flop.cell.name
        src_d = src_flop.d
        if len(src_d) != 1 or not isinstance(src_d[0], int):
            continue  # multi-bit source is CDC-004's domain
        drv = ctx.bit_drivers.get(src_d[0])
        if drv is None:
            continue
        drv_cell_name, drv_port, _drv_idx = drv
        if drv_port != "Y":
            continue  # only true comb-cell outputs count
        drv_cell = module.cells.get(drv_cell_name)
        if drv_cell is None:
            continue
        if is_ff_cell(drv_cell.type):
            continue
        # Decoder shape requires the driver cell to have ≥2 output bits
        # — a WIDTH=1 gate fanning out to multiple flops is just normal
        # fan-out, not a one-hot decode.
        y_bits = drv_cell.connections.get("Y", ())
        int_y_bits = [b for b in y_bits if isinstance(b, int)]
        if len(int_y_bits) < 2:
            continue
        key = (drv_cell_name, crossing.dst_clock)
        # Suppression: any participating src flop tagged by the user.
        if (
            src_name in ctx.user_grays
            or src_name in ctx.user_statics
            or src_name in ctx.user_syncs
        ):
            suppressed_groups.add(key)
            continue
        groups[key][src_name] = crossing.dst_flop.cell.name

    violations: list[Violation] = []
    for (driver, dst_clock), lanes in sorted(groups.items()):
        if (driver, dst_clock) in suppressed_groups:
            continue
        if len(lanes) < 2:
            continue
        src_names = sorted(lanes.keys())
        lane_desc = ", ".join(src_names[:4]) + (
            f", ... ({len(src_names)} lanes total)" if len(src_names) > 4 else ""
        )
        violations.append(
            Violation(
                rule_id="CDC-019",
                severity="warning",
                message=(
                    f"independently-synced one-hot decode across CDC: comb "
                    f"cell {driver} drives {len(src_names)} separate WIDTH=1 "
                    f"source flops ({lane_desc}) that each cross independently "
                    f"to clock domain {dst_clock}. The shared decoder makes "
                    f"the lanes change together at the source, but each "
                    f"destination sync chain resolves on its own schedule — "
                    f"intermediate combinations the encoder never emits can "
                    f"appear transiently at the destination. Fix: gray-code "
                    f"the source (mark the registering flops `(* cdc_gray *)` "
                    f"or refactor to encode the value rather than the decoded "
                    f"one-hot), or gate the destination sample behind a "
                    f"req/ack handshake."
                ),
                cell_name=src_names[0],
            )
        )
    return violations


def check_cdc_020(
    module: Module,
    crossings: list[Crossing],
    clock_spec: ClockSpec | None = None,
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """CDC-020 — Sliced-bus reconvergence across CDC.

    Fires when a genuinely-multi-bit source flop (``WIDTH≥2``) has
    its bits sliced into N≥2 width=1 crossings that each independently
    cross to flops in the same destination clock domain. CDC-004
    misses this shape because each crossing's width is 1 — the
    multi-bit-bus detector's ``width <= 1`` skip drops every per-lane
    crossing even though the source bus is genuinely multi-bit.

    Sibling of CDC-019: same per-lane-independent-sync hazard, but the
    source is a true multi-bit register rather than a shared
    combinational decoder. The destination resolves each lane on its
    own schedule, so transitions where ≥2 source bits change
    simultaneously can be sampled at the destination as intermediate
    combinations the source never emits.

    Severity ``warning`` — sometimes intentional (the destination
    only reads one bit at a time, or a separate handshake gates the
    sample). Textbook fixes: gray-code the source (mark with
    ``(* cdc_gray *)``), use a handshake, or replace the per-lane
    sync chains with a single multi-bit sync chain.

    Detection groups by ``(src_flop, dst_clock)``: one finding per
    affected source register per destination domain, listing every
    sliced lane.

    Suppressed when:

    * source flop is marked ``(* cdc_gray *)`` (gray-coded bus
      handles per-bit coherence) or ``(* cdc_static *)`` (quasi-
      static), or
    * source flop is structurally gray-encoded
      (:func:`_is_gray_encoded_source`) and at least one per-lane
      destination is a multi-bit sync first stage
      (:func:`_is_multibit_sync_first_stage`).
    """
    if ctx is None:
        ctx = _build_context(module, clock_spec)

    groups: dict[tuple[str, str], dict[str, Flop]] = defaultdict(dict)
    src_flop_lookup: dict[str, Flop] = {}
    for c in crossings:
        if c.src_flop is None:
            continue
        src_flop = c.src_flop
        if len(src_flop.q) < 2:
            continue  # truly width-1 source — CDC-001/004's domain
        src_name = src_flop.cell.name
        src_flop_lookup[src_name] = src_flop
        key = (src_name, c.dst_clock)
        groups[key][c.dst_flop.cell.name] = c.dst_flop

    violations: list[Violation] = []
    for (src_name, dst_clock), dst_map in sorted(groups.items()):
        if len(dst_map) < 2:
            continue  # not sliced — single dst recombines it natively
        src_flop = src_flop_lookup[src_name]
        if src_name in ctx.user_grays:
            continue
        if src_name in ctx.user_statics:
            continue
        if src_name in ctx.user_handshakes:
            continue  # (* cdc_handshake *) — payload held stable across req/ack window (#247)
        # Structural gray + multi-bit-sync suppression — mirrors CDC-004.
        if _is_gray_encoded_source(module, src_flop, ctx.bit_drivers) and any(
            _is_multibit_sync_first_stage(module, dst, dst_clock, ctx.domains)
            for dst in dst_map.values()
        ):
            continue
        lanes = sorted(dst_map.keys())
        lane_desc = ", ".join(lanes[:4]) + (
            f", ... ({len(lanes)} lanes total)" if len(lanes) > 4 else ""
        )
        violations.append(
            Violation(
                rule_id="CDC-020",
                severity="warning",
                message=(
                    f"sliced-bus reconvergence across CDC: source flop "
                    f"{src_name} (WIDTH={len(src_flop.q)}) is sliced into "
                    f"{len(lanes)} per-lane crossings to clock domain "
                    f"{dst_clock} ({lane_desc}). Each lane resolves "
                    f"metastability on its own schedule — transitions "
                    f"where ≥2 source bits change simultaneously can be "
                    f"sampled at the destination as intermediate "
                    f"combinations the source never emits. Fix: gray-code "
                    f"the source (mark `(* cdc_gray *)`), use a req/ack "
                    f"handshake, or replace the per-lane sync chains with "
                    f"a single multi-bit sync chain."
                ),
                cell_name=src_name,
            )
        )
    return violations


def check_cdc_021(
    module: Module,
    crossings: list[Crossing],  # noqa: ARG001
    clock_spec: ClockSpec | None = None,
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """CDC-021 — Flop CLK driven by undeclared top-level port.

    Fires when a flop's ``CLK`` pin traces back to a top-level input
    port that has no ``create_clock`` declaration in the SDC. CDC-011
    is the data-pin equivalent (port reaches ``D`` without a
    ``set_input_delay -clock``); CDC-021 closes the clock-pin side.

    The failure mode is silent. An undeclared port-as-clock doesn't
    appear in any ``set_clock_groups -asynchronous`` declaration, so
    ``are_async`` returns False against every declared clock and
    ``_filter_async`` drops every crossing involving the undeclared
    domain — the rest of the rule pack stays completely silent on
    flops in that domain. CDC-021 surfaces the methodology bug
    (missing ``create_clock``) so the user can declare the clock and
    let the other rules do their job.

    Severity ``error`` — an undeclared clock disables every other
    CDC check that touches the domain; this is methodology-broken,
    not stylistic.

    Detection groups by ``(undeclared_port, list_of_consumer_flops)``:
    one finding per affected port, regardless of how many flops live
    in that domain.

    Skipped when no SDC was supplied (the existing convention: no
    SDC → no rules fire). Generated clocks declared via
    ``[get_pins ...]`` are handled implicitly — the domain name they
    yield is a clock name, not a port name, so the port-membership
    check filters them out.
    """
    if clock_spec is None:
        return []
    if ctx is None:
        ctx = _build_context(module, clock_spec)

    # All top-level input port names that appear as the source of a
    # `create_clock` (any one of its `ports` tuple).
    declared_clock_ports: set[str] = set()
    for clk in clock_spec.clocks.values():
        declared_clock_ports.update(clk.ports)

    undeclared: dict[str, list[str]] = defaultdict(list)
    for f in ctx.flops:
        domain = ctx.domains.get(f.cell.name)
        if domain is None:
            continue
        port = module.ports.get(domain)
        if port is None or port.direction != "input":
            continue  # not a port — e.g. an internal generated clock
        if domain in declared_clock_ports:
            continue  # has a create_clock — the rule's only condition fails
        undeclared[domain].append(f.cell.name)

    violations: list[Violation] = []
    for port_name, consumers in sorted(undeclared.items()):
        flop_names = sorted(set(consumers))
        if len(flop_names) == 1:
            consumer_desc = f"flop {flop_names[0]}"
        else:
            consumer_desc = (
                f"{len(flop_names)} flops ({', '.join(flop_names[:3])}"
                + (", ..." if len(flop_names) > 3 else "")
                + ")"
            )
        violations.append(
            Violation(
                rule_id="CDC-021",
                severity="error",
                message=(
                    f"flop CLK driven by undeclared port: "
                    f"top-level input port '{port_name}' drives "
                    f"{consumer_desc} but has no create_clock "
                    f"declaration in the SDC. All other CDC checks "
                    f"that touch this domain are silently skipped "
                    f"(undeclared clocks don't appear in any async "
                    f"group, so cross-domain crossings are dropped "
                    f"before any rule sees them). Add "
                    f"`create_clock -name {port_name} -period <T> "
                    f"[get_ports {port_name}]` to the SDC."
                ),
                cell_name=flop_names[0],
            )
        )
    return violations


# CDC-018 default chain-depth threshold. A 2FF sync is the textbook
# minimum (so chains of depth 2 or 3 are accepted); chains of depth
# 4+ are flagged as quality-of-life smells. Configurable via
# ``run_all(cdc_018_depth_threshold=N)``.
CDC_018_DEFAULT_THRESHOLD = 4


def check_cdc_018(
    module: Module,
    crossings: list[Crossing],
    clock_spec: ClockSpec | None = None,
    depth_threshold: int = CDC_018_DEFAULT_THRESHOLD,
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """CDC-018 — Cascaded synchroniser warning.

    Fires when a CDC crossing's destination-domain sync chain depth
    reaches ``depth_threshold`` (default 4) — the classic "two
    engineers each added their own 2FF sync" or "left the original
    chain in place during a refactor" smell. The chain still works
    (extra latency, slightly worse MTBF tail), so this is a quality-
    of-life check, not a functional bug.

    Severity ``warning`` — surfaces the smell without forcing a fix.
    Designs that legitimately want deeper chains (high-MTBF
    requirements) can raise the threshold or mark the chain head
    ``(* cdc_sync *)`` to suppress.

    Detection walks each crossing's dst flop through
    :func:`_sync_chain_depth`; the walker only follows pure flop→flop
    same-domain hops with a single reader (so a chain whose tail is
    used by anything else terminates correctly and doesn't trip the
    rule on legitimate fanout). Groups by ``(src_flop, dst_clock)``
    so a sliced-bus shape doesn't multi-fire.

    Suppression:

    * Chain head marked ``(* cdc_sync *)``: user has explicitly vetted
      the chain (including its depth).
    * Depth threshold defaults to 4 — set ``depth_threshold`` higher
      for designs that intentionally run deeper chains.
    """
    if depth_threshold < 2:
        raise ValueError("CDC-018 depth_threshold must be >= 2")
    if ctx is None:
        ctx = _build_context(module, clock_spec)

    # Per-(src_flop_name, dst_clock) bookkeeping: pick the deepest chain
    # in the group so we report a single, maximal finding per group.
    best: dict[tuple[str, str], tuple[int, Flop]] = {}
    for c in crossings:
        if c.src_flop is None or c.dst_flop is None:
            continue
        head = c.dst_flop
        if head.cell.name in ctx.user_syncs:
            continue
        if len(head.q) != 1 or len(head.d) != 1:
            continue
        if not isinstance(head.q[0], int) or not isinstance(head.d[0], int):
            continue
        depth = _sync_chain_depth(
            module,
            head,
            c.dst_clock,
            ctx.domains,
            ctx.reader_counts,
            d_bit_to_single_bit_flop=ctx.d_bit_to_single_bit_flop,
        )
        if depth < depth_threshold:
            continue
        key = (c.src_flop.cell.name, c.dst_clock)
        prev = best.get(key)
        if prev is None or depth > prev[0]:
            best[key] = (depth, head)

    violations: list[Violation] = []
    for (src_name, dst_clock), (depth, head) in sorted(best.items()):
        violations.append(
            Violation(
                rule_id="CDC-018",
                severity="warning",
                message=(
                    f"cascaded synchroniser chain: {depth}-flop chain "
                    f"in {dst_clock} starting at {head.cell.name} (source: "
                    f"{src_name}). The textbook 2FF sync is sufficient; "
                    f"every flop beyond the first 2 adds latency without "
                    f"improving metastability resolution. Common causes: "
                    f"two engineers each adding their own sync, or a "
                    f"refactor leaving the original chain in place. "
                    f"Trim the chain to 2 flops, or mark the chain head "
                    f"`(* cdc_sync *)` if the depth is intentional "
                    f"(high-MTBF designs)."
                ),
                cell_name=head.cell.name,
            )
        )
    return violations


def check_cdc_022(
    module: Module,
    crossings: list[Crossing],  # noqa: ARG001
    clock_spec: ClockSpec | None = None,  # noqa: ARG001
    required_depth: int = 2,
    *,
    ctx: _RuleContext | None = None,  # noqa: ARG001
    sync_primitives: frozenset[str] = frozenset(),
) -> list[Violation]:
    """CDC-022 — Recognised CDC primitive with insufficient sync depth.

    The blackbox analogue of CDC-002 (issue #275). A sanctioned CDC
    macro — the ``xpm_cdc_*`` family, or a site registration via
    ``--sync-primitive`` — carries its synchroniser stage count as a
    *parameter* (``DEST_SYNC_FF``, plus ``SRC_SYNC_FF`` on
    ``xpm_cdc_handshake``), not as a flop chain the analyzer can walk.
    Once the macro is recognised as a synchroniser its crossing stops
    being reported, so without this rule a project that requires
    ``--sync-depth 3`` would silently accept every ``DEST_SYNC_FF=2``
    instance in the design. CDC-022 restores that check at the only
    place the depth is visible: the instance parameter.

    Severity ``warning`` (``--strict`` promotes to ``error``), matching
    CDC-002 — a 2-stage synchroniser is correct engineering at most
    clock rates; the finding is "shallower than *this project* asked
    for", not "broken".

    Reads only ``Cell.type`` / ``Cell.parameters``, so it fires whether
    or not a blackbox sibling module was loaded for the macro. An XPM
    instantiation that leaves the parameter at its default gets UG974's
    documented default (4) rather than being skipped; a user-registered
    primitive with no ``DEST_SYNC_FF`` override is skipped (no known
    default to assume).
    """
    violations: list[Violation] = []
    for inst_name, cell in sorted(module.cells.items()):
        if not is_sync_primitive(cell.type, sync_primitives):
            continue
        # XPM documents a 2..10 range for its depth parameters; a
        # site-registered primitive has no range we can quote.
        range_hint = " (XPM accepts 2..10)" if is_xpm_primitive(cell.type) else ""
        for param, depth in sorted(
            primitive_sync_depths(cell, sync_primitives).items()
        ):
            if depth >= required_depth:
                continue
            violations.append(
                Violation(
                    rule_id="CDC-022",
                    severity="warning",
                    message=(
                        f"synchroniser primitive `{inst_name}` "
                        f"(`{normalise_primitive_type(cell.type)}`) declares "
                        f"{param}={depth}, below the project's required "
                        f"synchroniser depth of {required_depth}. The macro's "
                        f"stage count is a parameter, not a flop chain, so "
                        f"CDC-002 cannot see it. Raise {param} on the "
                        f"instantiation{range_hint} or lower --sync-depth if "
                        f"{depth} stages are sufficient here."
                    ),
                    cell_name=inst_name,
                )
            )
    return violations


# How many sink-flop cell names a CDC-023 message quotes inline.
_CDC_023_SINK_SAMPLE = 3


def check_cdc_023(
    module: Module,
    crossings: list[Crossing],  # noqa: ARG001
    clock_spec: ClockSpec | None = None,
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """CDC-023 — CLK net driven by a combine of two declared clocks.

    A clock gate (``$and`` / ``$or`` …) or a clock-path transparent latch
    (``$dlatch`` / ``$_DLATCH_*``) whose legs carry **two or more
    distinct declared clocks** mixes clock domains combinationally. The
    resulting net toggles on both clocks, so it glitches, runs at no
    declared frequency, and gives every flop behind it an ambiguous
    domain.

    Issue #263 made the clock-root tracer *decline* on exactly this
    shape — better than silently asserting one leg, but the flop then
    just joined the generic ``domain_unknown`` tally with no cause
    attached. CDC-023 (issue #269) is that decline, named: it reports
    the combining **cell**, the combined **net**, and the two **clocks**
    so the user can fix or waive it instead of hunting through the
    under-resolution list.

    Findings come from :func:`~rtl_buddy_cdc.domain.find_clock_combines`,
    which attaches a recorder to the ordinary clock-root walk and
    reports whatever ``_pick_combining_root`` declined on. The rule
    therefore fires **iff** the tracer declined — one predicate, one
    traversal, no second implementation to drift out of step. In
    particular it inherits the ``clock_identity`` rules for free: a
    plain enable port is not a declared clock, and neither is the
    ``<unconstrained>`` sentinel that
    ``sdc.synthesize_unconstrained_inputs`` stamps on untyped input
    ports, so a normal ICG (clock + untyped enable port) is *not* a
    combine and never fires here. A **mux** never fires either — it
    selects one clock rather than combining them.

    Severity ``warning`` (``--strict`` → error), the deliberate choice
    for a new waivable diagnostic. Combining two clocks is nearly always
    a bug, but the shape also covers deliberate, characterised
    test/debug clock chopping and non-glitch-critical mixes, and a new
    rule id that turns a previously-clean run into a hard `error` on
    landing is the wrong default. Sites that disagree get `error`
    with ``--strict``.

    Skipped when no SDC was supplied: with no declared clocks there are
    no clock identities to combine.
    """
    if clock_spec is None:
        return []
    max_depth = ctx.max_depth if ctx is not None else 16
    violations: list[Violation] = []
    for combine in find_clock_combines(
        module,
        clock_spec.pin_clocks,
        clock_for_port=clock_spec.clock_for_port,
        max_depth=max_depth,
    ):
        shown = combine.sinks[:_CDC_023_SINK_SAMPLE]
        sample = ", ".join(f"`{name}`" for name in shown)
        extra = len(combine.sinks) - len(shown)
        if extra > 0:
            sample += f", +{extra} more"
        clocks = ", ".join(combine.clocks)
        violations.append(
            Violation(
                rule_id="CDC-023",
                severity="warning",
                message=(
                    f"CLK net `{combine.net}` driven by combine of "
                    f"{{{clocks}}} at `{combine.cell}` "
                    f"(`{combine.cell_type}`) — two distinct declared clocks "
                    f"are mixed combinationally onto one clock net. The net "
                    f"toggles on both, so it glitches and runs at no declared "
                    f"frequency; the clock-root tracer declines to assign a "
                    f"domain through it, leaving the "
                    f"{len(combine.sinks)} flop(s) it reaches "
                    f"({sample}) domain-unknown and their crossings "
                    f"unchecked. Gate ONE clock and use the other only as a "
                    f"synchronised enable, or select between them with a "
                    f"glitchless clock mux (a mux selects and is not "
                    f"reported here). Waive CDC-023 if the combine is "
                    f"intentional and characterised."
                ),
                cell_name=combine.cell,
            )
        )
    return violations


RULES: dict[str, RuleFn] = {
    "CDC-001": check_cdc_001,
    "CDC-002": check_cdc_002,
    "CDC-003": check_cdc_003,
    "CDC-004": check_cdc_004,
    "CDC-005": check_cdc_005,
    "CDC-006": check_cdc_006,
    "RDC-001": check_rdc_001,
    "RDC-002": check_rdc_002,
    "RDC-003": check_rdc_003,
    "RDC-004": check_rdc_004,
    "RDC-005": check_rdc_005,
    "RDC-006": check_rdc_006,
    "RDC-007": check_rdc_007,
    "RDC-008": check_rdc_008,
    "CDC-008": check_cdc_008,
    "CDC-009": check_cdc_009,
    "CDC-010": check_cdc_010,
    "CDC-011": check_cdc_011,
    "CDC-012": check_cdc_012,
    "CDC-013": check_cdc_013,
    "CDC-014": check_cdc_014,
    "CDC-015": check_cdc_015,
    "CDC-016": check_cdc_016,
    "CDC-017": check_cdc_017,
    "CDC-018": check_cdc_018,
    "CDC-019": check_cdc_019,
    "CDC-020": check_cdc_020,
    "CDC-021": check_cdc_021,
    "CDC-022": check_cdc_022,
    "CDC-023": check_cdc_023,
}


def _tag_handshake_related(violations: list[Violation]) -> list[Violation]:
    """Reporter refinement (G-5): link CDC-001 / CDC-002 findings to a
    paired CDC-012 finding on the same async domain pair.

    The two rule families catch *different views* of the same
    incomplete-handshake protocol — CDC-012 sees "gated bus with no
    synced-back ack", CDC-001 / CDC-002 sees "src→dst single-bit
    crossing lacks a 2FF sync chain" — and a user looking at one
    finding shouldn't have to mentally correlate the other.

    Detection is by *async domain pair*: for every CDC-012 finding,
    record its ``(src_clock, dst_clock)`` (the gated bus's src→dst
    direction); for every CDC-001 / CDC-002 finding whose
    ``crossing.src_clock`` / ``dst_clock`` matches that pair in either
    direction, append a one-line ``[handshake-related]`` tag naming
    the gated bus's source flop. The tag is a *pure suffix* on
    ``Violation.message`` — no field changes, no new violations.

    No-op when the violations list has no CDC-012 finding (the common
    case).
    """
    cdc_012_by_pair: dict[frozenset[str], Violation] = {}
    for v in violations:
        if v.rule_id != "CDC-012" or v.crossing is None:
            continue
        cdc_012_by_pair.setdefault(
            frozenset({v.crossing.src_clock, v.crossing.dst_clock}), v
        )
    if not cdc_012_by_pair:
        return violations

    out: list[Violation] = []
    for v in violations:
        if v.rule_id not in {"CDC-001", "CDC-002"} or v.crossing is None:
            out.append(v)
            continue
        pair_key = frozenset({v.crossing.src_clock, v.crossing.dst_clock})
        partner = cdc_012_by_pair.get(pair_key)
        if partner is None or partner.crossing is None:
            out.append(v)
            continue
        gated_src = partner.crossing.src_flop
        gated_dst = partner.crossing.dst_flop
        src_name = (
            gated_src.name if gated_src is not None else partner.crossing.src_name
        )
        dst_name = gated_dst.name
        tagged = Violation(
            rule_id=v.rule_id,
            severity=v.severity,
            message=(
                f"{v.message}\n"
                f"  [handshake-related] same async domain pair "
                f"({v.crossing.src_clock} ↔ {v.crossing.dst_clock}) carries "
                f"a CDC-012-firing gated bus crossing "
                f"({src_name} → {dst_name}). The complete fix is a req/ack "
                f"handshake — synchronise the ack back to "
                f"{partner.crossing.src_clock} and use it to retire the "
                f"gated bus."
            ),
            crossing=v.crossing,
            cell_name=v.cell_name,
        )
        out.append(tagged)
    return out


def run_all(
    module: Module,
    crossings: list[Crossing],
    clock_spec: ClockSpec | None = None,
    required_depth: int = 2,
    *,
    reset_hints: ResetHints | None = None,
    cdc_010_heuristic: bool = True,
    cdc_018_depth_threshold: int = CDC_018_DEFAULT_THRESHOLD,
    boundary_modules: frozenset[str] = frozenset(),
    blackbox_modules: frozenset[str] = frozenset(),
    boundary_clock_pins: dict[str, frozenset[str]] | None = None,
    max_depth: int = 16,
    sync_primitives: frozenset[str] = frozenset(),
) -> list[Violation]:
    # Build the cached structural views once and thread them through
    # every rule. See :class:`_RuleContext` for the motivation —
    # before this change, ``assign_domains`` / ``find_flops`` /
    # ``_bit_drivers`` were each rebuilt per rule, with
    # ``_sync_chain_depth`` re-scanning every flop per chain-extension
    # step (the worst hot path).
    ctx = _build_context(
        module,
        clock_spec,
        reset_hints=reset_hints,
        boundary_modules=boundary_modules,
        blackbox_modules=blackbox_modules,
        boundary_clock_pins=boundary_clock_pins,
        max_depth=max_depth,
    )
    out: list[Violation] = []
    for rule_id, rule in RULES.items():
        if rule_id == "CDC-002":
            out.extend(
                check_cdc_002(module, crossings, clock_spec, required_depth, ctx=ctx)
            )
        elif rule_id == "CDC-010":
            out.extend(
                check_cdc_010(
                    module,
                    crossings,
                    clock_spec,
                    ctx=ctx,
                    use_heuristic=cdc_010_heuristic,
                )
            )
        elif rule_id == "CDC-022":
            out.extend(
                check_cdc_022(
                    module,
                    crossings,
                    clock_spec,
                    required_depth,
                    ctx=ctx,
                    sync_primitives=sync_primitives,
                )
            )
        elif rule_id == "CDC-018":
            out.extend(
                check_cdc_018(
                    module,
                    crossings,
                    clock_spec,
                    cdc_018_depth_threshold,
                    ctx=ctx,
                )
            )
        else:
            out.extend(rule(module, crossings, clock_spec, ctx=ctx))
    # Reporter refinement (G-5, rtl-buddy-cdc#214): annotate CDC-001 /
    # CDC-002 findings that share an async domain pair with a CDC-012
    # finding so the user sees the missing-ack relationship without
    # having to correlate the two findings manually.
    return _tag_handshake_related(out)
