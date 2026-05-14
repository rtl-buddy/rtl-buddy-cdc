"""slang frontend — elaborate SystemVerilog via pyslang into a
Yosys-shape :class:`netlist.Module` consumable by the rule pack.

Status
------
**Stage 2 (fourth slice — full fixture parity).** All 25
SDC-equipped fixtures reach parity with the Yosys frontend,
covering register-to-register CDC, combinational shapes,
multi-bit LHS bit-selects, and now multi-module hierarchies.
The two ``*_source_sync_internal`` fixtures that embed Yosys-
internal ``$_BUF_`` primitives in SV source are correctly
rejected by slang as not being legal SystemVerilog and stay
Yosys-frontend-only by design.

Rule parity matrix (today)
~~~~~~~~~~~~~~~~~~~~~~~~~~
Driven by ``tests/test_slang_elaboration.py``; each row is a fixture
that produces the same violation set as the Yosys frontend.

==================================  =================
fixture                             expected rules
==================================  =================
bad_single_ff_sync                  CDC-001
bad_port_no_sync                    CDC-001
bad_comb_before_sync                CDC-003
bad_bus_crossing                    CDC-004
bad_reconvergent_sync               CDC-005
bad_comb_source                     CDC-006
bad_input_delay_cross_domain        CDC-006
bad_reset_crossing                  CDC-007
bad_reset_tree                      CDC-007 (grouped)
bad_clock_as_data                   CDC-008
bad_source_sync_chain               CDC-001 × 4 (4-module hierarchy)
good_2ff_sync                       (none)
good_gray_counter_crossing          (none)
good_registered_before_sync         (none)
good_registered_source              (none)
good_exclusive_clock_mux            (none)
good_false_path_pair                (none)
good_generated_clock_div2           (none)
good_port_typed_sync                (none)
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
  source variable's bits (matches Yosys post-``opt_clean``). The
  aliasing is propagated globally: every entry in the bit-id maps
  that holds the old tuple is rewritten, so chains across the
  hierarchy boundary (parent ``a_q`` ← child ``q``) collapse to a
  single net rather than leaving stale aliases.
- Multi-module hierarchies — child :class:`InstanceSymbol`s are
  walked recursively; their flops, comb cells, and netnames land
  in the same flat ``Module`` with dotted prefixes (``u_b0.q``,
  ``u_b0.$slang$adff$3``) matching Yosys-flatten output. Port
  connections are the wiring step: each child internal variable
  backing a port is aliased to the parent's connection expression
  bits, so flop A's Q in one instance and flop B's D in the next
  resolve to the same net.
- Combinational primitive lowering: see :meth:`_lower_binary`
  / :meth:`_lower_unary` / :meth:`_lower_conditional`. Pyslang
  expression operators round-trip to the Yosys cell zoo
  (``$and``/``$or``/``$xor``/``$mux``/…), so the rule pack walks
  the comb cone correctly.
- LHS bit-selects (``q[0] <= ...``) and contiguous ranges
  (``bus[3:0] <= ...``) — see :meth:`_lvalue_bits`.
- Fatal pyslang diagnostics are surfaced through
  ``TextDiagnosticClient`` with file:line:col + caret summaries —
  the usual compiler-error format users already recognise.

What is NOT yet implemented (next slices)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
- **Concat / part-select / replication on the RHS.** Today these
  fall through :meth:`_bits_of_expression` and the caller emits a
  ``$_UNKNOWN_`` placeholder driver. Real lowering needs
  :class:`ConcatenationExpression` → bit-tuple concat,
  :class:`RangeSelectExpression` → bit-tuple slice, and
  :class:`ReplicationExpression` → repeated bits.
- **``always_comb`` with ``if`` / ``case``.** Body walks descend into
  both branches and may produce inconsistent aliasing when each
  branch writes the same variable. The full lowering builds a
  ``$mux`` per LHS that appears in both branches; today we
  conservatively pass through whichever branch wrote last.
- **Source locations** — :class:`Cell.attributes["src"]` is left
  empty, so JSON/SARIF reports lose their file:line context on the
  slang path. pyslang carries source ranges on every Symbol /
  Expression; threading those through is a follow-up.
- **Width-N $dff / $adff parameters** (``WIDTH``, ``ARST_VALUE``)
  — partially populated today; not yet read by any rule but should
  reach full Yosys parity for future rule extensions.
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
        # Stack of dotted hierarchy prefixes used by ``_fresh_cell_name``
        # so cells emitted while walking a child instance carry the
        # ``u_child.`` prefix matching Yosys-flatten output. ``""`` at
        # the top level. Push/pop bracket each ``_walk_instance`` call.
        self._hier_stack: list[str] = [""]

    # -- public ----------------------------------------------------------

    def build(self) -> Module:
        # The top instance's ports become the flat ``Module``'s ports;
        # child instances contribute flops / comb / netnames into the
        # same flat namespace, hierarchically prefixed (``u_b0.q``).
        self._walk_instance(self.top, hier_prefix="", bound_internals=frozenset())
        return Module(
            name=self.top.name,
            ports=self._ports,
            cells=self._cells,
            netnames=self._netnames,
        )

    def _walk_instance(
        self,
        inst: Any,
        hier_prefix: str,
        bound_internals: frozenset[Any],
    ) -> None:
        """Walk an :class:`InstanceSymbol`'s body in three passes —
        variables, ports (top only), then cells / continuous assigns /
        child instances.

        ``hier_prefix`` is the dotted instance path (``""`` at the top,
        ``"u_a."`` one level in, ``"u_top.u_a."`` two levels) used to
        namespace netname keys and emitted cell names. ``bound_internals``
        is the set of port-internal variables this instance has already
        had its bits aliased to a parent expression — they get skipped
        in pass 1 so we don't allocate fresh bits for them.
        """
        self._hier_stack.append(hier_prefix)
        try:
            for member in inst.body:
                if (
                    self._kind_name(member) == "VariableSymbol"
                    and member not in bound_internals
                ):
                    self._collect_variable(member, hier_prefix)
            # Only the top module exposes ports in the flat ``Module``;
            # child ports get folded into their parents' connections.
            if hier_prefix == "":
                for member in inst.body:
                    self._collect_port(member)
            for member in inst.body:
                self._emit_for_member(member, hier_prefix)
        finally:
            self._hier_stack.pop()

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
        # waiver regexes that target the cell name readable. The
        # current hierarchy prefix (e.g. ``u_b0.``) matches what
        # Yosys ``flatten`` would produce for cells inside a child
        # instance.
        n = self._cell_counters.get(type_str, 0) + 1
        self._cell_counters[type_str] = n
        prefix = self._hier_stack[-1] if self._hier_stack else ""
        return f"{prefix}$slang${type_str.strip('$')}${n}"

    def _kind_name(self, sym: Any) -> str:
        """Class-name shortcut. pyslang exposes ``__class__.__name__``
        and a ``kind`` enum; the class name is sufficient and avoids
        having to thread the enum import through every comparison."""
        return type(sym).__name__

    # -- pass 1: variables ----------------------------------------------

    def _collect_variable(self, member: Any, hier_prefix: str = "") -> None:
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
        # Hierarchical name matches Yosys-flatten output: ``u_b0.q``,
        # ``u_top.u_b0.q``, etc. At the top level ``hier_prefix`` is
        # empty so the bare name is used. The rule pack consults
        # netname attributes via the bits→netname reverse-lookup, so
        # the name is only consulted for debug / source-location use.
        full_name = f"{hier_prefix}{member.name}"
        self._netnames[full_name] = Netname(name=full_name, bits=bits, attributes=attrs)

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

    def _emit_for_member(self, member: Any, hier_prefix: str = "") -> None:
        kind = self._kind_name(member)
        if kind == "ProceduralBlockSymbol":
            self._emit_procedural_block(member, hier_prefix)
        elif kind == "ContinuousAssignSymbol":
            self._emit_continuous_assign(member)
        elif kind == "InstanceSymbol":
            self._emit_child_instance(member, hier_prefix)
        # GenerateBlockSymbol and other unmodelled kinds fall through
        # silently — the lack of expected flops will be visible in
        # `analyze` output, which is a better failure mode than
        # crashing on an unrecognised member kind.

    def _emit_child_instance(self, child: Any, parent_prefix: str) -> None:
        """Inline a child instance's body into the flat ``Module``.

        Port connections are the load-bearing wiring step: the child's
        internal variables backing each port get their bits aliased to
        the parent's connection expression so net identity is preserved
        across the hierarchy boundary. After that, the child body is
        walked exactly like the top.
        """
        bound: set[Any] = set()
        for pc in getattr(child, "portConnections", []) or []:
            port = pc.port
            internal = getattr(port, "internalSymbol", None)
            if internal is None:
                continue
            parent_bits = self._port_connection_bits(pc)
            if parent_bits is None:
                continue
            # Alias the child's internal-variable bits to the parent's
            # wire. ``_alloc_bits`` keys on the internal Symbol identity
            # which is unique per elaborated instance, so two
            # instantiations of the same module don't share bits.
            self._var_bits[self._canonical_var(internal)] = parent_bits
            bound.add(internal)

        child_prefix = f"{parent_prefix}{child.name}."
        self._walk_instance(
            child, hier_prefix=child_prefix, bound_internals=frozenset(bound)
        )

    def _port_connection_bits(self, pc: Any) -> tuple[Bit, ...] | None:
        """Resolve a :class:`PortConnection` to the parent-side bits.

        Three shapes appear in practice:
        - **Input port**: ``pc.expression`` is the parent-side driver
          expression (typically a :class:`NamedValueExpression`).
          Just lower it.
        - **Output port**: ``pc.expression`` is an
          :class:`AssignmentExpression` whose ``.left`` is the
          parent's capturing variable. ``.right`` is an
          :class:`EmptyArgumentExpression` placeholder. Use the LHS.
        - **Unconnected port**: ``pc.expression`` is None — nothing
          to alias to. Return None so the child's internal allocates
          its own fresh bits.
        """
        ex = pc.expression
        if ex is None:
            return None
        kind = type(ex).__name__
        if kind == "AssignmentExpression":
            return self._lvalue_bits(ex.left)
        if kind == "EmptyArgumentExpression":
            return None
        return self._bits_of_expression(ex)

    # -- always_ff lowering ----------------------------------------------

    def _emit_procedural_block(self, block: Any, hier_prefix: str = "") -> None:
        # hier_prefix is read indirectly through ``_hier_stack`` which
        # the walker keeps in sync via the ``_walk_instance`` push/pop;
        # accept it on the signature for symmetry with the other
        # ``_emit_*`` callbacks.
        del hier_prefix
        kind_name = str(block.procedureKind).rsplit(".", 1)[-1]
        if kind_name == "AlwaysComb":
            self._emit_always_comb(block)
            return
        if kind_name != "AlwaysFF":
            return  # initial / always_latch / final — not modelled
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
        # Resolve the LHS to the subset of a variable's bits that the
        # flop's Q should drive. Supports bare ``NamedValueExpression``
        # (full width), ``ElementSelectExpression`` (single bit), and
        # ``RangeSelectExpression`` (a contiguous range). Other LHS
        # shapes (struct member, hierarchical reference, …) are skipped.
        q_bits = self._lvalue_bits(lhs)
        if q_bits is None:
            return
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

    def _lvalue_bits(self, expr: Any) -> tuple[Bit, ...] | None:
        """Resolve an LHS expression to the variable-bit subset it
        writes."""
        kind = type(expr).__name__
        if kind == "NamedValueExpression":
            return self._alloc_bits(expr.symbol)
        if kind == "ElementSelectExpression":
            return self._element_select_bits(expr)
        if kind == "RangeSelectExpression":
            return self._range_select_bits(expr)
        return None

    def _element_select_bits(self, expr: Any) -> tuple[Bit, ...] | None:
        """``var[i]`` — return the single-bit subset of ``var``'s bits.

        Falls back to ``None`` when the underlying value isn't a bare
        named variable (e.g. nested selects, slices of expressions),
        and when the selector isn't a constant integer — dynamic
        indexing would require muxing across all possible bits, which
        no rule today is worth.
        """
        inner = expr.value
        if type(inner).__name__ != "NamedValueExpression":
            return None
        idx = self._const_int(getattr(expr, "selector", None))
        if idx is None:
            return None
        var_bits = self._alloc_bits(inner.symbol)
        if not (0 <= idx < len(var_bits)):
            return None
        return (var_bits[idx],)

    def _range_select_bits(self, expr: Any) -> tuple[Bit, ...] | None:
        """``var[hi:lo]`` — return the contiguous bit subset.

        Yosys stores bits LSB-first, so ``var[3:0]`` returns
        ``var_bits[0:4]``. Non-constant ranges and indexed parts
        (``var[i+:N]``) return ``None``.
        """
        inner = expr.value
        if type(inner).__name__ != "NamedValueExpression":
            return None
        left = self._const_int(getattr(expr, "left", None))
        right = self._const_int(getattr(expr, "right", None))
        if left is None or right is None:
            return None
        lo, hi = min(left, right), max(left, right)
        var_bits = self._alloc_bits(inner.symbol)
        if lo < 0 or hi >= len(var_bits):
            return None
        return tuple(var_bits[lo : hi + 1])

    @staticmethod
    def _const_int(expr: Any) -> int | None:
        """Best-effort: extract an ``int`` from a constant pyslang
        expression. Returns ``None`` if the expression isn't a
        compile-time constant — selectors on the LHS need to be
        static for the flop-shape model to work."""
        if expr is None:
            return None
        # pyslang exposes ``.constant`` (an SVInt) on most expressions
        # once compilation has folded the operand; an IntegerLiteral
        # additionally has ``.value``.
        for attr in ("constant", "value"):
            v = getattr(expr, attr, None)
            if v is None:
                continue
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
        return None

    def _bits_of_expression(self, expr: Any) -> tuple[Bit, ...] | None:
        """Return the bit tuple for an RHS expression, or ``None`` if
        the shape isn't supported yet.

        Handles the common cases:
        - :class:`NamedValueExpression` — direct variable read.
        - :class:`ConversionExpression` — pass through (pyslang inserts
          these for implicit width / type unification; the underlying
          bit identity is what we want).
        - :class:`IntegerLiteral` — single-bit constants ``1'b0`` /
          ``1'b1`` (the only widths we use in fixtures today).
        - :class:`BinaryExpression` — lowered to a Yosys-shape comb
          cell (``$and``/``$or``/``$xor``/…) via :meth:`_lower_binary`.
        - :class:`UnaryExpression` — lowered to ``$not``/``$logic_not``/
          ``$neg``/``$reduce_*`` via :meth:`_lower_unary`.
        - :class:`ConditionalExpression` — lowered to a ``$mux`` cell.

        Anything else (concat, part-select, function call, …) returns
        ``None``; the caller substitutes an ``$_UNKNOWN_`` driver so
        the rule pack treats the upstream as opaque rather than
        crashing.
        """
        kind = type(expr).__name__
        if kind == "NamedValueExpression":
            return self._alloc_bits(expr.symbol)
        if kind == "ConversionExpression":
            inner = getattr(expr, "operand", None)
            if inner is not None:
                return self._bits_of_expression(inner)
        if kind == "IntegerLiteral":
            return self._bits_of_integer_literal(expr)
        if kind == "BinaryExpression":
            return self._lower_binary(expr)
        if kind == "UnaryExpression":
            return self._lower_unary(expr)
        if kind == "ConditionalExpression":
            return self._lower_conditional(expr)
        if kind == "ElementSelectExpression":
            return self._element_select_bits(expr)
        if kind == "RangeSelectExpression":
            return self._range_select_bits(expr)
        return None

    # --- expression lowering --------------------------------------------

    # pyslang BinaryOperator → Yosys cell type. Where Yosys distinguishes
    # bitwise (``$and``) from reduction-style logical (``$logic_and``),
    # we follow the same split. Comparisons and arithmetic round-trip to
    # the same Yosys op names the post-``proc`` netlist would carry.
    _BINOP_CELL: dict[str, str] = {
        "BinaryAnd": "$and",
        "BinaryOr": "$or",
        "BinaryXor": "$xor",
        "BinaryXnor": "$xnor",
        "LogicalAnd": "$logic_and",
        "LogicalOr": "$logic_or",
        "Equality": "$eq",
        "Inequality": "$ne",
        "CaseEquality": "$eqx",
        "CaseInequality": "$nex",
        "LessThan": "$lt",
        "LessThanEqual": "$le",
        "GreaterThan": "$gt",
        "GreaterThanEqual": "$ge",
        "Add": "$add",
        "Subtract": "$sub",
        "Multiply": "$mul",
        "Divide": "$div",
        "Mod": "$mod",
        "LogicalShiftLeft": "$shl",
        "LogicalShiftRight": "$shr",
        "ArithmeticShiftLeft": "$sshl",
        "ArithmeticShiftRight": "$sshr",
    }

    _UNOP_CELL: dict[str, str] = {
        "BitwiseNot": "$not",
        "LogicalNot": "$logic_not",
        "Minus": "$neg",
        # Reduction operators — these collapse a multi-bit operand to a
        # single bit. ``Plus`` is identity and skipped at the call site.
        "BitwiseAnd": "$reduce_and",
        "BitwiseOr": "$reduce_or",
        "BitwiseXor": "$reduce_xor",
        "BitwiseNand": "$reduce_and",  # then invert; we don't model the
        # inversion explicitly — the rule
        # pack doesn't read the cell name
        # beyond category membership.
        "BitwiseNor": "$reduce_or",
        "BitwiseXnor": "$reduce_xor",
    }

    def _lower_binary(self, expr: Any) -> tuple[Bit, ...] | None:
        op_name = str(expr.op).rsplit(".", 1)[-1]
        cell_type = self._BINOP_CELL.get(op_name)
        if cell_type is None:
            return None  # unmodelled op → unknown driver
        a_bits = self._bits_of_expression(expr.left)
        b_bits = self._bits_of_expression(expr.right)
        if a_bits is None or b_bits is None:
            return None
        width = self._expr_width(expr)
        return self._emit_comb_cell(
            cell_type, {"A": a_bits, "B": b_bits}, output_width=width
        )

    def _lower_unary(self, expr: Any) -> tuple[Bit, ...] | None:
        op_name = str(expr.op).rsplit(".", 1)[-1]
        if op_name == "Plus":
            # Unary plus is identity in SV — peek through.
            return self._bits_of_expression(expr.operand)
        cell_type = self._UNOP_CELL.get(op_name)
        if cell_type is None:
            return None
        a_bits = self._bits_of_expression(expr.operand)
        if a_bits is None:
            return None
        # Reduction operators produce a single bit regardless of input
        # width; everything else preserves the operand width.
        if cell_type.startswith("$reduce_") or cell_type == "$logic_not":
            output_width = 1
        else:
            output_width = self._expr_width(expr) or len(a_bits)
        return self._emit_comb_cell(cell_type, {"A": a_bits}, output_width=output_width)

    def _lower_conditional(self, expr: Any) -> tuple[Bit, ...] | None:
        # pyslang ConditionalExpression has ``conditions`` (a list) and
        # ``left``/``right`` — the conditional-pattern grammar is more
        # general than ``cond ? a : b``, but for our purposes we only
        # need the single-condition shape.
        conds = getattr(expr, "conditions", None)
        if not conds:
            return None
        sel_bits = self._bits_of_expression(conds[0].expr)
        a_bits = self._bits_of_expression(expr.left)
        b_bits = self._bits_of_expression(expr.right)
        if sel_bits is None or a_bits is None or b_bits is None:
            return None
        width = self._expr_width(expr) or max(len(a_bits), len(b_bits))
        # Yosys ``$mux`` selects between A (sel=0) and B (sel=1); same
        # convention we use here. The S pin takes the (single-bit)
        # selector.
        return self._emit_comb_cell(
            cell_type="$mux",
            inputs={"A": a_bits, "B": b_bits, "S": sel_bits[:1]},
            output_width=width,
        )

    def _bits_of_integer_literal(self, expr: Any) -> tuple[Bit, ...] | None:
        """Map an :class:`IntegerLiteral` to Yosys constant bits.

        Yosys represents constant bits as the strings ``"0"``, ``"1"``,
        ``"x"``, ``"z"``. The bit tuple is LSB-first to match Yosys
        write_json ordering.
        """
        # pyslang exposes the value via ``expr.value`` as an SVInt.
        # SVInt has a ``__int__`` for small concrete values; bigger
        # ones we'd need to walk per-bit. For our fixtures the literals
        # are 1-bit (``1'b0`` / ``1'b1``).
        val = getattr(expr, "value", None)
        if val is None:
            return None
        try:
            as_int = int(val)
        except (TypeError, ValueError):
            return None
        width = self._expr_width(expr) or 1
        # LSB-first per Yosys convention.
        return tuple("1" if (as_int >> i) & 1 else "0" for i in range(width))

    def _expr_width(self, expr: Any) -> int:
        t = getattr(expr, "type", None)
        if t is None:
            return 0
        return int(getattr(t, "bitWidth", 0) or 0)

    def _emit_comb_cell(
        self,
        cell_type: str,
        inputs: dict[str, tuple[Bit, ...]],
        output_width: int,
    ) -> tuple[Bit, ...]:
        """Allocate a fresh Y net and emit a ``cell_type`` cell with the
        given inputs. Returns the Y bits so callers can wire them up.

        Width defensively clamped to at least 1 so we never emit a
        zero-width cell (which would confuse the rule pack's bit walks).
        """
        width = max(int(output_width or 1), 1)
        y_bits: tuple[Bit, ...] = tuple(
            range(self._next_bit_id, self._next_bit_id + width)
        )
        self._next_bit_id += width
        name = self._fresh_cell_name(cell_type)
        self._cells[name] = Cell(
            name=name,
            type=cell_type,
            connections={**inputs, "Y": y_bits},
            parameters={},
            attributes={},
        )
        return y_bits

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

    # -- always_comb ----------------------------------------------------

    def _emit_always_comb(self, block: Any) -> None:
        """Treat each blocking assignment in an ``always_comb`` block as
        a continuous assign of its RHS to its LHS.

        SV semantics already require an ``always_comb`` to be
        combinational, so the rewrite is semantically faithful (no
        priority encoding or latching to model). We descend through
        the typical ``BlockStatement(body=ExpressionStatement(...))``
        wrapper plus optional ``ListStatement`` for multi-assign
        bodies.
        """
        self._walk_comb_statement(block.body)

    def _walk_comb_statement(self, stmt: Any) -> None:
        kind = self._kind_name(stmt)
        if kind == "BlockStatement":
            self._walk_comb_statement(stmt.body)
            return
        if kind in {"StatementList", "ListStatement"}:
            for s in getattr(stmt, "list", []) or []:
                self._walk_comb_statement(s)
            return
        if kind == "ExpressionStatement":
            self._alias_assign(stmt.expr)
            return
        if kind == "ConditionalStatement":
            # if/else in an always_comb is a selection between two
            # assignments. The full lowering would build a $mux per LHS
            # variable that appears in both branches; that's a follow-up
            # — for now we descend into both branches so any unconditional
            # writes get aliased and any conditional-only writes are
            # missed (conservative).
            self._walk_comb_statement(stmt.ifTrue)
            if stmt.ifFalse is not None:
                self._walk_comb_statement(stmt.ifFalse)
            return
        # case statements, for/while loops, etc. → ignore for now.

    def _alias_assign(self, expr: Any) -> None:
        """For a blocking assignment ``lhs = rhs``, rewrite ``lhs``'s
        bits to point at ``rhs``'s lowered bits — same aliasing trick
        the continuous-assign path uses."""
        if type(expr).__name__ != "AssignmentExpression":
            return
        if getattr(expr, "isNonBlocking", False):
            return  # nonblocking in always_comb is a SV style violation
        if type(expr.left).__name__ != "NamedValueExpression":
            return
        rhs_bits = self._bits_of_expression(expr.right)
        if rhs_bits is None:
            return
        self._rewrite_aliased(expr.left, rhs_bits)

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
        self._rewrite_aliased(lhs, rhs_bits)

    def _rewrite_aliased(self, lhs: Any, rhs_bits: tuple[Bit, ...]) -> None:
        """Make every alias that currently holds ``lhs``'s bits point at
        ``rhs_bits`` instead.

        Crossing the hierarchy boundary, a single net (the parent's
        ``a_q`` wire, say) is represented by several aliased entries:
        the parent's :class:`VariableSymbol`, the child instance's
        :class:`PortSymbol`-internal variable, and any local wires
        that capture it. When ``assign a_q = q`` runs inside the child,
        every one of those aliases needs to follow — not just the
        child's own a_q entry — or the parent's reader sees stale bits.

        We do the cheap thing: scan ``_var_bits`` / ``_ports`` /
        ``_netnames`` for entries whose bits equal the old tuple and
        rewrite them. O(n) per assign, fine for the design sizes the
        analyzer targets.
        """
        canonical = self._canonical_var(lhs.symbol)
        old_bits = self._var_bits.get(canonical)
        self._var_bits[canonical] = rhs_bits
        if old_bits is None or old_bits == rhs_bits:
            return
        for sym, bits in list(self._var_bits.items()):
            if bits == old_bits:
                self._var_bits[sym] = rhs_bits
        for name, port in list(self._ports.items()):
            if port.bits == old_bits:
                self._ports[name] = Port(
                    name=port.name, direction=port.direction, bits=rhs_bits
                )
        for name, nn in list(self._netnames.items()):
            if nn.bits == old_bits:
                self._netnames[name] = Netname(
                    name=nn.name, bits=rhs_bits, attributes=nn.attributes
                )
