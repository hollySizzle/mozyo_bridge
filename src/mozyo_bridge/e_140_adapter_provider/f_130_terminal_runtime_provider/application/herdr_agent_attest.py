"""Startup self-attestation self-check, run BEFORE the provider exec (Redmine #13637).

The managed launch no longer execs the provider (``claude`` / ``codex``) directly.
It wraps it in this bounded self-check use-case (Design Answer j#76462 refinement 3):
the wrapper runs *as the herdr-spawned agent process*, so — and only here — it can
inspect its OWN ``os.environ`` and truthfully observe whether the identity triplet
herdr injected via ``--env`` actually reached the process the provider will inherit.
It records that observation as a generation-bound self-attestation
(:mod:`mozyo_bridge.core.state.herdr_identity_attestation`) and then ``exec``s the
provider, replacing itself — so from the provider's point of view nothing changed
except that its parent left a durable, honest record of its boot identity env.

Why a wrapper and not a launcher-side check: herdr exposes no surface returning a
launched process's env, and a live process's env cannot be mutated externally
(POSIX). The launcher can prove it *passed* ``--env`` but not that the value *landed*;
only the spawned process can read its own env. This is that read (j#76456
characterisation, j#76462 Answer).

Generation binding (refinement 2): the record pins the **live locator** the agent
resolves for itself here (``herdr agent list`` self-lookup), the only externally
observable discriminant a later adopt / doctor can compare against the live
inventory — so a stale record from a previous process generation is never re-used.

Non-blocking by contract: every step is best-effort. A failed self-lookup records an
empty locator (which the read side treats as ``stale`` — fail-closed), a failed
store write degrades to an absent record (fail-closed too), and NEITHER stops the
``exec``. Blocking the boot on a missing env would kill the operator's pane; the
adopt / doctor / send-time layers are where the fail-closed enforcement lives, not
here. This wrapper never sends, never mutates a workspace, never closes a pane.
"""

from __future__ import annotations

import argparse
import os
import subprocess
from typing import Callable, Mapping, Optional, Sequence

from mozyo_bridge.core.state.herdr_identity_attestation import (
    IdentityAttestationRecord,
    classify_identity_env,
    record_identity_attestation,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    AGENT_KEY_NAME,
    DEFAULT_LANE,
    _agent_locator,
    _norm,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_state import (
    _extract_list_rows,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (
    COMMAND_TIMEOUT_SECONDS,
    TerminalTransportError,
    resolve_herdr_binary,
)

#: The identity var whose value is the wrapper's own attest CLI flag, kept as a
#: literal so this module needs no dependency other than the domain / infra it
#: already uses for the live self-lookup.
_ATTEST_LIST_RETRIES = 3

#: A live ``agent list`` lister: returns raw herdr rows, or ``None`` on any failure.
Lister = Callable[[], Optional[Sequence[Mapping[str, object]]]]


def _own_locator(assigned_name: str, lister: Optional[Lister]) -> str:
    """Resolve THIS agent's live locator by self-lookup, ``""`` on any ambiguity.

    Runs the injected ``lister`` (``herdr agent list``) and returns the locator of
    the single row whose durable name equals ``assigned_name``. Zero rows (herdr has
    not surfaced the just-started agent yet), more than one (a duplicate name), an
    empty locator, or a lister failure all resolve to ``""`` — which the read side
    treats as ``stale`` / fail-closed. Never raises: a self-lookup problem must not
    stop the boot.
    """
    if lister is None:
        return ""
    try:
        rows = lister()
    except Exception:  # noqa: BLE001 — a self-lookup failure must never block exec
        return ""
    if not rows:
        return ""
    matches = [
        row
        for row in rows
        if isinstance(row, Mapping) and _norm(row.get(AGENT_KEY_NAME)) == assigned_name
    ]
    if len(matches) != 1:
        return ""
    return _norm(_agent_locator(matches[0]))


def perform_self_attestation(
    *,
    assigned_name: str,
    workspace_id: str,
    role: str,
    lane: str,
    env: Mapping[str, str],
    lister: Optional[Lister] = None,
    home=None,
    now: Optional[str] = None,
) -> IdentityAttestationRecord:
    """Observe this process's identity env + live locator; record it (best-effort).

    Pure over its inputs apart from the single best-effort store write: it classifies
    the ``env`` mapping (the caller passes ``os.environ``) against the
    launcher-expected identity, self-resolves the live locator via ``lister``, and
    upserts a generation-bound record. Returns the record (persisted form when the
    write succeeded, else the in-memory record) so a caller / test can assert on it.
    Never raises.
    """
    verdict, detail = classify_identity_env(
        expected_workspace_id=workspace_id,
        expected_role=role,
        expected_lane=lane,
        env=env,
    )
    locator = _own_locator(assigned_name, lister)
    record = IdentityAttestationRecord(
        assigned_name=assigned_name,
        workspace_id=_norm(workspace_id),
        role=_norm(role),
        lane_id=_norm(lane) or DEFAULT_LANE,
        locator=locator,
        verdict=verdict,
        detail=detail,
        observed_at=now,
    )
    persisted = record_identity_attestation(record, home=home)
    return persisted or record


def _live_lister(env: Mapping[str, str]) -> Lister:
    """Build a live ``herdr agent list`` lister with a small bounded retry.

    Resolves the herdr binary from the SAME trusted environment the rest of the
    herdr code uses (``MOZYO_HERDR_BINARY`` / trusted PATH, Redmine #13496 —
    injected onto this agent at launch), then runs ``agent list`` up to
    :data:`_ATTEST_LIST_RETRIES` times so a herdr-registration lag right after start
    still resolves the agent's own row. Returns ``None`` on unresolved binary /
    repeated failure (the caller records an empty locator — fail-closed).
    """

    def _list() -> Optional[Sequence[Mapping[str, object]]]:
        try:
            binary = resolve_herdr_binary(env).path
        except TerminalTransportError:
            return None
        last: Optional[Sequence[Mapping[str, object]]] = None
        for _ in range(_ATTEST_LIST_RETRIES):
            try:
                completed = subprocess.run(
                    [binary, "agent", "list"],
                    capture_output=True,
                    text=True,
                    timeout=COMMAND_TIMEOUT_SECONDS,
                    env=dict(env),
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if completed.returncode != 0:
                continue
            rows = _extract_list_rows(completed.stdout)
            if rows:
                return rows
            last = rows
        return last

    return _list


def cmd_herdr_agent_attest(args: argparse.Namespace) -> int:
    """CLI entry: self-attest this agent's boot identity env, then exec the provider.

    Reached only as the wrapped managed launch argv
    (``... herdr agent-attest --assigned-name <NAME> --workspace-id <WS>
    --role <PROVIDER> --lane <LANE> -- <provider argv...>``). Writes the
    self-attestation (best-effort) and then ``os.execvp``s the provider argv,
    replacing this process — so the provider is the real herdr pane occupant and
    inherits exactly the env this wrapper observed. A missing provider argv is the
    only hard error (nothing to exec); every attestation step is non-blocking.
    """
    provider_argv = list(getattr(args, "provider_argv", None) or [])
    # argparse REMAINDER keeps a leading ``--`` separator; drop it so argv[0] is the
    # provider executable.
    if provider_argv and provider_argv[0] == "--":
        provider_argv = provider_argv[1:]
    if not provider_argv:
        from mozyo_bridge.shared.errors import die

        die(
            "herdr agent-attest requires a provider command after `--` to exec "
            "(usage: herdr agent-attest --assigned-name ... -- <provider> [args...])"
        )
        raise AssertionError("unreachable")

    env = os.environ
    perform_self_attestation(
        assigned_name=_norm(getattr(args, "assigned_name", "")),
        workspace_id=_norm(getattr(args, "workspace_id", "")),
        role=_norm(getattr(args, "role", "")),
        lane=_norm(getattr(args, "lane", "")),
        env=env,
        lister=_live_lister(env),
    )
    os.execvp(provider_argv[0], provider_argv)
    raise AssertionError("unreachable")  # pragma: no cover - execvp replaces process


__all__ = (
    "Lister",
    "cmd_herdr_agent_attest",
    "perform_self_attestation",
)
