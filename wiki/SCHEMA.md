# Wiki Schema

## Domain
EDA tooling and CDC (Clock Domain Crossing) analysis — the architecture, algorithms, and design decisions behind `rtl-buddy-cdc`, a Python-based CDC linter for flattened Yosys netlists. Covers netlist data models, clock-domain tracing, SDC parsing, CDC rule packs, waiver systems, and reporter formats.

## Conventions
- File names: lowercase, hyphens, no spaces (e.g., `clock-domain-tracing.md`)
- Every wiki page starts with YAML frontmatter (see below)
- Use `[[wikilinks]]` to link between pages (minimum 2 outbound links per page)
- When updating a page, always bump the `updated` date
- Every new page must be added to `index.md` under the correct section
- Every action must be appended to `log.md`
- **Provenance markers:** On pages that synthesize 3+ sources, append `^[raw/articles/source-file.md]`
  at the end of paragraphs whose claims come from a specific source. Optional on single-source pages
  where the `sources:` frontmatter is enough.

## Frontmatter
```yaml
---
title: Page Title
created: YYYY-MM-DD
updated: YYYY-MM-DD
type: entity | concept | comparison | query | summary
tags: [from taxonomy below]
sources: [raw/articles/source-name.md]
confidence: high | medium | low
contested: true                        # optional
contradictions: [other-page-slug]      # optional
---
```

## Tag Taxonomy
- Architecture: pipeline, data-model, algorithm, extension-point, design-decision, architecture, bfs, parsing
- CDC: cdc, clock-domain, synchronization, crossing
- EDA: yosys, sdc, netlist, flops, eda
- Tooling: reporting, waivers, testing, cli, sarif
- Meta: integration

Rule: every tag on a page must appear in this taxonomy. If a new tag is needed,
add it here first, then use it.

## Page Thresholds
- **Create a page** when an entity/concept appears in 2+ sources OR is central to one source
- **Add to existing page** when a source mentions something already covered
- **DON'T create a page** for passing mentions, minor details, or things outside the domain
- **Split a page** when it exceeds ~200 lines — break into sub-topics with cross-links
- **Archive a page** when its content is fully superseded — move to `_archive/`, remove from index

## Entity Pages
One page per notable entity. Include:
- Overview / what it is
- Key facts and dates
- Relationships to other entities ([[wikilinks]])
- Source references

## Concept Pages
One page per concept or topic. Include:
- Definition / explanation
- Current state of knowledge
- Open questions or debates
- Related concepts ([[wikilinks]])

## Update Policy
When new information conflicts with existing content:
1. Check the dates — newer sources generally supersede older ones
2. If genuinely contradictory, note both positions with dates and sources
3. Mark the contradiction in frontmatter: `contradictions: [page-name]`
4. Flag for user review in the lint report
