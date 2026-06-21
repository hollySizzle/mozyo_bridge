"""Command handlers for the rules / scaffold / docs command families.

Split out of ``application/commands.py`` (Redmine #12142). ``commands.py``
re-exports these names so existing imports and monkeypatch targets
(``mozyo_bridge.application.commands.cmd_*``) keep working. Behavior-preserving:
handler bodies (including their lazy local imports) are moved verbatim.
"""
from __future__ import annotations

import argparse
import difflib
import os
import sys
from pathlib import Path

from mozyo_bridge.application.commands_common import (
    repo_root_from_args,
    scaffold_target_from_args,
)
from mozyo_bridge.scaffold.rules import (
    PORTABLE_HOME_EXPRESSION,
    install_rules,
    mozyo_bridge_home,
    render_scaffold_files,
    resolve_rules_store,
    rules_status,
    scaffold_status,
    write_scaffold,
)


def _rules_store_from_args(args: argparse.Namespace):
    """Resolve the rules store the CLI command should operate against.

    The CLI parser already enforces ``--home`` / ``--repo-local`` as a
    mutually exclusive group; this helper just translates whichever was
    supplied into a ``RulesStore`` so command bodies stay declarative.
    """
    home = getattr(args, "home", None)
    repo_local = getattr(args, "repo_local", None)
    return resolve_rules_store(home=home, repo_local=repo_local)


def cmd_rules_install(args: argparse.Namespace) -> int:
    store = _rules_store_from_args(args)
    written = install_rules(store=store)
    if written:
        for path in written:
            print(f"installed: {path}")
    else:
        print("rules: already up to date")
    return 0


def cmd_rules_status(args: argparse.Namespace) -> int:
    store = _rules_store_from_args(args)
    print("PRESET\tSTATUS\tINSTALLED\tPACKAGED\tPATH")
    ok = True
    for row in rules_status(store=store):
        print("\t".join([row["preset"], row["status"], row["installed"], row["packaged"], row["path"]]))
        if row["status"] != "ok":
            ok = False
    return 0 if ok else 1


def cmd_rules_home(args: argparse.Namespace) -> int:
    if getattr(args, "resolved", False):
        print(str(mozyo_bridge_home()))
    else:
        print(PORTABLE_HOME_EXPRESSION)
    return 0


def _skip_categories_from_args(args: argparse.Namespace) -> set[str]:
    """Collect skip-category flags off the parsed argparse namespace.

    Each `--skip-<category>` flag is namespaced as `skip_<category>` on
    the argparse object. We only forward labels for flags that are
    actually present *and* true, so callers building Namespace objects
    programmatically (tests, library entry points) don't have to know
    every flag name to opt in to the default behaviour.
    """
    labels: set[str] = set()
    if getattr(args, "skip_tmux_ui", False):
        labels.add("tmux-ui")
    if getattr(args, "skip_nagger", False):
        labels.add("nagger")
    return labels


def _with_categories_from_args(args: argparse.Namespace) -> set[str]:
    """Collect opt-in `--with-<category>` flags off the parsed namespace.

    Each `--with-<category>` flag is namespaced as `with_<category>` on
    the argparse object. Only labels for flags that are present *and*
    true are forwarded, so callers building Namespace objects
    programmatically (tests, library entry points) default to the
    opt-in-off behaviour without naming every flag.
    """
    labels: set[str] = set()
    if getattr(args, "with_worktree_runbook", False):
        labels.add("worktree-runbook")
    if getattr(args, "with_sublane_flow", False):
        labels.add("sublane-flow")
    return labels


def cmd_scaffold_apply(args: argparse.Namespace) -> int:
    home = Path(args.home).expanduser().resolve() if getattr(args, "home", None) else None
    repo_local = bool(getattr(args, "repo_local", False))
    target = scaffold_target_from_args(args)
    paths = write_scaffold(
        args.preset,
        target,
        dry_run=args.dry_run,
        backup=args.backup,
        force=args.force,
        home=home,
        repo_local=repo_local,
        skip_categories=_skip_categories_from_args(args),
        with_categories=_with_categories_from_args(args),
    )
    action = "would write" if args.dry_run else "wrote"
    for path in paths:
        print(f"{action}: {path}")
    return 0


def cmd_scaffold_diff(args: argparse.Namespace) -> int:
    """Print a unified diff of what ``scaffold apply <preset>`` would change.

    Compares each rendered router / manifest file against the on-disk
    content (treated as empty when missing). Returns 0 when the worktree
    already matches the rendered output and 1 when at least one file would
    change, mirroring ``git diff --exit-code`` so callers can gate.
    """
    home = Path(args.home).expanduser().resolve() if getattr(args, "home", None) else None
    repo_local = bool(getattr(args, "repo_local", False))
    target = scaffold_target_from_args(args)
    rendered = render_scaffold_files(
        args.preset,
        target,
        home=home,
        repo_local=repo_local,
        skip_categories=_skip_categories_from_args(args),
        with_categories=_with_categories_from_args(args),
    )
    any_changes = False
    for item in rendered:
        on_disk_path = target / item.path
        if on_disk_path.exists():
            current = on_disk_path.read_text(encoding="utf-8")
        else:
            current = ""
        if current == item.content:
            continue
        any_changes = True
        diff = difflib.unified_diff(
            current.splitlines(keepends=True),
            item.content.splitlines(keepends=True),
            fromfile=f"a/{item.path}",
            tofile=f"b/{item.path}",
        )
        for line in diff:
            print(line, end="" if line.endswith("\n") else "\n")
    if not any_changes:
        print(f"scaffold diff: clean ({args.preset} -> {target})")
        return 0
    return 1


def cmd_scaffold_status(args: argparse.Namespace) -> int:
    home = Path(args.home).expanduser().resolve() if getattr(args, "home", None) else None
    target = scaffold_target_from_args(args)
    status = scaffold_status(target, home=home)

    if getattr(args, "json", False):
        import json as _json

        print(_json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if status.get("clean") else 1

    print(f"target: {status['target']}")
    print(f"manifest: {status['manifest']}")
    if status["manifest"] != "present":
        if status["manifest"] == "missing":
            print(f"  no scaffold manifest at {status['manifest_path']}")
            print("  run `mozyo-bridge scaffold apply <preset>` first")
        elif status["manifest"] == "invalid":
            print(f"  manifest at {status['manifest_path']} is invalid")
            if "error" in status:
                print(f"  {status['error']}")
        return 1

    print(f"preset: {status['preset']}")
    print(f"schema_version: {status.get('schema_version')}")
    print(f"mode: {status.get('mode')}")
    print(f"rule_path: {status['rule_path']}")
    print(
        "central preset version: "
        f"manifest={status.get('manifest_preset_version')!r} "
        f"installed={status.get('installed_preset_version')!r}"
    )
    print(
        "central preset hash: "
        f"manifest={status.get('manifest_preset_hash')!r} "
        f"installed={status.get('installed_preset_hash')!r}"
    )
    print(f"central status: {status.get('central_status')}")
    # Manifest tracks every file scaffold writes — routers plus the
    # repo-local artifacts shipped by governed presets — so use a neutral
    # label rather than "router files:" which understated the scope.
    print("tracked files:")
    for row in status.get("files", []):
        print(f"  {row['path']}: {row['status']}")

    if status.get("clean"):
        print("result: clean")
        return 0

    print("result: drift detected")
    central_status = status.get("central_status")
    if central_status == "missing":
        if status.get("mode") == "repo-local":
            print(
                "  - repo-local preset is missing on disk; run "
                f"`mozyo-bridge rules install --repo-local {status['target']}`"
            )
        else:
            print("  - central preset is missing on disk; run `mozyo-bridge rules install`")
    elif central_status == "drifted-content":
        print("  - central preset content has changed since scaffold time")
        print(
            "    run `mozyo-bridge scaffold apply <preset> --backup` to regenerate routers,"
            " or `--force` to accept the new central preset"
        )
    elif central_status == "drifted-version":
        print("  - central preset version label changed since scaffold time")
    elif central_status == "ok-version-only":
        print(
            "  - manifest is schema v1 (no preset_hash); cannot detect content drift."
            " Regenerate the manifest by running `mozyo-bridge scaffold apply <preset> --backup` to upgrade."
        )
    for row in status.get("files", []):
        if row["status"] == "drifted":
            print(f"  - router {row['path']} was modified locally")
        elif row["status"] == "missing":
            print(f"  - router {row['path']} is missing on disk")
        elif row["status"] == "manifest-missing-hash":
            print(f"  - manifest entry for {row['path']} has no recorded hash")
    return 1


def _docs_context_from_args(args: argparse.Namespace):
    """Build a CatalogContext from argparse `--repo` / `--catalog` values.

    `--repo` defaults to cwd. The catalog defaults to the standard
    governed-preset path; the import stays local so the docs_tools
    package only gets pulled in when the operator uses a `docs ...`
    subcommand.
    """
    from mozyo_bridge.docs_tools import CatalogContext

    repo_raw = getattr(args, "repo", None) or os.getcwd()
    catalog_raw = getattr(args, "catalog", None)
    overlay_raw = getattr(args, "overlay", None)
    return CatalogContext.build(repo_raw, catalog_raw, overlay_raw)


def _docs_overlay_relpath(context, overlay_path) -> str:
    """Repo-relative overlay path for human-facing notices."""
    try:
        return overlay_path.relative_to(context.repo_root).as_posix()
    except ValueError:
        return overlay_path.as_posix()


def _docs_include_local(args: argparse.Namespace) -> bool:
    """Local overlay is merged by default; ``--no-local`` forces the
    public-only view (what a fresh clone / CI sees)."""
    return not bool(getattr(args, "no_local", False))


def cmd_docs_validate(args: argparse.Namespace) -> int:
    from mozyo_bridge.docs_tools import (
        validate_catalog,
        validate_file_coverage,
        validate_overlay,
    )

    context = _docs_context_from_args(args)
    errors = validate_catalog(
        context, strict_metadata=bool(getattr(args, "strict_metadata", False))
    )
    notices: list[str] = []
    if getattr(args, "check_file_coverage", False):
        coverage_errors, coverage_notices = validate_file_coverage(
            context, roots=getattr(args, "coverage_root", None)
        )
        errors.extend(coverage_errors)
        notices.extend(coverage_notices)
    if getattr(args, "include_local", False):
        overlay_errors = validate_overlay(context)
        if context.overlay_path.exists():
            notices.append(
                "local overlay validated: "
                f"{_docs_overlay_relpath(context, context.overlay_path)}"
            )
        else:
            notices.append("no local overlay present (catalog.local.yaml)")
        errors.extend(overlay_errors)
    for notice in notices:
        print(f"notice: {notice}")
    if errors:
        print("catalog validation failed")
        for error in errors:
            print(f"- {error}")
        return 1
    print("catalog validation passed")
    return 0


def cmd_docs_resolve(args: argparse.Namespace) -> int:
    from mozyo_bridge.docs_tools import (
        OverlayError,
        render_resolution_json,
        render_resolution_markdown,
        render_resolution_text,
        resolve_paths_detailed,
    )

    context = _docs_context_from_args(args)
    try:
        results, overlay = resolve_paths_detailed(
            context, list(args.paths), include_local=_docs_include_local(args)
        )
    except OverlayError as exc:
        print(f"local overlay error: {exc}", file=sys.stderr)
        return 1
    fmt = getattr(args, "format", "text")
    # The overlay notice goes to stderr so the json/markdown payload on
    # stdout stays machine-parseable.
    if overlay.applied:
        print(
            "notice: local overlay applied: "
            f"{_docs_overlay_relpath(context, overlay.path)} "
            f"({overlay.document_count} document(s), "
            f"{overlay.file_convention_count} file_convention(s))",
            file=sys.stderr,
        )
    if fmt == "json":
        print(render_resolution_json(results))
    elif fmt == "markdown":
        print(render_resolution_markdown(results))
    else:
        print(render_resolution_text(results))
    return 0


def cmd_docs_generate(args: argparse.Namespace) -> int:
    from mozyo_bridge.docs_tools import generate_file_conventions, run_generate_check

    context = _docs_context_from_args(args)
    output = getattr(args, "output", None)
    if getattr(args, "check", False):
        ok, output_path, detail = run_generate_check(context, output)
        if not ok:
            print(detail, file=sys.stderr)
            return 1
        print(detail)
        return 0
    output_path = generate_file_conventions(context, output)
    print(output_path.as_posix())
    return 0


def cmd_scaffold_canonical(args: argparse.Namespace) -> int:
    """Render or check the canonical-sourced scaffold artifacts.

    Operates on the mozyo-bridge source tree pointed at by ``--repo``
    (default cwd). ``--check`` re-renders every canonical source and
    fails on drift; without ``--check`` the rendered outputs are
    written to disk.
    """
    from mozyo_bridge.scaffold.canonical import (
        collect_render_results,
        write_render_results,
    )

    repo_root = repo_root_from_args(args)
    results = collect_render_results(repo_root)
    check_only = bool(getattr(args, "check", False))

    def _relative(path: Path) -> str:
        try:
            return path.relative_to(repo_root).as_posix()
        except ValueError:
            return path.as_posix()

    if check_only:
        drifted = [result for result in results if result.drift]
        if drifted:
            for result in drifted:
                print(
                    f"{_relative(result.output_path)} is {result.reason}; rerun "
                    f"`mozyo-bridge scaffold canonical` (without --check) to regenerate.",
                    file=sys.stderr,
                )
            return 1
        for result in results:
            print(f"{_relative(result.output_path)} is up to date")
        return 0

    written = write_render_results(results)
    for path in written:
        print(_relative(path))
    return 0


def cmd_docs_audit_impact(args: argparse.Namespace) -> int:
    from mozyo_bridge.docs_tools import (
        OverlayError,
        audit_doc_impact_detailed,
        run_generate_check,
    )

    context = _docs_context_from_args(args)
    try:
        results, overlay = audit_doc_impact_detailed(
            context,
            staged=bool(getattr(args, "staged", False)),
            all_changed=bool(getattr(args, "all_changed", False)),
            include_local=_docs_include_local(args),
        )
    except OverlayError as exc:
        print(f"local overlay error: {exc}", file=sys.stderr)
        return 1
    if overlay.applied:
        print(
            "notice: local overlay applied: "
            f"{_docs_overlay_relpath(context, overlay.path)} "
            f"({overlay.document_count} document(s), "
            f"{overlay.file_convention_count} file_convention(s))"
        )
    if not results:
        print("No changed paths.")
    for result in results:
        print(f"[{result['path']}]")
        documents = result["documents"]
        if documents:
            print("documents_to_read:")
            for document in documents:
                sources = ", ".join(document["sources"])
                print(
                    f"- {document['type']} {document['id']} -> {document['canonical_path']} (source: {sources})"
                )
        else:
            print("documents_to_read:")
            print("- none")
        notes = result["notes"]
        if notes:
            print("notes:")
            for note in notes:
                print(f"- {note}")
        print()
    if getattr(args, "check_generated", False):
        ok, _, detail = run_generate_check(
            context, getattr(args, "generated_output", None)
        )
        if not ok:
            print(detail, file=sys.stderr)
            return 1
        print(detail)
    return 0
