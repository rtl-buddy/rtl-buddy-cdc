"""Inferred-memory ports as crossing endpoints (issue #326).

Yosys keeps an inferred RAM as memory cells rather than flops: after
``proc`` (the ``lint`` wrapper's flow, with either ``read_verilog`` or
``read_slang``) as one ``$memwr_v2`` per write port and one ``$memrd`` /
``$memrd_v2`` per read port, all sharing a ``MEMID``; after
``memory_collect`` (or ``memory -nomap``) as a single ``$mem_v2`` whose
per-port fields are packed side by side. The flop-to-flop crossing walk
cannot see through either form, so a dual-clock RAM used to be invisible
to every rule.

This module is the netlist-side half of the fix and stays pure — it
imports nothing from the analysis layers:

- :func:`find_memories` decodes both forms (and the v1 ``$memwr`` /
  ``$memrd`` / ``$mem`` cells) into one :class:`Memory` per ``MEMID``
  with its write and read :class:`MemPort`\\ s, in one pass over the
  cells. ``$meminit*`` cells are constant initialisation and carry no
  clock or data path, so they are ignored.
- :func:`crossing_view` rewrites a module for the crossing walk: every
  memory cell is removed, each **write port** and each **clocked read
  port** becomes a *virtual flop* (a sink — address / data / enable on
  ``D`` — and, for a read port, a source — read data on ``Q``), and each
  **asynchronous read port** becomes a plain combinational transit cell
  from its address / enable to its read data. The storage path itself
  (write data → read data) is deliberately *not* an edge: it is a
  crossing only the memory rule (CDC-026) can judge, because the
  sanctioned async-FIFO shape crosses it without a synchroniser by
  design.

A design without memory cells costs one type check per cell and gets
the module back unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from rtl_buddy_cdc.flops import Flop
from rtl_buddy_cdc.netlist import Bit, Cell, Module

# Split (per-port) memory cells, v1 and v2.
WRITE_PORT_TYPES: frozenset[str] = frozenset({"$memwr", "$memwr_v2"})
READ_PORT_TYPES: frozenset[str] = frozenset({"$memrd", "$memrd_v2"})
# Collected (packed, all ports on one cell) memory cells, v1 and v2.
PACKED_MEMORY_TYPES: frozenset[str] = frozenset({"$mem", "$mem_v2"})
MEMORY_CELL_TYPES: frozenset[str] = (
    WRITE_PORT_TYPES | READ_PORT_TYPES | PACKED_MEMORY_TYPES
)

# Cell ``type`` of a virtual memory-port flop. Not a recognised FF type
# (``flops.is_ff_cell`` is False), so — exactly like the blackbox
# ``$boundary_sink`` — rule helpers that special-case real flops treat it
# as an opaque endpoint. It never appears in ``Module.cells``.
MEM_PORT_TYPE = "$mem_port"
# Cell ``type`` of the transit cell standing in for an asynchronous read
# port in :func:`crossing_view`. Not lane-aligned, so every address /
# enable bit reaches every read-data bit (a sound over-approximation of
# a decoder + mux).
MEM_ASYNC_READ_TYPE = "$mem_async_read"


@dataclass(frozen=True)
class MemPort:
    """One memory port.

    ``endpoint`` is the name the port goes by as a crossing endpoint:
    the cell's own name for a split ``$memwr*`` / ``$memrd*`` cell (so a
    finding anchored on it resolves the cell's ``src`` location and
    instance path like any other cell), ``<cell>[wr<i>]`` /
    ``<cell>[rd<i>]`` for port ``i`` of a packed ``$mem*`` cell.

    ``clocked`` is the port's ``CLK_ENABLE``. An unclocked *read* port
    is an asynchronous read (data follows the address combinationally);
    an unclocked *write* port has no clock domain and is left out of the
    crossing model, the same way a flop whose clock does not resolve is.
    """

    kind: str  # "write" | "read"
    cell: str
    index: int
    endpoint: str
    clocked: bool
    clk: Bit
    addr: tuple[Bit, ...]
    data: tuple[Bit, ...]
    en: tuple[Bit, ...]

    @property
    def control_bits(self) -> tuple[Bit, ...]:
        """The live (non-constant) address + enable bits."""
        return tuple(b for b in self.addr + self.en if isinstance(b, int))


@dataclass(frozen=True)
class Memory:
    """Every port of one inferred memory, in port order."""

    memid: str  # display name (``mem``, ``u_fifo.mem``)
    write_ports: tuple[MemPort, ...]
    read_ports: tuple[MemPort, ...]


@dataclass(frozen=True)
class CrossingView:
    """A module rewritten for the crossing walk (see :func:`crossing_view`).

    ``port_flops`` are the virtual flops, one per clocked write or read
    port, each tagged with the port it stands for. Their clock domain is
    resolved by the caller from ``Flop.clk`` with the same tracer that
    assigns real flops.
    """

    module: Module
    port_flops: tuple[tuple[Flop, MemPort], ...]


def display_memid(raw: str) -> str:
    """``\\mem`` → ``mem``; ``$flatten\\u_f.\\mem`` → ``u_f.mem``."""
    name = raw
    if name.startswith("$flatten"):
        name = name[len("$flatten") :]
    return name.replace("\\", "")


def _param_str(raw: object) -> str:
    if isinstance(raw, int):
        return format(raw, "b")
    return str(raw)


def _param_int(cell: Cell, name: str, default: int = 0) -> int:
    s = _param_str(cell.parameters.get(name, ""))
    try:
        return int(s, 2)
    except ValueError:
        try:
            return int(s)
        except ValueError:
            return default


def _param_flag(cell: Cell, name: str, index: int = 0) -> bool:
    """Bit ``index`` of a packed per-port parameter (LSB = port 0).

    Yosys writes bit-vector parameters MSB-first, so port ``i`` is the
    ``i``-th character from the *right*. A single-port cell's 32-bit
    integer form (``"000…01"``) reads the same way.
    """
    s = _param_str(cell.parameters.get(name, ""))
    pos = len(s) - 1 - index
    return 0 <= pos < len(s) and s[pos] == "1"


def _slice(bits: tuple[Bit, ...], index: int, width: int) -> tuple[Bit, ...]:
    return tuple(bits[index * width : (index + 1) * width])


def _bit_at(bits: tuple[Bit, ...], index: int) -> Bit:
    return bits[index] if 0 <= index < len(bits) else "x"


def _split_port(cell: Cell, kind: str) -> MemPort:
    conn = cell.connections
    return MemPort(
        kind=kind,
        cell=cell.name,
        index=0,
        endpoint=cell.name,
        clocked=_param_flag(cell, "CLK_ENABLE"),
        clk=_bit_at(conn.get("CLK", ()), 0),
        addr=tuple(conn.get("ADDR", ())),
        data=tuple(conn.get("DATA", ())),
        en=tuple(conn.get("EN", ())),
    )


def _packed_ports(cell: Cell) -> tuple[list[MemPort], list[MemPort]]:
    conn = cell.connections
    abits = _param_int(cell, "ABITS")
    width = _param_int(cell, "WIDTH")
    n_wr = _param_int(cell, "WR_PORTS", len(conn.get("WR_CLK", ())))
    n_rd = _param_int(cell, "RD_PORTS", len(conn.get("RD_CLK", ())))
    writes = [
        MemPort(
            kind="write",
            cell=cell.name,
            index=i,
            endpoint=f"{cell.name}[wr{i}]",
            clocked=_param_flag(cell, "WR_CLK_ENABLE", i),
            clk=_bit_at(conn.get("WR_CLK", ()), i),
            addr=_slice(conn.get("WR_ADDR", ()), i, abits),
            data=_slice(conn.get("WR_DATA", ()), i, width),
            en=_slice(conn.get("WR_EN", ()), i, width),
        )
        for i in range(n_wr)
    ]
    reads = [
        MemPort(
            kind="read",
            cell=cell.name,
            index=i,
            endpoint=f"{cell.name}[rd{i}]",
            clocked=_param_flag(cell, "RD_CLK_ENABLE", i),
            clk=_bit_at(conn.get("RD_CLK", ()), i),
            addr=_slice(conn.get("RD_ADDR", ()), i, abits),
            data=_slice(conn.get("RD_DATA", ()), i, width),
            en=(_bit_at(conn.get("RD_EN", ()), i),),
        )
        for i in range(n_rd)
    ]
    return writes, reads


def find_memories(module: Module) -> tuple[Memory, ...]:
    """Decode every inferred memory in ``module``, sorted by name.

    Split cells are grouped by their ``MEMID`` parameter; a packed
    ``$mem*`` cell contributes its own ports under its ``MEMID`` (a
    netlist mixing both forms for one memory is not something Yosys
    produces, but would simply merge). Write ports keep Yosys' port
    order (``PORTID`` for split cells); read ports keep cell order.
    """
    writes: dict[str, list[tuple[int, MemPort]]] = {}
    reads: dict[str, list[MemPort]] = {}
    for cell in module.cells.values():
        if cell.type not in MEMORY_CELL_TYPES:
            continue
        memid = _param_str(cell.parameters.get("MEMID", cell.name))
        if cell.type in WRITE_PORT_TYPES:
            port = _split_port(cell, "write")
            writes.setdefault(memid, []).append((_param_int(cell, "PORTID"), port))
        elif cell.type in READ_PORT_TYPES:
            reads.setdefault(memid, []).append(_split_port(cell, "read"))
        else:
            w, r = _packed_ports(cell)
            writes.setdefault(memid, []).extend((p.index, p) for p in w)
            reads.setdefault(memid, []).extend(r)
    out = [
        Memory(
            memid=display_memid(memid),
            write_ports=tuple(
                p for _, p in sorted(writes.get(memid, []), key=lambda t: t[0])
            ),
            read_ports=tuple(reads.get(memid, [])),
        )
        for memid in sorted(set(writes) | set(reads))
    ]
    return tuple(sorted(out, key=lambda m: m.memid))


def _virtual_flop(port: MemPort, d: tuple[Bit, ...], q: tuple[Bit, ...]) -> Flop:
    cell = Cell(
        name=port.endpoint,
        type=MEM_PORT_TYPE,
        connections={"CLK": (port.clk,), "D": d, "Q": q},
    )
    return Flop(cell=cell, clk=port.clk, d=d, q=q)


def crossing_view(module: Module, memories: tuple[Memory, ...]) -> CrossingView:
    """Rewrite ``module``'s memory cells into the crossing walk's terms.

    - write port (clocked) → virtual flop, ``D`` = address + data +
      enable, no ``Q``: a sink in the write-clock domain, so a
      foreign-domain address / data / enable is an ordinary crossing
      into it (CDC-001 / CDC-004 …);
    - clocked read port → virtual flop, ``D`` = address + enable,
      ``Q`` = read data: a sink *and* a source in the read-clock domain;
    - asynchronous read port → ``$mem_async_read`` transit cell, ``A`` =
      address + enable, ``Y`` = read data, so a foreign-domain address
      reaches whatever registers the read data, exactly as through a mux;
    - unclocked write port → dropped (no domain to give it).

    The memory cells themselves leave ``cells``; every other cell is the
    same object as in ``module``. Returns ``module`` unchanged (and no
    flops) when ``memories`` is empty.
    """
    if not memories:
        return CrossingView(module=module, port_flops=())
    cells = {
        name: cell
        for name, cell in module.cells.items()
        if cell.type not in MEMORY_CELL_TYPES
    }
    port_flops: list[tuple[Flop, MemPort]] = []
    for mem in memories:
        for port in mem.write_ports:
            if not port.clocked:
                continue
            d = tuple(b for b in port.addr + port.data + port.en if isinstance(b, int))
            port_flops.append((_virtual_flop(port, d, ()), port))
        for port in mem.read_ports:
            if port.clocked:
                port_flops.append(
                    (_virtual_flop(port, port.control_bits, port.data), port)
                )
            else:
                cells[port.endpoint] = Cell(
                    name=port.endpoint,
                    type=MEM_ASYNC_READ_TYPE,
                    connections={"A": port.control_bits, "Y": port.data},
                )
    return CrossingView(
        module=replace(module, cells=cells), port_flops=tuple(port_flops)
    )
