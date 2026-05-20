"""Catalog validator distributed by the mozyo-bridge governed preset."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from docs_catalog import (
    CATALOG_PATH,
    MANAGED_TYPES,
    document_index,
    load_catalog,
    matching_file_conventions,
    repo_abspath,
)


STRICT_METADATA_TYPES = {"rule", "spec", "task"}
DEFAULT_COVERAGE_ROOTS = (
    "app/controllers",
    "app/presenters",
    "app/services",
    "app/models",
    "app/jobs",
    "db/migrate",
    "spec",
)
DEFAULT_COVERAGE_SUFFIXES = {".rb", ".yml", ".yaml"}
DEFAULT_COVERAGE_IGNORED_PARTS = {".git", "__pycache__"}


def _has_non_empty_string(document: dict[str, object], field: str) -> bool:
    value = document.get(field)
    return isinstance(value, str) and bool(value.strip())


def validate_catalog(catalog_path: Path, strict_metadata: bool = False) -> list[str]:
    catalog = load_catalog(catalog_path)
    errors: list[str] = []
    documents = catalog.get("documents", [])
    seen_ids: set[str] = set()
    docs_by_id = document_index(catalog)

    if catalog.get("schema_version") != 1:
        errors.append("schema_version must be 1")

    managed_types = set(catalog.get("managed_types", []))
    if managed_types != MANAGED_TYPES:
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
            and not repo_abspath(canonical_path).exists()
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

    return errors


def validate_file_coverage(
    catalog_path: Path,
    roots: list[str] | None = None,
    suffixes: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Return (errors, notices).

    ``errors`` block the exit code (a real coverage gap inside an
    existing project layer). ``notices`` are informational only — for
    example, when one of the default Rails layers does not exist yet
    in the target project. The caller decides how to surface notices
    (printed but not counted toward failure).
    """
    catalog = load_catalog(catalog_path)
    errors: list[str] = []
    notices: list[str] = []
    coverage_roots = roots or list(DEFAULT_COVERAGE_ROOTS)
    coverage_suffixes = suffixes or DEFAULT_COVERAGE_SUFFIXES

    for root in coverage_roots:
        absolute_root = repo_abspath(root)
        if not absolute_root.exists():
            # Missing coverage root is informational, not an error: a fresh
            # Rails project may not yet have every layer. The validator
            # reports it for visibility but does not fail. Operators can
            # narrow `DEFAULT_COVERAGE_ROOTS` per-project via the
            # `--coverage-root` CLI option.
            notices.append(f"coverage root does not exist (informational): {root}")
            continue
        for path in absolute_root.rglob("*"):
            if not path.is_file() or path.suffix not in coverage_suffixes:
                continue
            if set(path.parts) & DEFAULT_COVERAGE_IGNORED_PARTS:
                continue
            relative_path = path.relative_to(repo_abspath(".")).as_posix()
            if not matching_file_conventions(catalog, relative_path):
                errors.append(f"no file_convention matched: {relative_path}")

    return errors, notices


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate .mozyo-bridge/docs/catalog.yaml")
    parser.add_argument(
        "--catalog",
        type=Path,
        default=CATALOG_PATH,
        help="Path to catalog YAML",
    )
    parser.add_argument(
        "--strict-metadata",
        action="store_true",
        help="Require purpose/audit_role/related_document_refs on active rule/spec/task documents",
    )
    parser.add_argument(
        "--check-file-coverage",
        action="store_true",
        help="Require important source files under project roots to match at least one file_convention",
    )
    parser.add_argument(
        "--coverage-root",
        action="append",
        default=None,
        help=(
            "Override the default coverage roots. Pass once per root. "
            "Use this when the project does not match the default Rails layer layout."
        ),
    )
    args = parser.parse_args()

    errors = validate_catalog(args.catalog, strict_metadata=args.strict_metadata)
    notices: list[str] = []
    if args.check_file_coverage:
        coverage_errors, coverage_notices = validate_file_coverage(
            args.catalog, roots=args.coverage_root
        )
        errors.extend(coverage_errors)
        notices.extend(coverage_notices)
    if notices:
        for notice in notices:
            print(f"notice: {notice}")
    if errors:
        print("catalog validation failed")
        for error in errors:
            print(f"- {error}")
        return 1

    print("catalog validation passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
