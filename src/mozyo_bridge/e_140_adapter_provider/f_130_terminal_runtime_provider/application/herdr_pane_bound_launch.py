"""Herdr 0.8 pane creation and action-private canonical executable shim."""

from __future__ import annotations

import json
import os
import shlex
import tempfile
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Mapping, Optional, Sequence

from mozyo_bridge.core.state.herdr_native_identity_binding import (
    HerdrNativeIdentityBinding,
    HerdrNativeIdentityBindingStore,
)

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (
    HerdrSessionStartError,
    _workspace_prefix,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (
    _invoke,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_transaction import (  # noqa: E501
    StartupTransaction,
    pane_bound_receipt,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    _agent_locator,
    _norm,
    decode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_slot_liveness import (
    SLOT_STALE,
    classify_named_slot,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    valid_target,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (
    Runner,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_executable import (
    ResolvedProviderLaunch,
)


@dataclass(frozen=True)
class PreparedPane:
    locator: str
    workspace_id: str
    tab_id: str


@dataclass(frozen=True)
class NativeLaunchAdmission:
    bindings: Mapping[str, HerdrNativeIdentityBinding]


AGENT_PANE_BUSY_RETRIES = 30
AGENT_PANE_BUSY_RETRY_SECONDS = 0.1


class ActionPrivateLaunchShimSet:
    """Own launch-scoped resources from preflight through deterministic cleanup.

    The session composition root owns one instance for its whole call.  ``prepare`` is
    all-or-nothing: if any provider shim cannot be created, previously prepared siblings
    are removed immediately and no path is returned to the caller.  This closes the
    second-provider preflight gap where the first agent could already be running before
    a later shim failure was discovered.  Other context-managed launch resources may be
    held on the same stack so their ``__exit__`` receives the real body exception rather
    than masking it during cleanup.
    """

    def __init__(self) -> None:
        self._stack = ExitStack()
        self._paths: dict[str, str] = {}
        self._prepared = False

    def __enter__(self) -> "ActionPrivateLaunchShimSet":
        return self

    def __exit__(self, *exc_details: object) -> bool:
        try:
            return bool(self._stack.__exit__(*exc_details))
        finally:
            self._paths.clear()

    def prepare(
        self,
        *,
        resolved_launches: Mapping[str, ResolvedProviderLaunch],
        attest_launcher: str,
        store_home: Path,
        action_id: str,
    ) -> Mapping[str, str]:
        if self._prepared:
            raise HerdrSessionStartError("launch shim set may be prepared only once")
        self._prepared = True
        try:
            for provider, resolved in resolved_launches.items():
                self._paths[provider] = self._stack.enter_context(
                    action_private_launch_shim(
                        provider=provider,
                        resolved=resolved,
                        attest_launcher=attest_launcher,
                        store_home=store_home,
                        action_id=action_id,
                    )
                )
        except BaseException:
            self.close()
            raise
        return dict(self._paths)

    def hold(self, context_manager: object) -> object:
        """Hold one launch-scoped context until the composition root exits."""
        return self._stack.enter_context(context_manager)  # type: ignore[arg-type]

    def close(self) -> None:
        try:
            self._stack.close()
        finally:
            self._paths.clear()


def bind_native_launch_set(
    logical_names: Sequence[str], *, store_home: Path, source_path: object
) -> NativeLaunchAdmission:
    """Atomically bind every logical name before any Herdr write."""
    if logical_names and not _norm(source_path):
        raise HerdrSessionStartError(
            "managed Herdr 0.8 launch requires a non-empty trusted PATH for its "
            "action-private executable shim; no Herdr write was attempted"
        )
    store = HerdrNativeIdentityBindingStore(home=store_home)
    try:
        bindings = store.bind_many(tuple(logical_names))
    except Exception as exc:
        raise HerdrSessionStartError(
            "Herdr native identity bindings could not be committed for the complete "
            "launch set; no Herdr workspace, tab, pane, or agent was created"
        ) from exc
    return NativeLaunchAdmission(
        bindings={item.logical_name: item for item in bindings},
    )


def prepare_complete_launch_shims(
    shim_set: Optional[ActionPrivateLaunchShimSet],
    *,
    providers: Sequence[str],
    resolved_launches: Mapping[str, ResolvedProviderLaunch],
    attest_launcher: str,
    store_home: Path,
    action_id: str,
) -> Mapping[str, str]:
    if not providers:
        return {}
    if shim_set is None:
        raise HerdrSessionStartError(
            "managed Herdr launch must enter through prepare_session so its complete "
            "provider shim set has deterministic cleanup"
        )
    return shim_set.prepare(
        resolved_launches={provider: resolved_launches[provider] for provider in providers},
        attest_launcher=attest_launcher,
        store_home=store_home,
        action_id=action_id,
    )


def prepared_pane_recorder(
    transaction: Optional[StartupTransaction],
) -> Callable[..., None]:
    """Return the callback that records a split pane before agent start."""

    def _record(**prepared: str) -> None:
        if transaction is None:
            raise HerdrSessionStartError(
                "a real Herdr pane was prepared without a startup transaction"
            )
        transaction.record_prepared_pane(
            role=prepared["role"],
            assigned_name=prepared["assigned_name"],
            locator=prepared["locator"],
            receipt=pane_bound_receipt(
                target_workspace=prepared["target_workspace"],
                target_tab=prepared["target_tab"],
                native_name=prepared["native_name"],
            ),
        )

    return _record


def _parse_pane(stdout: object) -> PreparedPane | None:
    if not isinstance(stdout, str):
        return None
    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    result = payload.get("result")
    if not isinstance(result, Mapping):
        return None
    if result.get("type") != "pane_info":
        return None
    pane = result.get("pane")
    if not isinstance(pane, Mapping):
        return None
    locator = _norm(pane.get("pane_id"))
    workspace_id = _norm(pane.get("workspace_id"))
    tab_id = _norm(pane.get("tab_id"))
    if not valid_target(locator) or not workspace_id or not tab_id:
        return None
    return PreparedPane(locator, workspace_id, tab_id)


def build_pane_split_argv(
    *,
    anchor_locator: str,
    direction: str,
    repo_root: Path,
    env_entries: Sequence[str],
) -> list[str]:
    if not valid_target(anchor_locator):
        raise HerdrSessionStartError("pane split requires one exact valid anchor locator")
    if direction not in {"right", "down"}:
        raise HerdrSessionStartError(
            f"pane split direction {direction!r} is outside right/down"
        )
    argv = [
        "pane",
        "split",
        anchor_locator,
        "--direction",
        direction,
        "--cwd",
        str(repo_root),
    ]
    for entry in env_entries:
        if not isinstance(entry, str) or "=" not in entry or entry.startswith("="):
            raise HerdrSessionStartError("pane split received a malformed environment entry")
        argv.extend(["--env", entry])
    argv.append("--no-focus")
    return argv


def split_prepared_pane(
    *,
    binary: str,
    anchor_locator: str,
    direction: str,
    repo_root: Path,
    env_entries: Sequence[str],
    runner: Runner,
    timeout: float,
    env: Mapping[str, str],
) -> PreparedPane:
    completed = _invoke(
        binary,
        build_pane_split_argv(
            anchor_locator=anchor_locator,
            direction=direction,
            repo_root=repo_root,
            env_entries=env_entries,
        ),
        runner,
        timeout,
        env=dict(env),
    )
    pane = _parse_pane(completed.stdout)
    if pane is None:
        raise HerdrSessionStartError(
            "herdr pane split returned no parseable pane identity; refuse to start an "
            "agent in an untracked pane"
        )
    return pane


def build_provider_shell_function_command(*, provider: str, shim_dir: str) -> str:
    """Define the canonical provider name only in the prepared pane's shell.

    Herdr 0.8 starts a supported kind by submitting its canonical executable name to
    the pane's already-running interactive shell.  On macOS that shell is a login shell
    and can replace the ``PATH`` supplied when the pane was created, so a PATH-only shim
    is not an execution authority.  A shell function survives that startup step and is
    scoped to this one pane.  Its target remains the action-private symlink prepared
    before any Herdr write.
    """
    if provider not in {"claude", "codex"}:
        raise HerdrSessionStartError(
            f"provider {provider!r} has no canonical Herdr managed-launch kind"
        )
    target = os.path.join(shim_dir, provider)
    if (
        not os.path.isabs(target)
        or not shim_dir
        or any(character in target for character in ("\x00", "\r", "\n"))
    ):
        raise HerdrSessionStartError(
            "managed-launch shell function requires an absolute control-free shim path"
        )
    return f'{provider}() {{ exec {shlex.quote(target)} "$@"; }}'


def prepare_provider_shell_function(
    *,
    binary: str,
    pane_locator: str,
    provider: str,
    shim_dir: str,
    runner: Runner,
    timeout: float,
    env: Mapping[str, str],
) -> None:
    """Install the action-private canonical function in one exact prepared pane."""
    if not valid_target(pane_locator):
        raise HerdrSessionStartError(
            "pane-local provider preparation requires one exact valid pane locator"
        )
    _invoke(
        binary,
        [
            "pane",
            "run",
            pane_locator,
            build_provider_shell_function_command(
                provider=provider,
                shim_dir=shim_dir,
            ),
        ],
        runner,
        timeout,
        env=dict(env),
    )


def _agent_pane_busy(completed: object) -> bool:
    """Whether Herdr typed this failed start as a not-yet-ready shell pane."""
    if getattr(completed, "returncode", 1) == 0:
        return False
    for raw in (getattr(completed, "stderr", ""), getattr(completed, "stdout", "")):
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        error = payload.get("error") if isinstance(payload, Mapping) else None
        if isinstance(error, Mapping) and error.get("code") == "agent_pane_busy":
            return True
    return False


def start_agent_in_prepared_pane(
    *,
    binary: str,
    launch_argv: Sequence[str],
    runner: Runner,
    timeout: float,
    env: Mapping[str, str],
    sleeper: Optional[Callable[[float], None]] = None,
) -> object:
    """Start in this run's exact pane, tolerating only the shell-startup race.

    Herdr 0.8 can return ``agent_pane_busy`` immediately after ``pane split`` while
    the new pane's login shell is still becoming interactive.  Repeating the same
    pane-bound command is safe only for that typed refusal: Herdr has stated that it
    did not start the agent, and the locator is the pane this run just recorded.
    Every other error is returned to :func:`_invoke` unchanged and fails immediately.
    """
    sleep = sleeper or time.sleep
    retries = 0

    def _retrying_runner(argv, *args, **kwargs):
        nonlocal retries
        while True:
            completed = runner(argv, *args, **kwargs)
            if not _agent_pane_busy(completed) or retries >= AGENT_PANE_BUSY_RETRIES:
                return completed
            retries += 1
            sleep(AGENT_PANE_BUSY_RETRY_SECONDS)

    return _invoke(
        binary,
        list(launch_argv),
        _retrying_runner,
        max(timeout, 31.0),
        env=dict(env),
    )


def choose_split_anchor(
    rows: Sequence[Mapping[str, object]],
    *,
    target_workspace: str,
    target_tab: str,
    preferred_locator: str = "",
) -> str:
    """Choose a deterministic live agent pane already in the target container."""
    candidates: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if not decode_assigned_name(row.get("name")).ok:
            continue
        if classify_named_slot(row) == SLOT_STALE:
            continue
        locator = _norm(_agent_locator(row))
        tab_id = _norm(row.get("tab_id"))
        if not locator or _workspace_prefix(locator) != target_workspace:
            continue
        if target_tab and tab_id != target_tab:
            continue
        candidates.add(locator)
    preferred = _norm(preferred_locator)
    if preferred and preferred in candidates:
        return preferred
    return sorted(candidates)[-1] if candidates else ""


def resolve_required_split_anchor(
    rows: Sequence[Mapping[str, object]],
    *,
    launch_required: bool,
    created_root: str,
    target_workspace: str,
    target_tab: str,
    preferred_locator: str = "",
) -> str:
    """Resolve only a run-owned root or an exact live managed pane."""
    anchor = created_root
    if launch_required and not anchor:
        anchor = choose_split_anchor(
            rows,
            target_workspace=target_workspace,
            target_tab=target_tab,
            preferred_locator=preferred_locator,
        )
    if launch_required and not anchor:
        raise HerdrSessionStartError(
            "target Herdr container has neither a root created by this run nor an "
            "exact live mozyo-managed agent pane to split; refusing to use an "
            "unowned shell pane"
        )
    return anchor


def list_workspace_panes(
    *,
    binary: str,
    workspace_id: str,
    runner: Runner,
    timeout: float,
    env: Mapping[str, str],
) -> tuple[Mapping[str, object], ...]:
    completed = _invoke(
        binary,
        ["pane", "list", "--workspace", workspace_id],
        runner,
        timeout,
        env=dict(env),
    )
    if not isinstance(completed.stdout, str):
        raise HerdrSessionStartError("herdr pane list returned a non-text payload")
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, ValueError) as exc:
        raise HerdrSessionStartError("herdr pane list returned invalid JSON") from exc
    if isinstance(payload, Mapping):
        payload = payload.get("result", payload)
    rows = payload.get("panes") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise HerdrSessionStartError(
            "herdr pane list payload did not contain a complete pane array"
        )
    return tuple(rows)


def choose_workspace_pane_anchor(
    pane_rows: Sequence[Mapping[str, object]],
    *,
    workspace_id: str,
    tab_id: str,
) -> str:
    candidates: list[str] = []
    for row in pane_rows:
        locator = _norm(row.get("pane_id") or row.get("id"))
        row_workspace = _norm(row.get("workspace_id")) or _workspace_prefix(locator)
        row_tab = _norm(row.get("tab_id"))
        if not valid_target(locator) or row_workspace != workspace_id:
            continue
        if tab_id and row_tab != tab_id:
            continue
        candidates.append(locator)
    if not candidates:
        raise HerdrSessionStartError(
            "target Herdr container has no readable pane to split; no agent was started"
        )
    return sorted(set(candidates))[-1]


@contextmanager
def action_private_launch_shim(
    *,
    provider: str,
    resolved: ResolvedProviderLaunch,
    attest_launcher: str,
    store_home: Path,
    action_id: str,
) -> Iterator[str]:
    """Expose one canonical Herdr executable name without changing global PATH."""
    if provider not in {"claude", "codex"}:
        raise HerdrSessionStartError(
            f"provider {provider!r} has no canonical Herdr managed-launch kind"
        )
    target = attest_launcher or resolved.executable
    if (
        not os.path.isabs(target)
        or not os.path.isfile(os.path.realpath(target))
        or not os.access(os.path.realpath(target), os.X_OK)
    ):
        raise HerdrSessionStartError(
            "managed-launch shim target is not an absolute executable file"
        )
    parent = Path(store_home) / "herdr-launch-shims"
    shim_dir: Optional[Path] = None
    link: Optional[Path] = None
    try:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not parent.is_dir() or parent.is_symlink():
            raise OSError("shim parent is not a plain directory")
        os.chmod(parent, 0o700)
        prefix = f"{(action_id or 'unmanaged')[:16]}-{provider}-"
        shim_dir = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
        os.chmod(shim_dir, 0o700)
        link = shim_dir / provider
        link.symlink_to(target)
    except OSError as exc:
        # Symlink creation can fail after the private directory exists. Remove only
        # artifacts this call can prove it created; unexpected content remains visible.
        try:
            if link is not None and link.is_symlink() and os.readlink(link) == target:
                link.unlink()
            if shim_dir is not None:
                shim_dir.rmdir()
        except OSError:
            pass
        raise HerdrSessionStartError(
            "could not create the action-private Herdr launch shim before pane creation"
        ) from exc
    try:
        yield str(shim_dir)
    finally:
        # Exact, non-recursive cleanup only.  Unexpected content leaves a visible residue
        # instead of broadening this helper into a directory deletion authority.
        try:
            if link is not None and link.is_symlink() and os.readlink(link) == target:
                link.unlink()
            if shim_dir is not None:
                shim_dir.rmdir()
        except OSError:
            pass


__all__ = (
    "ActionPrivateLaunchShimSet",
    "NativeLaunchAdmission",
    "PreparedPane",
    "action_private_launch_shim",
    "bind_native_launch_set",
    "build_provider_shell_function_command",
    "build_pane_split_argv",
    "choose_split_anchor",
    "choose_workspace_pane_anchor",
    "list_workspace_panes",
    "prepare_complete_launch_shims",
    "prepared_pane_recorder",
    "prepare_provider_shell_function",
    "resolve_required_split_anchor",
    "split_prepared_pane",
    "start_agent_in_prepared_pane",
)
