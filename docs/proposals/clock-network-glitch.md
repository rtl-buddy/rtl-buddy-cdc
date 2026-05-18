# CDC-010 — Glitch on the clock network from a wrong-domain control signal

**Status:** proposal (issue #48). Implementation tracked by follow-up issues
listed at the end.

**Suggested rule ID:** `CDC-010`. The next free slot after the upcoming
`CDC-009` (pulse-width / fast-to-slow data loss, design in issue #47 /
`docs/proposals/cdc-009-pulse-width.md`).

**Severity:** `error`. A glitched clock edge propagates to every flop
downstream of the gate and is not recoverable by a synchronizer at the
sink — the damage is done at the source.

## 1. Failure mode

A clock mux's *select* — or a clock-gate's *enable* — is a control
signal: it must transition only when the cell's *clock* inputs are
quiescent (or guaranteed to produce no glitch on the output). When the
select / enable is driven by a flop in the **wrong** clock domain, a
plain combinational change can chop the output clock mid-cycle. The
canonical bad shape:

```systemverilog
// ck1_dom_sel is a flop clocked by ck1, so it transitions on a ck1
// edge — asynchronous to ck0. When it flips, the ck0 stream on the
// output (whichever input the mux is selecting at that nanosecond)
// can be truncated mid-high or mid-low, producing a sub-period
// runt pulse the downstream flops will sample as a real edge.

logic mux_sel_q;
always_ff @(posedge ck1) mux_sel_q <= mux_sel_d;

// "clock mux" — both inputs are legitimate clocks, but the select is
// from ck1's domain while the cell distributes ck0/ck0_div2 to the
// downstream flops.
wire ck_out = mux_sel_q ? ck0 : ck0_div2;

always_ff @(posedge ck_out) ...;  // every flop here is exposed
```

Even with `A === B` (selecting between two copies of the same clock),
an asynchronous select transition is unsafe — the analyzer is right to
fire structurally rather than try to prove input equivalence.

The same shape applies to a `$dffe`-style ICG: the enable must be
quiescent during the input clock's active edge. An enable driven by a
flop in a foreign domain violates that contract — the gate's output
clock acquires a sliver / runt pulse on every enable change.

## 2. Why the existing rules don't catch it

- **CDC-008 (clock used as data)** fires on the dual problem: a clock
  signal arriving at a non-CLK pin. It explicitly *exempts*
  cells in `_clock_network_cells` so legitimate ICG / mux / divider
  structures stay silent. CDC-010 is the complement — the cell is on
  the clock network, but its *non-clock* input is sourced from a flop
  in a foreign domain. The two rules partition the failure mode by
  which side of the gate is wrong:
  - CDC-008: clock on a data pin (wiring a clock somewhere it should
    not go).
  - CDC-010: data from the wrong domain on a clock-gate control pin
    (the gate is in the right place, the signal driving it is not).
- **CDC-007 (async reset crossing)** has the same *shape* — fanin
  walks to a foreign-domain flop and fires — but it's keyed on `ARST`
  pins, not clock-network gate controls. The detection helper is the
  same (`_backward_flop_fanin`), the cell-class is different.
- **CDC-001 / CDC-003 / CDC-004** all key off `find_crossings`, which
  enumerates **flop→flop D-pin** crossings. The select / enable of a
  clock mux or ICG is not a flop D pin (it's a control pin on a
  combinational or memory cell), so the crossing enumerator never
  produces a record for it — those rules cannot see it even in
  principle.

The overlap with CDC-008 is the closest neighbour and should be
called out in the rule's docstring. They are not redundant: it is
possible (and useful) for a single bad design to trigger both — a
clock being sampled as data on the data pin of one cell *and* the
same clock-gating cell having a foreign-domain enable.

## 3. Detection shape

Reusing existing helpers:

- `_clock_network_cells(module, drivers) -> set[str]` — already
  defined in `rules.py` (used by CDC-008 to exempt the clock
  distribution). Returns every cell whose output transitively reaches
  some flop's `CLK` pin.
- `_backward_flop_fanin(module, start_bits, drivers) -> set[str]` —
  already defined in `rules.py` (used by CDC-007 / CDC-004 gating
  detection). Returns the set of flop cell names reached by a
  backward BFS through combinational cells from `start_bits`.

Sketch of the check:

```python
def check_cdc_010(
    module: Module,
    crossings: list[Crossing],  # unused — flop-D-pin enumeration
    clock_spec: ClockSpec | None = None,
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    if ctx is None:
        ctx = _build_context(module, clock_spec)

    clock_net_cells = _clock_network_cells(module, ctx.bit_drivers)
    violations: list[Violation] = []
    seen: set[tuple[str, str]] = set()  # (cell_name, control_port)

    for cell_name in clock_net_cells:
        cell = module.cells[cell_name]
        for ctrl_port in _control_pins_for(cell.type):
            ctrl_bits = cell.connections.get(ctrl_port, ())
            if not ctrl_bits:
                continue
            ctrl_fanin = _backward_flop_fanin(
                module, ctrl_bits, ctx.bit_drivers
            )
            if not ctrl_fanin:
                continue  # control comes from a port / constant — fine

            # The cell's clock-input domains: every other input pin's
            # backward fanin, classified by the source flop's domain.
            clock_input_domains = _clock_input_domains_for(
                module, cell, ctx
            )
            for src_flop in ctrl_fanin:
                src_clk = ctx.domains.get(src_flop)
                if src_clk is None:
                    continue
                if src_clk in clock_input_domains:
                    continue  # same domain as one of the gated clocks
                if not _async(src_clk, clock_input_domains, clock_spec):
                    continue  # related by SDC clock-groups
                key = (cell_name, ctrl_port)
                if key in seen:
                    continue
                seen.add(key)
                violations.append(Violation(
                    rule_id="CDC-010",
                    severity="error",
                    message=(
                        f"clock-network cell {cell_name} "
                        f"({cell.type}) control pin {ctrl_port} is "
                        f"driven by flop {src_flop} in domain "
                        f"{src_clk}, async to the cell's clock "
                        f"input domains "
                        f"({sorted(clock_input_domains)})"
                    ),
                    cell_name=cell_name,
                ))
    return violations
```

The two new helpers are small and rule-local:

- `_control_pins_for(cell_type)` returns the control-pin names by
  cell type. For Yosys primitives that's `{"S"}` for `$mux`, `{"EN"}`
  for `$dffe` / `$dlatch`, and the empty set for `$buf` / `$not` /
  `$pos` (no control). For tech-mapped cell types we either widen
  the set conservatively (any non-clock input named like `EN`, `SE`,
  `CE`, `GATE`) or skip and rely on the structural pattern catching
  the Yosys-flatten shape — see *open question 1*.
- `_clock_input_domains_for(module, cell, ctx)` classifies the cell's
  *non-control* input pins by the domain of the flop that ultimately
  drives them. For an ICG it's `{ck0}` (the gated clock); for a clock
  mux it's `{ck0, ck1}` (both candidates). We don't need to prove the
  *output* clock's domain — we only need to know whether the control
  signal is in any of the source-clock domains.

## 4. Suppressions and false positives

- **Foreign-domain control with the same SDC clock group.** When
  `set_clock_groups` puts the control's domain in the *same group*
  as the cell's clock-input domains, the user is asserting the
  signals are related (e.g. one is a divided version of the other,
  or they're explicitly declared synchronous). Defer to the same
  `_async` helper CDC-001/007 already use — if the groups call them
  synchronous, no violation.
- **Constant or port-driven control.** A control pin driven directly
  by a top-level port or a constant has no flop fanin, so the
  `ctrl_fanin` set is empty and the rule short-circuits. This is
  correct: a top-level port is the user's responsibility to drive
  (same posture as CDC-007 for ARST sources from ports).
- **`(* cdc_sync *)` on the controlling flop.** If the foreign-domain
  control transitions through a synchronizer first, the *first stage*
  is in the destination clock-input domain, so the rule sees a
  same-domain flop and is silent. This composes naturally — we don't
  need a separate suppression path.

## 5. Paired fixture sketch

Two new fixtures under `tests/fixtures/`:

### 5.1 `bad_async_clock_mux/`

```systemverilog
// CDC-010 negative case — a clock mux whose select is driven by a
// flop in a foreign clock domain. Without synchronization the
// select's transitions chop the output clock into runt pulses on
// every downstream flop.
module bad_async_clock_mux (
    input  logic ck0, ck1,
    input  logic rst_n,
    input  logic sel_d,
    input  logic d_in,
    output logic q_out
);
    // Select is in ck1's domain.
    logic sel_q;
    always_ff @(posedge ck1 or negedge rst_n)
        if (!rst_n) sel_q <= 1'b0;
        else        sel_q <= sel_d;

    // "Clock mux" — both inputs are gated versions of ck0, but the
    // select is from ck1. The structural shape is what the rule
    // detects; the analyzer makes no claim about whether ck0_a and
    // ck0_b are equivalent at the AC level.
    wire ck0_a = ck0;
    wire ck0_b = ck0;
    wire ck_out;
    assign ck_out = sel_q ? ck0_a : ck0_b;

    // Downstream flop driven by the muxed clock.
    always_ff @(posedge ck_out or negedge rst_n)
        if (!rst_n) q_out <= 1'b0;
        else        q_out <= d_in;
endmodule
```

Companion `bad_async_clock_mux.sdc`:

```tcl
create_clock -name ck0 -period 10.0 [get_ports ck0]
create_clock -name ck1 -period 13.3 [get_ports ck1]
set_clock_groups -asynchronous -group {ck0} -group {ck1}
```

Acceptance: `test_bad_async_clock_mux.py` asserts CDC-010 fires
exactly once, naming the mux cell and `sel_q` as the foreign-domain
source. Verifies no other rule fires (CDC-001/004 don't see this —
the select is on a mux, not a flop D — and CDC-008 stays silent
because no clock signal is on a data pin here).

### 5.2 `good_sync_clock_mux/`

```systemverilog
// CDC-010 paired fix — synchronize the mux select into the
// destination-clock domain before it reaches the mux. (For a real
// clock mux you'd use a glitch-free clock-mux library cell; this
// fixture shows the structural fix the rule recognises.)
module good_sync_clock_mux (
    input  logic ck0, ck1,
    input  logic rst_n,
    input  logic sel_d,
    input  logic d_in,
    output logic q_out
);
    logic sel_q;
    always_ff @(posedge ck1 or negedge rst_n)
        if (!rst_n) sel_q <= 1'b0;
        else        sel_q <= sel_d;

    // 2FF synchronizer into ck0's domain.
    (* cdc_sync *) logic sel_meta;
    logic              sel_sync;
    always_ff @(posedge ck0 or negedge rst_n) begin
        if (!rst_n) begin sel_meta <= 1'b0; sel_sync <= 1'b0; end
        else        begin sel_meta <= sel_q; sel_sync <= sel_meta; end
    end

    wire ck0_a = ck0, ck0_b = ck0;
    wire ck_out = sel_sync ? ck0_a : ck0_b;  // select now ck0-domain

    always_ff @(posedge ck_out or negedge rst_n)
        if (!rst_n) q_out <= 1'b0;
        else        q_out <= d_in;
endmodule
```

Companion SDC mirrors the bad fixture. Acceptance: appended to
`GOOD_FIXTURES` in `tests/test_good_fixtures.py` with expected
violation count `0`.

## 6. Open questions

1. **Tech-mapped vs Yosys-primitive cell types.** The
   `_control_pins_for` helper is straightforward on Yosys primitives
   (`$mux.S`, `$dffe.EN`, `$dlatch.EN`). On a flattened netlist that
   has been tech-mapped through a real library, the control pin name
   is library-specific (`E`, `CE`, `GATE`, `EN`, `SE`, …). The first
   implementation pass should target Yosys primitives only — the
   pre-built JSON fixtures and the slang frontend both stay in
   primitive form — and document the gap. A second pass can extend
   with a configurable cell-type → control-pin map.
2. **`$mux` with both inputs literally tied to the same net.** If
   structural equivalence (`A === B`, same wire bit) is detectable,
   should the rule downgrade to a warning? Probably not — the
   downstream flop is still exposed to whatever metastability the
   library cell has on `S`. Keep `error` severity; rely on the
   waiver file for the rare hand-vetted case.
3. **Interaction with `set_case_analysis`.** A user who sets a
   constant case analysis on the select effectively pins one input
   — the mux degenerates to a buffer in that case. We don't parse
   `set_case_analysis` today (out of SDC subset); not in scope for
   the first implementation.

## 7. Acceptance for this proposal

- This document exists at `docs/proposals/clock-network-glitch.md`
  and is reviewable.
- Follow-up issues are spawned for: (a) rule implementation +
  helpers, (b) paired fixtures (`bad_async_clock_mux/`,
  `good_sync_clock_mux/`), (c) `wiki/raw/articles/rtl-buddy-cdc-architecture.md`
  + `README.md` updates (rule table row, roadmap line, severity entry).

## 8. Out of scope

- Implementation.
- Modelling the gate's enable timing — STA-level distinction between
  DC and AC paths is not attempted; structural detection is enough.
- Glitch-free clock-mux library cell recognition. A specific library
  cell type that's been validated as glitch-free can be exempted via
  a waiver, or as a second-phase enhancement, via an attribute on the
  module or cell. Out of scope for the first cut.
