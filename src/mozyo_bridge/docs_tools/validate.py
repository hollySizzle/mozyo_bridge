"""Catalog validator: structure, references, coverage."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .catalog import (
    CatalogContext,
    MANAGED_TYPES,
    document_index,
    load_catalog,
    matching_file_conventions,
)
from .overlay import (
    OverlayError,
    merge_catalog_with_overlay,
    read_overlay_document,
    scan_for_secret_shaped_values,
)


STRICT_METADATA_TYPES = frozenset({"rule", "spec", "task"})
DEFAULT_COVERAGE_ROOTS = (
    "app/controllers",
    "app/presenters",
    "app/services",
    "app/models",
    "app/jobs",
    "db/migrate",
    "spec",
)
DEFAULT_COVERAGE_SUFFIXES = frozenset({".rb", ".yml", ".yaml"})
DEFAULT_COVERAGE_IGNORED_PARTS = frozenset({".git", "__pycache__"})


def _has_non_empty_string(document: dict[str, object], field: str) -> bool:
    value = document.get(field)
    return isinstance(value, str) and bool(value.strip())


def validate_catalog(
    context: CatalogContext,
    *,
    strict_metadata: bool = False,
) -> list[str]:
    """Structural validation: ids, refs, canonical paths, coverage_roots shape."""
    catalog = load_catalog(context.catalog_path)
    errors: list[str] = []
    documents = catalog.get("documents", [])
    seen_ids: set[str] = set()
    docs_by_id = document_index(catalog)

    if catalog.get("schema_version") != 1:
        errors.append("schema_version must be 1")

    managed_types = set(catalog.get("managed_types", []))
    if managed_types != set(MANAGED_TYPES):
        errors.append(
            f"managed_types must be exactly {sorted(MANAGED_TYPES)}, got {sorted(managed_types)}"
        )

    for document in documents:
        document_id = document.get("id")
        if not document_id:
            errors.append("document id is required")
            continue
        if document_id in seen_ids:
            errors.append(f"duplicate document id: {document_id}")
        seen_ids.add(document_id)

        for field in ("type", "status", "canonical_path"):
            if field not in document:
                errors.append(f"{document_id}: missing required field `{field}`")

        status = document.get("status")
        document_type = document.get("type")
        canonical_path = document.get("canonical_path")

        if status == "deprecated" and not document.get("replacement"):
            errors.append(f"{document_id}: deprecated documents must declare `replacement`")

        if status == "active" and document_type not in MANAGED_TYPES:
            errors.append(f"{document_id}: active document type `{document_type}` is not managed")

        if strict_metadata and status == "active" and document_type in STRICT_METADATA_TYPES:
            if not _has_non_empty_string(document, "purpose"):
                errors.append(f"{document_id}: strict metadata requires non-empty `purpose`")
            if not _has_non_empty_string(document, "audit_role"):
                errors.append(f"{document_id}: strict metadata requires non-empty `audit_role`")
            if not document.get("related_document_refs"):
                errors.append(
                    f"{document_id}: strict metadata requires non-empty `related_document_refs`"
                )

        if (
            isinstance(canonical_path, str)
            and status in {"active", "deprecated"}
            and not context.repo_abspath(canonical_path).exists()
        ):
            errors.append(f"{document_id}: canonical_path does not exist: {canonical_path}")

        related_document_refs = document.get("related_document_refs", [])
        if related_document_refs and not isinstance(related_document_refs, list):
            errors.append(f"{document_id}: related_document_refs must be a list")
            related_document_refs = []

        for related_ref in related_document_refs:
            related_document = docs_by_id.get(related_ref)
            if related_ref == document_id:
                errors.append(f"{document_id}: related_document_refs must not self-reference")
                continue
            if related_document is None:
                errors.append(f"{document_id}: unknown related_document_ref `{related_ref}`")
                continue
            if related_document.get("status") != "active":
                errors.append(
                    f"{document_id}: related_document_ref `{related_ref}` must point to an active document"
                )

    for item in catalog.get("file_conventions", []):
        convention_id = item.get("id", "<unknown>")
        for field in ("id", "name", "patterns", "severity"):
            if field not in item:
                errors.append(f"{convention_id}: missing required field `{field}`")
        for document_ref in item.get("document_refs", []):
            document = docs_by_id.get(document_ref)
            if document is None:
                errors.append(f"{convention_id}: unknown document_ref `{document_ref}`")
                continue
            if document.get("status") != "active":
                errors.append(
                    f"{convention_id}: document_ref `{document_ref}` must point to an active document"
                )

    coverage_roots = catalog.get("coverage_roots")
    if coverage_roots is not None:
        if not isinstance(coverage_roots, list):
            errors.append("coverage_roots must be a list of repo-relative path strings")
        else:
            for index, root in enumerate(coverage_roots):
                if not isinstance(root, str) or not root.strip():
                    errors.append(
                        f"coverage_roots[{index}] must be a non-empty repo-relative string"
                    )

    return errors


def validate_overlay(context: CatalogContext) -> list[str]:
    """Validate the optional local-only overlay (Redmine #11819).

    Returns an empty list when no overlay file exists — public
    ``docs validate`` stays green on a fresh clone / CI. When the overlay
    is present it is checked for: secret-shaped values, a usable
    structure (required document / file_convention fields), local
    canonical paths that actually exist, and id collisions against the
    public catalog. Errors are collected (not raised) so the caller can
    print them alongside the public catalog's own errors.
    """
    overlay_path = context.overlay_path
    if not overlay_path.exists():
        return []

    try:
        overlay = read_overlay_document(overlay_path)
    except OverlayError as exc:
        return [f"overlay: {exc}"]
    if not overlay:
        return []

    errors: list[str] = []
    for finding in scan_for_secret_shaped_values(overlay):
        errors.append(f"overlay secret-shaped value: {finding}")

    for document in overlay.get("documents", []):
        if not isinstance(document, dict):
            errors.append("overlay document must be a mapping")
            continue
        document_id = document.get("id", "<unknown>")
        for field in ("id", "type", "status", "canonical_path"):
            if field not in document:
                errors.append(f"overlay document {document_id}: missing `{field}`")
        canonical_path = document.get("canonical_path")
        if (
            isinstance(canonical_path, str)
            and document.get("status") in {"active", "deprecated"}
            and not context.repo_abspath(canonical_path).exists()
        ):
            errors.append(
                f"overlay document {document_id}: canonical_path does not exist: "
                f"{canonical_path}"
            )

    for convention in overlay.get("file_conventions", []):
        if not isinstance(convention, dict):
            errors.append("overlay file_convention must be a mapping")
            continue
        convention_id = convention.get("id", "<unknown>")
        for field in ("id", "name", "patterns", "severity"):
            if field not in convention:
                errors.append(
                    f"overlay file_convention {convention_id}: missing `{field}`"
                )

    try:
        merge_catalog_with_overlay(load_catalog(context.catalog_path), overlay)
    except OverlayError as exc:
        errors.append(f"overlay merge: {exc}")

    return errors


def resolve_coverage_roots(
    catalog: dict[str, Any],
    cli_roots: list[str] | None,
) -> tuple[list[str], str]:
    """CLI > catalog > validator default. Returns (roots, source_label)."""
    if cli_roots:
        return list(cli_roots), "cli"
    catalog_roots = catalog.get("coverage_roots")
    if isinstance(catalog_roots, list) and catalog_roots:
        cleaned = [r for r in catalog_roots if isinstance(r, str) and r.strip()]
        if cleaned:
            return cleaned, "catalog"
    return list(DEFAULT_COVERAGE_ROOTS), "default"


def validate_file_coverage(
    context: CatalogContext,
    *,
    roots: list[str] | None = None,
    suffixes: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Return (errors, notices). Missing roots are informational notices;
    real coverage gaps (existing root, file unmatched) are errors.
    """
    catalog = load_catalog(context.catalog_path)
    errors: list[str] = []
    notices: list[str] = []
    coverage_roots, source = resolve_coverage_roots(catalog, roots)
    notices.append(f"coverage_roots source: {source} ({len(coverage_roots)} root(s))")
    coverage_suffixes = suffixes or set(DEFAULT_COVERAGE_SUFFIXES)

    for root in coverage_roots:
        absolute_root = context.repo_abspath(root)
        if not absolute_root.exists():
            notices.append(f"coverage root does not exist (informational): {root}")
            continue
        for path in absolute_root.rglob("*"):
            if not path.is_file() or path.suffix not in coverage_suffixes:
                continue
            if set(path.parts) & DEFAULT_COVERAGE_IGNORED_PARTS:
                continue
            relative_path = path.relative_to(context.repo_root).as_posix()
            if not matching_file_conventions(catalog, relative_path):
                errors.append(f"no file_convention matched: {relative_path}")

    return errors, notices
