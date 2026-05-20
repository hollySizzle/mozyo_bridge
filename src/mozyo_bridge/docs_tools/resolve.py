"""Resolve audit docs for a list of paths.

The CLI exposes three output formats so operators can pipe results
through review tools, render markdown into ticket comments, or
consume JSON from automation.
"""

from __future__ import annotations

import json
from typing import Any

from .catalog import CatalogContext, load_catalog, resolve_audit_documents


def resolve_paths(
    context: CatalogContext,
    paths: list[str],
) -> list[dict[str, Any]]:
    catalog = load_catalog(context.catalog_path)
    return [resolve_audit_documents(context, catalog, path) for path in paths]


def render_resolution_text(results: list[dict[str, Any]]) -> str:
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


def render_resolution_markdown(results: list[dict[str, Any]]) -> str:
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


def render_resolution_json(results: list[dict[str, Any]]) -> str:
    return json.dumps(results, ensure_ascii=False, indent=2)
