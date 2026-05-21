# Signoff CDC Coverage Gaps

This note tracks CDC/RDC feature classes that are common in signoff
CDC methodologies but are not yet fully modeled by rtl-buddy-cdc.
The fixtures added with this proposal are regression probes for those
classes; most are expected-failing tests until the corresponding rules
are implemented.

## Functional Data-Enable Stability

Current CDC-004 handling accepts some gated multi-bit data crossings
structurally: a destination-domain synchronized load or enable controls
when the destination bus samples source-domain data.

That structural shape is necessary but not sufficient. A full
signoff-quality check also has to prove that the source bus remains
stable for the window in which the synchronized enable can sample it.
If the payload can change independently while the enable is in flight,
the destination can capture an incoherent or stale value even though
the enable path itself uses a 2FF synchronizer.

Proposed coverage:

- `bad_functional_datahold_enable`: data changes independently of the
  source load request and can be sampled by a delayed destination
  enable.
- Future good counterpart: payload held until a returned acknowledgement
  proves the destination has sampled the value.

Candidate rule family: `CDC-012` or a sub-rule under `CDC-004`.

## Fast-To-Slow Control Loss

CDC-009 catches narrow source-domain pulses that can be missed by a
slower destination clock. A related signoff class is control-event loss
where a synchronized level/toggle can change more than once between
destination samples. The synchronizer is structurally safe for
metastability, but it does not guarantee event accounting.

Proposed coverage:

- `bad_fast_to_slow_control_loss`: a fast-domain toggle can change twice
  before the slow domain observes it, losing an event.
- Future good counterpart: a handshake or event counter with backpressure
  prevents a second event until the first is acknowledged.

Candidate rule family: `CDC-013` or an extension to `CDC-009`.

## Unsynchronized Derived Async Resets

The current RDC-001 implementation catches async reset pins driven by a
flop in a foreign clock domain. It intentionally accepts top-level reset
ports as user-owned inputs and RDC-005 suppresses explicit reset-source
muxes to avoid flagging intentional reset selection.

There is still a gap: a selected, muxed, or combinationally derived
reset can feed an asynchronous clear pin directly without a local
reset synchronizer. The mux may make source selection intentional, but
it does not make deassertion synchronized to the consumer clock.

Proposed coverage:

- `bad_derived_async_reset_unsync`: mux-selected reset source feeds a
  flop async clear directly.
- `good_derived_async_reset_synced`: selected reset is first synchronized
  in the consumer clock domain, then used by downstream flops.

Candidate rule family: `RDC-006`, or a scoped extension of `RDC-001`.

## Fixture Matrix Gaps

The current fixture suite is strong for structural CDC:

- scalar unsynchronized crossings
- insufficient synchronizer depth
- combinational logic before synchronizers
- multi-bit structural bus crossings
- reconvergence/coherency
- glitchy source paths
- clock-as-data and async clock mux cases
- reset-domain structural checks through RDC-001 through RDC-005

The weaker areas are functional CDC signoff classes:

- data-enable sequencing and stability
- event-loss checks beyond one-cycle pulse width
- protocol-level request/ack validation
- derived reset synchronization after muxing or combinational selection
- explicit good/bad counterparts for each functional rule

Filling those gaps should happen in two steps:

1. Add focused fixtures that isolate each missing feature without
   relying on broad hand-written IP examples.
2. Implement analyzer rules incrementally, promoting each expected
   failure to a normal passing assertion once behavior is supported.
