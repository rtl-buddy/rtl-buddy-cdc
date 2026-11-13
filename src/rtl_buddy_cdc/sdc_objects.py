"""Resolve SDC design-object constraints against the netlist (rtl-buddy-cdc#328).

:mod:`rtl_buddy_cdc.sdc` is a pure parser: it records an object-scoped
``set_false_path`` (:class:`~rtl_buddy_cdc.sdc.FalsePathException`) and
the object endpoints of ``set_max_delay -datapath_only``
(:class:`~rtl_buddy_cdc.sdc.DatapathOnlyDelay`) as patterns, exactly as
written. This module is the one place those patterns meet the netlist:

1. :func:`resolve` maps every pattern to flops, ports and net bits
   through one name index built once per design
   (:class:`NameIndex`), and reports each pattern that matches nothing.
2. :func:`coverage` decides which crossings each statement covers.
3. :func:`acknowledge` moves the findings an object-scoped false path
   declares reviewed out of the kept list and into the suppressed one,
   with the SDC statement as the justification.

Name matching
-------------

An SDC object name is normalised before lookup: ``\\`` escapes are
dropped and the ``/`` hierarchy separator becomes ``.`` — the separator
both frontends use in flattened names (``u_cfg/reg_q`` → ``u_cfg.reg_q``).
``*`` and ``?`` are globs that never cross a hierarchy level;
``get_* -hierarchical`` lets the pattern match at any depth. ``[…]`` is a
literal bus index.

**Registers** (``get_cells``, and the owner of a ``get_pins``) are named
after their ``Q`` net, not their cell: Yosys calls the cell
``$flatten\\u_cfg.$procdff$8`` and the slang frontend
``u_cfg.$slang$dff$3``, neither of which is the register's name, while
both name the ``Q`` net ``u_cfg.reg_q``. A flop answers to the name of
every non-internal (no ``$``) netname on its ``Q`` whose scope is the
flop's own instance or a generate scope inside it (``u_cfg.g[1].r``) —
not a parent's wire it drives through a port, and not a child
instance's port it feeds. Each such name is indexed bare and with the
synthesis ``_reg`` suffix on its leaf (``u_cfg.reg_q_reg``,
``u_sync.sync_chain_reg[1]`` for an unpacked element). A trailing
``[i]`` selects one bit, numbered by the net's declared range. A cell
whose own name is meaningful (a gate-level netlist read back:
``u_cfg.reg_q_reg[3]``) also answers to that name. Two consequences,
both documented in the architecture article: an output port written
straight from a register is an alias of it (``u_cfg/q_reg`` names the
same flop as ``u_cfg/reg_q_reg``), and a synthesis rename this module
cannot see (``reg_q_reg_3_``, a generate scope flattened to
``g_1__r``) does not match.

**Pins** resolve to the flop owning them — any pin of a register
(``…/Q``, ``…/D``, ``…/CK``) names the register. A pin whose owner is no
register is read as a hierarchical pin of a flattened instance, i.e. the
net of that name (``u_cfg/mode_o`` → net ``u_cfg.mode_o``). **Ports** are
top-level ports. **Nets** are netname bits.

As a ``-from`` endpoint a net names the flop (or input port) driving it;
as a ``-to`` endpoint, the flops whose ``D`` it feeds.
"""

from __future__ import annotations

import bisect
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from rtl_buddy_cdc.domain import Crossing, _build_bit_consumers, _lane_targets
from rtl_buddy_cdc.flops import Flop, find_flops, is_ff_cell
from rtl_buddy_cdc.netlist import Bit, Module
from rtl_buddy_cdc.reporter import _instance_path
from rtl_buddy_cdc.rules import Violation, _bit_drivers
from rtl_buddy_cdc.sdc import (
    ClockSpec,
    DatapathOnlyDelay,
    FalsePathException,
    SdcObject,
)
from rtl_buddy_cdc.waivers import SdcJustification, SuppressedViolation, Waiver

__all__ = [
    "ACKNOWLEDGED_RULES",
    "CrossingCoverage",
    "NameIndex",
    "ResolvedConstraint",
    "Resolution",
    "acknowledge",
    "coverage",
    "crossing_key",
    "domain_map_exceptions",
    "resolve",
    "resolve_port_delays",
]

#: Rules an object-scoped false path acknowledges on a crossing it fully
#: covers — exactly the set ``(* cdc_static *)`` silences for its source
#: flop (``rules.user_static_flop_names``): the missing / short
#: synchroniser and bus-coherence findings, which a source that does not
#: toggle cannot trigger. Every other rule stays live; the table and the
#: reasons are in the architecture article, §6.4.
ACKNOWLEDGED_RULES: frozenset[str] = frozenset(
    {"CDC-001", "CDC-002", "CDC-003", "CDC-004", "CDC-019", "CDC-020"}
)

# Rules among ACKNOWLEDGED_RULES that report on a single attached crossing.
_CROSSING_RULES: frozenset[str] = frozenset(
    {"CDC-001", "CDC-002", "CDC-003", "CDC-004"}
)

# The crossing walk's data-fanout hop budget (``domain.find_crossings``'s
# ``max_hops`` default, which the CLI uses). The ``-through`` test walks
# the same paths, so it must use the same budget.
_CROSSING_MAX_HOPS = 4

_FLATTEN_PREFIX = "$flatten\\"
_GLOB_CHARS = frozenset("*?")
_TRAILING_INDEX_RE = re.compile(r"^(?P<base>.*[^\]])\[(?P<idx>[^\[\]]+)\]$")

# Identity of a data crossing — the same tuple ``domain_map._crossing_key``
# builds: (src_clock, dst_clock, dst cell, source kind, source id).
CrossingKey = tuple[str, str, str, str, str]


def crossing_key(c: Crossing) -> CrossingKey:
    """Identity of ``c``, matching ``domain_map._crossing_key``."""
    if c.src_flop is not None:
        kind, src = "flop", c.src_flop.cell.name
    elif c.src_port is not None:
        kind, src = "port", c.src_port
    elif c.src_boundary is not None:
        kind, src = "boundary", ".".join(c.src_boundary)
    else:
        kind, src = "", ""
    return (c.src_clock, c.dst_clock, c.dst_flop.cell.name, kind, src)


# --- the name index ----------------------------------------------------------

# A table entry: owner (flop cell / port / net name) → {declared index:
# element}. ``element`` is a bit position on the flop / port, or the
# net ``Bit`` itself.
_Entry = dict[str, dict[int, int]]
_NetEntry = dict[int, Bit]


def _normalise(pattern: str) -> str:
    """SDC object name → the netlist's flattened spelling."""
    return pattern.replace("\\", "").replace("/", ".")


def _has_glob(text: str) -> bool:
    return any(ch in _GLOB_CHARS for ch in text)


def _glob_regex(
    pattern: str, *, hierarchical: bool, sep: bool = True
) -> re.Pattern[str]:
    """Compile an SDC glob. ``*`` / ``?`` never cross ``.`` when ``sep``."""
    star, one = ("[^.]*", "[^.]") if sep else (".*", ".")
    body = "".join(
        star if ch == "*" else one if ch == "?" else re.escape(ch) for ch in pattern
    )
    if hierarchical:
        body = r"(?:.*\.)?" + body
    return re.compile(body + r"\Z")


def _leaf_with_reg(name: str) -> str:
    """``u.reg_q`` → ``u.reg_q_reg``; ``u.chain[1]`` → ``u.chain_reg[1]``."""
    scope, dot, leaf = name.rpartition(".")
    head, bracket, tail = leaf.partition("[")
    return f"{scope}{dot}{head}_reg{bracket}{tail}"


def _declared_index(offset: int, upto: bool, width: int, pos: int) -> int:
    return offset + (width - 1 - pos if upto else pos)


class _Table:
    """``name → entry`` with a sorted key list for prefix-bounded globs."""

    def __init__(self, entries: dict[str, dict[str, dict[int, object]]]) -> None:
        self.entries = entries
        self._keys: list[str] | None = None

    def keys(self) -> list[str]:
        if self._keys is None:
            self._keys = sorted(self.entries)
        return self._keys

    def names(self, pattern: str, *, hierarchical: bool) -> list[str]:
        """Every key matching the normalised ``pattern``."""
        if not hierarchical and not _has_glob(pattern):
            return [pattern] if pattern in self.entries else []
        rx = _glob_regex(pattern, hierarchical=hierarchical)
        keys = self.keys()
        if hierarchical:
            lo, hi = 0, len(keys)
        else:
            # Only keys sharing the literal prefix can match: bisect to
            # that slice instead of scanning the whole design.
            cut = min((pattern.index(ch) for ch in _GLOB_CHARS if ch in pattern))
            prefix = pattern[:cut]
            lo = bisect.bisect_left(keys, prefix)
            hi = bisect.bisect_left(keys, prefix + "\U0010ffff")
        return [k for k in keys[lo:hi] if rx.match(k)]


class NameIndex:
    """Registers, ports and nets of one ``Module``, indexed by name.

    Built once per design; each pattern is resolved once and cached, so
    thousands of constraint lines naming overlapping objects cost one
    lookup each, and an exact name is a dict hit. Glob patterns scan only
    the sorted-key slice sharing their literal prefix (a ``-hierarchical``
    or leading-``*`` pattern scans the table once).
    """

    def __init__(self, module: Module) -> None:
        self.module = module
        self.flops: dict[str, Flop] = {f.cell.name: f for f in find_flops(module)}
        self._registers: _Table | None = None
        self._ports: _Table | None = None
        self._nets: _Table | None = None
        self._q_owner: dict[Bit, tuple[str, int]] | None = None
        self._d_owner: dict[Bit, list[tuple[str, int]]] | None = None
        self._cache: dict[tuple[str, str, bool], dict[str, set[object]]] = {}

    # -- tables (built lazily: a design without net patterns never pays
    #    for the net table) ----------------------------------------------

    def _register_table(self) -> _Table:
        if self._registers is not None:
            return self._registers
        module = self.module
        cell_scopes = {name: _instance_path(module, name) for name in module.cells}
        instance_scopes: set[tuple[str, ...]] = set()
        for path in cell_scopes.values():
            for k in range(1, len(path) + 1):
                instance_scopes.add(path[:k])
        bit_names: dict[Bit, list[tuple[str, int]]] = defaultdict(list)
        for nn in module.netnames.values():
            if "$" in nn.name:
                continue  # Yosys / slang internal name, never an SDC name
            for pos, b in enumerate(nn.bits):
                if isinstance(b, int):
                    bit_names[b].append((nn.name, pos))
        entries: dict[str, dict[str, dict[int, object]]] = defaultdict(dict)

        def add(name: str, flop: str, index: int, qpos: int) -> None:
            entries[name].setdefault(flop, {})[index] = qpos

        for fname, flop in self.flops.items():
            scope = cell_scopes.get(fname, ())
            qpos_of = {b: i for i, b in enumerate(flop.q) if isinstance(b, int)}
            aliases: dict[str, list[tuple[int, int]]] = defaultdict(list)
            for b, qpos in qpos_of.items():
                for name, pos in bit_names.get(b, ()):
                    aliases[name].append((pos, qpos))
            kept = [
                n for n in aliases if self._alias_in_scope(n, scope, instance_scopes)
            ]
            for name in kept or list(aliases):
                nn = module.netnames[name]
                width = len(nn.bits)
                for full in (name, _leaf_with_reg(name)):
                    for pos, qpos in aliases[name]:
                        idx = _declared_index(nn.offset, nn.upto, width, pos)
                        add(full, fname, idx, qpos)
            own = self._meaningful_cell_name(fname)
            if own is not None:
                for qpos in qpos_of.values():
                    add(own, fname, qpos, qpos)
        self._registers = _Table(dict(entries))
        return self._registers

    @staticmethod
    def _alias_in_scope(
        name: str, scope: tuple[str, ...], instance_scopes: set[tuple[str, ...]]
    ) -> bool:
        """Is netname ``name`` declared in the flop's own scope?

        Its scope must extend the flop's instance path; any extra
        components must be generate scopes, not another instance (whose
        port the flop merely drives).
        """
        parts = tuple(name.split("."))[:-1]
        if parts[: len(scope)] != scope:
            return False
        return not any(
            parts[:k] in instance_scopes for k in range(len(scope) + 1, len(parts) + 1)
        )

    @staticmethod
    def _meaningful_cell_name(cell_name: str) -> str | None:
        name = cell_name
        if name.startswith(_FLATTEN_PREFIX):
            name = name[len(_FLATTEN_PREFIX) :]
        parts = [p.lstrip("\\") for p in name.split(".")]
        if not parts or not parts[-1] or "$" in parts[-1]:
            return None
        return ".".join(parts)

    def _port_table(self) -> _Table:
        if self._ports is None:
            entries: dict[str, dict[str, dict[int, object]]] = {}
            for pname, port in self.module.ports.items():
                nn = self.module.netnames.get(pname)
                offset, upto = (nn.offset, nn.upto) if nn is not None else (0, False)
                width = len(port.bits)
                entries[pname] = {
                    pname: {
                        _declared_index(offset, upto, width, pos): pos
                        for pos in range(width)
                    }
                }
            self._ports = _Table(entries)
        return self._ports

    def _net_table(self) -> _Table:
        if self._nets is None:
            entries: dict[str, dict[str, dict[int, object]]] = {}
            for nn in self.module.netnames.values():
                if "$" in nn.name:
                    continue
                width = len(nn.bits)
                entries[nn.name] = {
                    nn.name: {
                        _declared_index(nn.offset, nn.upto, width, pos): b
                        for pos, b in enumerate(nn.bits)
                        if isinstance(b, int)
                    }
                }
            self._nets = _Table(entries)
        return self._nets

    def q_owner(self) -> dict[Bit, tuple[str, int]]:
        """Net bit → (flop, Q position) for every flop output bit."""
        if self._q_owner is None:
            self._q_owner = {
                b: (name, i)
                for name, f in self.flops.items()
                for i, b in enumerate(f.q)
                if isinstance(b, int)
            }
        return self._q_owner

    def d_owner(self) -> dict[Bit, list[tuple[str, int]]]:
        """Net bit → every (flop, D position) it feeds."""
        if self._d_owner is None:
            out: dict[Bit, list[tuple[str, int]]] = defaultdict(list)
            for name, f in self.flops.items():
                for i, b in enumerate(f.d):
                    if isinstance(b, int):
                        out[b].append((name, i))
            self._d_owner = dict(out)
        return self._d_owner

    # -- lookups ----------------------------------------------------------

    def _lookup(
        self, table: str, pattern: str, hierarchical: bool
    ) -> dict[str, set[object]]:
        """Owner → selected elements, for every name ``pattern`` matches.

        The whole pattern is tried as a name (an unpacked element is
        indexed as ``chain[1]``), and — when it ends in ``[i]`` — the
        part before the index as a bus, selecting the matching bits.
        """
        key = (table, pattern, hierarchical)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        tbl = {
            "register": self._register_table,
            "port": self._port_table,
            "net": self._net_table,
        }[table]()
        norm = _normalise(pattern)
        out: dict[str, set[object]] = defaultdict(set)
        for name in tbl.names(norm, hierarchical=hierarchical):
            for owner, elems in tbl.entries[name].items():
                out[owner].update(elems.values())
        m = _TRAILING_INDEX_RE.match(norm)
        if m is not None:
            idx_pat = m.group("idx")
            idx_rx = (
                _glob_regex(idx_pat, hierarchical=False, sep=False)
                if _has_glob(idx_pat)
                else None
            )
            for name in tbl.names(m.group("base"), hierarchical=hierarchical):
                for owner, elems in tbl.entries[name].items():
                    for idx, elem in elems.items():
                        text = str(idx)
                        if idx_rx.match(text) if idx_rx else text == idx_pat:
                            out[owner].add(elem)
        result = dict(out)
        self._cache[key] = result
        return result

    def registers(
        self, pattern: str, hierarchical: bool = False
    ) -> dict[str, set[int]]:
        """Flop cell → selected ``Q`` positions."""
        return self._lookup("register", pattern, hierarchical)  # type: ignore[return-value]

    def ports(self, pattern: str, hierarchical: bool = False) -> dict[str, set[int]]:
        """Top-level port → selected bit positions."""
        return self._lookup("port", pattern, hierarchical)  # type: ignore[return-value]

    def net_bits(self, pattern: str, hierarchical: bool = False) -> set[Bit]:
        """Every net bit the pattern selects."""
        out: set[Bit] = set()
        for bits in self._lookup("net", pattern, hierarchical).values():
            out.update(bits)  # type: ignore[arg-type]
        return out


# --- resolution ----------------------------------------------------------------


@dataclass(frozen=True)
class _Side:
    """One resolved endpoint list (``-from`` or ``-to``).

    ``unconstrained`` is True when the statement gave no such flag at all
    (any startpoint / endpoint). ``flops`` / ``ports`` map an owner to the
    bit positions named; ``clocks`` are clock endpoints.
    """

    unconstrained: bool
    clocks: frozenset[str] = frozenset()
    flops: Mapping[str, frozenset[int]] = field(default_factory=dict)
    ports: Mapping[str, frozenset[int]] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedConstraint:
    """One SDC statement with its objects resolved against the netlist."""

    index: int  # file order across both commands — the tie-breaker
    command: str  # "set_false_path" | "set_max_delay -datapath_only"
    statement: str
    line: int | None
    src: _Side
    dst: _Side
    # One frozenset of net bits per ``-through`` group.
    through: tuple[frozenset[Bit], ...]
    # A false path with no edge / check qualifier: it may acknowledge
    # findings (when it names the source by object, or via -through).
    full_check: bool

    @property
    def is_false_path(self) -> bool:
        return self.command == "set_false_path"


@dataclass(frozen=True)
class Resolution:
    """Every resolved object-scoped constraint, plus the lookup warnings."""

    constraints: tuple[ResolvedConstraint, ...]
    warnings: tuple[str, ...]
    index: NameIndex | None = None


def _at_line(line: int | None) -> str:
    return f" (line {line})" if line is not None else ""


def resolve(module: Module, spec: ClockSpec) -> Resolution:
    """Resolve every object-scoped constraint on ``spec`` against ``module``.

    Covers each :attr:`ClockSpec.false_path_exceptions` entry and each
    :attr:`ClockSpec.datapath_only_delays` entry with an object endpoint
    (clock-pair records are already fully described by their clocks).
    A pattern that matches nothing usable yields one warning naming it —
    the usual cause is a typo, and the exception then silently covers
    less than its author meant.
    """
    fps = spec.false_path_exceptions
    dpos = [d for d in spec.datapath_only_delays if not d.is_clock_pair]
    if not fps and not dpos:
        return Resolution(constraints=(), warnings=())
    index = NameIndex(module)
    warnings: list[str] = []
    seen_warnings: set[str] = set()

    def warn(text: str) -> None:
        if text not in seen_warnings:
            seen_warnings.add(text)
            warnings.append(text)

    out: list[ResolvedConstraint] = []
    records: list[FalsePathException | DatapathOnlyDelay] = [*fps, *dpos]
    # File order when the reader tracked lines; otherwise false paths
    # first, then datapath-only records, each in file order.
    order = sorted(
        range(len(records)),
        key=lambda i: (records[i].line or 0, i >= len(fps), i),
    )
    for n, i in enumerate(order):
        rec = records[i]
        from_objs: tuple[SdcObject, ...]
        to_objs: tuple[SdcObject, ...]
        through_groups: tuple[tuple[SdcObject, ...], ...]
        if isinstance(rec, FalsePathException):
            command = "set_false_path"
            from_objs = rec.from_objects
            to_objs = rec.to_objects
            through_groups = rec.through
            full_check = not rec.qualifiers
        else:
            command = "set_max_delay -datapath_only"
            from_objs = tuple(SdcObject(k, nm) for k, nm in rec.from_objects)
            to_objs = tuple(SdcObject(k, nm) for k, nm in rec.to_objects)
            through_groups = ()
            full_check = False
        where = f"{command}{_at_line(rec.line)}"
        src = _resolve_side(index, rec.from_clocks, from_objs, "from", where, warn)
        dst = _resolve_side(index, rec.to_clocks, to_objs, "to", where, warn)
        through: list[frozenset[Bit]] = []
        for group in through_groups:
            bits: set[Bit] = set()
            for obj in group:
                got = _through_bits(index, obj)
                if not got:
                    warn(_no_match(where, obj, "-through"))
                bits.update(got)
            through.append(frozenset(bits))
        out.append(
            ResolvedConstraint(
                index=n,
                command=command,
                statement=rec.statement,
                line=rec.line,
                src=src,
                dst=dst,
                through=tuple(through),
                full_check=full_check,
            )
        )
    return Resolution(constraints=tuple(out), warnings=tuple(warnings), index=index)


# Port directions each delay command's *glob* patterns may select.
_DELAY_DIRECTIONS: dict[str, frozenset[str]] = {
    "set_input_delay": frozenset({"input", "inout"}),
    "set_output_delay": frozenset({"output", "inout"}),
}


def resolve_port_delays(module: Module, spec: ClockSpec) -> list[str]:
    """Resolve ``set_*_delay`` port targets against ``module``'s top-level
    ports, rewriting :attr:`ClockSpec.port_clock` in place
    (rtl-buddy-cdc#357). Returns the warnings.

    The parser records each target as written (:class:`~rtl_buddy_cdc.sdc.PortDelay`);
    an exact port name already keys ``port_clock`` correctly, so an SDC
    naming only whole ports returns here untouched. Otherwise, in file
    order (last write wins, as for exact names):

    - A **bus index** (``d[3]``, braced in Tcl) types the **whole port**
      ``d``. Port typing — every consumer of ``port_clock`` — is per
      port, not per bit, so this over-approximates a partial selection;
      when the bits of one port are given *different* clocks the port
      takes the last one, and that is warned about.
    - A **glob** (``d*``, ``a?``, ``d*[3]``) types every top-level port it
      matches, through the same :class:`NameIndex` matcher as the object
      patterns (``*`` / ``?`` never cross a hierarchy level). A glob only
      selects ports of the command's direction (inputs / inouts for
      ``set_input_delay``, outputs / inouts for ``set_output_delay``) and
      never a port a ``create_clock`` declares a clock on: ``[get_ports
      *]`` is not meant to re-type the clock pins.
    - A target that matches nothing is warned about and kept as written,
      which, as before, types nothing.
    """
    targets = [(d, t) for d in spec.port_delays for t in d.targets]
    if all(t in module.ports for _d, t in targets):
        return []
    index = NameIndex(module)
    clock_ports = {p for clk in spec.clocks.values() for p in clk.ports}
    warnings: list[str] = []
    rebuilt: dict[str, str] = {}
    # Port → the (clock, target) assignments still in force on its bits:
    # a whole-port typing replaces them all, a bit-select adds one.
    in_force: dict[str, list[tuple[str, str]]] = {}
    for d, target in targets:
        if target in module.ports:
            rebuilt[target] = d.clock
            in_force[target] = [(d.clock, target)]
            continue
        where = f"{d.command} -clock {d.clock}{_at_line(d.line)}"
        is_glob = _has_glob(target)
        hits = index.ports(target)
        if is_glob:
            allowed = _DELAY_DIRECTIONS.get(d.command, frozenset())
            hits = {
                name: bits
                for name, bits in hits.items()
                if module.ports[name].direction in allowed and name not in clock_ports
            }
        if not hits:
            what = (
                f"no {'input' if d.command == 'set_input_delay' else 'output'} "
                f"port that is not a clock source"
                if is_glob
                else "no top-level port"
            )
            warnings.append(
                f"{where}: port pattern {target!r} matches {what} — constraint "
                f"typo? (it types nothing)"
            )
            rebuilt[target] = d.clock
            continue
        for name in sorted(hits):
            rebuilt[name] = d.clock
            if len(hits[name]) < len(module.ports[name].bits):
                in_force.setdefault(name, []).append((d.clock, target))
            else:
                in_force[name] = [(d.clock, target)]
    # Entries that did not come from a recorded delay statement (a spec
    # built by hand) are kept.
    written = {t for _d, t in targets}
    for name, clock in spec.port_clock.items():
        if name not in written:
            rebuilt.setdefault(name, clock)
    for name, assigned in sorted(in_force.items()):
        if len({clock for clock, _t in assigned}) < 2:
            continue
        named = ", ".join(f"{t} → {clock}" for clock, t in assigned)
        warnings.append(
            f"set_*_delay: bits of port '{name}' are typed to different clocks "
            f"({named}); port typing is per port, so the whole port is typed "
            f"to the last one, '{rebuilt[name]}'"
        )
    spec.port_clock.clear()
    spec.port_clock.update(rebuilt)
    return warnings


def _no_match(where: str, obj: SdcObject, role: str) -> str:
    return (
        f"{where}: {role} {obj.kind if obj.kind != 'any' else 'object'} pattern "
        f"{obj.name!r} matches nothing usable in the netlist — constraint typo? "
        f"(the exception does not cover it)"
    )


def _resolve_side(
    index: NameIndex,
    clocks: tuple[str, ...],
    objects: tuple[SdcObject, ...],
    role: str,
    where: str,
    warn,
) -> _Side:
    if not clocks and not objects:
        return _Side(unconstrained=True)
    flops: dict[str, set[int]] = defaultdict(set)
    ports: dict[str, set[int]] = defaultdict(set)
    for obj in objects:
        got_f, got_p = _endpoint(index, obj, role)
        if not got_f and not got_p:
            warn(_no_match(where, obj, f"-{role}"))
        for k, v in got_f.items():
            flops[k].update(v)
        for k, v in got_p.items():
            ports[k].update(v)
    return _Side(
        unconstrained=False,
        clocks=frozenset(clocks),
        flops={k: frozenset(v) for k, v in flops.items()},
        ports={k: frozenset(v) for k, v in ports.items()},
    )


def _endpoint(
    index: NameIndex, obj: SdcObject, role: str
) -> tuple[dict[str, set[int]], dict[str, set[int]]]:
    """Resolve one ``-from`` / ``-to`` object to (flops, ports)."""
    kind, name, hier = obj.kind, obj.name, obj.hierarchical
    if kind == "cell":
        return index.registers(name, hier), {}
    if kind == "port":
        return {}, index.ports(name, hier)
    if kind == "pin":
        owner, sep, _pin = name.rpartition("/")
        if sep:
            flops = index.registers(owner, hier)
            if flops:
                return flops, {}
        return _net_endpoint(index, index.net_bits(name, hier), role)
    if kind == "net":
        return _net_endpoint(index, index.net_bits(name, hier), role)
    return {}, {}


def _net_endpoint(
    index: NameIndex, bits: set[Bit], role: str
) -> tuple[dict[str, set[int]], dict[str, set[int]]]:
    flops: dict[str, set[int]] = defaultdict(set)
    ports: dict[str, set[int]] = defaultdict(set)
    if role == "from":
        q_owner = index.q_owner()
        port_bits = {
            b: (pname, i)
            for pname, p in index.module.ports.items()
            if p.direction == "input"
            for i, b in enumerate(p.bits)
            if isinstance(b, int)
        }
        for b in bits:
            if b in q_owner:
                f, i = q_owner[b]
                flops[f].add(i)
            elif b in port_bits:
                p, i = port_bits[b]
                ports[p].add(i)
    else:
        d_owner = index.d_owner()
        for b in bits:
            for f, i in d_owner.get(b, ()):
                flops[f].add(i)
    return dict(flops), dict(ports)


def _through_bits(index: NameIndex, obj: SdcObject) -> set[Bit]:
    """Net bits a ``-through`` object names (pins, nets, ports)."""
    kind, name, hier = obj.kind, obj.name, obj.hierarchical
    out: set[Bit] = set()
    if kind in ("net", "any"):
        out |= index.net_bits(name, hier)
    if kind in ("pin", "any") and not out:
        owner, sep, pin = name.rpartition("/")
        flops = index.registers(owner, hier) if sep else {}
        for fname, positions in flops.items():
            f = index.flops[fname]
            pins = (
                f.d if pin.upper() == "D" else f.q if pin.upper() in ("Q", "QN") else ()
            )
            out.update(
                pins[i] for i in positions if i < len(pins) and isinstance(pins[i], int)
            )
        if not flops and kind == "pin":
            out |= index.net_bits(name, hier)
    if kind in ("port", "any") and not out:
        for pname, positions in index.ports(name, hier).items():
            pbits = index.module.ports[pname].bits
            out.update(pbits[i] for i in positions if isinstance(pbits[i], int))
    return out


# --- coverage --------------------------------------------------------------------


@dataclass(frozen=True)
class CrossingCoverage:
    """The constraints that fully cover one crossing.

    ``constraints`` lists every statement (false path or datapath-only)
    contributing to a full cover, in file order. ``acknowledged_by`` is
    the subset of false paths that may acknowledge findings on it: no
    edge / check qualifier, and the source named by object or the path
    pinned by ``-through``. Empty when nothing may be acknowledged.
    """

    constraints: tuple[ResolvedConstraint, ...]
    acknowledged_by: tuple[ResolvedConstraint, ...] = ()


def coverage(
    module: Module,
    crossings: Sequence[Crossing],
    resolution: Resolution,
    spec: ClockSpec,
) -> dict[CrossingKey, CrossingCoverage]:
    """Which object-scoped constraints fully cover each crossing.

    A crossing is covered when every (source bit, destination bit) pair
    it can carry is named by some statement's ``-from`` × ``-to``, so a
    register constrained one bit per line (as synthesis-generated SDCs
    write them) is covered by the lines together. A clock endpoint
    covers its side when every clock that side's domain stands for
    (:meth:`ClockSpec.colocated_clocks`) is listed; an absent flag
    covers anything. A statement with ``-through`` must cover the
    crossing on its own, and only when *every* path from source to
    destination within the crossing walk's hop budget passes through
    each ``-through`` group. Partial covers count for nothing.

    Datapath-only and false-path statements are evaluated separately —
    a crossing half covered by each is covered by neither.
    """
    if not resolution.constraints:
        return {}
    walker = _ThroughWalker(module)
    by_src: dict[tuple[str, str], list[ResolvedConstraint]] = defaultdict(list)
    for rc in resolution.constraints:
        if rc.src.unconstrained:
            by_src[("any", "")].append(rc)
            continue
        for f in rc.src.flops:
            by_src[("flop", f)].append(rc)
        for p in rc.src.ports:
            by_src[("port", p)].append(rc)
        for clk in rc.src.clocks:
            by_src[("clock", clk)].append(rc)

    out: dict[CrossingKey, CrossingCoverage] = {}
    for c in crossings:
        cands: dict[int, ResolvedConstraint] = {}
        keys = [("any", ""), ("clock", c.src_clock)]
        if c.src_flop is not None:
            keys.append(("flop", c.src_flop.cell.name))
        if c.src_port is not None:
            keys.append(("port", c.src_port))
        for k in keys:
            for rc in by_src.get(k, ()):
                cands[rc.index] = rc
        if not cands:
            continue
        ordered = [cands[i] for i in sorted(cands)]
        used: list[ResolvedConstraint] = []
        acked: list[ResolvedConstraint] = []
        for is_fp in (True, False):
            group = [rc for rc in ordered if rc.is_false_path == is_fp]
            got = _cover(c, group, spec, walker, eligible_only=False)
            if got:
                used.extend(got)
        if not used:
            continue
        eligible = [rc for rc in ordered if rc.is_false_path and rc.full_check]
        got = _cover(c, eligible, spec, walker, eligible_only=True)
        if got:
            acked.extend(got)
        out[crossing_key(c)] = CrossingCoverage(
            constraints=tuple(sorted(used, key=lambda r: r.index)),
            acknowledged_by=tuple(acked),
        )
    return out


def _src_width(c: Crossing, module: Module) -> int | None:
    if c.src_flop is not None:
        return len(c.src_flop.q)
    if c.src_port is not None:
        port = module.ports.get(c.src_port)
        return len(port.bits) if port is not None else None
    return None


def _clocks_cover(side: _Side, clock: str, spec: ClockSpec) -> bool:
    return bool(side.clocks) and all(
        k in side.clocks for k in spec.colocated_clocks(clock)
    )


def _src_positions(
    rc: ResolvedConstraint,
    c: Crossing,
    width: int | None,
    spec: ClockSpec,
    *,
    objects_only: bool,
) -> frozenset[int] | None:
    """Source bit positions ``rc`` names on ``c`` (``None`` = none)."""
    full = frozenset(range(width or 1))
    if not objects_only:
        if rc.src.unconstrained or _clocks_cover(rc.src, c.src_clock, spec):
            return full
    got: frozenset[int] = frozenset()
    if c.src_flop is not None:
        got = rc.src.flops.get(c.src_flop.cell.name, frozenset())
    elif c.src_port is not None:
        got = rc.src.ports.get(c.src_port, frozenset())
    return got or None


def _dst_positions(
    rc: ResolvedConstraint, c: Crossing, spec: ClockSpec
) -> frozenset[int] | None:
    full = frozenset(range(len(c.dst_flop.d) or 1))
    if rc.dst.unconstrained or _clocks_cover(rc.dst, c.dst_clock, spec):
        return full
    return rc.dst.flops.get(c.dst_flop.cell.name) or None


def _cover(
    c: Crossing,
    group: list[ResolvedConstraint],
    spec: ClockSpec,
    walker: _ThroughWalker,
    *,
    eligible_only: bool,
) -> list[ResolvedConstraint]:
    """The statements of ``group`` that together fully cover ``c``, or []."""
    if not group:
        return []
    width = _src_width(c, walker.module)
    src_full = frozenset(range(width or 1))
    dst_full = frozenset(range(len(c.dst_flop.d) or 1))
    rects: list[tuple[ResolvedConstraint, frozenset[int], frozenset[int]]] = []
    for rc in group:
        # Acknowledging needs the source named by object; a -through
        # pins the path itself, so there any source form will do.
        objects_only = eligible_only and not rc.through
        sb = _src_positions(rc, c, width, spec, objects_only=objects_only)
        if sb is None:
            continue
        db = _dst_positions(rc, c, spec)
        if db is None:
            continue
        if rc.through:
            if sb == src_full and db == dst_full and walker.all_paths_through(c, rc):
                return [rc]
            continue
        if sb == src_full and db == dst_full:
            return [rc]
        rects.append((rc, sb, db))
    if not rects or width is None:
        return []
    for s in src_full:
        reach: set[int] = set()
        for _, sb, db in rects:
            if s in sb:
                reach |= db
        if reach != dst_full:
            return []
    return [rc for rc, _, _ in rects]


class _ThroughWalker:
    """Exact "every path passes through" test for ``-through`` (#328).

    Re-walks a crossing's source fanout the way
    :func:`rtl_buddy_cdc.domain.find_crossings` does — same cells, same
    lane-precise edges, same hop budget — but treats the ``-through``
    bits as cut points. If the destination's ``D`` is still reachable,
    some path avoids the group and the crossing is not covered. One
    walk per (source, statement, group), cached; the consumer map is
    built once.
    """

    def __init__(self, module: Module) -> None:
        self.module = module
        self._consumers: dict[Bit, list[tuple[str, str, int]]] | None = None
        self._cache: dict[tuple[str, str, int, int], frozenset[Bit]] = {}

    def _source_bits(
        self, c: Crossing
    ) -> tuple[tuple[str, str], tuple[Bit, ...]] | None:
        if c.src_flop is not None:
            return ("flop", c.src_flop.cell.name), c.src_flop.q
        if c.src_port is not None and c.src_port in self.module.ports:
            return ("port", c.src_port), self.module.ports[c.src_port].bits
        return None

    def all_paths_through(self, c: Crossing, rc: ResolvedConstraint) -> bool:
        src = self._source_bits(c)
        if src is None:
            return False
        (kind, name), bits = src
        dst_bits = {b for b in c.dst_flop.d if isinstance(b, int)}
        for gi, group in enumerate(rc.through):
            key = (kind, name, rc.index, gi)
            reached = self._cache.get(key)
            if reached is None:
                reached = self._walk(bits, group)
                self._cache[key] = reached
            if reached & dst_bits:
                return False
        return True

    def _walk(self, start: Iterable[Bit], cut: frozenset[Bit]) -> frozenset[Bit]:
        if self._consumers is None:
            self._consumers = _build_bit_consumers(self.module)
        consumers = self._consumers
        cells = self.module.cells
        seen: dict[Bit, int] = {}
        frontier: list[tuple[Bit, int]] = [
            (b, 0) for b in start if isinstance(b, int) and b not in cut
        ]
        for b, h in frontier:
            seen[b] = h
        while frontier:
            nxt: list[tuple[Bit, int]] = []
            for bit, hops in frontier:
                if hops >= _CROSSING_MAX_HOPS:
                    continue
                for cell_name, cport, idx in consumers.get(bit, ()):
                    cell = cells[cell_name]
                    if cell.type == "$scopeinfo" or is_ff_cell(cell.type):
                        continue
                    for ob in _lane_targets(cell, cport, idx):
                        if not isinstance(ob, int) or ob in cut:
                            continue
                        prev = seen.get(ob)
                        if prev is None or hops + 1 < prev:
                            seen[ob] = hops + 1
                            nxt.append((ob, hops + 1))
            frontier = nxt
        return frozenset(seen)


# --- effect on findings --------------------------------------------------------


def _justification(rc: ResolvedConstraint, sdc_file: str | None) -> SdcJustification:
    return SdcJustification(statement=rc.statement, line=rc.line, file=sdc_file)


def _sdc_waiver(
    v: Violation, rc: ResolvedConstraint, sdc_file: str | None, *, extra: int
) -> Waiver:
    more = f" (+{extra} more)" if extra else ""
    target = v.cell_name or ""
    return Waiver(
        rule_pattern=v.rule_id,
        regex=re.compile(re.escape(target)),
        reason=f"SDC false path: {rc.statement}{more}",
        source_line=rc.line or 0,
        sdc=_justification(rc, sdc_file),
    )


def acknowledge(
    module: Module,
    violations: list[Violation],
    crossings: Sequence[Crossing],
    covered: Mapping[CrossingKey, CrossingCoverage],
    *,
    sdc_file: str | None = None,
) -> tuple[list[Violation], list[SuppressedViolation]]:
    """Split ``violations`` into (kept, acknowledged-by-an-SDC-false-path).

    Only :data:`ACKNOWLEDGED_RULES` are eligible, and only on crossings
    whose :class:`CrossingCoverage` has a non-empty ``acknowledged_by``:

    - CDC-001 / -002 / -003 / -004 carry their crossing — that crossing
      must be acknowledged.
    - CDC-020 (sliced bus) speaks for every crossing out of its source
      register, CDC-019 (one-hot decode) for every crossing out of a
      register fed by the same decoder. Every such crossing in
      ``crossings`` (the list the rules ran on) must be acknowledged —
      stricter than ``(* cdc_static *)``, which silences CDC-019 when any
      one lane is static.

    Acknowledged findings come back as :class:`SuppressedViolation` whose
    waiver carries the first acknowledging statement as an
    :class:`~rtl_buddy_cdc.waivers.SdcJustification` — the same channel
    waivers use, so they stay visible in every report format and never
    drive the exit code.
    """
    if not covered or not violations:
        return list(violations), []
    by_src: dict[str, list[Crossing]] = defaultdict(list)
    for c in crossings:
        if c.src_flop is not None:
            by_src[c.src_flop.cell.name].append(c)
    drivers: dict[Bit, tuple[str, str, int]] | None = None
    kept: list[Violation] = []
    suppressed: list[SuppressedViolation] = []
    for v in violations:
        group: list[Crossing] = []
        if v.rule_id in _CROSSING_RULES and v.crossing is not None:
            # A finding lifted from inside an abstracted block (#261)
            # carries a crossing in that block's namespace and is
            # re-anchored on the boundary instance; its raw names could
            # collide with a top-level crossing's, so require the anchor
            # to be one of the crossing's endpoints (as domain_map does).
            ends = {v.crossing.dst_flop.cell.name}
            if v.crossing.src_flop is not None:
                ends.add(v.crossing.src_flop.cell.name)
            if v.cell_name is None or v.cell_name in ends:
                group = [v.crossing]
        elif v.rule_id == "CDC-020" and v.cell_name is not None:
            group = by_src.get(v.cell_name, [])
        elif v.rule_id == "CDC-019" and v.cell_name is not None:
            if drivers is None:
                drivers = _bit_drivers(module)
            group = _decoder_group(v.cell_name, by_src, drivers)
        acks = [covered.get(crossing_key(c)) for c in group]
        if group and all(a is not None and a.acknowledged_by for a in acks):
            stmts = sorted(
                {rc.index: rc for a in acks if a for rc in a.acknowledged_by}.values(),
                key=lambda r: r.index,
            )
            suppressed.append(
                SuppressedViolation(
                    violation=v,
                    waiver=_sdc_waiver(v, stmts[0], sdc_file, extra=len(stmts) - 1),
                )
            )
        else:
            kept.append(v)
    return kept, suppressed


def _decoder_group(
    src_name: str,
    by_src: Mapping[str, list[Crossing]],
    drivers: Mapping[Bit, tuple[str, str, int]],
) -> list[Crossing]:
    """Crossings out of every 1-bit register fed by ``src_name``'s decoder."""
    first = by_src.get(src_name)
    if not first or first[0].src_flop is None:
        return []
    d = first[0].src_flop.d
    if len(d) != 1 or d[0] not in drivers:
        return []
    decoder = drivers[d[0]][0]
    out: list[Crossing] = []
    for crossings in by_src.values():
        f = crossings[0].src_flop
        if f is None or len(f.d) != 1:
            continue
        drv = drivers.get(f.d[0])
        if drv is not None and drv[0] == decoder:
            out.extend(crossings)
    return out


def domain_map_exceptions(
    covered: Mapping[CrossingKey, CrossingCoverage],
) -> dict[CrossingKey, dict[str, object]]:
    """Per-crossing domain-map annotation (schema 1.3, additive)."""
    return {
        key: {
            "sdc_exceptions": [
                {"command": rc.command, "statement": rc.statement, "line": rc.line}
                for rc in cov.constraints
            ],
            "sdc_acknowledged": bool(cov.acknowledged_by),
        }
        for key, cov in covered.items()
    }
