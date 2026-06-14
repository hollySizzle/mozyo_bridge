"""Resolve audit docs for a list of paths.

The CLI exposes three output formats so operators can pipe results
through review tools, render markdown into ticket comments, or
consume JSON from automation.
"""

from __future__ import annotations

import json
from typing import Any

from .catalog import CatalogContext, resolve_audit_documents
from .overlay import OverlayInfo, load_effective_catalog


def resolve_paths_detailed(
    context: CatalogContext,
    paths: list[str],
    *,
    include_local: bool = True,
) -> tuple[list[dict[str, Any]], OverlayInfo]:
    """Resolve docs for ``paths`` and report whether the local overlay applied.

    The effective catalog merges the git-ignored ``catalog.local.yaml``
    overlay on top of the public catalog when present (Redmine #11819);
    ``include_local=False`` forces the public-only view CI would see.
    """
    catalog, overlay_info = load_effective_catalog(
        context, include_local=include_local
    )
    results = [resolve_audit_documents(context, catalog, path) for path in paths]
    return results, overlay_info


def resolve_paths(
    context: CatalogContext,
    paths: list[str],
    *,
    include_local: bool = True,
) -> list[dict[str, Any]]:
    results, _ = resolve_paths_detailed(
        context, paths, include_local=include_local
    )
    return results


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
