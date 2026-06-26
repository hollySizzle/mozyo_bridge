"""Command handlers for the module-health family (Redmine #12321).

``mozyo-bridge health report`` prints per-module LOC / approximate complexity /
top-level symbol count for the runtime package, sorted largest-first.
``mozyo-bridge health check`` runs the oversized-module gate and exits non-zero
when a new oversized module appears or an allowlisted module grows past its
recorded baseline.

The handlers are thin: all measurement and gate logic lives in the pure
:mod:`mozyo_bridge.domain.module_health`; these handlers resolve the repo root
and config path, call the core, and render text or JSON — failing closed
(non-zero exit, no bare traceback) on a
:class:`~mozyo_bridge.domain.module_health.ModuleHealthError`, matching the
``state`` / ``doctor`` CLI convention.
"""

from __future__ import annotations

import argparse
import json as _json
import sys

from mozyo_bridge.domain.module_health import (
    GateResult,
    ModuleHealthError,
    default_config_path,
    evaluate,
    load_config,
)
from mozyo_bridge.shared.paths import resolve_repo_root


def _repo_root(args: argparse.Namespace):
    return resolve_repo_root(getattr(args, "repo", None))


def _config_path(args: argparse.Namespace, repo_root):
    return default_config_path(repo_root, getattr(args, "config", None))


def _print_json(payload: dict) -> None:
    print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def cmd_health_report(args: argparse.Namespace) -> int:
    """Per-module LOC / complexity / top-level symbol report (read-only)."""
    repo_root = _repo_root(args)
    config_path = _config_path(args, repo_root)
    try:
        config = load_config(config_path)
    except ModuleHealthError as exc:
        print(f"mozyo-bridge health: {exc}", file=sys.stderr)
        return 2

    result = evaluate(repo_root, config)
    metrics = result.metrics
    limit = getattr(args, "limit", None)
    if limit is not None and limit >= 0:
        metrics = metrics[:limit]

    if bool(getattr(args, "as_json", False)):
        _print_json(
            {
                "threshold": config.max_module_lines,
                "include": list(config.include),
                "module_count": len(result.metrics),
                "oversized_count": sum(
                    1 for m in result.metrics if m.lines > config.max_module_lines
                ),
                "modules": [m.as_dict() for m in metrics],
            }
        )
        return 0

    allowlisted = config.allowlist_by_path
    threshold = config.max_module_lines
    oversized = sum(1 for m in result.metrics if m.lines > threshold)
    print(
        f"module health: {len(result.metrics)} modules in scope "
        f"({', '.join(config.include)}), threshold {threshold} lines, "
        f"{oversized} oversized."
    )
    print(f"{'LINES':>6}  {'CX':>5}  {'SYMS':>5}  FLAG  PATH")
    for m in metrics:
        if m.lines > threshold:
            flag = "ALLOW" if m.path in allowlisted else "NEW!!"
        else:
            flag = "  ok "
        print(f"{m.lines:>6}  {m.complexity:>5}  {m.top_level_symbols:>5}  {flag}  {m.path}")
    return 0


def cmd_health_check(args: argparse.Namespace) -> int:
    """Run the oversized-module gate; exit 1 on any fatal violation."""
    repo_root = _repo_root(args)
    config_path = _config_path(args, repo_root)
    try:
        config = load_config(config_path)
    except ModuleHealthError as exc:
        print(f"mozyo-bridge health: {exc}", file=sys.stderr)
        return 2

    result = evaluate(repo_root, config)

    if bool(getattr(args, "as_json", False)):
        payload = result.as_dict()
        payload["threshold"] = config.max_module_lines
        _print_json(payload)
        return 0 if result.ok else 1

    return _render_check_text(result, config.max_module_lines)


def _render_check_text(result: GateResult, threshold: int) -> int:
    fatal = result.fatal_violations
    warnings = result.warnings
    oversized = sum(1 for m in result.metrics if m.lines > threshold)

    if result.ok:
        print(
            f"module-health gate OK: {len(result.metrics)} modules, "
            f"{oversized} allowlisted oversized, threshold {threshold} lines."
        )
        for warning in warnings:
            print(f"  warning [{warning.kind}]: {warning.message}", file=sys.stderr)
        return 0

    print(
        f"module-health gate FAILED: {len(fatal)} violation(s), "
        f"threshold {threshold} lines.",
        file=sys.stderr,
    )
    for violation in fatal:
        print(f"  FAIL [{violation.kind}]: {violation.message}", file=sys.stderr)
    for warning in warnings:
        print(f"  warning [{warning.kind}]: {warning.message}", file=sys.stderr)
    return 1
