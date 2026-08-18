"""Action-time pre-mutation preflights for the herdr sublane actuator (Redmine #14258).

Every check the herdr creation-side ops adapter runs **before** the actuation touches
anything — no worktree, no pane, no dispatch — collected in one cohesive module and driven
by the adapter's thin port methods:

* :func:`evaluate_dispatch_sender` — the command shell must be the attested coordinator on
  the default lane (Redmine #13613), so a lane is never mutated by an unattested sender;
* :func:`evaluate_dispatch_sender_authority` — the same contract extended (Redmine #15706)
  with exactly one additional admission: a launch-time attested delegated_coordinator
  lane's gateway slot creating a child implementation lane under itself, verified against
  the lifecycle store / attestation store durable records (never the caller env alone);
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

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

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
    SENDER_GATEWAY_LIVE_AMBIGUOUS,
    SENDER_GATEWAY_LIVE_MISSING,
    SENDER_GATEWAY_LOCATOR_MISSING,
    SENDER_GATEWAY_UNATTESTED,
    SENDER_LANE_LIFECYCLE_UNREADABLE,
    SENDER_LANE_NOT_DELEGATED_COORDINATOR,
    SENDER_LANE_UNESTABLISHED,
    SENDER_PROVIDER_NOT_GATEWAY,
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


# --- delegated-gateway sender authority (Redmine #15706) ----------------------- #

#: The sender verified as the workspace's default-lane coordinator (the pre-#15706 pass).
SENDER_KIND_DEFAULT_COORDINATOR = "default_lane_coordinator"
#: The sender verified as the launch-time attested gateway slot of a delegated_coordinator
#: lane, creating a child implementation lane under itself.
SENDER_KIND_DELEGATED_GATEWAY = "delegated_coordinator_gateway"

# The typed refusal tokens for the delegated-gateway branch (closed vocabulary; every
# branch is typed and fail-closed — Redmine #15706 design constraint 4) are DEFINED in
# the domain blocked-reason vocabulary (review j#108076 finding_typedreasonprojection)
# so the actuation outcome can project each branch to an honest next action; they are
# imported at the top of this module and re-exported via ``__all__`` because this
# producer module is the established import site.


@dataclass(frozen=True)
class DispatchSenderAuthority:
    """The typed verdict of :func:`evaluate_dispatch_sender_authority`.

    ``ok`` / ``detail`` carry the exact contract :func:`evaluate_dispatch_sender`
    reports (byte-identical texts on every pre-#15706 branch). ``sender_kind`` names
    which authority admitted (:data:`SENDER_KIND_DEFAULT_COORDINATOR` /
    :data:`SENDER_KIND_DELEGATED_GATEWAY`; empty on refusal). ``parent_lane_id`` is the
    VERIFIED sender lane when the delegated-gateway branch admitted — the value the
    child lane's lifecycle declaration records as its parent binding — and empty
    otherwise, so a caller can never bind a parent the verdict did not verify.

    ``reason`` (review j#108076 finding_typedreasonprojection) is the STRUCTURED typed
    branch token of a delegated-gateway refusal — one of the ``SENDER_*`` vocabulary —
    carried as a field rather than only prose, so the actuation outcome can surface it
    in ``blocked_reasons`` and map it to an honest next action. It is EMPTY on success
    and on every pre-#15706 refusal (whose public projection stays byte-invariant).
    """

    ok: bool
    detail: str
    sender_kind: str = ""
    parent_lane_id: str = ""
    reason: str = ""


def evaluate_dispatch_sender_authority(
    env: Mapping[str, str],
    repo_root: Path,
    *,
    requested_lane_kind: str = "",
    lifecycle_record_reader: Optional[Callable[[str], object]] = None,
    gateway_provider_resolver: Optional[Callable[[], str]] = None,
    agent_rows_reader: Optional[Callable[[], Sequence[Mapping]]] = None,
    inventory_workspace_resolver: Optional[Callable[[], str]] = None,
    lane_target_resolver: Optional[Callable[..., object]] = None,
) -> DispatchSenderAuthority:
    """The #15706 sender-authority contract: coordinator pass OR delegated-gateway pass.

    Every pre-#15706 case reports the EXACT :func:`evaluate_dispatch_sender` outcome
    (same texts), so the default-lane coordinator path and every legacy refusal are
    byte-invariant. The single extension (Redmine #15706 design constraint 1): a
    NON-default-lane sender creating a CHILD IMPLEMENTATION lane
    (``requested_lane_kind == "implementation"``) is admitted iff durable records —
    never the caller env alone — verify it as the launch-time attested gateway slot of
    a ``delegated_coordinator`` lane:

    1. the lane lifecycle store holds an ACTIVE, positive-generation row for
       ``(workspace scope, sender lane)`` whose generation-bound ``lane_kind`` is
       ``delegated_coordinator`` (the durable geometry fact, #13647);
    2. the sender's provider is the workspace's configured GATEWAY provider (the
       delegated_coordinator lane's gateway slot, #13569);
    3. exactly one live, launch-time attested occupant of that ``(workspace, gateway
       provider, sender lane)`` slot exists — resolved through the coordinator proxy
       rail's own :func:`~...coordinator_proxy_send.resolve_proxy_target` policy
       (attestation store v4 join: identity + locator + terminal identity + verdict),
       not a second scan that could drift.

    A non-default-lane sender whose request is NOT a child implementation lane keeps
    the legacy refusal byte-for-byte (acceptance condition 2: the pre-#15706
    refusals are unchanged). Every new branch refuses with its own typed token; every
    read failure verifies nothing (fail-closed).

    The ``*_reader`` / ``*_resolver`` seams are test injections; ``None`` builds the
    live reads (lifecycle store / provider binding / herdr inventory).
    """
    try:
        anchor = read_anchor(repo_root)
    except Exception as exc:  # noqa: BLE001 — fail closed at the external read boundary.
        return DispatchSenderAuthority(False, f"workspace anchor unreadable ({exc})")
    anchor_workspace_id = _norm(
        anchor.get("workspace_id") if isinstance(anchor, Mapping) else ""
    )
    result = resolve_sender_identity(env, anchor_workspace_id=anchor_workspace_id or None)
    if not result.ok or result.identity is None:
        return DispatchSenderAuthority(False, f"{result.reason}: {result.detail}")
    try:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.main_lane_guard_gate import (  # noqa: E501
            resolve_coordinator_provider,
        )

        coordinator_provider = resolve_coordinator_provider(str(repo_root))
    except Exception as exc:  # noqa: BLE001 — config/binding IO is fail-closed here.
        return DispatchSenderAuthority(
            False, f"coordinator provider binding is unreadable ({exc})"
        )
    sender_lane = result.identity.lane_id
    provider_refusal = (
        f"sender provider {result.identity.role!r} is not the configured "
        f"coordinator provider {coordinator_provider!r}"
    )
    if sender_lane == DEFAULT_LANE:
        if result.identity.role != coordinator_provider:
            return DispatchSenderAuthority(False, provider_refusal)
        return DispatchSenderAuthority(
            True,
            "sender identity matches the coordinator binding and default lane",
            sender_kind=SENDER_KIND_DEFAULT_COORDINATOR,
        )

    from mozyo_bridge.core.state.lane_kind import (
        LANE_KIND_DELEGATED_COORDINATOR,
        LANE_KIND_IMPLEMENTATION,
    )

    if (requested_lane_kind or "").strip() != LANE_KIND_IMPLEMENTATION:
        # Not a child implementation lane: the extension does not apply and the
        # pre-#15706 evaluator's outcome is reproduced byte-for-byte INCLUDING its
        # precedence — the provider mismatch is reported BEFORE the lane mismatch
        # (acceptance condition 2; review j#108076 finding_legacyrefusalprecedence).
        if result.identity.role != coordinator_provider:
            return DispatchSenderAuthority(False, provider_refusal)
        return DispatchSenderAuthority(
            False,
            f"sender lane {sender_lane!r} is not the coordinator "
            f"default lane {DEFAULT_LANE!r}",
        )

    # 1. Durable geometry: the SENDER lane must be an active delegated_coordinator lane
    # of this workspace, read from the lifecycle authority (never the caller env).
    if lifecycle_record_reader is None:

        def lifecycle_record_reader(lane: str) -> object:
            from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore
            from mozyo_bridge.core.state.lane_lifecycle_model import LaneLifecycleKey
            from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
                repo_scope_workspace_id,
            )

            scope = repo_scope_workspace_id(Path(repo_root)) or ""
            if not scope:
                # An unresolved workspace scope cannot scope the authority read: report
                # UNREADABLE (fail-closed), never "no row" (which claims a resolved read).
                raise LookupError("workspace scope unresolved")
            return LaneLifecycleStore().get(LaneLifecycleKey(scope, lane))

    try:
        record = lifecycle_record_reader(sender_lane)
    except Exception:  # noqa: BLE001 — an unreadable lifecycle authority verifies nothing.
        return DispatchSenderAuthority(
            False,
            f"{SENDER_LANE_LIFECYCLE_UNREADABLE}: the lane lifecycle authority for "
            f"sender lane {sender_lane!r} could not be read",
            reason=SENDER_LANE_LIFECYCLE_UNREADABLE,
        )
    if record is None or getattr(record, "lane_disposition", "") != "active" or int(
        getattr(record, "lane_generation", 0) or 0
    ) <= 0:
        return DispatchSenderAuthority(
            False,
            f"{SENDER_LANE_UNESTABLISHED}: sender lane {sender_lane!r} owns no active "
            f"positive-generation lifecycle row in this workspace",
            reason=SENDER_LANE_UNESTABLISHED,
        )
    stored_kind = str(getattr(record, "lane_kind", "") or "")
    if stored_kind != LANE_KIND_DELEGATED_COORDINATOR:
        return DispatchSenderAuthority(
            False,
            f"{SENDER_LANE_NOT_DELEGATED_COORDINATOR}: sender lane {sender_lane!r} is "
            f"recorded with lane_kind {stored_kind!r}, not "
            f"{LANE_KIND_DELEGATED_COORDINATOR!r}",
            reason=SENDER_LANE_NOT_DELEGATED_COORDINATOR,
        )

    # 2. The gateway slot: the sender's provider must be the workspace's configured
    # gateway provider (the slot a delegated_coordinator lane's gateway runs as).
    if gateway_provider_resolver is None:

        def gateway_provider_resolver() -> str:
            from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
                resolve_gateway_provider,
            )

            return resolve_gateway_provider(str(repo_root))

    try:
        gateway_provider = _norm(gateway_provider_resolver())
    except Exception as exc:  # noqa: BLE001 — an unresolved gateway binding admits nothing.
        return DispatchSenderAuthority(
            False,
            f"{SENDER_PROVIDER_NOT_GATEWAY}: gateway provider unresolved ({exc})",
            reason=SENDER_PROVIDER_NOT_GATEWAY,
        )
    if not gateway_provider or result.identity.role != gateway_provider:
        return DispatchSenderAuthority(
            False,
            f"{SENDER_PROVIDER_NOT_GATEWAY}: sender provider {result.identity.role!r} "
            f"is not the configured gateway provider {gateway_provider!r}",
            reason=SENDER_PROVIDER_NOT_GATEWAY,
        )

    # 3. Launch-time attestation: exactly one live attested occupant of the sender
    # lane's gateway slot, through the coordinator proxy rail's own policy.
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.coordinator_proxy_send import (  # noqa: E501
        live_agent_rows,
        live_workspace_id,
        resolve_proxy_target,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.coordinator_proxy import (  # noqa: E501
        TARGET_AMBIGUOUS,
        TARGET_LOCATOR_MISSING,
        TARGET_MISSING,
        TARGET_OK,
        TARGET_UNATTESTED,
    )

    try:
        inventory_ws = _norm(
            inventory_workspace_resolver()
            if inventory_workspace_resolver is not None
            else live_workspace_id(repo_root)
        )
        rows = (
            agent_rows_reader() if agent_rows_reader is not None else live_agent_rows(env)
        )
        resolver = lane_target_resolver or resolve_proxy_target
        target = resolver(
            rows,
            workspace_id=inventory_ws,
            provider=gateway_provider,
            lane_id=sender_lane,
        )
    except Exception:  # noqa: BLE001 — an unreadable inventory attests nothing.
        return DispatchSenderAuthority(
            False,
            f"{SENDER_GATEWAY_UNATTESTED}: the live inventory / attestation join for "
            f"sender lane {sender_lane!r} could not be read",
            reason=SENDER_GATEWAY_UNATTESTED,
        )
    if target.status != TARGET_OK:
        refusal = {
            TARGET_MISSING: SENDER_GATEWAY_LIVE_MISSING,
            TARGET_AMBIGUOUS: SENDER_GATEWAY_LIVE_AMBIGUOUS,
            TARGET_LOCATOR_MISSING: SENDER_GATEWAY_LOCATOR_MISSING,
            TARGET_UNATTESTED: SENDER_GATEWAY_UNATTESTED,
        }.get(target.status, SENDER_GATEWAY_UNATTESTED)
        return DispatchSenderAuthority(
            False,
            f"{refusal}: sender lane {sender_lane!r} has no single live, launch-time "
            f"attested {gateway_provider!r} gateway slot (live={target.live} "
            f"with_locator={target.with_locator}"
            + (
                f" attestation={target.attestation_state}"
                f" ({target.attestation_reason})"
                if target.attestation_state
                else ""
            )
            + ")",
            reason=refusal,
        )
    return DispatchSenderAuthority(
        True,
        (
            f"sender is the launch-time attested gateway slot of delegated_coordinator "
            f"lane {sender_lane!r}, creating a child implementation lane under it"
        ),
        sender_kind=SENDER_KIND_DELEGATED_GATEWAY,
        parent_lane_id=sender_lane,
    )


def run_dispatch_sender_preflight(ops) -> tuple[bool, str]:
    """The ops adapter's sender gate (#13613), on the #15706 authority contract.

    The adapter's thin ``preflight_dispatch_sender`` delegate calls this (module-health
    leaf, like every other body in this module). It runs
    :func:`evaluate_dispatch_sender_authority` with the CREATE REQUEST's lane kind
    (``ops.lane_kind``) as the requested child kind, and stashes the admitted verdict's
    VERIFIED parent lane on ``ops.verified_parent_lane_id`` — cleared on every refusal
    and on the default-lane coordinator pass — so the child lane's lifecycle declaration
    and dispatch read the parent binding from the verdict, never from raw caller env.
    """
    verdict = evaluate_dispatch_sender_authority(
        ops.env, ops.repo_root, requested_lane_kind=ops.lane_kind
    )
    ops.verified_parent_lane_id = verdict.parent_lane_id if verdict.ok else ""
    # j#108076 finding_typedreasonprojection: the typed branch token survives to the
    # public blocked_reasons via this stash; empty on success and on legacy refusals.
    ops.dispatch_sender_refusal_reason = "" if verdict.ok else verdict.reason
    return verdict.ok, verdict.detail


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
            # Redmine #14756: `epoch_launch` is deliberately left at its default False here,
            # and that is a decision rather than an omission. This is the pre-worktree CREATE
            # gate: the lane it is about does not exist yet, so its lifecycle row has minted
            # no epoch and the launch it gates cannot carry one. `prepare_session` runs the
            # same conjunction again at step 2, by which point the row exists and the flag is
            # resolved from it — so an epoch-bearing launch is still refused before any herdr
            # write, just at the boundary that can actually observe one.
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
    "DispatchSenderAuthority",
    "SENDER_GATEWAY_LIVE_AMBIGUOUS",
    "SENDER_GATEWAY_LIVE_MISSING",
    "SENDER_GATEWAY_LOCATOR_MISSING",
    "SENDER_GATEWAY_UNATTESTED",
    "SENDER_KIND_DEFAULT_COORDINATOR",
    "SENDER_KIND_DELEGATED_GATEWAY",
    "SENDER_LANE_LIFECYCLE_UNREADABLE",
    "SENDER_LANE_NOT_DELEGATED_COORDINATOR",
    "SENDER_LANE_UNESTABLISHED",
    "SENDER_PROVIDER_NOT_GATEWAY",
    "evaluate_dispatch_sender",
    "evaluate_dispatch_sender_authority",
    "evaluate_launcher_compatibility",
    "evaluate_runtime_placement",
    "read_lane_target_config_text",
    "run_dispatch_sender_preflight",
)
