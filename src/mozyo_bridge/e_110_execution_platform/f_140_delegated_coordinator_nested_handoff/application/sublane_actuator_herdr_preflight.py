"""Action-time pre-mutation preflights for the herdr sublane actuator (Redmine #14258).

Every check the herdr creation-side ops adapter runs **before** the actuation touches
anything — no worktree, no pane, no dispatch — collected in one cohesive module and driven
by the adapter's thin port methods:

* :func:`evaluate_dispatch_sender` — the command shell must be the attested coordinator on
  the default lane (Redmine #13613), so a lane is never mutated by an unattested sender;
* :func:`evaluate_runtime_placement` — the action-time runtime must not be a source /
  installed skew that would place the pair incorrectly (Redmine #13705 R1-F1);
* :func:`evaluate_launcher_compatibility` — the selected managed-launch launcher must be
  compatible with the authorities the lane will point it at: the wrapper subcommand and
  attestation schema (#13748 / #13847), the attestation store it will write (#13882), and the
  target repo config + shared lane lifecycle authority it must read (#14258).

They live here rather than on the adapter for two reasons. They are one *kind* of thing —
"the answer that must be known before the first mutation" — and each is pure orchestration
over injected collaborators, so they are testable without standing up the adapter. Carrying
them out also performs the reduction the adapter's ``module_health.yaml`` entry deferred:
with these three bodies here, the adapter is back under the module-health threshold and its
allowlist exception is gone rather than raised again.

Each returns a plain tuple rather than raising, because the use case turns a refusal into a
typed ``blocked`` outcome with zero actuation; an exception would have to be caught and
re-shaped at every call site. Every failure fails **closed**: an answer that cannot be
established is a refusal, never a pass.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping, Optional

from mozyo_bridge.core.state.workspace_registry import read_anchor
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_integration import (  # noqa: E501
    BLOB_ABSENT,
    BLOB_MAY_BE_TRANSFORMED,
    BLOB_NOT_REGULAR,
    BLOB_PRESENT,
    BLOB_TRANSFORM_UNKNOWN,
    BLOB_UNDECODABLE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
    REASON_LAUNCHER_INCOMPATIBLE,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    DEFAULT_LANE,
    _norm,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_target_resolution import (  # noqa: E501
    resolve_sender_identity,
)


def evaluate_dispatch_sender(
    env: Mapping[str, str], repo_root: Path
) -> tuple[bool, str]:
    """Verify the command-shell sender before any lane mutation (Redmine #13613).

    The sender must resolve to an attested identity whose workspace matches this repo's
    anchor, whose provider is the configured coordinator provider, and whose lane is the
    coordinator default lane. Any unreadable anchor / binding fails closed: a sender that
    cannot be established must not mutate a lane.
    """
    try:
        anchor = read_anchor(repo_root)
    except Exception as exc:  # noqa: BLE001 — fail closed at the external read boundary.
        return False, f"workspace anchor unreadable ({exc})"
    anchor_workspace_id = _norm(
        anchor.get("workspace_id") if isinstance(anchor, Mapping) else ""
    )
    result = resolve_sender_identity(env, anchor_workspace_id=anchor_workspace_id or None)
    if not result.ok or result.identity is None:
        return False, f"{result.reason}: {result.detail}"
    try:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.main_lane_guard_gate import (  # noqa: E501
            resolve_coordinator_provider,
        )

        coordinator_provider = resolve_coordinator_provider(str(repo_root))
    except Exception as exc:  # noqa: BLE001 — config/binding IO is fail-closed here.
        return False, f"coordinator provider binding is unreadable ({exc})"
    if result.identity.role != coordinator_provider:
        return False, (
            f"sender provider {result.identity.role!r} is not the configured "
            f"coordinator provider {coordinator_provider!r}"
        )
    if result.identity.lane_id != DEFAULT_LANE:
        return False, (
            f"sender lane {result.identity.lane_id!r} is not the coordinator "
            f"default lane {DEFAULT_LANE!r}"
        )
    return True, "sender identity matches the coordinator binding and default lane"


def evaluate_runtime_placement(
    repo_root: Path, fingerprint_reader: Optional[Callable[[], dict]] = None
) -> tuple[bool, str]:
    """Action-time runtime fingerprint gate — the mutation front door (#13705 R1-F1).

    Verifies BEFORE any worktree / lane side effect that the action-time runtime is not a
    source/installed skew that would place the gateway/worker pair incorrectly. Reads a
    :func:`run_runtime_fingerprint` result (active loaded package vs repo-local source) and
    blocks ONLY when the active runtime is missing the same-tab placement behavior the source
    ships — the exact skew that split the #13441 lane (the pure
    :func:`evaluate_mutation_placement_gate` policy).

    This is the achievable authority the R1 review asked for: the OFFICIAL mutating front
    door goes zero-write on an installed/source fingerprint mismatch, detected by a REAL
    active-vs-source probe (not a hard-coded capability). It cannot stop a runtime that
    predates all fence code (no code we ship runs there); that residual is closed by the
    #13524 reinstall fingerprint gate. A run with no repo-local source to compare is
    unverifiable and allowed (again the reinstall gate covers it). Any read failure fails
    closed (a fingerprint that cannot be established must not greenlight a mutation).
    """
    from mozyo_bridge.application.doctor_runtime import evaluate_mutation_placement_gate

    reader = fingerprint_reader
    if reader is None:
        import argparse

        from mozyo_bridge.application.doctor_runtime import run_runtime_fingerprint

        def reader() -> dict:
            return run_runtime_fingerprint(argparse.Namespace(repo=str(repo_root)))

    try:
        fingerprint = reader()
    except Exception as exc:  # noqa: BLE001 — an unresolvable fingerprint fails closed.
        return False, (
            f"the action-time runtime fingerprint could not be established ({exc}); "
            "refuse to actuate a lane from a runtime of unverifiable provenance "
            "(Redmine #13705)"
        )
    return evaluate_mutation_placement_gate(fingerprint)


def read_lane_target_config_text(
    committed_blob: Callable[..., tuple[str, str]],
    *,
    base_commit: str,
    lane_runtime_root: str,
    from_base_ref: bool,
) -> tuple[str, Optional[str]]:
    """The exact config document the LANE will present: ``(state, text)`` (Redmine #14258).

    Not this checkout's config — the lane's. Two sources, decided by the caller from the
    launch decision rather than guessed here:

    - ``from_base_ref`` (a worktree this run will CREATE): the blob at ``base_commit``, which
      the caller has already pinned to an immutable full commit SHA (review j#87746 R1). A
      *ref* would not do: a branch that advances between this read and ``git worktree add``
      makes the verified bytes and the materialized bytes different documents — measured, a
      v2 config verified and a v99 config materialized;
    - otherwise (a reused worktree, or a non-git lane that runs in the workspace root):
      ``lane_runtime_root``'s own file, because that directory already exists and IS what the
      wrapper will be given as ``--cwd``.

    A base whose blob cannot be read yields :data:`CONFIG_TEXT_UNREADABLE`, which fails the
    join closed — nothing about the lane's config is knowable, and it must never collapse into
    the "absent" case that admits every launcher.
    """
    from mozyo_bridge.application.repo_local_config_loader import CONFIG_FILE_RELPATH
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
        CONFIG_TEXT_ABSENT,
        CONFIG_TEXT_NOT_REGULAR,
        CONFIG_TEXT_TRANSFORM_UNKNOWN,
        CONFIG_TEXT_TRANSFORM_UNVERIFIABLE,
        CONFIG_TEXT_UNDECODABLE,
        CONFIG_TEXT_UNREADABLE,
        classify_config_text,
        read_target_config_text,
    )

    if not from_base_ref:
        root = (lane_runtime_root or "").strip()
        if not root:
            return CONFIG_TEXT_UNREADABLE, None
        return read_target_config_text(Path(root))
    pinned = (base_commit or "").strip()
    if not pinned:
        return CONFIG_TEXT_UNREADABLE, None
    state, text = committed_blob(ref=pinned, relpath=str(CONFIG_FILE_RELPATH))
    if state == BLOB_ABSENT:
        return CONFIG_TEXT_ABSENT, None
    if state == BLOB_MAY_BE_TRANSFORMED:
        # A checkout conversion applies to the path: the document may be entirely valid, so
        # this must NOT be reported as a broken config (consultation j#87807). It travels as
        # its own state all the way to the public refusal.
        return CONFIG_TEXT_TRANSFORM_UNVERIFIABLE, None
    if state == BLOB_UNDECODABLE:
        return CONFIG_TEXT_UNDECODABLE, None
    if state == BLOB_TRANSFORM_UNKNOWN:
        # Unknown, not observed (j#87811): the public cause must not claim a conversion.
        return CONFIG_TEXT_TRANSFORM_UNKNOWN, None
    if state == BLOB_NOT_REGULAR:
        # A symlink's object is its target string and a submodule's is a commit id, so neither
        # is the document a checkout writes. Same class as the transform case, and reported
        # with its own cause rather than as a broken config (j#87809).
        return CONFIG_TEXT_NOT_REGULAR, None
    if state != BLOB_PRESENT:
        return CONFIG_TEXT_UNREADABLE, None
    # The SAME "declares nothing" authority the session-start path uses (#14258 R14), so a
    # comment-only config cannot be absent on one path and present on the other.
    return classify_config_text(text)


def evaluate_launcher_compatibility(
    *,
    env: Mapping[str, str],
    runner,
    timeout: float,
    repo_root: Path,
    store_home: Path,
    replacement_action_id: str,
    committed_blob: Callable[..., tuple[str, str]],
    base_commit: str,
    lane_runtime_root: str,
    from_base_ref: bool,
) -> tuple[bool, str, str]:
    """Verify the managed-launch launcher BEFORE the first worktree / process (#14258).

    Returns ``(ok, reason_token, detail)``. The launcher compatibility conjunction already
    runs inside :func:`prepare_session`, but that is reached at *step 2* of the create
    actuation — **after** ``git worktree add``. The measured failure (#14258 j#85834) is
    exactly that shape: the worktree was created, then both slots exited because the selected
    launcher could not parse the lane's v2 config, leaving a worktree behind and a pair in
    ``provider_exited / rollback_owed``. Running the same conjunction here makes the refusal a
    genuine zero-mutation one, which is the issue's first close condition.

    Read-only throughout: one capability probe (``--help``, which short-circuits before any
    actuation), two ``git`` reads, and two read-only store probes. An unwrapped launch (no
    attest launcher resolved) has no launcher to verify and is admitted unchanged — the
    #13637 byte-invariant fallback.
    """
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
        HerdrSessionStartError,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_argv import (  # noqa: E501
        resolve_attest_launcher,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
        HerdrLauncherIncompatibleError,
        measure_config_parse_compatibility,
        preflight_launcher_compatibility,
    )

    launcher = resolve_attest_launcher(env)
    if not launcher:
        return True, "", "unwrapped launch; no managed-launch launcher to verify"
    config_state, config_text = read_lane_target_config_text(
        committed_blob,
        base_commit=base_commit,
        lane_runtime_root=lane_runtime_root,
        from_base_ref=from_base_ref,
    )
    config_parse = measure_config_parse_compatibility(
        launcher, runner, timeout, env, config_state, config_text
    )
    # Probe in the wrapper's OWN cwd whenever that directory already exists (#14231: a
    # launcher's exit code is cwd-sensitive, so probing elsewhere hides a skew). For a
    # worktree this run will CREATE it does not exist yet, so the probe falls back to this
    # checkout — and the config axis does not depend on that cwd at all, because it was
    # already settled above by running the launcher's own parser against the exact target
    # bytes (`measure_config_parse_compatibility`). That is a direct measurement, not a
    # declaration join: no summary of the grammar can answer it (#14258 j#87752 R4).
    lane_dir = Path(lane_runtime_root) if (lane_runtime_root or "").strip() else None
    probe_cwd = lane_dir if lane_dir is not None and lane_dir.is_dir() else repo_root
    try:
        preflight_launcher_compatibility(
            launcher,
            runner,
            timeout,
            env,
            repo_root=probe_cwd,
            store_home=Path(store_home),
            replacement_launch=bool((replacement_action_id or "").strip()),
            config_parse=config_parse,
        )
    except HerdrLauncherIncompatibleError as exc:
        return False, exc.reason, str(exc)
    except HerdrSessionStartError as exc:
        # A mechanical probe failure (launcher unrunnable / timed out) is not a typed
        # capability verdict, but it is still a refusal to launch — reported as one rather
        # than letting an unverified launcher through.
        return False, REASON_LAUNCHER_INCOMPATIBLE, str(exc)
    return True, "", "the selected managed-launch launcher is compatible with this target"


__all__ = (
    "evaluate_dispatch_sender",
    "evaluate_launcher_compatibility",
    "evaluate_runtime_placement",
    "read_lane_target_config_text",
)
