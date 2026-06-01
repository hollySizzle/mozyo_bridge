"""Opt-in instruction doctor for repo-local LLM runtime config.

`mozyo-bridge instruction doctor --target . --profile redmine-codex` is a
read-only check that a Redmine/Codex workspace actually carries the
repo-root runtime config the bootstrap docs require (Redmine #10814 /
#10821 / #10854). The startup docs ask agents to place
`<repo>/.codex/config.toml` (and, when used, `<repo>/.mcp.json`) at the
repo root, but an LLM can skim past prose; this command turns the
checklist into a machine-checkable, profile-aware result.

Design constraints (Redmine #10854):

- Opt-in only. The standard `mozyo-bridge doctor` is unchanged; this
  command is the thing a Redmine/Codex project runs deliberately. Other
  presets (Asana / Claude-only / none) are not failed here.
- Read-only. No network call to the Redmine MCP, no autogeneration, no
  autofix, no writes to home config.
- `.mcp.json` deferral is preserved: a runtime has not been verified to
  read the repo-root file, so its absence is informational, not a hard
  failure, and the command never declares it authoritative.
- Credential-shape values in either file are a hard failure, reusing the
  same heuristics as the workspace-defaults / release-tree gates.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from mozyo_bridge.shared.paths import resolve_repo_root
from mozyo_bridge.workspace_defaults import _is_secret_key, _value_looks_secret

# `tomllib` is stdlib on Python 3.11+. The package supports >=3.10, so fall
# back to the third-party `tomli` (same API) on 3.10. Resolved at import time
# so a genuinely missing TOML parser surfaces clearly rather than at call time.
try:  # Python 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # Python 3.10
    import tomli as _toml  # type: ignore[no-redef]

_TOMLDecodeError = _toml.TOMLDecodeError

# Status vocabulary for individual checks. `fail` drives a non-zero exit;
# `warn` / `info` are surfaced but do not fail the command (the `.mcp.json`
# deferral relies on this distinction).
STATUS_OK = "ok"
STATUS_FAIL = "fail"
STATUS_WARN = "warn"
STATUS_INFO = "info"

PROFILE_REDMINE_CODEX = "redmine-codex"
KNOWN_PROFILES = (PROFILE_REDMINE_CODEX,)

_REQUIRED_REDMINE_KEYS = (
    "default_project",
    "default_project_name",
    "default_project_url",
)


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def _scan_credentials(parsed: object, path: str) -> list[str]:
    """Return dotted paths whose key or string value looks like a secret.

    Walks a parsed TOML/JSON tree. Reuses the workspace-defaults
    credential-shape predicates so this command stays consistent with the
    release-tree hygiene gate instead of inventing a second heuristic.
    """
    findings: list[str] = []
    if isinstance(parsed, dict):
        for key, value in parsed.items():
            key_str = str(key)
            child = f"{path}.{key_str}" if path else key_str
            if _is_secret_key(key_str):
                findings.append(child)
            findings.extend(_scan_credentials(value, child))
    elif isinstance(parsed, list):
        for index, item in enumerate(parsed):
            findings.extend(_scan_credentials(item, f"{path}[{index}]"))
    elif isinstance(parsed, str) and _value_looks_secret(f"{path} = {parsed}"):
        findings.append(path)
    # De-duplicate while preserving first-seen order: a single field can match
    # both the secret-key-name and secret-value heuristics.
    seen: set[str] = set()
    deduped: list[str] = []
    for item in findings:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped


def _codex_config_checks(target: Path) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    config_path = target / ".codex" / "config.toml"
    rel = "<repo>/.codex/config.toml"

    if not config_path.exists():
        checks.append(
            _check(
                "codex_config_present",
                STATUS_FAIL,
                f"{rel} is missing; a Redmine/Codex workspace must declare the "
                "verified default project there (see bootstrap.md). Ask the "
                "operator before creating it; do not put it in a home config.",
            )
        )
        return checks
    checks.append(_check("codex_config_present", STATUS_OK, f"{rel} exists"))

    try:
        raw_bytes = config_path.read_bytes()
        parsed = _toml.loads(raw_bytes.decode("utf-8"))
    except (_TOMLDecodeError, UnicodeDecodeError) as exc:
        checks.append(
            _check("codex_config_parse", STATUS_FAIL, f"{rel} is not valid TOML: {exc}")
        )
        return checks
    checks.append(_check("codex_config_parse", STATUS_OK, f"{rel} parses as TOML"))

    # Credential-shape scan first: a leaked secret is the highest-severity
    # finding and must fail even if the structural keys are also wrong.
    cred = _scan_credentials(parsed, "")
    if cred:
        checks.append(
            _check(
                "codex_config_no_credentials",
                STATUS_FAIL,
                f"{rel} carries credential-shape value(s) at "
                f"{', '.join(sorted(cred))}; repo-local config must not hold "
                "tokens/keys/secrets — use user-level config or a secret store.",
            )
        )
    else:
        checks.append(
            _check(
                "codex_config_no_credentials",
                STATUS_OK,
                f"{rel} contains no credential-shape values",
            )
        )

    redmine = parsed.get("redmine")
    if not isinstance(redmine, dict):
        checks.append(
            _check(
                "codex_redmine_table",
                STATUS_FAIL,
                f"{rel} has no `[redmine]` table",
            )
        )
        redmine = {}
    else:
        checks.append(_check("codex_redmine_table", STATUS_OK, "[redmine] present"))

    for key in _REQUIRED_REDMINE_KEYS:
        value = redmine.get(key)
        if isinstance(value, str) and value.strip():
            checks.append(
                _check(f"codex_redmine_{key}", STATUS_OK, f"[redmine].{key} set")
            )
        else:
            checks.append(
                _check(
                    f"codex_redmine_{key}",
                    STATUS_FAIL,
                    f"[redmine].{key} is missing or empty",
                )
            )

    mcp_servers = parsed.get("mcp_servers")
    server = (
        mcp_servers.get("redmine_epic_grid")
        if isinstance(mcp_servers, dict)
        else None
    )
    if not isinstance(server, dict):
        checks.append(
            _check(
                "codex_mcp_server_table",
                STATUS_FAIL,
                f"{rel} has no `[mcp_servers.redmine_epic_grid]` table",
            )
        )
        return checks
    checks.append(
        _check(
            "codex_mcp_server_table",
            STATUS_OK,
            "[mcp_servers.redmine_epic_grid] present",
        )
    )

    url = server.get("url")
    if isinstance(url, str) and url.strip():
        checks.append(_check("codex_mcp_url", STATUS_OK, "redmine_epic_grid.url set"))
    else:
        checks.append(
            _check(
                "codex_mcp_url",
                STATUS_FAIL,
                "[mcp_servers.redmine_epic_grid].url is missing or empty",
            )
        )

    http_headers = server.get("http_headers")
    header_project = (
        http_headers.get("X-Default-Project")
        if isinstance(http_headers, dict)
        else None
    )
    if not (isinstance(header_project, str) and header_project.strip()):
        checks.append(
            _check(
                "codex_mcp_default_project_header",
                STATUS_FAIL,
                "[mcp_servers.redmine_epic_grid].http_headers.X-Default-Project "
                "is missing or empty",
            )
        )
        return checks
    checks.append(
        _check(
            "codex_mcp_default_project_header",
            STATUS_OK,
            "http_headers.X-Default-Project set",
        )
    )

    redmine_default = redmine.get("default_project")
    if isinstance(redmine_default, str) and redmine_default.strip():
        if header_project.strip() == redmine_default.strip():
            checks.append(
                _check(
                    "codex_default_project_consistent",
                    STATUS_OK,
                    "X-Default-Project matches [redmine].default_project",
                )
            )
        else:
            checks.append(
                _check(
                    "codex_default_project_consistent",
                    STATUS_FAIL,
                    "X-Default-Project "
                    f"({header_project!r}) does not match "
                    f"[redmine].default_project ({redmine_default!r}); the MCP "
                    "header and the declared default must agree.",
                )
            )
    # When [redmine].default_project is missing the dedicated check above
    # already fails; do not emit a redundant mismatch finding here.
    return checks


def _mcp_json_checks(target: Path) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    mcp_path = target / ".mcp.json"
    rel = "<repo>/.mcp.json"

    if not mcp_path.exists():
        # Deferral: no runtime has been verified to read the repo-root file,
        # so absence is informational, never a hard failure.
        checks.append(
            _check(
                "mcp_json_present",
                STATUS_INFO,
                f"{rel} not present; deferred and non-authoritative until a "
                "runtime is verified to read it (this is expected).",
            )
        )
        return checks

    checks.append(
        _check(
            "mcp_json_present",
            STATUS_INFO,
            f"{rel} present; treated as non-authoritative (deferral preserved) "
            "but scanned for parse errors and credential shapes.",
        )
    )
    try:
        parsed = json.loads(mcp_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        checks.append(
            _check("mcp_json_parse", STATUS_FAIL, f"{rel} is not valid JSON: {exc}")
        )
        return checks
    checks.append(_check("mcp_json_parse", STATUS_OK, f"{rel} parses as JSON"))

    cred = _scan_credentials(parsed, "")
    if cred:
        checks.append(
            _check(
                "mcp_json_no_credentials",
                STATUS_FAIL,
                f"{rel} carries credential-shape value(s) at "
                f"{', '.join(sorted(cred))}; repo-local config must not hold "
                "tokens/keys/secrets.",
            )
        )
    else:
        checks.append(
            _check(
                "mcp_json_no_credentials",
                STATUS_OK,
                f"{rel} contains no credential-shape values",
            )
        )
    return checks


def run_instruction_doctor(args: argparse.Namespace) -> dict[str, Any]:
    profile = getattr(args, "profile", PROFILE_REDMINE_CODEX) or PROFILE_REDMINE_CODEX
    # Match the rest of the CLI's repo resolution: explicit --target/--repo wins,
    # else MOZYO_REPO, else the nearest repo marker from cwd. This is what the
    # `--target` help text promises (Redmine #10854 review #52114 Finding 2).
    target = resolve_repo_root(getattr(args, "target", None))

    checks: list[dict[str, str]] = []
    if profile == PROFILE_REDMINE_CODEX:
        checks.extend(_codex_config_checks(target))
        checks.extend(_mcp_json_checks(target))
    else:  # pragma: no cover - argparse choices guards this
        checks.append(
            _check("profile", STATUS_FAIL, f"unknown profile {profile!r}")
        )

    ok = not any(c["status"] == STATUS_FAIL for c in checks)
    return {
        "ok": ok,
        "profile": profile,
        "target": str(target),
        "checks": checks,
    }


def format_instruction_doctor_text(result: dict[str, Any]) -> str:
    lines = [
        f"instruction doctor: {'ok' if result['ok'] else 'FAIL'} "
        f"profile={result['profile']} target={result['target']}",
    ]
    symbols = {
        STATUS_OK: "ok",
        STATUS_FAIL: "FAIL",
        STATUS_WARN: "warn",
        STATUS_INFO: "info",
    }
    for check in result["checks"]:
        sym = symbols.get(check["status"], check["status"])
        lines.append(f"  [{sym}] {check['name']}: {check['detail']}")
    if not result["ok"]:
        lines.append(
            "Read-only check failed. This command does not autofix; ask the "
            "operator to correct <repo>/.codex/config.toml and re-run."
        )
    return "\n".join(lines)
