---
source_url: docs/proposals/hierarchical-reporting.md
ingested: 2026-05-15
status: proposal
---

# Hierarchical reporting (design proposal)

> **Status.** Proposal only. No code changes ship with this document.
> The follow-up issues listed in [§7](#7-follow-up-issues) are the
> implementation contract.

## 1. Problem

Every `Violation` today reports a `cell_name` that is a flat, post-
flatten string (`u_block_a.u_sync_inst.dst_q`). On an SoC with 1000+
instances the rendered report becomes a flat list of dotted names with
no grouping — unreadable in text, awkward to triage in JSON, no
hierarchy-aware navigation in SARIF (which has a dedicated
`logicalLocation` shape for exactly this).

The pipeline assumption that the netlist is fully flattened (arch
spec §1, §11) is **not** changed by this proposal. The change is
purely how findings are *presented*: same analysis, richer rendering.

## 2. Goals and non-goals

**Goals.**

- Per-instance violation grouping in all three output formats (text,
  JSON, SARIF).
- A deterministic mapping `cell_name → instance_path: tuple[str, ...]`
  that works for both the Yosys frontend (post-`flatten`) and the
  slang frontend (in-process hierarchical naming) without each
  reporter having to know which frontend produced the `Module`.
- Additive JSON / SARIF changes only: no existing key is renamed,
  retyped, or removed; the `JSON_CONTRACT` constants (arch spec
  §10, AGENTS.md § Cross-repo coupling) stay green.
- Sensible behavior for cells that genuinely have no instance path —
  Yosys auto-named cells at the top scope (`$procdff$42`,
  `$logic_and$<file>:<line>$N`) report as living in the top instance,
  not as malformed input.

**Non-goals.**

- **Hierarchical analysis** — re-elaborating per module, sharing
  results across instances, etc. That is the much larger
  architectural change called out in arch spec §11 ("not trying to
  be extensible: Hierarchy") and is explicitly out of scope.
- **Instance-scoped waivers** (`waive CDC-001 inst:u_block_a/...`).
  Naturally falls out of having `instance_path` on the violation,
  but is queued as a separate follow-up so this proposal stays a
  rendering change.
- **Hierarchical clock-domain assignment.** Domain identity remains
  a function of the flop's CLK net traced to a top-level port
  (arch spec §5); instance prefixes do not influence
  `trace_clock_root`.
- **Backward-compatible report parsing** for downstream consumers
  beyond the three pinned JSON keys. We do not pretend the text
  layout is API.

## 3. Status quo

The relevant pieces of the existing design (arch spec §3, §7, §10):

- `Violation` is a frozen dataclass with `rule_id`, `severity`,
  `message`, `crossing`, `cell_name`. The reporter resolves
  `cell_name → cell → attributes["src"]` to get a file/line/column.
- `reporter.render_text` groups by rule, then renders each
  violation as a single line with severity + source location +
  wrapped message body.
- `reporter.render_json` emits a top-level `violations` list of
  `_violation_to_dict` entries. The contract pinned in
  `JSON_CONTRACT` is `summary.violations`, `summary.suppressed`,
  `summary.crossings`. Other keys evolve freely.
- `reporter.render_sarif` emits SARIF 2.1.0 with
  `physicalLocation.region` parsed from `attributes["src"]`. SARIF
  `logicalLocation` (the natural home for hierarchical names) is
  **not** populated today.
- `waivers.py` matches a violation's `cell_name` (plus crossing
  text and message) against a regex — the full dotted name is one
  of the three match surfaces. This proposal does not change that.

## 4. Cell-name shapes in the wild

This is the part of the design that most easily goes wrong. The
flat `cell_name` already in `Violation` is **not** a uniform "dotted
hierarchical path". The two shipping frontends produce three
distinct shapes, sometimes in the same `Module`:

| Source | Example | Notes |
|---|---|---|
| Yosys `flatten`, user-named child instance | `$flatten\u_sync_ack.$procdff$84` | `$flatten\` prefix + child name + `.` separator + leaf cell name. The leaf may itself be a `$<kind>$N` auto-name. |
| Yosys, top-level proc cell | `$procdff$42` | No instance prefix at all — lives at top scope. |
| Yosys, top-level expression cell | `$logic_and$tests/fixtures/.../foo.sv:55$13` | No instance prefix; the file path is part of the cell name, dots and colons embedded. |
| slang frontend, child instance | `u_top.u_b0.q` | Plain dotted path, no `$flatten\` prefix, no leading separator. Top instance is unnamed (`""`). |
| slang frontend, top-level cell | `$mux$23` or `$add$45` | Yosys-style auto-name with no prefix, identical to the Yosys top-level shape. |

A naïve `cell_name.split('.')` is wrong: it fragments the file path
inside `$logic_and$...:55$13` and the `$flatten\u_sync_ack` token,
and it produces a one-element `("u_top",)` from the slang
`u_top.u_b0.q` even though `u_top` is the top instance and the real
hierarchy is `("u_b0",)`.

## 5. Proposed data model

### 5.1 `Violation.instance_path`

Add one frozen field to `Violation`:

```python
@dataclass(frozen=True)
class Violation:
    rule_id: str
    severity: str
    message: str
    crossing: Crossing | None = None
    cell_name: str | None = None
    instance_path: tuple[str, ...] = ()      # NEW
```

Semantics:

- `()` means "top instance" — the default and the answer for every
  Yosys-flatten cell with no `$flatten\` prefix and every slang
  cell without a dotted prefix.
- `("u_block_a", "u_sync")` means the cell lives inside
  `u_block_a/u_sync` in the original hierarchy.
- Empty-string components are dropped (top-level names emit `()`
  rather than `("",)`).
- Default `()` preserves source compatibility for any test
  constructing a `Violation` literal (the rule pack will pass it
  explicitly via the resolver below).

### 5.2 Resolver

A single helper, placed in `reporter.py` next to `_source_location`:

```python
def _instance_path(module: Module, cell_name: str | None) -> tuple[str, ...]:
    """Map a cell name to its hierarchical instance path."""
```

Resolution rules, in order:

1. `cell_name is None` → `()`.
2. The name begins with `$flatten\` (Yosys post-flatten user
   instance) — strip the prefix, split on the **first** `.`
   (everything after the first dot is the leaf cell name, not
   further hierarchy under flatten); the part before is one
   instance component. Repeat as long as the leaf still starts
   with `$flatten\`.
3. The name contains no `$flatten\` and no `$` before the first
   `.` — this is the slang shape. Split on `.`, return all
   components *except the last* (the last is the leaf symbol
   name).
4. Otherwise (top-level Yosys auto-name, `$logic_and$.../...:55$13`,
   `$procdff$42`, slang `$mux$23` at top) → `()`.

The "no `$` before the first `.`" guard in rule 3 is what protects
the `$logic_and$<file>:<line>$13` shape from being mistakenly split
on the `.` inside the source path.

Rule 2 covers nested-flatten cases (a child of a child) by
iterating. In practice Yosys emits a single `$flatten\` regardless
of nesting depth, but the loop is cheap and the explicit handling
matches what `flatten -separator .` actually produces under unusual
options.

### 5.3 Where the resolver runs

The resolver is called once per violation, at the boundary between
the rule pack and the reporter — specifically in
`cli._analyze_and_report` immediately before constructing the
`AnalysisResult` (or, equivalently, in a small post-pass over
`result.violations` before the formatter dispatch). The rules
themselves stay unchanged: they emit `Violation(..., cell_name=...)`
exactly as today, and the boundary code fills in `instance_path`.

This keeps the resolver out of the rule pack (no `reporter` import
in `rules.py`) and keeps `rules.py` frontend-agnostic.

### 5.4 Frontend normalization (the load-bearing decision)

Yosys `flatten` and the slang frontend disagree on what
`cell_name` looks like (§4). The resolver normalises both into the
same `tuple[str, ...]` shape so every downstream consumer sees one
view:

- Yosys `$flatten\u_sync_ack.$procdff$84` → `("u_sync_ack",)`.
- slang `u_top.u_sync_ack.q` (where `u_top` is the top instance) →
  `("u_sync_ack",)`.

Both collapse to the same instance path. The top instance is *not*
a component of the path under either frontend — the path is the
hierarchy *below* the analyzed top.

For the slang frontend specifically: today `_ModuleBuilder` walks
the top with `hier_prefix=""` and `_emit_child_instance` prefixes
children with `f"{parent_prefix}{child.name}."`.
That means the top's name does **not** appear in `cell_name` — a
child instance under top reads `u_b0.q`, not `u_top.u_b0.q`. The
resolver's rule 3 ("return all components *except the last*")
therefore yields `("u_b0",)` directly, matching the Yosys-flatten
output above.

If a future frontend chooses to include the top in the prefix, it
must strip the top before yielding cell names to `Module`, or the
resolver gains a frontend-aware branch — preference is the former
(keep `Module` shape uniform; arch spec §3.1 frontend contract).

### 5.5 Edge cases the resolver must handle (test surface)

| Input | `instance_path` |
|---|---|
| `cell_name=None` | `()` |
| `"$procdff$42"` | `()` |
| `"$logic_and$tests/foo.sv:55$13"` | `()` |
| `"$flatten\u_sync_ack.$procdff$84"` | `("u_sync_ack",)` |
| `"$flatten\u_a.$flatten\u_b.$dff$1"` (nested) | `("u_a", "u_b")` |
| `"u_b0.q"` (slang) | `("u_b0",)` |
| `"u_top.u_b0.q"` (hypothetical frontend that includes top) | `("u_top", "u_b0")` — accepted; documenting top-stripping is the frontend's responsibility |
| `"u_b0.cell_name_with.dots_in_it"` (pathological) | `("u_b0", "cell_name_with")` — accepted as a known limitation; cell names with dots are not produced by either shipping frontend |

These are the cases that go into `tests/test_reporter.py` for the
helper directly.

## 6. Rendering changes

### 6.1 Text reporter

Inside each rule group (which is preserved as the outer grouping —
the user still scans rules first), violations are bucketed by
`instance_path` and printed in that order. Top-instance findings
(`instance_path=()`) come first, then each child instance in
sorted order, then the violations within that instance keep their
existing layout (`severity` + source location + wrapped message).

A short header per instance keeps the grouping visible without
adding noise to small designs:

```
Violations  (3 errors)

  CDC-001 — Unsynchronized control crossing (no second-stage flop)
    [top]
      error  alu_accel.sv:42
        flop dst_q crosses src_clk → dst_clk with no second stage
    u_block_a / u_sync
      error  block_a/sync.sv:18
        flop u_sync.dst_q crosses src_clk → dst_clk with no second stage
```

When every violation in a rule group lives at the top (the common
case on IP-block fixtures), the `[top]` header is omitted to keep
the small-design output identical to today.

### 6.2 JSON reporter

Two additive changes:

1. Every entry in `violations`, `suppressed`, and
   `baseline_carryover` gains an `instance_path` field of type
   `list[str]` (JSON has no tuples; the resolver's tuple becomes a
   list at serialise time). The field is always present and is
   `[]` for top-level findings — never `null`, never missing — so
   downstream schema is unconditional.

2. A new top-level `by_instance` summary:

   ```json
   "by_instance": [
     {
       "instance_path": [],
       "violations": 1,
       "rules": {"CDC-001": 1}
     },
     {
       "instance_path": ["u_block_a", "u_sync"],
       "violations": 2,
       "rules": {"CDC-001": 1, "CDC-002": 1}
     }
   ]
   ```

   Entries are sorted by `instance_path` (empty path first, then
   lexicographic). `suppressed` and `baseline_carryover` findings
   are **not** counted in `by_instance` (consistent with how
   `summary.violations` excludes them).

JSON contract check: `JSON_CONTRACT` keys (`summary.violations`,
`summary.suppressed`, `summary.crossings`) are untouched. `cell_name`
on each violation is untouched. `--baseline` match key
(`rule_id`, `cell_name`, `message`) is untouched — `instance_path`
is **not** part of the match key, so adding it does not retroactively
re-flag baseline carryover entries. `tests/test_reporter.py
::test_json_contract_keys_are_stable` continues to pass unchanged.

### 6.3 SARIF reporter

Each result gains a `logicalLocations` field alongside the
existing `physicalLocation`:

```json
"locations": [
  {
    "physicalLocation": {
      "artifactLocation": {"uri": "block_a/sync.sv"},
      "region": {"startLine": 18, ...}
    },
    "logicalLocations": [
      {
        "name": "u_sync",
        "fullyQualifiedName": "u_block_a.u_sync",
        "kind": "module"
      }
    ]
  }
]
```

`physicalLocation` is **kept** — it points at the source file/line,
which is independent of the instance path and remains the right
target for editor-jump UX. `logicalLocations` is what GitHub Code
Scanning and the SARIF viewer use to group results by component;
populating it is what makes the SoC use case tractable in those
UIs.

`logicalLocations` is omitted when `instance_path == ()` (rather
than emitted as an empty list) — SARIF treats absent and empty
identically, and omitting it keeps the output diff minimal on
small designs.

## 7. Follow-up issues

These are the concrete spawn-points the issue-46 acceptance
criterion asks for. Each is sized to fit a single ~1hr session.

1. **Data model + resolver.** Add `instance_path: tuple[str, ...]`
   to `Violation`. Add `_instance_path(module, cell_name)` in
   `reporter.py` and wire it in at the
   `cli._analyze_and_report` boundary. Cover every row of the
   §5.5 table in `tests/test_reporter.py`. **No reporter output
   changes in this phase** — the field is computed and stored, but
   not yet rendered. (Ships green: full suite stays green; JSON
   schema gains the field as documented.)

2. **JSON `by_instance` summary + per-violation field.** Emit
   `instance_path` on every violation dict (already-present field
   from phase 1) into the JSON payload, and add the top-level
   `by_instance` aggregation. Cover with a fixture that has at
   least one nested-instance violation (the
   `ip_cdc_handshake` fixture's `u_sync_ack` / `u_sync_req`
   instances are an existing candidate — check what their
   `cell_name`s actually resolve to before promising). Pin the new
   JSON shape in `test_reporter.py`. Confirm the
   `JSON_CONTRACT` test still passes.

3. **SARIF `logicalLocations` emission.** Emit `logicalLocations`
   on each result whose `instance_path` is non-empty, with
   `fullyQualifiedName` derived from the path. Update the SARIF
   contract test to assert the field is present where expected
   and absent where expected.

4. **Text reporter grouping.** Implement the §6.1 layout: bucket
   per `instance_path` inside each rule group, with the `[top]`
   header omitted when every violation in the group is top-level.
   Cover with a snapshot test on the
   `ip_cdc_handshake` fixture output (or a synthetic fixture with
   multiple instances).

5. **(stretch) Instance-scoped waivers.** Allow
   `waive CDC-001 inst:u_block_a/.*` as a fourth match surface
   (`waivers.py` currently tries `cell_name`, crossing text,
   message). The instance-path string for matching is
   `"/".join(instance_path)` — `/` rather than `.` so the regex
   doesn't have to escape `\.` everywhere. This is the explicit
   non-goal from §2, listed here as the natural next step once
   the data is on the violation.

Phases 1–4 are independent of each other once phase 1 lands;
phases 2–4 can be parallelised. Phase 5 is optional.

## 8. Out of scope (recap)

- Hierarchical analysis (per-module re-elaboration). Different
  problem; different proposal.
- Changing how `cell_name` is generated by either frontend. The
  resolver consumes whatever the frontend produces today.
- Reformatting the existing text layout beyond the per-instance
  bucketing. No new colors, no new severity icons, no per-rule
  description rewrites.
- Promoting `instance_path` into the `--baseline` match key. The
  match key change would re-flag historical baseline entries the
  first time a project bumps the analyzer — out of scope here.

## 9. Acceptance criteria for this proposal

(Mirrors the issue body for clarity.)

- This document exists at
  `wiki/raw/articles/hierarchical-reporting.md` and is reviewable
  in <30 minutes.
- §7 names at least three concrete follow-up issues, each sized
  for a single ~1hr session.
- The data model, JSON additivity, and frontend normalization are
  resolved in enough detail that the phase-1 implementer does not
  need to redesign during the session.
