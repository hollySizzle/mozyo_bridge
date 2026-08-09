"""Client-side multi-source Unit board runtime (Redmine #15138).

The local server is observed in-process through the existing
:class:`HerdrUnitBoardRuntime`.  Every other configured server is asked for its
*own* public-safe board — ``mozyo-bridge herdr unit-board show --json`` executed
through that source's fixed argv shape — so the far host resolves its own
workspace registry, workflow-role bindings, and lane metadata.  Nothing here
reaches into another host's registry, socket, or database, and no server state
is shared or synchronised.

Display metadata sync stays deliberately local.  Writing Herdr pane metadata is
a mutation of another server's live panes, and each host's own plugin already
does it there; doing it from the client would be a second writer with no lock
in common.  The client's relationship to a remote server is read-only
observation plus one routed, preview-first action.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.herdr_unit_board import (
    IDENTITY_RESOLVED,
    SOURCE_LIVE,
    UnitBoardRow,
    UnitBoardSnapshot,
)
from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.unit_board_aggregate import (
    DEFAULT_SOURCE_FRESHNESS_SECONDS,
    SourceObservation,
    actionable_workspace_id,
    aggregate_sources,
    local_source_observation,
    mark_stale,
    parse_remote_board_payload,
    unavailable_source_observation,
)
from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.unit_board_sources import (
    UnitBoardSource,
    UnitBoardSourceError,
    UnitBoardSourcesConfig,
    source_command_argv,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_unit_board_runtime import (
    HerdrUnitBoardRuntime,
    resolve_unit_board_binary,
)


#: The read-only command a remote source is asked for.  Fixed argv, no operator
#: substitution: the client asks every host the same question.
REMOTE_BOARD_ARGS = ("herdr", "unit-board", "show", "--json")

#: The read-only registry projection used to turn a Unit's ``workspace_id`` into
#: the repository root the gateway resolution needs on that host.
REMOTE_WORKSPACE_ARGS = ("workspace", "list", "--json")

#: Extra seconds allowed beyond a source's connection timeout for the remote
#: command itself to run and answer.
COMMAND_GRACE_SECONDS = 20

Runner = Callable[..., subprocess.CompletedProcess]
Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


@dataclass(frozen=True)
class SourceUnitTarget:
    """Everything an action needs about one Unit, resolved on a live source.

    ``remote_unit_id`` is the key the *source* uses for this Unit.  Re-addressing
    a Unit on its own host through that key is what keeps the client from
    rebuilding a far host's identity out of values that were shaped for display.
    """

    unit_id: str
    remote_unit_id: str
    source: UnitBoardSource
    workspace_id: str
    lane_id: str
    project_label: str
    observed_at: str


class MultiSourceUnitBoardRuntime:
    """Observe every configured Herdr server and merge the public-safe boards."""

    def __init__(
        self,
        config: UnitBoardSourcesConfig,
        *,
        local_runtime: Optional[HerdrUnitBoardRuntime] = None,
        runner: Optional[Runner] = None,
        clock: Clock = _utc_now,
        freshness_seconds: int = DEFAULT_SOURCE_FRESHNESS_SECONDS,
    ) -> None:
        self._config = config
        self._local_runtime = local_runtime
        self._runner: Runner = runner if runner is not None else subprocess.run
        self._clock = clock
        self._freshness_seconds = freshness_seconds

    @property
    def config(self) -> UnitBoardSourcesConfig:
        return self._config

    def _local(self) -> HerdrUnitBoardRuntime:
        if self._local_runtime is None:
            self._local_runtime = HerdrUnitBoardRuntime(resolve_unit_board_binary())
        return self._local_runtime

    def run_source_command(
        self, source: UnitBoardSource, args: Sequence[str]
    ) -> Optional[subprocess.CompletedProcess]:
        """Run one mozyo-bridge command on ``source`` through its fixed argv.

        The single subprocess seam for everything that crosses a host boundary,
        observation and action alike.  One seam means one place builds argv, one
        place applies the timeout, and a test that injects a runner cannot leave
        a second real-subprocess path behind.

        Returns ``None`` for every mechanical failure — unresolvable argv, spawn
        error, timeout — so callers never mistake "could not run" for an answer.
        """
        try:
            argv = source_command_argv(source, tuple(args), by_id=self._config.by_id)
        except UnitBoardSourceError:
            return None
        try:
            return self._runner(
                list(argv),
                capture_output=True,
                text=True,
                timeout=source.connect_timeout + COMMAND_GRACE_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    def _run_source_json(
        self, source: UnitBoardSource, args: Sequence[str]
    ) -> Optional[object]:
        """Run one read-only command on a source and decode its JSON answer.

        Returns ``None`` for a non-zero exit or unreadable output too.  The
        caller turns that into a visible ``unavailable`` source rather than an
        empty one, so a host that cannot be reached never reads as a host with
        nothing running.
        """
        completed = self.run_source_command(source, args)
        if completed is None:
            return None
        if completed.returncode != 0 or not isinstance(completed.stdout, str):
            return None
        try:
            return json.loads(completed.stdout)
        except (TypeError, ValueError):
            return None

    def _observe_source(self, source: UnitBoardSource) -> SourceObservation:
        if source.is_local:
            try:
                snapshot = self._local().snapshot()
            except Exception:
                return unavailable_source_observation(
                    source, observed_at=_stamp(self._clock())
                )
            return local_source_observation(snapshot, source=source)
        payload = self._run_source_json(source, REMOTE_BOARD_ARGS)
        observed_at = _stamp(self._clock())
        if payload is None:
            return unavailable_source_observation(source, observed_at=observed_at)
        return parse_remote_board_payload(
            payload, source=source, observed_at=observed_at
        )

    def observe(self) -> tuple[SourceObservation, ...]:
        """Observe every configured source once, newest-first freshness applied."""
        observations = [
            self._observe_source(source) for source in self._config.sources
        ]
        now = self._clock()
        return tuple(
            mark_stale(observation, now, max_age_seconds=self._freshness_seconds)
            for observation in observations
        )

    def snapshot(self) -> UnitBoardSnapshot:
        """The merged board.

        With only the local source configured this returns the local snapshot
        unchanged, so an operator who never opts in keeps the previous
        behaviour byte for byte.
        """
        if self._config.is_local_only:
            # No merge, no source envelope, no extra payload key: the local-only
            # board is the pre-#15138 board.
            return self._local().snapshot()
        observations = self.observe()
        return aggregate_sources(observations, observed_at=_stamp(self._clock()))

    def resolve_unit_target(self, unit_id: str) -> Optional[SourceUnitTarget]:
        """Resolve one opaque board key to a Unit on a live, fresh source.

        A fresh observation is taken here rather than trusting a rendered row:
        the board the operator is looking at may be seconds or minutes old, and
        a Unit that has since moved, ended, or become ambiguous must not be
        addressable.  Every fail-closed case returns ``None``:

        - the source is not live (unreachable, unreadable, or stale);
        - the key does not resolve to exactly one Unit on exactly one source;
        - the Unit's identity is ambiguous within its own source;
        - the workspace id is not a whole registry identity, so it may be a
          value that was bounded for display rather than the identity itself.
        """
        if not isinstance(unit_id, str) or not unit_id:
            return None
        observations = self.observe()
        matches: list[tuple[SourceObservation, UnitBoardRow]] = []
        for observation in observations:
            if observation.status.source_state != SOURCE_LIVE:
                continue
            for row in observation.rows:
                if row.unit_id == unit_id:
                    matches.append((observation, row))
        if len(matches) != 1:
            return None
        observation, row = matches[0]
        if row.identity_state != IDENTITY_RESOLVED:
            return None
        workspace_id = actionable_workspace_id(row)
        if workspace_id is None:
            return None
        source = self._config.by_id.get(observation.status.host_id)
        if source is None:
            return None
        remote_unit_id = (
            row.unit_id if source.is_local else observation.remote_unit_ids.get(unit_id, "")
        )
        if not remote_unit_id:
            return None
        return SourceUnitTarget(
            unit_id=unit_id,
            remote_unit_id=remote_unit_id,
            source=source,
            workspace_id=workspace_id,
            lane_id=row.lane_id,
            project_label=row.project_label,
            observed_at=observation.status.observed_at,
        )

    def resolve_source_workspace(
        self, source: UnitBoardSource, workspace_id: str
    ) -> Optional["SourceWorkspace"]:
        """Resolve a workspace id against the registry *on that source's host*.

        Returns the canonical root and the registry project name in one round
        trip, because both are needed together and asking twice would let the
        two answers come from different registry states.  ``canonical_path``
        exists only to be an argv value on the far host: it is never rendered,
        journalled, or stored.  Resolution is fail-closed — an unreadable
        registry, a missing row, or more than one row for the same id yields
        ``None`` and the action refuses.
        """
        payload = self._run_source_json(source, REMOTE_WORKSPACE_ARGS)
        if not isinstance(payload, dict):
            return None
        rows = payload.get("workspaces")
        if not isinstance(rows, list):
            return None
        matches = [
            row
            for row in rows
            if isinstance(row, dict) and row.get("workspace_id") == workspace_id
        ]
        if len(matches) != 1:
            return None
        canonical_path = matches[0].get("canonical_path")
        project_name = matches[0].get("project_name")
        if not isinstance(canonical_path, str) or not canonical_path.startswith("/"):
            return None
        if not isinstance(project_name, str) or not project_name:
            return None
        return SourceWorkspace(
            workspace_id=workspace_id,
            canonical_path=canonical_path,
            project_name=project_name,
        )


@dataclass(frozen=True)
class SourceWorkspace:
    """A workspace as the *source host's* registry describes it.

    ``canonical_path`` is a path on that host.  It is an argv input for a
    command executed there and must not reach any rendered, stored, or
    journalled surface on the client.
    """

    workspace_id: str
    canonical_path: str
    project_name: str


__all__ = (
    "COMMAND_GRACE_SECONDS",
    "REMOTE_BOARD_ARGS",
    "REMOTE_WORKSPACE_ARGS",
    "MultiSourceUnitBoardRuntime",
    "SourceUnitTarget",
    "SourceWorkspace",
)
