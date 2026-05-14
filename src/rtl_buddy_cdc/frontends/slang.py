"""slang frontend — elaborate SystemVerilog via pyslang into a
Yosys-shape :class:`netlist.Module` consumable by the rule pack.

Status
------
**Stage 2 (first slice landed).** Single-module designs with direct
register-to-register CDC paths, multi-bit declarations, async-reset
flops, gray-coded bus crossings, and the ``(* cdc_sync *)`` /
``(* cdc_gray *)`` attribute escape hatches reach parity with the
Yosys frontend on their corresponding fixtures. Designs that need
combinational primitive lowering (any non-trivial RHS) or
cross-module flattening do not. See issue #5 for the broader plan.

Rule parity matrix (today)
~~~~~~~~~~~~~~~~~~~~~~~~~~
Driven by ``tests/test_slang_elaboration.py``; each row is a fixture
that produces the same violation set as the Yosys frontend.

==================================  =================
fixture                             expected rules
==================================  =================
bad_single_ff_sync                  CDC-001
bad_port_no_sync                    CDC-001
bad_bus_crossing                    CDC-004
bad_reconvergent_sync               CDC-005
bad_reset_crossing                  CDC-007
bad_clock_as_data                   CDC-008
good_2ff_sync                       (none)
good_gray_counter_crossing          (none)
marked_user_sync                    (none — attribute suppression)
==================================  =================

What currently works
~~~~~~~~~~~~~~~~~~~~
- Parse + elaborate one or more SV sources, resolve the top module.
- Ports (input / output / inout) with their bit-id allocation.
- Local ``logic`` / ``reg`` variables become :class:`Netname`s, with
  any ``(* attr *)`` declaration attributes propagated (looked up via
  ``Compilation.getAttributes(symbol)`` — pyslang stores attributes
  on the compilation, not on the symbol).
- ``always_ff`` blocks with the canonical async-reset shape::

      always_ff @(posedge clk or negedge rst_n)
          if (!rst_n) q <= 0; else q <= d;

  emit ``$adff`` cells with ``CLK``/``D``/``Q``/``ARST`` connections.
  Plain ``always_ff @(posedge clk) q <= d;`` emits ``$dff``.
- Direct ``port = var`` continuous assigns are aliased into the
  source variable's bits (matches Yosys post-``opt_clean``).
- Fatal pyslang diagnostics are surfaced through
  ``TextDiagnosticClient`` with file:line:col + caret summaries —
  the usual compiler-error format users already recognise.

What is NOT yet implemented (next slices)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
- **Combinational primitive lowering.** RHS expressions that aren't a
  bare ``NamedValueExpression`` (binary ops, conditionals, concats,
  slices, conversions beyond identity) get a ``$_UNKNOWN_`` driver
  cell that the rule pack treats as opaque. This is what's blocking
  CDC-003 / CDC-006 parity (comb between flops or from a top-level
  port). The work item is to map pyslang ``BinaryExpression`` /
  ``ConditionalExpression`` / etc. to the Yosys cell zoo
  (``$and``/``$or``/``$xor``/``$mux``/``$pmux``).
- **Multi-instance / cross-module flattening.** Only the top
  :class:`InstanceSymbol`'s direct body is walked. Child instances
  exist as opaque symbols; their contents don't contribute flops or
  comb. Designs that span modules need a recursive walker that emits
  every leaf instance's flops into the same flat ``Module`` (matching
  what Yosys ``flatten`` produces).
- **``always_comb`` blocks** — like continuous assigns but with
  procedural shape; needs primitive lowering to be useful.
- **Source locations** — :class:`Cell.attributes["src"]` is left
  empty, so JSON/SARIF reports lose their file:line context on the
  slang path. pyslang carries source ranges on every Symbol /
  Expression; threading those through is a follow-up.
- **Width-N $dff / $adff parameters** (``WIDTH``, ``CLK_POLARITY``,
  ``ARST_VALUE``) — partially populated today; not yet read by any
  rule but should reach full Yosys parity for future rule extensions.
- **Yosys-only constructs** (``$_BUF_`` / ``$_NOT_`` primitives in SV
  source) are correctly rejected by slang — they're post-synthesis
  cells, not legal SV. Fixtures that rely on them
  (``*_source_sync_internal``) stay Yosys-frontend-only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rtl_buddy_cdc.netlist import Bit, Cell, Module, Netname, Port

_PYSLANG_INSTALL_HINT = (
    "pyslang is required for the slang frontend. Install via:\n"
    "    pip install 'rtl-buddy-cdc[slang]'\n"
    "or directly:\n"
    "    pip install pyslang"
)


class SlangFrontendUnavailable(RuntimeError):
    """pyslang import failed (extra not installed)."""


class SlangElaborationError(RuntimeError):
    """pyslang parsed the sources but the elaboration / lowering step
    couldn't produce a usable :class:`Module` (top not found, source
    diagnostics, unsupported construct in this slice)."""


def _import_pyslang():
    """Lazy import. Raises :class:`SlangFrontendUnavailable` with an
    install hint if pyslang isn't on the path. Importing here (not at
    module load) keeps pyslang truly optional — the default install is
    typer-only per the AGENTS.md runtime-deps policy."""
    try:
        import pyslang  # type: ignore[import-not-found]
    except ImportError as e:
        raise SlangFrontendUnavailable(_PYSLANG_INSTALL_HINT) from e
    return pyslang


def elaborate(sources: list[Path], top: str) -> Module:
    """Elaborate ``sources`` via pyslang and produce a :class:`Module`."""
    pyslang = _import_pyslang()

    comp = pyslang.Compilation()
    for src in sources:
        tree = pyslang.SyntaxTree.fromFile(str(src))
        comp.addSyntaxTree(tree)

    # Surface fatal parse / elaboration errors — keep going through
    # warnings (matches Yosys' default behaviour: warnings don't block
    # write_json). We route diagnostics through pyslang's
    # ``TextDiagnosticClient`` which emits the usual file:line:col +
    # caret summary that users already recognise from compiler output.
    diags = comp.getAllDiagnostics()
    fatal = [d for d in diags if d.isError()]
    if fatal:
        engine = pyslang.DiagnosticEngine(comp.sourceManager)
        client = pyslang.TextDiagnosticClient()
        client.showColors(False)
        engine.addClient(client)
        for d in fatal:
            engine.issue(d)
        raise SlangElaborationError(
            "pyslang elaboration produced errors:\n" + client.getString()
        )

    root = comp.getRoot()
    matching = [i for i in root.topInstances if i.name == top]
    if not matching:
        available = [i.name for i in root.topInstances]
        raise SlangElaborationError(
            f"top module {top!r} not found among elaborated instances: {available}"
        )

    return _ModuleBuilder(matching[0], comp, pyslang).build()


# --- internal: build a Yosys-shape Module from a pyslang InstanceSymbol -----


class _ModuleBuilder:
    """Translate a pyslang elaborated :class:`InstanceSymbol` into a
    :class:`netlist.Module`.

    The Yosys-shape contract the rule pack consumes:

    - ports + cells + netnames keyed by name, all at one (flat) level,
    - net bits as integers (one ID per signal-bit), with the constants
      ``"0"`` / ``"1"`` / ``"x"`` / ``"z"`` represented as those strings,
    - cell type strings matching the Yosys ``$dff`` / ``$adff`` / ``$and``
      / ``$xor`` / … zoo,
    - pin names ``CLK`` / ``D`` / ``Q`` / ``ARST`` on flops, ``A`` / ``B``
      / ``Y`` (etc.) on comb cells.

    Bit IDs start at 2 so the constant chars ``"0"`` and ``"1"`` never
    collide with a real net — same convention Yosys uses in its JSON.
    """

    def __init__(self, top_inst: Any, compilation: Any, pyslang: Any) -> None:
        self.top = top_inst
        self.comp = compilation
        self.pyslang = pyslang
        self._next_bit_id = 2
        # Map canonical pyslang VariableSymbol → tuple of bit refs.
        # PortSymbols are normalised to their internal Variable before
        # lookup so a port and its underlying variable share bits.
        # Entries start as integer-only tuples from the allocator, but
        # the continuous-assign aliasing pass can rewrite them to point
        # at constants or other variables' bits — hence ``Bit``.
        self._var_bits: dict[Any, tuple[Bit, ...]] = {}
        self._ports: dict[str, Port] = {}
        self._cells: dict[str, Cell] = {}
        self._netnames: dict[str, Netname] = {}
        self._cell_counters: dict[str, int] = {}

    # -- public ----------------------------------------------------------

    def build(self) -> Module:
        # Order matters: variables get IDs first so flops/assigns can
        # reference them without ambiguity.
        for member in self.top.body:
            self._collect_variable(member)
        for member in self.top.body:
            self._collect_port(member)
        for member in self.top.body:
            self._emit_for_member(member)
        return Module(
            name=self.top.name,
            ports=self._ports,
            cells=self._cells,
            netnames=self._netnames,
        )

    # -- helpers ---------------------------------------------------------

    def _alloc_bits(self, var_sym: Any) -> tuple[Bit, ...]:
        """Allocate (or recall) bit refs for ``var_sym``.

        Ports and variables that share an underlying storage symbol
        (i.e. a port and its corresponding internal ``logic``) must
        return the same bit tuple. ``_canonical_var`` resolves that
        aliasing before we hit the cache.
        """
        var_sym = self._canonical_var(var_sym)
        cached = self._var_bits.get(var_sym)
        if cached is not None:
            return cached
        width = self._width_of(var_sym)
        bits: tuple[Bit, ...] = tuple(
            range(self._next_bit_id, self._next_bit_id + width)
        )
        self._next_bit_id += width
        self._var_bits[var_sym] = bits
        return bits

    @staticmethod
    def _canonical_var(sym: Any) -> Any:
        """Resolve a port symbol to its backing variable symbol.

        pyslang represents a port and its body-level ``logic`` as two
        separate :class:`Symbol`s connected by ``PortSymbol.internalSymbol``.
        We canonicalise to the internal variable so the bit-id cache
        keys on a single object identity.
        """
        internal = getattr(sym, "internalSymbol", None)
        if internal is not None:
            return internal
        return sym

    @staticmethod
    def _width_of(var_sym: Any) -> int:
        t = getattr(var_sym, "type", None)
        if t is None:
            return 1
        # bitWidth is present on integral types; non-integral types
        # (e.g. interfaces, modports) aren't supported yet — width 1
        # is a safe placeholder that surfaces as a malformed cell
        # downstream rather than crashing the build.
        return int(getattr(t, "bitWidth", 1) or 1)

    def _fresh_cell_name(self, type_str: str) -> str:
        # Yosys autogen names look like "$procdff$3" — we don't need
        # bit-exact parity, but a stable prefix-and-counter keeps
        # waiver regexes that target the cell name readable.
        n = self._cell_counters.get(type_str, 0) + 1
        self._cell_counters[type_str] = n
        return f"$slang${type_str.strip('$')}${n}"

    def _kind_name(self, sym: Any) -> str:
        """Class-name shortcut. pyslang exposes ``__class__.__name__``
        and a ``kind`` enum; the class name is sufficient and avoids
        having to thread the enum import through every comparison."""
        return type(sym).__name__

    # -- pass 1: variables ----------------------------------------------

    def _collect_variable(self, member: Any) -> None:
        if self._kind_name(member) != "VariableSymbol":
            return
        bits = self._alloc_bits(member)
        # Forward declaration-site attributes. pyslang stores them on
        # the :class:`Compilation`, not the symbol — ``getAttributes``
        # returns the AttributeSymbols whose scope contains ``member``.
        # The rule pack only cares about the attribute **name**
        # (``cdc_sync`` / ``cdc_gray`` / ``async_reg`` / …); the value
        # is rendered as its syntax text for round-tripping but isn't
        # consulted by any rule today.
        attrs: dict[str, str] = {}
        for attr in self.comp.getAttributes(member) or []:
            name = getattr(attr, "name", None)
            if not name:
                continue
            val = getattr(attr, "value", None)
            attrs[name] = str(val) if val is not None else "1"
        # Skip if a netname with this name already exists (e.g. a port
        # shares the name with its internal variable). Ports take
        # precedence in the port table; the variable still owns the
        # netname so attributes attached to the wire/reg survive.
        self._netnames[member.name] = Netname(
            name=member.name, bits=bits, attributes=attrs
        )

    # -- pass 2: ports ---------------------------------------------------

    def _collect_port(self, member: Any) -> None:
        if self._kind_name(member) != "PortSymbol":
            return
        # PortSymbol.direction is an ``ArgumentDirection`` enum.
        d = str(getattr(member, "direction", "")).rsplit(".", 1)[-1]
        direction = {
            "In": "input",
            "Out": "output",
            "InOut": "inout",
            "Ref": "input",  # ref ports are uncommon at the boundary;
            # treat as input for CDC purposes.
        }.get(d, "input")
        internal = getattr(member, "internalSymbol", None)
        if internal is None:
            return  # nothing to wire to
        bits = self._alloc_bits(internal)
        self._ports[member.name] = Port(
            name=member.name, direction=direction, bits=bits
        )

    # -- pass 3: cells + continuous assigns -----------------------------

    def _emit_for_member(self, member: Any) -> None:
        kind = self._kind_name(member)
        if kind == "ProceduralBlockSymbol":
            self._emit_procedural_block(member)
        elif kind == "ContinuousAssignSymbol":
            self._emit_continuous_assign(member)
        # InstanceSymbol (child instances), GenerateBlockSymbol, etc.
        # are deliberately ignored in this slice — the roadmap covers
        # them. Silently skipping is preferable to crashing; the lack
        # of flops in the resulting module will be visible in `analyze`
        # output and surfaces the gap loudly to anyone running it.

    # -- always_ff lowering ----------------------------------------------

    def _emit_procedural_block(self, block: Any) -> None:
        kind = block.procedureKind
        if str(kind).rsplit(".", 1)[-1] != "AlwaysFF":
            return  # always_comb / initial / always_latch — TODO
        body = block.body
        # Expected shape: TimedStatement(timing=EventListControl, stmt=...)
        if self._kind_name(body) != "TimedStatement":
            return
        clk_sym, reset_sym, reset_active_low = self._analyse_event_list(body.timing)
        if clk_sym is None:
            return  # can't identify clock — skip rather than crash
        # Drill through the optional BlockStatement wrapper.
        inner = body.stmt
        if self._kind_name(inner) == "BlockStatement":
            inner = inner.body
        # Two canonical shapes:
        #   (a) ConditionalStatement: if (<reset_check>) q <= 0;
        #                             else                q <= d;
        #   (b) ExpressionStatement: q <= d;   (no async reset)
        if self._kind_name(inner) == "ConditionalStatement":
            # The if-condition's variable IS the reset signal — confirm
            # against the event-list-derived candidate, but trust the
            # body shape when they disagree (event lists are sometimes
            # ordered unconventionally).
            cond_reset = self._extract_condition_symbol(inner.conditions[0].expr)
            if cond_reset is not None:
                reset_sym = cond_reset
            data_branch = inner.ifFalse
            if data_branch is None:
                return  # no else → no data assignment to model
            self._emit_assignments_in(data_branch, clk_sym, reset_sym, reset_active_low)
        elif self._kind_name(inner) == "ExpressionStatement":
            self._emit_assignment_expression(
                inner.expr, clk_sym, reset_sym=None, reset_active_low=False
            )

    def _analyse_event_list(self, timing: Any) -> tuple[Any | None, Any | None, bool]:
        """Pick clock + (optional) async-reset symbols out of the event
        list.

        Returns ``(clock_sym, reset_sym, reset_active_low)``. The reset
        identification is provisional — the canonical conditional body
        shape gives the authoritative answer and we override here when
        we see it. The polarity comes from the event edge: ``negedge``
        on the reset event means the reset asserts low.
        """
        events = list(getattr(timing, "events", []) or [])
        if not events:
            return None, None, False
        # Single-event case: that's the clock, no reset.
        if len(events) == 1:
            ev = events[0]
            return getattr(ev.expr, "symbol", None), None, False
        # Two-event case (the common async-reset shape): the event
        # whose edge matches the conventional clock side is the clock;
        # the other is provisionally the reset.
        clk_ev, rst_ev = events[0], events[1]
        clk_sym = getattr(clk_ev.expr, "symbol", None)
        rst_sym = getattr(rst_ev.expr, "symbol", None)
        rst_edge = str(getattr(rst_ev, "edge", "")).rsplit(".", 1)[-1]
        return clk_sym, rst_sym, rst_edge == "NegEdge"

    @staticmethod
    def _extract_condition_symbol(expr: Any) -> Any | None:
        """For an if-condition that's the reset check (``rst`` or
        ``!rst_n``), return the underlying VariableSymbol. Anything
        more elaborate (``rst && enable``, function calls, …) → None,
        and the caller falls back to the event-list guess."""
        # UnaryExpression(operand=NamedValueExpression)  → !rst form
        if type(expr).__name__ == "UnaryExpression":
            operand = getattr(expr, "operand", None)
            if operand is not None and type(operand).__name__ == "NamedValueExpression":
                return operand.symbol
        # NamedValueExpression directly  → positive-polarity reset
        if type(expr).__name__ == "NamedValueExpression":
            return expr.symbol
        return None

    def _emit_assignments_in(
        self,
        statement: Any,
        clk_sym: Any,
        reset_sym: Any | None,
        reset_active_low: bool,
    ) -> None:
        """Find every nonblocking assignment inside ``statement`` (a
        BlockStatement or single ExpressionStatement) and emit a flop
        cell per assignment."""
        kind = self._kind_name(statement)
        if kind == "BlockStatement":
            statement = statement.body
            kind = self._kind_name(statement)
        if kind == "ExpressionStatement":
            self._emit_assignment_expression(
                statement.expr, clk_sym, reset_sym, reset_active_low
            )
            return
        if kind in {"StatementList", "ListStatement"}:
            # Older pyslang names; included defensively. Real walk is
            # via the .list attribute when present.
            for s in getattr(statement, "list", []) or []:
                self._emit_assignments_in(s, clk_sym, reset_sym, reset_active_low)

    def _emit_assignment_expression(
        self,
        expr: Any,
        clk_sym: Any,
        reset_sym: Any | None,
        reset_active_low: bool,
    ) -> None:
        if type(expr).__name__ != "AssignmentExpression":
            return
        if not getattr(expr, "isNonBlocking", False):
            # Blocking inside always_ff is a SV style violation; skip
            # rather than try to model it.
            return
        lhs = expr.left
        rhs = expr.right
        # We need a bare register reference on the LHS so we know which
        # variable's bits become Q. Anything more complex (slice, part-
        # select, struct member) → skip for now.
        if type(lhs).__name__ != "NamedValueExpression":
            return
        q_var = lhs.symbol
        q_bits = self._alloc_bits(q_var)
        d_bits = self._bits_of_expression(rhs)
        if d_bits is None:
            # RHS isn't a bare named value yet — we'd need primitive
            # lowering to model it. Emit a $_UNKNOWN_ driver so the
            # netlist isn't corrupt; the rule pack will treat the D
            # input as opaque (no upstream flops trace through it).
            d_bits = self._unknown_driver(width=len(q_bits))

        connections: dict[str, tuple[Bit, ...]] = {
            "CLK": self._alloc_bits(clk_sym),
            "D": d_bits,
            "Q": q_bits,
        }
        cell_type = "$dff"
        if reset_sym is not None:
            connections["ARST"] = self._alloc_bits(reset_sym)
            cell_type = "$adff"
        # Polarity parameter mirrors Yosys' parameter encoding so any
        # rule that later inspects polarity has it available; the rule
        # pack doesn't read these today but the contract is cheap to
        # preserve.
        parameters: dict[str, str] = {
            "CLK_POLARITY": "1",
        }
        if reset_sym is not None:
            parameters["ARST_POLARITY"] = "0" if reset_active_low else "1"
        name = self._fresh_cell_name(cell_type)
        self._cells[name] = Cell(
            name=name,
            type=cell_type,
            connections=connections,
            parameters=parameters,
            attributes={},
        )

    def _bits_of_expression(self, expr: Any) -> tuple[Bit, ...] | None:
        """Return the bit tuple for an RHS expression, or ``None`` if
        the shape isn't supported yet.

        Today we only handle a bare :class:`NamedValueExpression` —
        that's enough for the direct flop→flop wire in
        ``bad_single_ff_sync``. Anything else (binary ops, conditional
        expr, concat, part-select, conversions) lands in the
        primitive-lowering work item; the caller substitutes an
        unknown driver in the meantime.
        """
        kind = type(expr).__name__
        if kind == "NamedValueExpression":
            return self._alloc_bits(expr.symbol)
        # ConversionExpression often wraps integer-width adjustment
        # without affecting bit identity for width-1 signals — peek
        # through it.
        if kind == "ConversionExpression":
            inner = getattr(expr, "operand", None)
            if inner is not None:
                return self._bits_of_expression(inner)
        return None

    def _unknown_driver(self, width: int) -> tuple[Bit, ...]:
        """Allocate a fresh net driven by a stub ``$_UNKNOWN_`` cell.

        Used for RHS shapes the elaborator doesn't model yet. The
        driver cell exists so the rule pack's driver lookup still
        finds *something* — but with type ``$_UNKNOWN_`` it isn't
        recognised as a comb primitive or a flop, so traversal stops
        there. That's the conservative behaviour: we miss real
        crossings rather than emit phantom ones.
        """
        bits: tuple[Bit, ...] = tuple(
            range(self._next_bit_id, self._next_bit_id + width)
        )
        self._next_bit_id += width
        name = self._fresh_cell_name("$_UNKNOWN_")
        self._cells[name] = Cell(
            name=name,
            type="$_UNKNOWN_",
            connections={"Y": bits},
            parameters={},
            attributes={},
        )
        return bits

    # -- continuous assigns ---------------------------------------------

    def _emit_continuous_assign(self, member: Any) -> None:
        a = getattr(member, "assignment", None)
        if a is None or type(a).__name__ != "AssignmentExpression":
            return
        lhs, rhs = a.left, a.right
        if type(lhs).__name__ != "NamedValueExpression":
            return
        rhs_bits = self._bits_of_expression(rhs)
        if rhs_bits is None:
            # Comb-driven port — primitive lowering will handle this.
            return
        # Aliasing trick: rewrite the LHS variable's bits to point at
        # the RHS's bits so any reader of the LHS sees the RHS source
        # directly. This matches Yosys' post-opt_clean output for
        # ``assign out = sig`` and means we don't need a $buf cell.
        canonical = self._canonical_var(lhs.symbol)
        existing = self._var_bits.get(canonical)
        self._var_bits[canonical] = rhs_bits
        # Update any Port that already pointed at the old bits.
        if existing is not None:
            self._rewrite_bits_in_ports(existing, rhs_bits)
            self._rewrite_bits_in_netname(canonical, rhs_bits)

    def _rewrite_bits_in_ports(
        self, old: tuple[Bit, ...], new: tuple[Bit, ...]
    ) -> None:
        for name, port in list(self._ports.items()):
            if port.bits == old:
                self._ports[name] = Port(
                    name=port.name, direction=port.direction, bits=new
                )

    def _rewrite_bits_in_netname(self, var_sym: Any, new: tuple[Bit, ...]) -> None:
        nn = self._netnames.get(var_sym.name)
        if nn is None:
            return
        self._netnames[var_sym.name] = Netname(
            name=nn.name, bits=new, attributes=nn.attributes
        )
