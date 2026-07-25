"""herdr command invocation + workspace/tab creation & root-pane reclaim.

The third cohesive sibling of :mod:`herdr_session_start`, alongside
:mod:`herdr_lane_topology` (the pure placement / parse decision core) and
:mod:`herdr_launch_argv` (the pure ``agent start`` argv assembly). Where those two are
pure, this module owns the session-start flow's **side-effecting herdr commands**: the one
fail-closed command runner every step shares (:func:`_invoke`), the ``agent list`` read
(:func:`_list_rows`), and the empty-base-pane / tab lifecycle helpers
(:func:`_create_workspace`, :func:`_create_tab`, :func:`_close_base_pane`).

Homing them here — the extraction the ``module_health.yaml`` allowlist entry for
``herdr_session_start`` pre-declared ("a further reduction could extract the empty-base-pane
/ tab reclaim helpers if this grows"), carried out when Redmine #13646 added the
config-driven ``lane_placement`` axis — keeps the session-start composition root focused on
classification / placement / reclaim *orchestration* and drops it back under the
module-health threshold.

The reclaim contract is unchanged (Redmine #13330 / #13411): a workspace / tab this run
**creates** is the only reclaim target, so its empty root pane is a *known* handle rather
than one scanned for (a user's own shell can never be mis-closed); a create that returns an
unparseable payload fails closed rather than guessing a pane; and a ``pane close`` failure
is recorded non-fatally (the agents are already live, an empty root pane is only cosmetic).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Mapping, Optional, Sequence

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_state import (
    _extract_list_rows,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (
    Runner,
    _bounded_detail,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (
    HerdrSessionStartError,
    _parse_tab_created,
    _parse_workspace_created,
    _parse_workspace_list,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_argv import (
    CONFIG_PARSE_REJECTED_EXIT,
    MOZYO_BRIDGE_LAUNCHER_ENV,
    build_attest_capability_probe_argv,
    build_config_parse_probe_argv,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launcher_capability import (
    CONFIG_PARSE_BOTH_OK,
    CONFIG_PARSE_CONTRACT_VERSION,
    CONFIG_PARSE_LAUNCHER_REJECTED,
    CONFIG_PARSE_LAUNCHER_UNUSABLE,
    CONFIG_PARSE_SELF_REJECTED,
    CONFIG_PARSE_TARGET_ABSENT,
    TARGET_SCHEMA_ABSENT,
    TARGET_SCHEMA_DECLARED,
    TARGET_SCHEMA_UNREADABLE,
    TARGET_SCHEMA_UNSUPPORTED,
    ConfigParseObservation,
    LauncherCapabilityObservation,
    TargetSchemaObservation,
    decide_config_parse_compatibility,
    decide_launcher_capability,
    decide_lifecycle_reader_compatibility,
    decide_store_compatibility,
    parse_launcher_capability_output,
)
from mozyo_bridge.core.state.herdr_identity_attestation import (
    HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION,
    herdr_identity_attestation_path,
)
from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
    probe_store_schema,
)


class HerdrLauncherIncompatibleError(HerdrSessionStartError):
    """The probed managed-launch launcher is executable but capability-incompatible.

    Redmine #13847: a subclass of :class:`HerdrSessionStartError` so every existing
    caller that fails closed on a session-start error still does — but a caller that must
    surface a *typed* ``launcher_runtime_incompatible`` blocker (the ``sublane
    create/start`` path) can catch this specifically. Raised only for a launcher that ran
    the probe but failed the capability contract (wrong / absent ``agent-attest``
    subcommand, or an attestation-store schema that does not match the source runtime);
    a mechanical failure to run the probe stays a plain :class:`HerdrSessionStartError`.
    """

    def __init__(self, message: str, *, reason: str):
        super().__init__(message)
        #: The :mod:`herdr_launcher_capability` verdict reason token (for the typed blocker).
        self.reason = reason


def _invoke(
    binary: str,
    tail: Sequence[str],
    runner: Runner,
    timeout: float,
    *,
    env: Optional[Mapping[str, str]],
) -> "subprocess.CompletedProcess[str]":
    """Run ``binary tail...`` fail-closed; raise on any mechanical / non-zero failure."""
    argv = [binary, *tail]
    try:
        completed = runner(
            argv, capture_output=True, text=True, timeout=timeout, env=env
        )
    except FileNotFoundError:
        raise HerdrSessionStartError(f"herdr binary not found: {binary!r}")
    except subprocess.TimeoutExpired:
        raise HerdrSessionStartError(f"herdr command timed out: {list(tail)!r}")
    except OSError as exc:
        raise HerdrSessionStartError(
            f"herdr command failed ({exc.__class__.__name__}): {list(tail)!r}"
        )
    if completed.returncode != 0:
        raise HerdrSessionStartError(
            _bounded_detail(completed.stderr)
            or f"herdr {list(tail)!r} exited {completed.returncode}"
        )
    return completed


def preflight_attest_launcher_capability(
    launcher: str,
    runner: Runner,
    timeout: float,
    env: Mapping[str, str],
    repo_root=None,
) -> LauncherCapabilityObservation:
    """Fail closed unless ``launcher`` can run the ``herdr agent-attest`` wrapper.

    Redmine #13748. The #13637 managed launch execs every provider THROUGH
    ``<launcher> herdr agent-attest ...`` so the agent self-attests its injected
    identity env before the provider starts. :func:`resolve_attest_launcher` verifies
    the launcher is an *executable* but NOT that its CLI still carries that subcommand:
    an installed launcher can lag unreleased source (measured — installed
    ``mozyo-bridge 0.10.0`` answers the wrapper subcommand with argparse ``invalid
    choice`` / exit 2 while the source tree succeeds), so every wrapped pane exits ~0.4 s
    after start, ``sublane create`` returns a live locator, and the lane then vanishes —
    the failure this preflight closes.

    The probe (:func:`build_attest_capability_probe_argv`) runs the subcommand with
    ``--help``, which dispatches and short-circuits before any actuation (no attestation
    write, no provider exec, no pane). It is invoked HERE — before the caller's first
    ``workspace`` / ``tab`` / ``agent`` write — so an executable-but-incapable launcher
    aborts the run with zero side effect.

    A positive verdict requires an exit code of 0 AND — decided purely in
    :mod:`herdr_launcher_capability` from the probe output — the ``agent-attest``
    subcommand marker AND an advertised attestation-store schema that exactly matches this
    runtime's (Redmine #13847). The exit code alone is not proof: a success-exit
    non-launcher (e.g. ``/usr/bin/true``) ignores the args and exits 0 without the
    subcommand, then the real launch — which runs the SAME launcher as the wrapper's
    ``argv[0]`` — would still exit before ``exec``ing the provider, the exact vanishing
    lane this closes. The **schema** check is what #13847 adds over the #13748
    subcommand-only check: an older installed launcher carries ``agent-attest`` +
    ``--assigned-name`` (so the subcommand marker passes) but its attestation store is a
    stale schema; injected with this runtime's shared ``MOZYO_BRIDGE_HOME`` it opens the
    newer store, hits the exact-version write guard, silently drops the attestation, and
    the pair boots **live but unattested** — no public recovery, the failure #13847 closes.

    This function is the **probe adapter** only: it runs the subprocess, then delegates
    the verdict to the pure :func:`parse_launcher_capability_output` /
    :func:`decide_launcher_capability` (probe / decision separation, #13847 item 6). A
    capability-verdict failure raises the typed :class:`HerdrLauncherIncompatibleError`
    (carrying the verdict reason) so a caller can surface a typed
    ``launcher_runtime_incompatible`` blocker; a mechanical failure to run the probe stays
    a plain :class:`HerdrSessionStartError`. The error names the launcher path, the
    required command, and the two recovery actions (release/install a capable
    ``mozyo-bridge``, or pin an explicit absolute :data:`MOZYO_BRIDGE_LAUNCHER_ENV`); it is
    raised, never written to a durable store, so no personal path is persisted.

    Only an executable-but-incapable launcher reaches here. An unresolvable launcher is
    already ``""`` (wrapping disabled — the byte-invariant #13637 fallback), and the
    caller only probes when a wrapper will actually run (a launch plan under a resolved
    launcher); an adopt-only / dry-run session starts no wrapped process and is never
    probed.

    Returns the parsed :class:`LauncherCapabilityObservation` (Redmine #13882) so the
    caller can feed the same probe into :func:`preflight_attest_store_schema` without
    re-running the subprocess. Every check here remains **code vs code**; the store join
    is the separate step.
    """
    recovery = (
        f" Recovery: install or release a mozyo-bridge whose CLI has `herdr agent-attest` "
        f"at attestation schema v{HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION}, or set "
        f"{MOZYO_BRIDGE_LAUNCHER_ENV} to an absolute launcher that has it."
    )
    probe = build_attest_capability_probe_argv(launcher)
    # Redmine #14231 (root cause j#84906, disposition j#84910): the probe MUST run with the
    # same cwd the real wrapper gets. `build_agent_start_argv` passes `--cwd <repo_root>` to
    # `herdr agent start`, so the wrapper process starts inside the lane worktree — and a
    # mozyo-bridge CLI reads that directory's `.mozyo-bridge/config.yaml` at startup. A
    # launcher predating a config schema bump therefore exits non-zero THERE while exiting 0
    # in a config-less directory. Probing in the caller's cwd made that skew invisible:
    # measured on one binary, exit 0 from a config-less cwd and exit 2 from the v2 lane cwd,
    # so the probe passed and only the wrapper died — the vanishing pair of #14222 j#84620.
    # `None` keeps the caller's cwd (the pre-#14231 shape) for callers that have no repo
    # root to point at.
    try:
        completed = runner(
            probe,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=dict(env),
            cwd=str(repo_root) if repo_root else None,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HerdrSessionStartError(
            f"managed-launch preflight could not run launcher {launcher!r} to verify it "
            f"provides the required `herdr agent-attest` wrapper subcommand "
            f"({exc.__class__.__name__}); refusing to launch a provider whose "
            f"self-attestation wrapper may exit before the provider starts." + recovery
        ) from exc
    if completed.returncode != 0:
        raise HerdrSessionStartError(
            f"selected managed-launch launcher {launcher!r} cannot run the required "
            f"`herdr agent-attest` wrapper subcommand (probe exited "
            f"{completed.returncode}); its self-attestation wrapper would exit before the "
            f"provider starts, leaving a partial / immediately-vanishing lane." + recovery
        )
    # Exit 0 is necessary but NOT sufficient: the pure decision requires the subcommand
    # marker AND an exactly-matching advertised attestation schema (#13847). A success-exit
    # non-launcher lacks the marker; an older installed launcher carries the marker but
    # advertises a stale (or no) schema — both fail closed here, before any launch.
    output = (completed.stdout or "") + (completed.stderr or "")
    observation = parse_launcher_capability_output(output)
    verdict = decide_launcher_capability(
        observation,
        required_schema_version=HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION,
    )
    if not verdict.ok:
        raise HerdrLauncherIncompatibleError(
            f"selected managed-launch launcher {launcher!r} exited 0 for the "
            f"`herdr agent-attest` probe but {verdict.detail}; refusing to launch a "
            f"provider whose self-attestation would be dropped, leaving a partial / "
            f"immediately-vanishing or live-but-unattested lane." + recovery,
            reason=verdict.reason,
        )
    return observation


def preflight_attest_store_schema(
    observation: LauncherCapabilityObservation,
    *,
    store_home: Path,
    replacement_launch: bool = False,
) -> None:
    """Fail closed unless the SELECTED home store's real shape can be attested into.

    Redmine #13882 — the check the #13847 capability preflight structurally cannot make.
    That one joins the launcher's *advertised* schema against this runtime's *required*
    schema; both are **code**, so two v2 runtimes agree, the probe passes, and nothing
    ever opens the store that will actually be written. The measured failure: a shared
    ``MOZYO_BRIDGE_HOME`` holding the pre-0.12 v1 shape. The launch is wrapped with
    ``--env MOZYO_BRIDGE_HOME=<store_home>``, the child's best-effort write hits the store
    guard, swallows the error (an agent boot must never be blocked by a store failure),
    and the pair boots **live but unattested** with every downstream verify failing closed.

    Store-side only, and read-only: it probes the store as it lies and **never migrates**
    (a launch that silently migrated the shared home would break every older installed
    launcher the same way — see ``herdr_identity_attestation_schema``). Called next to the
    launcher probe, i.e. before the caller's first ``workspace`` / ``tab`` / ``agent``
    write, so an incompatible store aborts with zero herdr side effect (acceptance 1).

    A v1 store with a **normal** launch is admitted: the launcher writes it v1-shaped and
    ``replacement_action_id`` is empty, so nothing is dropped and no generation is
    fabricated. A **replacement** launch is refused there, because that field cannot
    survive the v1 shape. The error is raised, never persisted, so no personal path is
    written to a durable store.
    """
    verdict = decide_store_compatibility(
        observation,
        probe_store_schema(herdr_identity_attestation_path(Path(store_home))),
        required_schema_version=HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION,
        replacement_launch=replacement_launch,
    )
    if not verdict.ok:
        raise HerdrLauncherIncompatibleError(
            f"managed-launch preflight refused the selected attestation store: "
            f"{verdict.detail}. No workspace / tab / agent was created.",
            reason=verdict.reason,
        )


#: The filename the probe document is materialized under. The launcher parses whatever path
#: it is given, but keeping the canonical basename makes any error it prints recognizable.
_CONFIG_PROBE_BASENAME = "config.yaml"

#: The target declares no config document at all — nothing for any launcher to parse.
CONFIG_TEXT_ABSENT = "config_text_absent"
#: The exact document the lane will present was obtained.
CONFIG_TEXT_PRESENT = "config_text_present"
#: A document is there but this runtime could not obtain its bytes (unreadable file, an
#: unresolvable ref). Deliberately NOT folded into "absent": absent admits every launcher,
#: and a document nobody can read must never do that.
CONFIG_TEXT_UNREADABLE = "config_text_unreadable"


def measure_config_parse_compatibility(
    launcher: str,
    runner: Runner,
    timeout: float,
    env: Mapping[str, str],
    config_state: str,
    config_text: Optional[str],
) -> ConfigParseObservation:
    """Make BOTH parsers read the exact target bytes and record what each said (#14258 R4).

    The direct measurement that replaced an advertised summary of the config grammar. The
    summary was measured insufficient: commit ``d28e59e2`` added the nested
    ``lane_placement.by_lane_kind`` key while leaving the supported version set and the
    top-level key set untouched, so a launcher predating it advertised an identical contract
    and still rejected the config. No enumeration of the grammar can answer "can THIS
    launcher read THAT config" — only the launcher's own parser can, so it is asked.

    ``config_text`` is the exact document the lane will present (``None`` when the target
    declares none). It is written to a private temporary file, because the candidate launcher
    is a separate process and, for a lane this run will create, the bytes come from a commit
    rather than any file on disk. The file is created inside a fresh ``0700`` temp directory
    and removed on every path — the target config is committed, non-secret content, but a
    preflight still leaves nothing behind.

    Both sides are read-only: this runtime's loader parses in-process, and the launcher runs
    its ``config check-parse`` (which parses and returns without writing). A launcher that
    cannot be run, or that answers with an exit code outside the contract, yields
    :data:`CONFIG_PARSE_LAUNCHER_UNUSABLE` — an unanswerable probe proves nothing.
    """
    import tempfile

    from mozyo_bridge.application.repo_local_config_loader import (
        RepoLocalConfigError,
        load_repo_local_config_from_path,
    )

    if config_state == CONFIG_TEXT_ABSENT:
        return ConfigParseObservation(CONFIG_PARSE_TARGET_ABSENT)
    if config_state != CONFIG_TEXT_PRESENT or config_text is None:
        # A document exists but its bytes are unobtainable, so neither parser can be asked.
        # This runtime cannot read it either, which is exactly the "config defect, not a
        # launcher skew" classification — never an admit.
        return ConfigParseObservation(
            CONFIG_PARSE_SELF_REJECTED,
            "the target repo's config document exists but its bytes could not be read "
            "(unreadable file, undecodable content, or an unresolvable base ref)",
        )
    with tempfile.TemporaryDirectory(prefix="mozyo-config-parse-") as scratch:
        probe_path = Path(scratch) / _CONFIG_PROBE_BASENAME
        probe_path.write_text(config_text, encoding="utf-8")
        try:
            load_repo_local_config_from_path(probe_path)
        except RepoLocalConfigError as exc:
            # This runtime rejects it too: a config defect, not a launcher skew. Reported as
            # such so an operator is never told to upgrade a launcher over their own config.
            return ConfigParseObservation(
                CONFIG_PARSE_SELF_REJECTED, _bounded_detail(str(exc))
            )
        argv = build_config_parse_probe_argv(launcher, str(probe_path))
        try:
            completed = runner(
                argv, capture_output=True, text=True, timeout=timeout, env=dict(env)
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return ConfigParseObservation(
                CONFIG_PARSE_LAUNCHER_UNUSABLE,
                f"could not run the launcher's config validator ({exc.__class__.__name__})",
            )
        if completed.returncode == 0:
            return ConfigParseObservation(CONFIG_PARSE_BOTH_OK)
        detail = _bounded_detail(completed.stderr) or _bounded_detail(completed.stdout)
        if completed.returncode == CONFIG_PARSE_REJECTED_EXIT:
            return ConfigParseObservation(CONFIG_PARSE_LAUNCHER_REJECTED, detail)
        # Any other code is outside the contract (an old CLI's `invalid choice`, a crash, a
        # signal): it is NOT evidence that the config is bad, so it must not be reported as
        # a rejection — it is an unusable answer.
        return ConfigParseObservation(
            CONFIG_PARSE_LAUNCHER_UNUSABLE,
            f"validator exited {completed.returncode}"
            + (f": {detail}" if detail else ""),
        )


def read_target_config_text(repo_root) -> tuple[str, Optional[str]]:
    """The exact config document the directory ``repo_root`` presents: ``(state, text)``.

    ``state`` is one of :data:`CONFIG_TEXT_ABSENT` / :data:`CONFIG_TEXT_PRESENT` /
    :data:`CONFIG_TEXT_UNREADABLE`. The three are kept distinct on purpose: "absent" admits
    every launcher (there is nothing to parse), so a document that merely *could not be read*
    must never collapse into it — a missing file and an unreadable one are different facts,
    and only one of them is safe.

    An empty / comment-only document is absent: it declares nothing, so any launcher's
    ``RepoLocalConfig.default()`` handles it. Imports are local so this provider module does
    not pull the config loader (and its YAML dependency) in at import time.
    """
    from mozyo_bridge.application.repo_local_config_loader import CONFIG_FILE_RELPATH

    if repo_root is None:
        return CONFIG_TEXT_ABSENT, None
    path = Path(repo_root) / CONFIG_FILE_RELPATH
    if not path.exists():
        return CONFIG_TEXT_ABSENT, None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return CONFIG_TEXT_UNREADABLE, None
    return (CONFIG_TEXT_PRESENT, text) if text.strip() else (CONFIG_TEXT_ABSENT, None)


def _observe_shared_lifecycle_schema(store_home: Path) -> TargetSchemaObservation:
    """Probe the SHARED lane lifecycle authority's schema, normalized for the join (#14258)."""
    from mozyo_bridge.core.state.lane_lifecycle_readonly import (
        LIFECYCLE_SCHEMA_ABSENT,
        LIFECYCLE_SCHEMA_RECOGNIZED,
        LIFECYCLE_SCHEMA_UNSUPPORTED,
        probe_lane_lifecycle_schema,
    )

    probe = probe_lane_lifecycle_schema(home=Path(store_home))
    state = {
        LIFECYCLE_SCHEMA_ABSENT: TARGET_SCHEMA_ABSENT,
        LIFECYCLE_SCHEMA_RECOGNIZED: TARGET_SCHEMA_DECLARED,
        LIFECYCLE_SCHEMA_UNSUPPORTED: TARGET_SCHEMA_UNSUPPORTED,
    }.get(probe.state, TARGET_SCHEMA_UNREADABLE)
    return TargetSchemaObservation(
        state, probe.version, upgrade_required=probe.upgrade_required
    )


def preflight_launcher_target_authorities(
    observation: LauncherCapabilityObservation,
    *,
    config_parse: ConfigParseObservation,
    store_home: Path,
) -> None:
    """Fail closed unless the launcher can READ the authorities it will be pointed at.

    Redmine #14258 — the third and last conjunct of the managed-launch compatibility
    boundary, and the one the earlier two structurally could not make. #13748 / #13847 prove
    the launcher *carries* the wrapper subcommand and *advertises* the attestation schema;
    #13882 joins that against the real store it will *write*. All three are about one
    authority. But the launcher must also **read** two authorities it never writes, and a
    skew in either kills the lane after it has been created:

    - the TARGET repo's ``.mozyo-bridge/config.yaml``: the wrapper starts with
      ``--cwd <lane worktree>`` and a mozyo-bridge CLI parses that config at startup, so a
      launcher predating a schema bump exits before ``exec``ing the provider (measured:
      ``unknown key 'agents'`` / exit 2 against the v2 config, #14258 j#85834 — the pair
      booted, both slots reported ``provider_exited / rollback_owed``, and the worktree was
      already there);
    - the HOME-SCOPED SHARED lane lifecycle authority: it is additively migrated by whichever
      lane's source CLI is newest, and a launcher whose reader is older zero-starts the named
      lane with ``LaneLifecycleReaderUpgradeRequired`` (measured: a v7 store against a v6
      reader, #14258 j#85890).

    Both are *declaration* joins rather than exit-code observations. That is deliberate and is
    what #14231 could not give: probing in the lane's own cwd catches a config skew only
    because the launcher happens to read config at startup and happens to exit non-zero, and
    only once the lane worktree exists to be probed in. A declared capability can be checked
    **before the worktree is created**, which is what lets ``sublane create`` refuse without
    leaving one behind.

    Read-only on both authorities and never migrating either — a launch that migrated the
    shared home would break every older installed launcher the same way. Called on the same
    boundary as its two siblings, i.e. before the caller's first ``workspace`` / ``tab`` /
    ``agent`` write, so an incompatible launcher aborts with zero herdr side effect. The error
    is raised, never persisted, and names no absolute path.

    ``config_parse`` is supplied by the caller rather than measured here, because the two
    callers obtain *the same document* from different places: session-start reads the lane
    worktree's own file, while the ``sublane create`` gate must answer before that worktree
    exists and reads the committed blob at the lane's pinned base commit instead. Both are
    the exact bytes the launcher will face; neither is a proxy for the other.
    """
    for verdict in (
        decide_config_parse_compatibility(
            observation,
            config_parse,
            required_contract_version=CONFIG_PARSE_CONTRACT_VERSION,
        ),
        decide_lifecycle_reader_compatibility(
            observation, _observe_shared_lifecycle_schema(store_home)
        ),
    ):
        if not verdict.ok:
            raise HerdrLauncherIncompatibleError(
                f"managed-launch preflight refused the selected launcher: {verdict.detail}. "
                f"No workspace / tab / agent was created.",
                reason=verdict.reason,
            )


def preflight_launcher_compatibility(
    launcher: str,
    runner: Runner,
    timeout: float,
    env: Mapping[str, str],
    *,
    repo_root=None,
    store_home: Path,
    replacement_launch: bool = False,
    config_parse: Optional[ConfigParseObservation] = None,
) -> LauncherCapabilityObservation:
    """The WHOLE managed-launch compatibility conjunction, in one place (Redmine #14258).

    Every conjunct a selected launcher must satisfy before anything is created, in
    fail-closed order: the wrapper subcommand + advertised attestation schema (#13748 /
    #13847), the real attestation store it will write (#13882), and the target authorities it
    must read — the repo's config record and the shared lane lifecycle store (#14258).

    It exists because the conjunction has **two** call sites with different reach, and a
    conjunct present at only one of them is a gap that reappears as a live failure. The
    ``sublane create`` gate runs it before ``git worktree add``, so a skewed launcher leaves
    no worktree behind; ``prepare_session`` runs it again before the first herdr write, which
    is the only boundary reachable when a session starts without going through create (a heal,
    an explicit ``herdr session-start``). Both read-only and idempotent, so running the
    conjunction twice costs one extra probe and changes nothing.

    ``config_parse`` defaults to the document ``repo_root`` presents — the wrapper's own
    ``--cwd``. A caller that must answer before that directory exists passes the measurement
    it made for the target instead (see :func:`preflight_launcher_target_authorities`).
    """
    observation = preflight_attest_launcher_capability(
        launcher, runner, timeout, env, repo_root=repo_root
    )
    preflight_attest_store_schema(
        observation, store_home=Path(store_home), replacement_launch=replacement_launch
    )
    if config_parse is None:
        state, text = read_target_config_text(repo_root)
        config_parse = measure_config_parse_compatibility(
            launcher, runner, timeout, env, state, text
        )
    preflight_launcher_target_authorities(
        observation, config_parse=config_parse, store_home=Path(store_home)
    )
    return observation


def _list_rows(binary: str, runner: Runner, timeout: float) -> Sequence[Mapping[str, object]]:
    """Run herdr ``agent list`` and return raw rows (fail-closed)."""
    completed = _invoke(binary, ["agent", "list"], runner, timeout, env=None)
    rows = _extract_list_rows(completed.stdout)
    if rows is None:
        raise HerdrSessionStartError(
            "herdr agent list payload was not a recognised JSON array or agents object"
        )
    return rows


def _list_workspace_labels(
    binary: str, runner: Runner, timeout: float
) -> Optional[Mapping[str, str]]:
    """Run herdr ``workspace list`` and return ``{workspace_id: label}`` (or ``None``).

    The backend-readable label authority for the shared coordinators space
    (Redmine #14139 ``shared_space``, Design Answer j#83385 Decision 1). Called ONLY
    on the shared-space default-lane path, so ``per_project_space`` and every
    sublane launch never issue this extra command and stay byte-for-byte the
    pre-#14139 choreography. Returns ``None`` — "labels unreadable" — when the
    payload is not a recognisable ``workspace list`` shape, which the resolver
    treats as fail-closed (never a guessed shared space).
    """
    completed = _invoke(binary, ["workspace", "list"], runner, timeout, env=None)
    return _parse_workspace_list(completed.stdout)


def _create_workspace(
    binary: str,
    repo_root: Path,
    runner: Runner,
    timeout: float,
    env: Mapping[str, str],
    label: str = "",
) -> tuple[str, str]:
    """Explicitly create a herdr workspace; return ``(workspace_id, root_pane_id)``.

    Making the workspace ourselves (rather than letting the first ``agent start``
    auto-create it) is what turns the empty base pane into a *known* handle we can
    reclaim by id — never one we scan for. ``--no-focus`` avoids stealing the
    operator's focus. ``label`` names a minted workspace for the operator: for a
    **sublane host** (Redmine #13380) it is cosmetic and never a join key; for the
    **shared coordinators space** (Redmine #14139) the SAME label
    (``coordinators``) is the backend-readable adopt authority a later project
    re-reads via ``workspace list`` (:func:`_shared_coordinator_target`). Either
    way the label is set once at create and never mutated. Fails closed if the
    response is unparseable.
    """
    argv = ["workspace", "create", "--cwd", str(repo_root)]
    if label:
        argv.extend(["--label", label])
    argv.append("--no-focus")
    completed = _invoke(
        binary,
        argv,
        runner,
        timeout,
        env=dict(env),
    )
    parsed = _parse_workspace_created(completed.stdout)
    if parsed is None:
        raise HerdrSessionStartError(
            "herdr workspace create returned no parseable workspace id / root pane "
            "(expected result.workspace.workspace_id + result.root_pane.pane_id in a "
            "workspace_created payload); refuse to guess a pane to reclaim"
        )
    return parsed


def _create_tab(
    binary: str,
    workspace_id: str,
    runner: Runner,
    timeout: float,
    env: Mapping[str, str],
    label: str = "",
) -> tuple[str, str]:
    """Explicitly create a herdr tab in ``workspace_id``; return ``(tab_id, root_pane_id)``.

    Lane=tab subdivision (Redmine #13411): a non-default lane gets its OWN tab in
    the sublane host workspace, its gateway + worker placed as a split pair inside
    it. Minting the tab ourselves turns its empty root pane into a *known* handle
    to reclaim by id (the tab analogue of the #13330 workspace base pane), never
    one we scan for. ``--label`` (the lane label) is cosmetic and operator-readable
    only — every join decision keys on the live ``tab_id``, never the label.
    ``--no-focus`` avoids stealing the operator's focus. Fails closed if the
    response is unparseable.
    """
    argv = ["tab", "create", "--workspace", workspace_id]
    if label:
        argv.extend(["--label", label])
    argv.append("--no-focus")
    completed = _invoke(binary, argv, runner, timeout, env=dict(env))
    parsed = _parse_tab_created(completed.stdout)
    if parsed is None:
        raise HerdrSessionStartError(
            "herdr tab create returned no parseable tab id / root pane "
            "(expected result.tab.tab_id + result.root_pane.pane_id in a "
            "tab_created payload); refuse to guess a pane to reclaim"
        )
    return parsed


def _close_base_pane(
    binary: str,
    pane_id: str,
    runner: Runner,
    timeout: float,
    env: Mapping[str, str],
) -> tuple[bool, str]:
    """Reclaim a created root pane; **never hard-fail** (cosmetic residue only).

    Used for both the #13330 workspace base pane and the #13411 lane tab root
    pane. Returns ``(True, "")`` on a clean close, else ``(False, <detail>)``. A
    failed reclaim only leaves the harmless empty root pane behind — the agent
    slots are already live — so it is recorded, not raised (Redmine #13330 ruling
    j#73225).
    """
    try:
        _invoke(binary, ["pane", "close", pane_id], runner, timeout, env=dict(env))
    except HerdrSessionStartError as exc:
        return False, _bounded_detail(str(exc)) or "herdr pane close failed"
    return True, ""


__all__ = (
    "HerdrLauncherIncompatibleError",
    "_close_base_pane",
    "_create_tab",
    "_create_workspace",
    "_invoke",
    "_list_rows",
    "CONFIG_TEXT_ABSENT",
    "CONFIG_TEXT_PRESENT",
    "CONFIG_TEXT_UNREADABLE",
    "measure_config_parse_compatibility",
    "read_target_config_text",
    "preflight_attest_launcher_capability",
    "preflight_attest_store_schema",
    "preflight_launcher_compatibility",
    "preflight_launcher_target_authorities",
)
