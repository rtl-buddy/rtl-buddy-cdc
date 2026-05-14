"""slang frontend (in development — see issue #5).

Elaborate SystemVerilog directly via the ``pyslang`` Python binding to
the `slang <https://github.com/MikePopoloski/slang>`_ compiler. No
Yosys subprocess, no synthesis, no ``flatten`` step.

Status
------
Skeleton only. The function below raises :class:`NotImplementedError`
on use. The shape we need to produce is a :class:`netlist.Module` that
satisfies the rule pack's contract (Yosys-style cell types, pin names
``CLK``/``D``/``Q``/``Y``/``A``/``B``/…, integer bit IDs, SV attributes
on netnames). Producing that shape from a pyslang :class:`Compilation`
is a meaningful chunk of work, broken down below.

Implementation roadmap
----------------------
1. **Set up pyslang elaboration**
   - ``pyslang.SyntaxTree.fromFile`` per source, ``pyslang.Compilation``
     to drive elaboration; resolve the top via the chosen ``top``.
   - Surface elaboration diagnostics with source locations.
2. **Walk the elaborated instance tree**
   - Recurse through ``InstanceSymbol`` instances; collect every
     ``ProceduralBlockSymbol`` with ``ProceduralBlockKind.AlwaysFF``.
     These are our flop candidates.
3. **Flop inference**
   - From each ``always_ff`` event control extract the CLK signal
     (the ``posedge``/``negedge`` operand).
   - Recognise async-reset shape ``if (rst) <q> <= reset_val; else
     <q> <= <d>;`` to populate ``ARST`` + reset polarity; without it,
     emit a plain ``$dff``.
   - Width comes from the LHS symbol type.
4. **Net-bit allocation**
   - Allocate a unique integer Bit ID per ``(signal_symbol, bit_index)``.
     Hierarchical signals from inner instances get their own IDs;
     port connections create aliasing through a union-find.
   - Constants (``1'b0``/``1'b1``/``x``/``z``) map to the Yosys
     constant chars ``"0"`` / ``"1"`` / ``"x"`` / ``"z"``.
5. **Combinational primitive lowering**
   - Walk every continuous assign, every ``always_comb``, and every
     non-clocked expression evaluation reachable from a flop's
     ``D`` / ``ARST`` cone.
   - Lower expressions to Yosys-shaped primitives: ``BinaryOp::Add`` →
     ``$add``, ``BinaryOp::LogicalAnd`` → ``$logic_and``,
     ``ConditionalOp`` → ``$mux``, etc. Each lowered cell uses ``Y`` as
     its output and ``A``/``B``/``S`` as inputs to match the rule
     pack's expectations.
6. **Attribute propagation**
   - Forward SV attributes (``(* cdc_sync *)``, ``(* cdc_gray *)``,
     ``(* async_reg *)`` …) attached to wire/reg declarations onto the
     :class:`netlist.Netname` for the corresponding bits.
   - Forward source ranges into ``Cell.attributes["src"]`` so the
     reporter's source-location output still works.
7. **Cross-instance flattening**
   - The rule pack assumes a single flattened module. After collecting
     all flops/cells/nets in the hierarchical walk, emit them under a
     single :class:`Module` whose name is the top module.

Until that work lands, ``elaborate()`` raises ``NotImplementedError``
with an actionable message. ``--frontend slang`` on the CLI is gated
behind the same error.
"""

from __future__ import annotations

from pathlib import Path

from rtl_buddy_cdc.netlist import Module


_PYSLANG_INSTALL_HINT = (
    "pyslang is required for the slang frontend. Install via:\n"
    "    pip install 'rtl-buddy-cdc[slang]'\n"
    "or directly:\n"
    "    pip install pyslang"
)


class SlangFrontendUnavailable(RuntimeError):
    """pyslang import failed (extra not installed)."""


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
    """Elaborate ``sources`` via pyslang and produce a :class:`Module`.

    NOT YET IMPLEMENTED — see the roadmap in this module's docstring
    and issue #5. The pyslang import is exercised eagerly so callers
    get the install-hint error before hitting the not-implemented
    branch, which is the more common failure mode in practice.
    """
    _import_pyslang()
    raise NotImplementedError(
        "slang frontend is a work in progress (issue #5). "
        f"Cannot elaborate {len(sources)} source(s) with top={top!r} yet. "
        "Use --frontend yosys (the default) until the slang frontend lands."
    )
