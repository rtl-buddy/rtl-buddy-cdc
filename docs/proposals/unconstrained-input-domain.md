# CDC-011: unconstrained primary input captured by clocked logic

**Tracking issue:** rtl-buddy-cdc#97
**Status:** implemented (this proposal landed alongside the rule).
**Severity:** `warning` (single destination domain) /
`error` (multi-domain capture).

## 1. Failure mode

A top-level input port has no `set_input_delay -clock <name>` typing
in the SDC, but physically reaches a flop's `D` pin somewhere in the
design. Two distinct shapes share the same root cause:

```systemverilog
// SDC: create_clock clk_a/clk_b/async-group; NO set_input_delay on `in`
module top (input clk_a, clk_b, in, output q_a, q_b);
    always_ff @(posedge clk_a) q_a <= in;   // captures in clk_a
    always_ff @(posedge clk_b) q_b <= in;   // captures in clk_b
endmodule
```

Pre-implementation the rule pack was silent on every port that
`ClockSpec.port_clock` didn't carry — the destination flops looked
"unsourced" to `find_crossings` and never produced port-sourced
crossings, so CDC-001 / CDC-004 / CDC-006 saw nothing to compare.

The same gap covers `set_input_delay` invocations that name ports but
omit `-clock` (the constraint has no STA semantics; real timers reject
it). The parser previously dropped the constraint silently; CDC-011's
sentinel synthesis sweeps these ports up the same way as truly-
missing constraints, and `_handle_set_delay` now surfaces a
`partial_warnings` entry naming the affected ports.

## 2. Why not extend CDC-001?

`find_crossings` already walks input ports forward and emits port-
sourced `Crossing` records (`domain.py:413-481`). The rules already
discriminate on `src_port` vs `src_flop`; the reporter already
serialises `src_port` to JSON (`reporter.py:431`). Letting the
sentinel synthesis feed those existing paths means CDC-001 / CDC-004
would fire on every untyped input port that lands on any flop. Two
problems with that:

1. **Wrong primary fix advice.** CDC-001's remediation is "add a 2FF
   synchronizer" — but for an unconstrained port the fix is almost
   always "add `set_input_delay -clock <name>` to the SDC". A
   user-facing message that pushes the wrong fix is worse than
   silence.
2. **Coupled severity / opt-out.** Tying the new shape to CDC-001
   (`error`) means every project lacking full SDC discipline
   immediately gets a flood of `error`-level findings on the same
   rule that catches genuine missing synchronizers. Disabling
   CDC-001 to silence the noise also disables the real-bug coverage.

A dedicated rule with its own severity and message gives the user a
clean opt-out and lets us escalate severity by shape.

## 3. Implementation

Five seams, all small:

1. **`sdc.UNCONSTRAINED_SENTINEL = "<unconstrained>"`.** Angle
   brackets are illegal in Verilog/SDC names, so the sentinel never
   collides with a real clock identifier.
2. **`sdc.synthesize_unconstrained_inputs(spec, module)`.** Iterates
   every input port; assigns the sentinel to ``spec.port_clock``
   whenever `clock_for_port` returns `None`. Returns the list of
   ports it touched (verbose-mode reporting hook). Called from the
   CLI right after `parse_file`.
3. **`ClockSpec.are_async` sentinel short-circuit.** The sentinel is
   treated as async to every real clock — we don't know what domain
   the port lives in, so any flop capture is a potential cross.
   Keeps sentinel-sourced crossings in the post-filter list so
   CDC-011 sees them.
4. **`check_cdc_011` with grouping post-pass.** Buckets crossings by
   `src_port` (only sentinel-sourced ones). For each port:
   - `len({c.dst_clock}) >= 2` → one `error` listing every
     destination clock.
   - Otherwise → one `warning` naming the single destination clock
     and pointing the user at `set_input_delay`.
5. **Sentinel guards in CDC-001 / CDC-002 / CDC-006.** Each rule
   gains `if c.src_clock == UNCONSTRAINED_SENTINEL: continue` (or,
   for CDC-006's port-fanin loop, a per-port skip). Prevents
   double-fire with wrong fix advice.

No changes to `find_crossings`, the reporter contract, or the
existing `Crossing` schema — the sentinel rides through the existing
`src_clock` field. JSON consumers that key on `src_port` keep
working.

## 4. Severity escalation

The port-walk emits one `Crossing` per (port, destination flop), so a
port landing on flops in two clock domains produces two crossings
sharing `src_port` with different `dst_clock`s. Without consolidation
the rule would fire N independent warnings and lose the stronger
signal that the multi-domain case is intrinsically broken.

Single-port grouping recovers it:

| Shape | Severity | Reasoning |
|---|---|---|
| port → flop(s) in 1 dst domain | `warning` | "you forgot SDC typing" — methodology gap, easy fix at the constraint level |
| port → flops in ≥ 2 dst domains | `error` | a single port cannot be synchronous to two clocks; no amount of SDC typing alone fixes this, the RTL also needs a synchronizer somewhere |

## 5. Fixtures

Four paired (`bad_` + `good_`):

| Fixture | Destination clock | Severity |
|---|---|---|
| `unconstrained_input_two_domains` | `clk_a` + `clk_b` (two ff banks) | `error` |
| `unconstrained_input_derived_clock` | `clk_a & clk_b & clk_c` (AND tree) | `warning` |
| `unconstrained_input_bus_two_domains` | `clk_a` + `clk_b`, bus width 8 | `error` |
| `unconstrained_input_muxed_clock` | `tm ? tclk : sclk` | `warning` |

Each `good_*_typed` counterpart adds the appropriate `set_input_delay
-clock <name>` line (and a 2FF synchronizer on the foreign-domain
capture in the two-domains shapes) and is registered in
`tests/test_good_fixtures.py::GOOD_FIXTURES`.

The first fixture (`bad_unconstrained_input_two_domains`) was
originally landed in PR #98 as a regression baseline pinning the
*pre*-CDC-011 behaviour. Its test was flipped here to assert the new
positive shape — the SV / SDC / JSON files were unchanged so the same
disk-level artifact exercises both eras of behaviour.

## 6. Existing-fixture audit

Ten `bad_*` fixtures had untyped data inputs whose tests passed
because the relevant crossing was internal-flop → internal-flop, not
port → flop. With sentinel synthesis live, every one of them would
now also fire CDC-011 on its data input and pollute tests scoped to a
single rule. Each fixture's SDC gained a `set_input_delay -clock
src_clk [get_ports <data_input>]` line so it fires only the rule the
fixture was authored to test:

- `bad_bus_crossing`, `bad_comb_before_sync`,
  `bad_comb_before_sync_with_if`, `bad_comb_case_before_sync`,
  `bad_comb_source` (virtual `vclk_ext` rather than `src_clk` — the
  fixture has no source-clock flop, so a virtual clock + async group
  is the right idiom),
- `bad_reconvergent_sync`, `bad_reconvergent_with_recombine`,
  `bad_reset_crossing` (`d_in` typed to `dst_clk` because it's
  sampled by a dst-clock flop directly; `kill_req` typed to
  `src_clk`),
- `bad_reset_tree`, `bad_single_ff_sync`.

Two additional fixtures got the same treatment: `good_2ff_sync`
(`d_in` typed `src_clk`) and `ip_cdc_handshake` (`src_valid`,
`src_data` typed `src_clk`).

`good_input_delay_domain`, `good_port_typed_sync`,
`bad_port_no_sync`, and `bad_input_delay_cross_domain` already
exercised the SDC-discipline shape and didn't need changes.

## 7. Out of scope

- **Inferring source domain from heuristics** (port-name patterns,
  toplevel-clock-port co-occurrence). The whole point of the
  warning is that the analyzer can't guess — the SDC author has to
  assert.
- **`set_input_delay` without `-clock` and without ports** (delay-
  only constraint files). The parser stays silent on this shape;
  the warning is reserved for the more clearly-erroneous
  ports-named-but-no-clock case.
- **Custom sentinel name via CLI flag.** The sentinel is an internal
  identifier; users only see the rendered violation message, which
  doesn't reference it.

## 8. Open questions

- Should there be a way to *silence* CDC-011 on a per-port basis
  short of adding `set_input_delay`? E.g. an SV attribute
  `(* cdc_async_input *)` on the port declaration to mean "yes I
  know, this is genuinely an async logic-level signal and I've
  documented it elsewhere." Right now the only escape is the waiver
  file. Deferred until a real user asks.
- Whether `set_input_delay -clock <virtual_clock>` is the most
  ergonomic answer to the warning, or whether
  `set_false_path -from [get_ports <p>]` should also be accepted.
  Currently only the former silences CDC-011; the latter is
  STA-correct but loses the async-domain information the analyzer
  needs.
