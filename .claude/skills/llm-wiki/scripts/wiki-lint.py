#!/usr/bin/env python3
"""
Wiki Lint Script — run all 11 health checks against a Karpathy-style LLM wiki.
Usage: python scripts/wiki-lint.py <wiki-path>

Prints structured report grouped by severity, returns exit code:
  0 = clean (actionable items = 0)
  1 = issues found
  2 = usage error

Checks performed:
  - Orphan pages (no inbound [[wikilinks]])
  - Broken wikilinks (point to non-existent pages, including anchor links)
  - Index completeness (every page in index.md)
  - Frontmatter validation (required fields, tag taxonomy compliance)
  - Stale content (updated >90 days ago)
  - Contradictions (contested: true / contradictions: frontmatter)
  - Quality signals (confidence: low, missing confidence on single-source)
  - Source drift (sha256 mismatches in raw/)
  - Page size (>200 lines — split candidate)
  - Tag audit (tags in use but not in SCHEMA.md taxonomy)
  - Log rotation (log.md entries > 500)
"""

import os
import re
import sys
import yaml
import hashlib
from collections import defaultdict
from datetime import datetime, date


def find_wiki_pages(wiki_root):
    """Find all .md pages excluding raw/, _archive/, and top-level meta files."""
    pages = {}
    for root, dirs, files in os.walk(wiki_root):
        rel = os.path.relpath(root, wiki_root)
        if rel.startswith("raw") or rel.startswith("_archive") or rel == ".":
            continue
        for f in files:
            if not f.endswith(".md"):
                continue
            path = os.path.join(root, f)
            rel_path = os.path.relpath(path, wiki_root)
            with open(path, encoding="utf-8") as fh:
                content = fh.read()
            lines = content.split("\n")

            # Parse frontmatter
            fm = {}
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    try:
                        fm = yaml.safe_load(parts[1]) or {}
                    except yaml.YAMLError:
                        fm = {"_parse_error": parts[1][:80]}

            # Extract [[wikilinks]] (including [[page#anchor]] and [[page|alias]])
            wikilinks = re.findall(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", content)
            tags = fm.get("tags", []) if isinstance(fm.get("tags"), list) else []

            pages[rel_path] = {
                "content": content,
                "num_lines": len(lines),
                "frontmatter": fm,
                "wikilinks": wikilinks,
                "tags": tags,
            }
    return pages


def build_slug_set(pages):
    """Build set of valid page slugs from filenames and titles."""
    slugs = set()
    for rel_path in pages:
        slug = os.path.splitext(os.path.basename(rel_path))[0]
        slugs.add(slug)
        title = pages[rel_path]["frontmatter"].get("title", "")
        if title:
            slugs.add(title.lower().replace(" ", "-"))
    return slugs


def load_schema_tags(wiki_root):
    """Parse SCHEMA.md and extract all tags from the tag taxonomy section."""
    schema_path = os.path.join(wiki_root, "SCHEMA.md")
    if not os.path.exists(schema_path):
        return set()
    with open(schema_path, encoding="utf-8") as f:
        content = f.read()

    # Find the ## Tag Taxonomy section and extract tag names
    # Tags are listed as: `- Category: tag1, tag2, tag3`
    tags = set()
    in_taxonomy = False
    for line in content.split("\n"):
        if line.strip().startswith("## Tag Taxonomy"):
            in_taxonomy = True
            continue
        if in_taxonomy:
            if line.strip().startswith("## "):
                break
            # Match lines like `- Category: tag1, tag2, tag3`
            match = re.match(r"\s*-\s+\w+\s*:\s*(.+)", line)
            if match:
                for t in match.group(1).split(","):
                    t = t.strip()
                    if t:
                        tags.add(t)
    return tags


def lint_wiki(wiki_root):
    """Run all lint checks and return results as a dict of lists."""
    issues = defaultdict(list)  # severity -> [(message, path)]
    pages = find_wiki_pages(wiki_root)
    valid_slugs = build_slug_set(pages)
    schema_tags = load_schema_tags(wiki_root)
    today = date.today()

    # Build inbound link map
    inbound = defaultdict(set)
    for rel_path, info in pages.items():
        for link in info["wikilinks"]:
            # Separate [[page#anchor]] into page part and anchor part
            link_page = link.split("#")[0]
            link_slug = link_page.lower().replace(" ", "-")

            if link_slug in valid_slugs:
                # Valid target — record as inbound link
                for target_path in pages:
                    target_slug = os.path.splitext(os.path.basename(target_path))[0]
                    if target_slug == link_slug:
                        inbound[target_path].add(rel_path)
                        break
            else:
                issues["broken"].append(
                    (f"[[{link}]] → page '{link_page}' not found", rel_path)
                )

    # Load index.md content
    index_path = os.path.join(wiki_root, "index.md")
    index_content = ""
    if os.path.exists(index_path):
        with open(index_path, encoding="utf-8") as f:
            index_content = f.read()

    for rel_path, info in sorted(pages.items()):
        fm = info["frontmatter"]
        slug = os.path.splitext(os.path.basename(rel_path))[0]

        # --- 1. Orphans ---
        if len(inbound[rel_path]) == 0:
            issues["orphan"].append(("Zero inbound [[wikilinks]]", rel_path))

        # --- 3. Index completeness ---
        if rel_path not in ("index.md", "SCHEMA.md", "log.md"):
            if f"[[{slug}" not in index_content:
                issues["index"].append(("Missing from index.md", rel_path))

        # --- 4. Frontmatter validation ---
        required = ["title", "created", "updated", "type", "tags", "sources"]
        for field in required:
            if field not in fm or fm[field] is None:
                issues["frontmatter"].append(
                    (f"Missing required field '{field}'", rel_path)
                )
                break

        for t in info["tags"]:
            if t not in schema_tags:
                issues["frontmatter"].append(
                    (f"Tag '{t}' not in SCHEMA.md taxonomy", rel_path)
                )

        for df in ["created", "updated"]:
            if df in fm and fm[df]:
                try:
                    datetime.strptime(str(fm[df]), "%Y-%m-%d")
                except (ValueError, TypeError):
                    issues["frontmatter"].append(
                        (f"Invalid date '{df}': {fm[df]}", rel_path)
                    )

        # --- 5. Stale content (>90 days) ---
        updated_str = fm.get("updated", "")
        if updated_str:
            try:
                updated_date = datetime.strptime(str(updated_str), "%Y-%m-%d").date()
                delta = (today - updated_date).days
                if delta > 90:
                    issues["stale"].append(
                        (f"Last updated {delta} days ago (>90)", rel_path)
                    )
            except (ValueError, TypeError):
                pass

        # --- 6. Contradictions ---
        if fm.get("contested"):
            issues["contradiction"].append(("contested=true — needs review", rel_path))
        if fm.get("contradictions"):
            issues["contradiction"].append(
                (f"contradictions={fm['contradictions']}", rel_path)
            )

        # --- 7. Quality signals ---
        confidence = fm.get("confidence")
        sources = fm.get("sources", [])
        if confidence == "low":
            issues["quality"].append(("confidence=low (needs corroboration)", rel_path))
        elif (
            confidence is None
            and not sources
            and slug not in ("SCHEMA", "index", "log")
        ):
            issues["quality"].append(
                ("Single-source page, no confidence field set", rel_path)
            )

        # --- 9. Page size ---
        if info["num_lines"] > 200:
            issues["page-size"].append(
                (f"{info['num_lines']} lines (split at 200)", rel_path)
            )

    # --- 8. Source drift ---
    raw_dir = os.path.join(wiki_root, "raw")
    if os.path.exists(raw_dir):
        for root, dirs, files in os.walk(raw_dir):
            for f in files:
                if f.endswith(".md"):
                    path = os.path.join(root, f)
                    with open(path, encoding="utf-8") as fh:
                        content = fh.read()
                    if content.startswith("---"):
                        parts = content.split("---", 2)
                        if len(parts) >= 3:
                            try:
                                fm = yaml.safe_load(parts[1]) or {}
                            except yaml.YAMLError:
                                continue
                            stored_sha = fm.get("sha256", "")
                            if stored_sha:
                                body = parts[2]
                                actual = hashlib.sha256(body.encode()).hexdigest()
                                if actual != stored_sha:
                                    rel = os.path.relpath(path, wiki_root)
                                    issues["drift"].append(
                                        ("sha256 mismatch — raw file was edited", rel)
                                    )

    # --- 10. Tag audit ---
    if schema_tags:
        used_tags = set()
        for info in pages.values():
            used_tags.update(info["tags"])
        unused = [t for t in sorted(schema_tags) if t not in used_tags]
        if unused:
            issues["tag-audit"].append(
                (
                    f"Tags in schema but never used: {', '.join(unused[:15])}",
                    "SCHEMA.md",
                )
            )

    # --- 11. Log rotation ---
    log_path = os.path.join(wiki_root, "log.md")
    if os.path.exists(log_path):
        with open(log_path, encoding="utf-8") as f:
            log_content = f.read()
        entry_count = len(re.findall(r"^## \[", log_content, re.MULTILINE))
        if entry_count > 500:
            issues["log-rotation"].append(
                (f"{entry_count} entries — exceeds 500, needs rotation", "log.md")
            )

    return issues


def print_report(issues):
    """Print structured issue report grouped by severity."""
    severity_order = [
        ("broken", "BROKEN WIKILINKS", 1),
        ("orphan", "ORPHAN PAGES", 2),
        ("drift", "SOURCE DRIFT", 3),
        ("contradiction", "CONTRADICTIONS", 3),
        ("stale", "STALE CONTENT", 3),
        ("quality", "QUALITY SIGNALS", 3),
        ("frontmatter", "FRONTMATTER ISSUES", 4),
        ("page-size", "PAGE SIZE (>200 lines)", 4),
        ("index", "INDEX COMPLETENESS", 4),
        ("tag-audit", "TAG AUDIT", 4),
        ("log-rotation", "LOG ROTATION", 4),
    ]

    total = sum(len(issues[key]) for key, _, _ in severity_order)

    print("=== Wiki Lint Report ===\n")
    print(f"Total issues: {total}\n")

    for key, title, _ in severity_order:
        items = issues.get(key, [])
        if not items:
            continue
        print(f"--- {title} ---")
        for msg, path in items:
            print(f"  {path}: {msg}")
        print()

    if total == 0:
        print("Wiki is clean — no issues found.")


def main():
    if len(sys.argv) < 2:
        print("Usage: python wiki-lint.py <wiki-path>")
        sys.exit(2)

    wiki_root = os.path.abspath(sys.argv[1])
    if not os.path.isdir(wiki_root):
        print(f"Error: '{wiki_root}' is not a directory")
        sys.exit(2)

    # Verify it looks like a wiki
    if not os.path.exists(os.path.join(wiki_root, "SCHEMA.md")):
        print(f"Warning: '{wiki_root}' has no SCHEMA.md — is this a wiki directory?")

    issues = lint_wiki(wiki_root)
    print_report(issues)

    total_issues = sum(len(v) for v in issues.values())
    sys.exit(0 if total_issues == 0 else 1)


if __name__ == "__main__":
    main()
