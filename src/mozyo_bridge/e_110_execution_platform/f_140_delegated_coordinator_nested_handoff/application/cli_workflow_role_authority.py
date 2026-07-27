"""Public mint / readback surface for the durable workflow-role authority (Redmine #14546).

Increment 1 of #13583 gave the herdr default lane a durable role authority
(:mod:`...domain.workflow_role_authority`) stored in the repo-local static artifact
``.mozyo-bridge/workflow-role-bindings.json`` — but it gave no **product surface** to declare or
read one back. A bare ``mozyo`` fresh default pair therefore had no declarable authority at all:
the file simply did not exist, so ``workflow step`` fail-closed ``ambiguous_default_coordinator_role``
and the only "fix" was for an operator to hand-author JSON from a spec (Redmine #14500 observed
facts).

This module is that surface:

- **readback** (no flag) resolves the whole authority chain a restart / adopt has to recover from
  and reports each layer *separately*, so a reader can tell which one is missing: the repo-local
  declaration, the **registry + workspace anchor** (the workspace segment derived from the repo
  root — the same anchor the sender-identity gate attests against), the expected provider resolved
  out of band from ``provider_binding``, and the **live startup attestation** (the herdr inventory
  rows whose mzb1 assigned name decodes to this workspace's default lane at the expected provider).
  Each layer is reported as observed; a layer that cannot be read is reported as unavailable, never
  as satisfied.
- **mint** (``--mint-coordinator``) declares the single-workspace default ``coordinator`` binding.
  It is DRY-RUN by default (``--execute`` writes), idempotent for an identical existing binding,
  and **fail-closed** on a present-but-invalid declaration or a default lane already bound to a
  different role / scope — it never overwrites an authority it did not author. Every write is
  verified by a **readback**: the file is re-read and re-parsed after the write, and a declaration
  that does not parse back to the intended binding is reported as a failure rather than a success.

Boundaries this surface does NOT move: the declaration still stores no ``workspace_id`` literal, no
liveness / delivery / approval / locator (``vibes/docs/logics/managed-state-model.md`` repo-local
static-artifact boundary); the provider is still resolved separately from ``provider_binding`` (the
authority names a ROLE, so the default coordinator authority is provider-neutral); and minting an
authority is not attestation — a declared role still has to pass the action-time sender-identity /
provider cross-check before ``workflow step`` routes on it.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_role_authority import (
    BINDINGS_FILENAME,
    DEFAULT_LANE,
    DEFAULT_LANE_ROLES,
    SCHEMA_NAME,
    SCHEMA_VERSION,
    ParsedRoleBindings,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_runtime import (
    ROLE_COORDINATOR,
)

# ---------------------------------------------------------------------------
# Fixed outcome tokens (machine-readable; kept literal regardless of UI language).
# ---------------------------------------------------------------------------
#: The mint wrote (or would write, on a dry run) a fresh coordinator binding.
MINT_DECLARED = "declared"
#: The intended binding is already declared verbatim — an idempotent no-op, never a rewrite.
MINT_ALREADY_DECLARED = "already_declared"
#: The default lane already holds a different role / scope: fail closed, never overwrite.
MINT_BLOCKED_LANE_BOUND = "default_lane_already_bound"
#: The present declaration does not parse: fail closed rather than clobber an unreadable authority.
MINT_BLOCKED_DECLARATION_INVALID = "declaration_invalid"
#: The caller supplied no project scope (the coordinator binding requires one).
MINT_BLOCKED_SCOPE_REQUIRED = "project_scope_required"
#: The write landed but the readback did not reproduce the intended binding.
MINT_BLOCKED_READBACK_FAILED = "readback_failed"
#: The write itself failed (permissions / IO).
MINT_BLOCKED_WRITE_FAILED = "write_failed"

_BLOCKED_OUTCOMES = frozenset(
    {
        MINT_BLOCKED_LANE_BOUND,
        MINT_BLOCKED_DECLARATION_INVALID,
        MINT_BLOCKED_SCOPE_REQUIRED,
        MINT_BLOCKED_READBACK_FAILED,
        MINT_BLOCKED_WRITE_FAILED,
    }
)

#: Reported when a recovery layer could not be observed at all (never conflated with "absent").
LAYER_UNAVAILABLE = "unavailable"


# ---------------------------------------------------------------------------
# Recovery-layer probes. Each returns a plain dict; a probe that cannot read its layer says so.
# ---------------------------------------------------------------------------


def _declaration_layer(repo_root) -> dict:
    """The repo-local declaration layer: presence, parse status, and the parsed bindings."""
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_role_authority_source import (  # noqa: E501
        BINDINGS_RELATIVE_PATH,
        load_parsed_role_bindings,
        role_bindings_path,
    )

    path = role_bindings_path(repo_root)
    parsed = load_parsed_role_bindings(repo_root)
    return {
        # Repo-RELATIVE only: an absolute repo path must never land in a pasteable record.
        "path": str(BINDINGS_RELATIVE_PATH),
        "present": path.exists(),
        "parsed_ok": parsed.ok,
        "reason": parsed.reason,
        "detail": parsed.detail,
        "bindings": [
            {
                "role": b.role,
                "project_scope": b.project_scope,
                "lane_id": b.lane_id,
                "source_pointer": b.source_pointer,
            }
            for b in parsed.bindings
        ],
    }


def _workspace_anchor_layer(repo_root) -> dict:
    """The registry + workspace-anchor layer: the workspace segment derived from the repo root.

    This is the layer a reboot / adopt recovers **without** any launch env: the segment is derived
    from the checkout itself, so it is the anchor the sender-identity gate attests a lane against.
    An unresolvable segment is reported as :data:`LAYER_UNAVAILABLE`, never defaulted.
    """
    try:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
            herdr_workspace_segment,
        )

        segment = herdr_workspace_segment(repo_root) or ""
    except Exception:  # noqa: BLE001 - an unreadable registry is an unavailable layer, not an absence
        return {"workspace_id": "", "status": LAYER_UNAVAILABLE}
    if not segment:
        return {"workspace_id": "", "status": LAYER_UNAVAILABLE}
    return {"workspace_id": segment, "status": "resolved"}


def _expected_provider(repo_root, role: str) -> dict:
    """The provider ``provider_binding`` expects for ``role`` (resolved out of band, never stored)."""
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_binding_source import (  # noqa: E501
        load_workflow_binding,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding import (  # noqa: E501
        ROLE_COORDINATOR as BINDING_COORDINATOR,
        ROLE_PROJECT_GATEWAY as BINDING_PROJECT_GATEWAY,
        ROLE_ROOT_COORDINATOR as BINDING_ROOT_COORDINATOR,
    )
    from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (
        ROLE_GRANDPARENT_COORDINATOR,
    )

    key = {
        ROLE_GRANDPARENT_COORDINATOR: BINDING_ROOT_COORDINATOR,
        ROLE_COORDINATOR: BINDING_COORDINATOR,
    }.get(role, BINDING_PROJECT_GATEWAY)
    try:
        binding, _warnings = load_workflow_binding(repo_root)
    except Exception:  # noqa: BLE001 - a broken provider config cannot confirm a surface (R1)
        return {"provider": "", "status": LAYER_UNAVAILABLE, "binding_role": key}
    if binding is None:
        return {"provider": "", "status": LAYER_UNAVAILABLE, "binding_role": key}
    return {
        "provider": binding.provider_for(key) or "",
        "status": "resolved",
        "binding_role": key,
    }


def _live_attestation_layer(workspace_id: str, provider: str) -> dict:
    """The startup-attestation layer: live default-lane rows for this workspace + provider.

    Reads the herdr inventory and counts the rows whose **mzb1 assigned name** decodes to
    ``(workspace_id, provider, default lane)`` — the launch-time attestation a restart / adopt
    re-establishes. Reports the cardinality (0 / 1 / 2+) and whether the single row carries a
    usable locator, so a duplicate identity reads as ambiguity rather than as a target. An
    unreadable inventory is :data:`LAYER_UNAVAILABLE`.
    """
    if not workspace_id or not provider:
        return {"status": LAYER_UNAVAILABLE, "live": 0, "with_locator": 0}
    try:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
            list_herdr_agent_rows,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            AGENT_KEY_NAME,
            _agent_locator,
            _norm_lane,
            decode_assigned_name,
        )

        rows = list_herdr_agent_rows(os.environ)
    except Exception:  # noqa: BLE001 - an unreadable inventory is unavailable, never "absent"
        return {"status": LAYER_UNAVAILABLE, "live": 0, "with_locator": 0}

    live = 0
    with_locator = 0
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        decode = decode_assigned_name(row.get(AGENT_KEY_NAME))
        if not getattr(decode, "ok", False) or decode.identity is None:
            continue
        identity = decode.identity
        if identity.workspace_id != workspace_id or identity.role != provider:
            continue
        if _norm_lane(identity.lane_id) != DEFAULT_LANE:
            continue
        live += 1
        if _agent_locator(row):
            with_locator += 1
    if live == 1 and with_locator == 1:
        status = "attested"
    elif live == 0:
        status = "absent"
    elif live >= 2:
        status = "ambiguous"
    else:
        status = "locator_missing"
    return {"status": status, "live": live, "with_locator": with_locator}


def _default_lane_binding(parsed: ParsedRoleBindings):
    """The single binding that occupies the default lane, or ``None`` (pure over the parse)."""
    for binding in parsed.bindings:
        if binding.lane_id == DEFAULT_LANE:
            return binding
    return None


def resolve_role_authority_status(repo_root) -> dict:
    """The full readback: every recovery layer, reported separately (no layer is inferred).

    The layers are exactly what a restart / adopt has to re-establish before ``workflow step`` may
    route on a default-lane role: the durable **declaration**, the registry + **workspace anchor**,
    the **expected provider** from ``provider_binding``, and the **live startup attestation**. They
    are reported side by side so a caller can name which one is missing — collapsing them into one
    boolean is exactly what made the original failure unreadable (Redmine #14500).
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_role_authority_source import (  # noqa: E501
        load_parsed_role_bindings,
    )

    declaration = _declaration_layer(repo_root)
    anchor = _workspace_anchor_layer(repo_root)
    parsed = load_parsed_role_bindings(repo_root)
    binding = _default_lane_binding(parsed) if parsed.ok else None

    role = binding.role if binding is not None else ""
    provider = _expected_provider(repo_root, role) if role else {
        "provider": "", "status": LAYER_UNAVAILABLE, "binding_role": "",
    }
    attestation = _live_attestation_layer(
        anchor.get("workspace_id", ""), provider.get("provider", "")
    )
    return {
        "declaration": declaration,
        "workspace_anchor": anchor,
        "default_lane_role": role,
        "default_lane_project_scope": binding.project_scope if binding is not None else "",
        "expected_provider": provider,
        "live_attestation": attestation,
    }


# ---------------------------------------------------------------------------
# Mint.
# ---------------------------------------------------------------------------


def _declaration_record(parsed: ParsedRoleBindings) -> dict:
    """Rebuild the on-disk record shape from a parsed declaration (lane ids stay derived)."""
    entries = []
    for binding in parsed.bindings:
        entry: dict = {"role": binding.role}
        if binding.project_scope:
            entry["project_scope"] = binding.project_scope
        if binding.source_pointer:
            entry["source_pointer"] = binding.source_pointer
        entries.append(entry)
    return {"schema": SCHEMA_NAME, "version": SCHEMA_VERSION, "bindings": entries}


def plan_coordinator_mint(
    parsed: ParsedRoleBindings, *, project_scope: str, source_pointer: str
) -> dict:
    """Decide what a ``--mint-coordinator`` would do, purely over the parsed declaration.

    - a present-but-invalid declaration -> :data:`MINT_BLOCKED_DECLARATION_INVALID` (never clobber
      an authority that cannot be read: the operator has to see what is wrong first);
    - an empty project scope -> :data:`MINT_BLOCKED_SCOPE_REQUIRED`;
    - the default lane already bound to ``coordinator`` at the SAME scope ->
      :data:`MINT_ALREADY_DECLARED` (idempotent no-op — a re-mint is not a rewrite, so the existing
      ``source_pointer`` is preserved rather than silently replaced);
    - the default lane bound to any other role, or to ``coordinator`` at a different scope ->
      :data:`MINT_BLOCKED_LANE_BOUND` (one default lane holds one coordinator authority);
    - otherwise -> :data:`MINT_DECLARED` with the record that would be written.

    Pure: it decides, it does not write. The caller performs the write + readback.
    """
    scope = (project_scope or "").strip()
    if not parsed.ok:
        return {
            "outcome": MINT_BLOCKED_DECLARATION_INVALID,
            "detail": parsed.detail,
            "record": None,
        }
    if not scope:
        return {
            "outcome": MINT_BLOCKED_SCOPE_REQUIRED,
            "detail": (
                f"--project-scope is required to mint a {ROLE_COORDINATOR!r} binding; the scope is "
                "carried on the coordinator's one-step forward and partitions its forward fence, "
                "so it is declared explicitly rather than derived from display metadata"
            ),
            "record": None,
        }
    existing = _default_lane_binding(parsed)
    if existing is not None:
        if existing.role == ROLE_COORDINATOR and existing.project_scope == scope:
            return {
                "outcome": MINT_ALREADY_DECLARED,
                "detail": (
                    f"the default lane already declares {ROLE_COORDINATOR!r} at project scope "
                    f"{scope!r}; nothing to write"
                ),
                "record": None,
            }
        return {
            "outcome": MINT_BLOCKED_LANE_BOUND,
            "detail": (
                f"the default lane is already bound to role {existing.role!r} at project scope "
                f"{existing.project_scope!r}; a default lane holds exactly one coordinator "
                f"authority (declarable roles for this lane: {sorted(DEFAULT_LANE_ROLES)}). Edit "
                f"or remove the existing declaration deliberately — the mint never overwrites an "
                f"authority it did not author"
            ),
            "record": None,
        }
    record = _declaration_record(parsed)
    entry: dict = {"role": ROLE_COORDINATOR, "project_scope": scope}
    if (source_pointer or "").strip():
        entry["source_pointer"] = source_pointer.strip()
    record["bindings"] = list(record["bindings"]) + [entry]
    return {
        "outcome": MINT_DECLARED,
        "detail": (
            f"declare {ROLE_COORDINATOR!r} at project scope {scope!r} in the default lane"
        ),
        "record": record,
    }


def _write_declaration(path: Path, record: dict) -> Optional[str]:
    """Atomically write ``record`` as JSON; return an error string, or ``None`` on success."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(record, indent=2, ensure_ascii=False, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError as exc:
        # Path-free: a repo-local absolute path must never leak into a pasteable record.
        return f"{BINDINGS_FILENAME} could not be written ({type(exc).__name__})"
    return None


def _readback_matches(repo_root, *, project_scope: str) -> tuple[bool, str]:
    """Re-read + re-parse the declaration and confirm it yields the intended coordinator binding."""
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_role_authority_source import (  # noqa: E501
        load_parsed_role_bindings,
    )

    parsed = load_parsed_role_bindings(repo_root)
    if not parsed.ok:
        return False, f"the written declaration does not parse back: {parsed.detail}"
    binding = _default_lane_binding(parsed)
    if binding is None:
        return False, "the written declaration parses but binds no role to the default lane"
    if binding.role != ROLE_COORDINATOR or binding.project_scope != project_scope:
        return False, (
            f"the written declaration parses back to role {binding.role!r} at scope "
            f"{binding.project_scope!r}, not {ROLE_COORDINATOR!r} at {project_scope!r}"
        )
    return True, "readback verified"


def cmd_workflow_role_authority(args: argparse.Namespace) -> int:
    """``workflow role-authority``: mint / read back the durable workflow-role authority.

    With no mint flag this is a **read-only** readback of every recovery layer. With
    ``--mint-coordinator`` it declares the single-workspace default ``coordinator`` binding —
    DRY-RUN unless ``--execute`` is passed, idempotent for an identical binding, and fail-closed on
    an invalid declaration or an already-bound default lane. Returns 0 for a resolved readback / a
    declared-or-already-declared mint, 1 for a blocked mint or an unreadable declaration.
    """
    from mozyo_bridge.application.commands_common import repo_root_from_args
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_role_authority_source import (  # noqa: E501
        load_parsed_role_bindings,
        role_bindings_path,
    )

    repo_root = repo_root_from_args(args)
    as_json = bool(getattr(args, "as_json", False))

    if not getattr(args, "mint_coordinator", False):
        status = resolve_role_authority_status(repo_root)
        rc = 0 if status["declaration"]["parsed_ok"] else 1
        if as_json:
            print(json.dumps(status, ensure_ascii=False, sort_keys=True))
            return rc
        _print_status(status)
        return rc

    parsed = load_parsed_role_bindings(repo_root)
    scope = (getattr(args, "project_scope", "") or "").strip()
    pointer = (getattr(args, "source_pointer", "") or "").strip()
    plan = plan_coordinator_mint(parsed, project_scope=scope, source_pointer=pointer)
    execute = bool(getattr(args, "execute", False))

    result = {
        "outcome": plan["outcome"],
        "detail": plan["detail"],
        "executed": False,
        "project_scope": scope,
        "role": ROLE_COORDINATOR,
        "lane_id": DEFAULT_LANE,
    }
    if plan["outcome"] in _BLOCKED_OUTCOMES:
        return _emit_mint(result, as_json=as_json, rc=1)
    if plan["outcome"] == MINT_ALREADY_DECLARED or not execute:
        return _emit_mint(result, as_json=as_json, rc=0)

    error = _write_declaration(role_bindings_path(repo_root), plan["record"])
    if error is not None:
        result["outcome"] = MINT_BLOCKED_WRITE_FAILED
        result["detail"] = error
        return _emit_mint(result, as_json=as_json, rc=1)

    ok, detail = _readback_matches(repo_root, project_scope=scope)
    result["executed"] = True
    if not ok:
        result["outcome"] = MINT_BLOCKED_READBACK_FAILED
        result["detail"] = detail
        return _emit_mint(result, as_json=as_json, rc=1)
    result["detail"] = detail
    return _emit_mint(result, as_json=as_json, rc=0)


def _emit_mint(result: dict, *, as_json: bool, rc: int) -> int:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return rc
    print(f"workflow role-authority mint: {result['outcome']}")
    print(f"  role         : {result['role']} (lane {result['lane_id']})")
    print(f"  project_scope: {result['project_scope'] or '-'}")
    print(f"  executed     : {result['executed']}")
    print(f"  detail       : {result['detail']}")
    if not result["executed"] and result["outcome"] == MINT_DECLARED:
        print("  (dry run — re-run with --execute to write the declaration)")
    return rc


def _print_status(status: dict) -> None:
    declaration = status["declaration"]
    print(f"workflow role-authority: {declaration['path']}")
    print(f"  present   : {declaration['present']}")
    print(f"  parsed_ok : {declaration['parsed_ok']}")
    if not declaration["parsed_ok"]:
        print(f"  reason    : {declaration['reason']}")
        print(f"  detail    : {declaration['detail']}")
    for binding in declaration["bindings"]:
        print(
            f"  binding   : role={binding['role']} lane={binding['lane_id']} "
            f"scope={binding['project_scope'] or '-'} "
            f"source={binding['source_pointer'] or '-'}"
        )
    anchor = status["workspace_anchor"]
    print(f"  workspace anchor : {anchor['status']} ({anchor['workspace_id'] or '-'})")
    print(f"  default lane role: {status['default_lane_role'] or '-'}")
    provider = status["expected_provider"]
    print(
        f"  expected provider: {provider['status']} ({provider['provider'] or '-'}"
        f", binding role {provider['binding_role'] or '-'})"
    )
    attestation = status["live_attestation"]
    print(
        f"  live attestation : {attestation['status']} "
        f"(live={attestation['live']}, with_locator={attestation['with_locator']})"
    )


def register_role_authority_parser(workflow_sub) -> None:
    """Register ``workflow role-authority`` onto the ``workflow`` subparser group."""
    parser = workflow_sub.add_parser(
        "role-authority",
        description=(
            "Mint / read back the durable workflow-role authority for this workspace "
            "(Redmine #14546). With no flag it is a read-only readback of every layer a restart / "
            "adopt has to recover: the repo-local declaration, the registry + workspace anchor, the "
            "expected provider from provider_binding, and the live startup attestation — each "
            "reported separately so a missing layer is nameable. `--mint-coordinator "
            "--project-scope <scope>` declares the single-workspace default `coordinator` binding "
            "(DRY-RUN unless --execute). The mint is idempotent for an identical binding and fails "
            "closed on an unparseable declaration or a default lane already bound to another role."
        ),
        help="Mint / read back this workspace's durable workflow-role authority.",
    )
    parser.add_argument(
        "--mint-coordinator",
        dest="mint_coordinator",
        action="store_true",
        help="Declare the single-workspace default `coordinator` binding for the default lane.",
    )
    parser.add_argument(
        "--project-scope",
        dest="project_scope",
        default="",
        help="The coordinator binding's project scope (required with --mint-coordinator).",
    )
    parser.add_argument(
        "--source-pointer",
        dest="source_pointer",
        default="",
        help="Advisory durable pointer recorded on the binding (e.g. redmine:#14546). Review only "
             "— never a route authority.",
    )
    parser.add_argument(
        "--execute",
        dest="execute",
        action="store_true",
        help="Perform the mint write (without it, --mint-coordinator is a dry run).",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit exactly one structured envelope as JSON.",
    )
    parser.set_defaults(func=cmd_workflow_role_authority)


__all__ = (
    "MINT_DECLARED",
    "MINT_ALREADY_DECLARED",
    "MINT_BLOCKED_LANE_BOUND",
    "MINT_BLOCKED_DECLARATION_INVALID",
    "MINT_BLOCKED_SCOPE_REQUIRED",
    "MINT_BLOCKED_READBACK_FAILED",
    "MINT_BLOCKED_WRITE_FAILED",
    "LAYER_UNAVAILABLE",
    "plan_coordinator_mint",
    "resolve_role_authority_status",
    "cmd_workflow_role_authority",
    "register_role_authority_parser",
)
