"""Resolve active docs for audit from catalog and file conventions.

Distributed by the mozyo-bridge `redmine-rails-governed` preset.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from docs_catalog import CATALOG_PATH, load_catalog, resolve_audit_documents


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resolve active docs for audit from catalog and file conventions"
    )
    parser.add_argument(
        "paths",
        nargs="+",
        help="Repository-relative or absolute file paths to resolve",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=CATALOG_PATH,
        help="Path to catalog YAML",
    )
    parser.add_argument(
        "--format",
        choices=("text", "markdown", "json"),
        default="text",
        help="Output format",
    )
    return parser


def render_text(results: list[dict[str, object]]) -> str:
    lines: list[str] = []
    for result in results:
        path = result["path"]
        lines.append(f"[{path}]")
        lines.append("matched_file_conventions:")
        conventions = result["matched_file_conventions"]
        if conventions:
            for convention in conventions:
                refs = ", ".join(convention["document_refs"]) or "-"
                scope = f", scope={convention['scope']}" if convention.get("scope") else ""
                lines.append(
                    f"- {convention['id']} ({convention['name']}, severity={convention['severity']}{scope}, refs={refs})"
                )
        else:
            lines.append("- none")

        lines.append("documents_to_read:")
        documents = result["documents"]
        if documents:
            for document in documents:
                sources = ", ".join(document["sources"])
                lines.append(
                    f"- {document['type']} {document['id']} -> {document['canonical_path']} (source: {sources})"
                )
        else:
            lines.append("- none")

        notes = result["notes"]
        if notes:
            lines.append("notes:")
            for note in notes:
                lines.append(f"- {note}")
        lines.append("")

    return "\n".join(lines).rstrip()


def render_markdown(results: list[dict[str, object]]) -> str:
    lines: list[str] = ["## 規約マッチ結果"]
    for result in results:
        path = result["path"]
        lines.append("")
        lines.append(f"### `{path}`")
        lines.append("")
        lines.append("- matched_file_conventions:")
        conventions = result["matched_file_conventions"]
        if conventions:
            for convention in conventions:
                refs = ", ".join(convention["document_refs"]) or "-"
                scope = f", scope={convention['scope']}" if convention.get("scope") else ""
                lines.append(
                    f"  - `{convention['id']}` ({convention['name']}, severity={convention['severity']}{scope}, refs={refs})"
                )
        else:
            lines.append("  - none")

        lines.append("- documents_to_read:")
        documents = result["documents"]
        if documents:
            for document in documents:
                sources = ", ".join(document["sources"])
                lines.append(
                    f"  - `{document['type']}` `{document['id']}` -> `{document['canonical_path']}` (source: {sources})"
                )
        else:
            lines.append("  - none")

        notes = result["notes"]
        if notes:
            lines.append("- notes:")
            for note in notes:
                lines.append(f"  - {note}")

    return "\n".join(lines)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    catalog = load_catalog(args.catalog)
    results = [resolve_audit_documents(catalog, path) for path in args.paths]

    if args.format == "json":
        print(json.dumps(results, ensure_ascii=False, indent=2))
    elif args.format == "markdown":
        print(render_markdown(results))
    else:
        print(render_text(results))

    return 0


if __name__ == "__main__":
    sys.exit(main())
