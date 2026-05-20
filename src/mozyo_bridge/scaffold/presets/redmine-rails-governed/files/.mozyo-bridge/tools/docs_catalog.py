"""Docs catalog loader and matcher used by the governed-preset tools.

This module is distributed by the mozyo-bridge `redmine-rails-governed`
preset. It lives under `<repo>/.mozyo-bridge/tools/` once the scaffold is
applied. Repo-relative paths are resolved against `REPO_ROOT`, which is
the directory two levels above this file (`.mozyo-bridge/tools/..` =
`.mozyo-bridge`, then `..` again = repo root).
"""

from __future__ import annotations

import fnmatch
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = REPO_ROOT / ".mozyo-bridge/docs/catalog.yaml"
MANAGED_TYPES = {"rule", "spec", "logic", "manual_spec", "task"}
DEFAULT_DOC_MESSAGE = (
    "このファイルを変更する場合は､ {document_paths} をよく確認し従うこと\n"
    "作業を続けるためには､上記ドキュメントを確認したことを示してください\n"
)
AUDIT_DOCUMENT_ORDER = {
    "rule": 0,
    "spec": 1,
    "task": 2,
    "logic": 3,
    "manual_spec": 4,
}


def load_catalog(catalog_path: Path = CATALOG_PATH) -> dict[str, Any]:
    data = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("catalog root must be a mapping")
    return data


def document_index(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {document["id"]: document for document in catalog.get("documents", [])}


def active_document_index(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        document["id"]: document
        for document in catalog.get("documents", [])
        if document.get("status") == "active"
    }


def document_path_index(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        document["canonical_path"]: document
        for document in catalog.get("documents", [])
        if document.get("canonical_path")
    }


def repo_relative(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def repo_abspath(relative_path: str) -> Path:
    return REPO_ROOT / relative_path


def normalize_repo_relative_path(path_str: str) -> str:
    path = Path(path_str)
    if path.is_absolute():
        return repo_relative(path.resolve())
    normalized = PurePosixPath(path.as_posix()).as_posix()
    repo_name_prefix = f"{REPO_ROOT.parent.name}/{REPO_ROOT.name}/"
    if normalized.startswith(repo_name_prefix):
        return normalized[len(repo_name_prefix):]
    return normalized


def pattern_matches(relative_path: str, pattern: str) -> bool:
    if fnmatch.fnmatchcase(relative_path, pattern):
        return True
    if "/**/" in pattern:
        # `foo/**/*.rb` should also match `foo/bar.rb`. Python's fnmatch
        # treats `**` as ordinary `*` segments, so the zero-directory case
        # needs explicit handling.
        zero_directory_pattern = pattern.replace("/**/", "/")
        return fnmatch.fnmatchcase(relative_path, zero_directory_pattern)
    return False


def matching_file_conventions(
    catalog: dict[str, Any], relative_path: str
) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []

    for item in catalog.get("file_conventions", []):
        patterns = item.get("patterns", [])
        exclude_patterns = item.get("exclude_patterns", [])
        if not any(pattern_matches(relative_path, pattern) for pattern in patterns):
            continue
        if any(pattern_matches(relative_path, pattern) for pattern in exclude_patterns):
            continue
        matched.append(item)

    return matched


def build_file_conventions_payload(catalog: dict[str, Any]) -> dict[str, Any]:
    documents = document_index(catalog)
    generated_rules: list[dict[str, Any]] = []

    for item in catalog.get("file_conventions", []):
        rule: dict[str, Any] = {
            "name": item["name"],
            "patterns": item["patterns"],
            "severity": item["severity"],
        }

        for key in ("token_threshold", "scope", "exclude_patterns"):
            if key in item:
                rule[key] = item[key]

        document_refs = item.get("document_refs", [])
        if document_refs:
            document_paths = " と ".join(
                f"@{documents[ref]['canonical_path']}" for ref in document_refs
            )
            message = item.get("message_template", DEFAULT_DOC_MESSAGE).format(
                document_paths=document_paths
            )
        else:
            message = item["message"]

        rule["message"] = message
        generated_rules.append(rule)

    return {"rules": generated_rules}


def resolve_audit_documents(
    catalog: dict[str, Any], relative_path: str
) -> dict[str, Any]:
    normalized_path = normalize_repo_relative_path(relative_path)
    docs_by_id = document_index(catalog)
    active_docs_by_id = active_document_index(catalog)
    docs_by_path = document_path_index(catalog)
    matched_conventions = matching_file_conventions(catalog, normalized_path)
    resolved_docs: dict[str, dict[str, Any]] = {}
    notes: list[str] = []

    def add_document(document: dict[str, Any], source: str) -> None:
        current = resolved_docs.setdefault(
            document["id"], {"document": document, "sources": []}
        )
        if source not in current["sources"]:
            current["sources"].append(source)

    direct_document = docs_by_path.get(normalized_path)
    if direct_document is not None:
        if direct_document.get("status") == "active":
            add_document(direct_document, "document_path")
        elif direct_document.get("replacement"):
            replacement_document = docs_by_path.get(direct_document["replacement"])
            if (
                replacement_document is not None
                and replacement_document.get("status") == "active"
            ):
                add_document(replacement_document, f"replacement:{direct_document['id']}")
                notes.append(
                    f"deprecated `{normalized_path}` -> `{direct_document['replacement']}` を参照"
                )

    for convention in matched_conventions:
        for document_ref in convention.get("document_refs", []):
            document = docs_by_id.get(document_ref)
            if document is None or document.get("status") != "active":
                continue
            add_document(document, f"file_convention:{convention['id']}")

    for item in list(resolved_docs.values()):
        source_document = item["document"]
        for related_ref in source_document.get("related_document_refs", []):
            related_document = active_docs_by_id.get(related_ref)
            if related_document is None:
                continue
            add_document(related_document, f"related:{source_document['id']}")

    documents = sorted(
        (
            {
                "id": item["document"]["id"],
                "type": item["document"]["type"],
                "canonical_path": item["document"]["canonical_path"],
                "sources": item["sources"],
            }
            for item in resolved_docs.values()
        ),
        key=lambda item: (
            AUDIT_DOCUMENT_ORDER.get(item["type"], 99),
            item["canonical_path"],
        ),
    )

    conventions = [
        {
            "id": item["id"],
            "name": item["name"],
            "severity": item["severity"],
            "scope": item.get("scope"),
            "document_refs": item.get("document_refs", []),
        }
        for item in matched_conventions
    ]

    if not documents:
        notes.append("catalog と file_conventions から active docs を解決できなかった")

    return {
        "path": normalized_path,
        "matched_file_conventions": conventions,
        "documents": documents,
        "notes": notes,
    }
