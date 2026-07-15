# AGENTS.md — rtl-buddy-cdc

## Role

This repo is the source-of-truth implementation of the `rtl-buddy-cdc`
analyzer — a Python-based CDC (clock-domain-crossing) linter that
consumes a flattened Yosys netlist plus an SDC and produces text /
JSON / SARIF reports.

It is consumed by `rtl_buddy` (sibling repo) as a subprocess via
`rb cdc` / `rb cdc-regression`. Anything that breaks the JSON output
schema or the CLI surface is a downstream-breaking change — see
[§ Cross-repo coupling](#cross-repo-coupling).

## Read first

- [`wiki/raw/articles/rtl-buddy-cdc-architecture.md`](wiki/raw/articles/rtl-buddy-cdc-architecture.md) — full reference for
  the data model, pipeline, rule helpers, extension points. Read
  before changing anything in `domain.py`, `rules.py`, `sdc.py`, or
  the reporter contract.
- [`README.md`](README.md) — user-facing intro, CLI flags, supported
  SDC subset, current roadmap.

If your task touches behavior that lands in the JSON / SARIF schema
or the violation severity model, the architecture spec is the
authority — update it in the same PR if you change the contract.

## Key files

```text
src/rtl_buddy_cdc/
├── __init__.py        # exposes main()
├── cli.py             # Typer entry points (analyze / lint / version) + orchestration
├── frontend.py        # Frontend enum + elaborate() factory (dispatches by name)
├── frontends/
│   ├── yosys.py       # Yosys frontend: shell out + netlist.load
│   └── slang.py       # slang frontend (Yosys-parity in 0.2.0) — lazy pyslang import
├── netlist.py         # Yosys write_json loader (Module / Cell / Port / Netname)
├── flops.py           # FF cell zoo (FF_CELL_TYPES) + Flop dataclass
├── domain.py          # trace_clock_root + find_crossings (BFS) + Crossing
├── sdc.py             # SDC parser: Tcl tokenizer + per-command arg-spec table
├── rules.py           # CDC-001..-008 + RULES registry + run_all + helpers
├── waivers.py         # .swl-style waiver parser + apply()
└── reporter.py        # AnalysisResult + render_text / render_json / render_sarif
tests/
├── fixtures/
│   ├── bad_<rule>/    # negative case for each rule (must fire)
│   ├── good_<rule>/   # paired textbook fix (must NOT fire)
│   ├── ip_cdc_handshake/, clock_gating/, marked_user_sync/  # auxiliary
│   └── …
└── test_*.py          # one per fixture + cross-cutting (waivers, reporter, sdc)
.github/workflows/
├── lint.yml           # ruff check + ruff format --check
└── test.yml           # pytest
```

## Development rules

- Keep changes targeted. The repo is small; resist sprawling
  refactors unless the task requires them.
- Treat the JSON output schema and the CLI surface as **public**.
  Downstream `rtl_buddy` parses the JSON for violation counts and
  forwards CLI flags; breaking either ripples into the integration.
- `__init__.py` stays minimal. Public modules are imported directly
  (`from rtl_buddy_cdc import netlist, rules, ...`); don't re-export
  symbols at the top level.
- Frozen dataclasses by default. Mutability is for parser-built
  collections only (`ClockSpec`).
- The analyzer is a chain of **pure functions**. Side effects belong
  in `cli.py` (file I/O) and the reporter (writing to a file-like).
  Don't sneak I/O into `domain.py` / `rules.py` / `sdc.py`.
- The `lint` wrapper drives a frontend (Yosys subprocess or pyslang)
  to elaborate sources; the primary `analyze` path must never invoke
  a frontend — it consumes a pre-elaborated Yosys JSON via
  `netlist.load`. This split is deliberate — the rule pack has no
  toolchain runtime dependency.
- No new top-level dependencies without strong reason. The package
  ships with **only `typer`** as a runtime dep; the slang frontend
  is gated behind the `[slang]` optional extra so default installs
  stay typer-only. Lint/test groups are the place for tooling.

## Validation commands

```bash
# from repo root
uv sync                        # set up env (Python 3.13; see .python-version)
uv run ruff check              # lint (must pass)
uv run ruff format --check     # format check (CI enforces this)
uv run mypy                    # type check (must pass; src/ scope only)
uv run pytest -q               # full unit suite
uv run pytest tests/test_<x>.py -q   # single file

# end-to-end smoke
uv run rtl-buddy-cdc analyze \
    --netlist tests/fixtures/ip_cdc_handshake/ip_cdc_handshake.json \
    --sdc tests/fixtures/ip_cdc_handshake/ip_cdc_handshake.sdc

uv run rtl-buddy-cdc lint --top my_top --sdc design.sdc rtl/*.sv
```

CI runs ruff + mypy (`lint.yml`, two jobs) and pytest (`test.yml`,
matrix) on every PR. Run them locally before pushing — see issue #28
for the mypy job's lax baseline (it's a drift sentinel for the
typed surface, not a forcing function for full annotations).

## Adding a CDC rule

1. Write the check function in `rules.py`:
   ```python
   def check_cdc_NNN(
       module: Module,
       crossings: list[Crossing],
       clock_spec: ClockSpec | None = None,
   ) -> list[Violation]:
       ...
   ```
2. Reuse the existing helpers (`_sync_chain_depth`,
   `_is_multibit_sync_first_stage`, `_is_gray_encoded_source`,
   `_is_gated_bus_crossing`, `_clock_network_cells`,
   `user_sync_flop_names`, `user_gray_flop_names`) where they apply —
   don't duplicate structural detection.
3. Register: add `"CDC-NNN": check_cdc_NNN` to `RULES`. If the rule
   takes a configuration parameter, special-case it in `run_all`
   (CDC-002 / `required_depth` is the reference).
4. Pick severity per [`wiki/raw/articles/rtl-buddy-cdc-architecture.md`](wiki/raw/articles/rtl-buddy-cdc-architecture.md)
   §8.2: `error` for unambiguous bugs, `warning` for
   review-or-waive patterns.
5. Add a paired fixture (see next section).
6. Update the rule table in `README.md`.

## Adding a fixture

Every rule has at least one **bad** (must fire) and one **good**
(textbook fix; must not fire) fixture under `tests/fixtures/`. The
pairing is the regression net for false positives.

1. Create `tests/fixtures/<bad|good>_<name>/` containing:
   - `<name>.sv` — the design. Lead the file with a comment
     explaining what it tests.
   - `<name>.sdc` — `create_clock` for each input clock + a
     `set_clock_groups -asynchronous` declaring the pair async.
   - `<name>.json` — pre-built Yosys netlist. **Commit this.** The
     test suite must not require Yosys to be installed.
2. Build the JSON with the workspace Yosys:
   ```bash
   yosys -p 'read_verilog -sv path/to/<name>.sv; \
             hierarchy -top <name>; proc; flatten; \
             write_json tests/fixtures/<dir>/<name>.json'
   ```
3. Write the test:
   - For a **bad** fixture: a dedicated `tests/test_<dir>.py` that
     asserts the expected rule fires and **only** that rule fires.
   - For a **good** fixture: append `(<dir>, <expected_async>)` to
     `GOOD_FIXTURES` in `tests/test_good_fixtures.py`. Use a
     dedicated test file only when the assertions go beyond
     "no violations" (see `test_good_gray_counter_crossing.py`).
4. Confirm: `uv run pytest -q tests/test_<your>.py`.
5. Regenerate the fixture README: `uv run python scripts/gen_fixture_docs.py --only <name>`. The generator pulls the prose from the leading `//` comment block in your `.sv` file and embeds a mermaid clock-domain diagram — keep the comment block short and accurate, it's now user-facing documentation.

## Adding an SV attribute

1. Define the frozenset in `rules.py` next to `USER_SYNC_ATTRS` /
   `USER_GRAY_ATTRS`.
2. Add a `user_<x>_flop_names(module)` helper following the existing
   shape — Yosys preserves attributes on the **netname**, not the
   cell, so the helper maps tagged netname bits back to the flop's
   Q.
3. Consult it in the rules that should honor the annotation.
4. Cover with a `marked_<x>` fixture and update `README.md`'s
   "SV attributes" section.

## Extending the SDC parser

The parser is intentionally a Tcl-aware tokenizer plus per-command
arg-spec table, **not** a Tcl interpreter. Don't pull in
`tkinter.Tcl()` or a third-party Tcl host without first opening an
issue — the design choice is documented in
`wiki/raw/articles/rtl-buddy-cdc-architecture.md` §6.

The two-layer shape landed in #144 (after the pointwise fixes
#140 / #142 made the underlying bug class clear). Layer 1 is
`_tokenize`: a Tcl-aware word tokenizer where `{...}` braces and
`[...]` brackets are single tokens with nesting respected. Layer 2
is `ARG_SPECS`: a per-command table declaring each flag's arity
(`ZERO` / `ONE` / `GREEDY`); the dispatcher slices the word list
into a typed `Parsed(flags, tail)` bag the handlers consume directly.
The #144 issue thread records the rejected `tkinter.Tcl()`
alternative and the conditions under which we'd switch tracks
(`$var` expansion, `source` includes, `unknown` / `proc`-driven
vendor commands). If your work motivates any of those triggers,
raise it as a new issue referencing the #144 discussion rather than
growing variable expansion onto the current tokenizer.

When adding a new SDC command:

1. Extend `ClockSpec` with the new field (default-empty so existing
   callers keep working).
2. Add a new entry to `ARG_SPECS` declaring each flag's arity and any
   flags whose multiple occurrences are semantically distinct
   (`repeated=...`).
3. Add a `_handle_<command>` function in `sdc.py` that consumes a
   `Parsed` bag via `p.first(flag)` / `p.present(flag)` / `p.all(flag)`;
   wire it up in the `_DISPATCH` table.
4. Per the documented diagnostics policy: truly unrecognised commands
   are dropped at the `logging.DEBUG` level by the central dispatcher.
   Accumulate a `partial_warnings` entry when a CDC-relevant command
   was present but couldn't be fully parsed (e.g.
   `set_false_path -through`).
5. Add a `tests/test_sdc.py` case for the new command and a fixture
   exercising it end-to-end. Cover the three forms every
   collection-valued operand can take: bracket (`[get_ports x]`),
   brace (`{x}`), and bare identifier (`x`) — the
   "issue #144: brace / bracket / bare form coverage per command"
   section is the canonical example.

## Design proposals live on GitHub, not in `docs/proposals/`

Design discussion for a new rule, a behaviour change, or any other
non-trivial piece of work belongs in the GitHub issue body (and its
comment thread), not in a committed Markdown file. Do **not** create
new files under `docs/proposals/`.

Concretely, when you're scoped to "design X":

- Write the failure-mode description, approach, severity decision,
  fixture sketches, open questions, etc. directly into the issue body
  (edit it in place as the design evolves) or as comments on the
  issue. Use `gh issue edit <n>` / `gh issue comment <n>`.
- The PR that implements the work links back to the issue; the issue
  thread is the design record. The PR description summarises *what*
  shipped, not *why we chose this shape* — that's the issue's job.
- Don't seed `docs/proposals/<x>.md` in the issue's Scope list when
  filing a new issue. Past issues (#46, #48, #97) did this and
  propagated the pattern; new issues should not.

The three legacy files (`hierarchical-reporting.md`,
`clock-network-glitch.md`, `unconstrained-input-domain.md`) stay as
historical artifacts — don't delete or move them without an explicit
ask, but don't add to the pattern either.

Exception: cross-cutting reference material that outlives a single
issue (the architecture spec, the SDC subset reference) belongs in
`wiki/raw/articles/`, not `docs/proposals/`. If a design discussion
graduates into long-lived reference docs after shipping, promote it
to `wiki/raw/articles/` with a `source_url:` frontmatter pointing
at the issue.

## Cross-repo coupling

The `rtl_buddy` repo at `../rtl_buddy/` consumes this analyzer via
subprocess. The contract:

- **CLI flags consumed today**: `--netlist`, `--sdc`, `--top`,
  `--waivers`, `--sync-depth`, `--format`, `--output`. Renaming or
  removing any of these breaks `rtl_buddy/src/rtl_buddy/tools/cdc_rtl_buddy.py`.
- **JSON schema consumed today**: `summary.violations` (int),
  `summary.suppressed` (int), `summary.crossings` (int). Other
  fields can evolve freely; these three must keep their names and
  types. The contract is pinned in code by `reporter.JSON_CONTRACT`
  plus `tests/test_reporter.py::test_json_contract_keys_present_and_typed`
  — a rename or retype on either key fails the test.
- **Exit codes**: 0 = clean (or fully waived), 1 = at least one
  unsuppressed violation, 2 = `lint`-only (yosys elaboration
  failed). The wrapper treats {0, 1} as "ran successfully" and
  anything else as a hard fail.

When changing any of the above, update
`rtl_buddy/src/rtl_buddy/tools/cdc_rtl_buddy.py` in the same change
set and run the project-template's CDC suite end-to-end before
landing.

## Commit / branch / release conventions

- Commit messages: imperative subject, blank line, body that explains
  *why* not *what*. Co-author trailer
  `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`
  on assistant-authored commits.
- Branch off `main`. PRs land via squash-or-rebase; `main` is the
  release branch.
- Versioning: bump `pyproject.toml` `[project].version` and
  `reporter.TOOL_VERSION` in lockstep when cutting a release. SARIF
  consumers key off `TOOL_VERSION`. Drift between the two is caught
  by `tests/test_reporter.py::test_tool_version_matches_pyproject`.
  Record the release notes in `CHANGELOG.md` (Keep a Changelog
  format); the `[Unreleased]` section gets renamed to the new
  version + date on release, and a fresh `[Unreleased]` heading is
  added back on top.

## When the architecture spec is wrong

The architecture document is the canonical reference. If your work
contradicts it (e.g. a new rule needs a non-pure helper, or a new
output format needs to mutate `AnalysisResult`), **update the spec
in the same PR**. Drift between code and spec is the failure mode
that makes the document worthless. If you're not sure whether a
change is contract-level or implementation-detail, ask.
