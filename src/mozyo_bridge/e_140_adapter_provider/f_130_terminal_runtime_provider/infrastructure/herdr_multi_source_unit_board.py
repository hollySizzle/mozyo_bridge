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
import os
import selectors
import subprocess
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.herdr_unit_board import (
    AUTHORITY_RESOLVED,
    IDENTITY_RESOLVED,
    SOURCE_LIVE,
    SOURCE_RELOAD_REQUIRED,
    UnitBoardRow,
    UnitBoardSnapshot,
)
from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.unit_board_aggregate import (
    DEFAULT_SOURCE_FRESHNESS_SECONDS,
    SourceObservation,
    actionable_identity,
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
#:
#: ``--local-only`` is load-bearing, not a convenience: without it a host that
#: has its own observation sources answers with *its* merged board, so its rows
#: describe servers this client never asked about (Redmine #15138 review
#: j#101787 f2).  Mutually registered hosts would also fan out recursively.  The
#: parser independently rejects a merged answer, so an old remote that ignores
#: the flag fails closed rather than being trusted.
REMOTE_BOARD_ARGS = ("herdr", "unit-board", "show", "--json", "--local-only")

#: The read-only registry projection used to turn a Unit's ``workspace_id`` into
#: the repository root the gateway resolution needs on that host.
REMOTE_WORKSPACE_ARGS = ("workspace", "list", "--json")

#: Outcomes of one read-only source query, kept distinct so a source that never
#: answered is not reported the same way as one whose answer could not be read.
ANSWER_OK = "ok"
ANSWER_UNREACHABLE = "unreachable"
ANSWER_UNREADABLE = "unreadable"

#: Ceiling on what one source command may return.  Every other bound in this
#: module — unit count, agent count, timeout — applies *after* decoding, so
#: without this a reachable source could exhaust the client's memory before any
#: of them ran (review j#102018 finding_5).
MAX_SOURCE_OUTPUT_BYTES = 4 * 1024 * 1024


class UntrustedJsonError(ValueError):
    """An untrusted JSON document could not be read under the strict rules."""


def _reject_duplicate_keys(pairs: Sequence[tuple[str, object]]) -> dict:
    seen: dict = {}
    for key, value in pairs:
        if key in seen:
            # ``json.loads`` keeps the last value for a repeated key, so a
            # source could put a rejected value first and a canonical one second
            # and have every later check see only the second (review j#102018
            # finding_3).  Two claims for one field are not a document this
            # client can act on.
            raise UntrustedJsonError(f"duplicate key {key!r} in untrusted JSON")
        seen[key] = value
    return seen


def loads_untrusted_json(text: object) -> object:
    """Decode untrusted JSON, refusing duplicate keys at every object depth."""
    if not isinstance(text, str):
        raise UntrustedJsonError("untrusted JSON must be text")
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except UntrustedJsonError:
        raise
    except RecursionError as exc:
        # A deeply nested document exhausts the decoder's stack rather than
        # raising ValueError, and it does so only on some supported
        # interpreters — 3.10 and 3.11 raise here where 3.12+ decode the same
        # input (review j#102129 finding_5).  A failure the caller cannot catch
        # is a failure that skips the fail-closed path entirely.
        raise UntrustedJsonError("untrusted JSON exceeded the decoder's limits") from exc
    except (TypeError, ValueError) as exc:
        raise UntrustedJsonError("untrusted JSON could not be decoded") from exc


def bounded_capture_run(argv, *, timeout, **_ignored):
    """Run one command under BOTH a byte ceiling and a deadline.

    The production runner.  Two bounds that have to hold at once, and the
    obvious composition of them does not: ``subprocess.run(capture_output=True)``
    buffers everything before the caller sees it, while a single blocking
    ``read(ceiling + 1)`` waits for the child to reach the ceiling or close its
    output — so a source that stays under the ceiling and never closes hangs
    forever, and the timeout that was supposed to cover that sits behind the
    read (review j#102129 finding_2).

    So the read is incremental and deadline-aware: poll with the time remaining,
    stop at the ceiling, and on either limit kill the child and raise
    :class:`subprocess.TimeoutExpired` for the deadline — the same exception the
    shared seam already treats as a mechanical failure.
    """
    deadline = time.monotonic() + float(timeout)
    chunks: list[bytes] = []
    captured = 0
    with subprocess.Popen(
        list(argv), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    ) as process:
        assert process.stdout is not None
        stream = process.stdout
        os.set_blocking(stream.fileno(), False)
        timed_out = False
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(stream, selectors.EVENT_READ)
                while captured <= MAX_SOURCE_OUTPUT_BYTES:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    if not selector.select(remaining):
                        timed_out = True
                        break
                    chunk = stream.read(MAX_SOURCE_OUTPUT_BYTES + 1 - captured)
                    if chunk is None:
                        continue
                    if not chunk:
                        break
                    chunks.append(chunk)
                    captured += len(chunk)
            if timed_out:
                raise subprocess.TimeoutExpired(list(argv), timeout)
            if captured > MAX_SOURCE_OUTPUT_BYTES:
                # We stopped consuming on purpose, so a child with more to say
                # is now blocked on a full pipe and will never exit.  Waiting
                # for it would turn the ceiling into a hang.
                process.kill()
                process.wait()
            else:
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except BaseException:
            process.kill()
            process.wait()
            raise
        finally:
            stream.close()
    return subprocess.CompletedProcess(
        list(argv),
        process.returncode,
        b"".join(chunks).decode("utf-8", errors="replace"),
        "",
    )

#: Bound on a remote repository root.  The value comes from another host's
#: registry — untrusted input that becomes an argv element — so it is checked
#: for shape here rather than at the subprocess boundary, where the failure mode
#: is an exception instead of a refusal (review j#101846 finding_5).
MAX_REMOTE_PATH_LENGTH = 4096


def _usable_remote_path(value: object) -> bool:
    """True when a remote registry path is safe to pass as an argv element."""
    if not isinstance(value, str) or not value.startswith("/"):
        return False
    if len(value) > MAX_REMOTE_PATH_LENGTH:
        return False
    # Any control codepoint, by Unicode category rather than by an ASCII range.
    # A hand-written range check covered C0 and DEL but silently passed the C1
    # block (U+0080-U+009F), so the comment claimed more than the code did
    # (review j#101891 finding_4).  Surrogates are rejected on the same ground:
    # a path is a filesystem location, and neither belongs in one.
    return not any(unicodedata.category(char) in {"Cc", "Cs"} for char in value)

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
        self._runner: Runner = runner if runner is not None else bounded_capture_run
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
    ) -> "SourceCommandResult":
        """Run one mozyo-bridge command on ``source`` through its fixed argv.

        The single subprocess seam for everything that crosses a host boundary,
        observation and action alike.  One seam means one place builds argv, one
        place applies the timeout, and a test that injects a runner cannot leave
        a second real-subprocess path behind.

        The outcome distinguishes "could not run" from "answered too much": a
        source that overflowed the ceiling did respond, and reporting that as a
        connection failure mislabels it (review j#102129 finding_4).
        """
        try:
            argv = source_command_argv(source, tuple(args), by_id=self._config.by_id)
        except UnitBoardSourceError:
            return SourceCommandResult(ANSWER_UNREACHABLE, None)
        try:
            completed = self._runner(
                list(argv),
                capture_output=True,
                text=True,
                timeout=source.connect_timeout + COMMAND_GRACE_SECONDS,
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            # ``ValueError`` is the subprocess layer refusing the argv itself —
            # an embedded NUL is the reachable case, since part of the argv comes
            # from a remote registry this client does not control.  Left
            # uncaught it escapes as a raw exception instead of the fixed typed
            # refusal every other failure here produces (review j#101846
            # finding_5).
            return SourceCommandResult(ANSWER_UNREACHABLE, None)
        # Also enforced here, not only inside the production runner, so an
        # injected runner cannot hand back an unbounded answer.
        if isinstance(completed.stdout, str) and len(completed.stdout.encode(
            "utf-8", errors="replace"
        )) > MAX_SOURCE_OUTPUT_BYTES:
            return SourceCommandResult(ANSWER_UNREADABLE, None)
        return SourceCommandResult(ANSWER_OK, completed)

    def _run_source_json(
        self, source: UnitBoardSource, args: Sequence[str]
    ) -> tuple[str, Optional[object]]:
        """Run one read-only command on a source and decode its JSON answer.

        Returns the *reason* alongside the payload, because "the host did not
        answer" and "the host answered something unreadable" are different
        source states and collapsing them mislabels a schema break as a
        connection failure (Redmine #15138 review j#101787 f6):

        - :data:`ANSWER_UNREACHABLE` — argv unresolvable, spawn error, timeout,
          or a non-zero exit: the source did not answer.
        - :data:`ANSWER_UNREADABLE` — the source answered, but the answer could
          not be decoded.
        - :data:`ANSWER_OK` — a decoded payload.
        """
        result = self.run_source_command(source, args)
        if result.outcome != ANSWER_OK:
            return result.outcome, None
        completed = result.completed
        if completed is None or completed.returncode != 0:
            return ANSWER_UNREACHABLE, None
        if not isinstance(completed.stdout, str):
            return ANSWER_UNREADABLE, None
        try:
            return ANSWER_OK, loads_untrusted_json(completed.stdout)
        except UntrustedJsonError:
            return ANSWER_UNREADABLE, None

    def _observe_source(self, source: UnitBoardSource) -> SourceObservation:
        if source.is_local:
            try:
                snapshot = self._local().snapshot()
            except Exception:
                return unavailable_source_observation(
                    source, observed_at=_stamp(self._clock())
                )
            return local_source_observation(snapshot, source=source)
        answer, payload = self._run_source_json(source, REMOTE_BOARD_ARGS)
        now = self._clock()
        observed_at = _stamp(now)
        if answer == ANSWER_UNREACHABLE:
            return unavailable_source_observation(source, observed_at=observed_at)
        if answer == ANSWER_UNREADABLE:
            return unavailable_source_observation(
                source,
                observed_at=observed_at,
                source_state=SOURCE_RELOAD_REQUIRED,
                detail="source returned an unreadable Unit board payload",
            )
        return parse_remote_board_payload(
            payload, source=source, observed_at=observed_at, now=now
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
        - the workspace or the lane is not addressable as the source stated it
          (review j#102018 finding_1);
        - the Unit's display authority did not resolve, so the far host could
          not read the durable role binding that describes it (Redmine #15138
          review j#101787 f3);
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
        if row.authority_state != AUTHORITY_RESOLVED:
            return None
        # Both halves or neither: a lane the projection would rewrite is as
        # unusable an action input as a non-canonical workspace.
        identity = actionable_identity(row)
        if identity is None:
            return None
        workspace_id, lane_id = identity
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
            # The source's own lane, already proven to survive the public-safe
            # projection unchanged, so the preview-to-apply comparison and the
            # rendered preview agree on one value.
            lane_id=lane_id,
            project_label=row.project_label,
            observed_at=observation.status.observed_at,
        )

    def resolve_source_workspace(
        self, source: UnitBoardSource, workspace_id: str
    ) -> Optional["SourceWorkspace"]:
        """Resolve a workspace id against the registry *on that source's host*.

        Returns the canonical Git root only.  The registry also carries a
        ``project_name``, and this deliberately does not read it: that field is
        display metadata and a directory-name default, never a role or scope
        authority (``workflow-step-command-design.md`` "registry project_name を
        role/scope authority にしない"; Redmine #15138 review j#101787 f1).  The
        repository root *is* the workspace authority, so it is the one value
        taken from here.

        ``canonical_path`` exists only to be an argv value on the far host: it
        is never rendered, journalled, or stored.  Resolution is fail-closed —
        an unreadable registry, a missing row, or more than one row for the same
        id yields ``None`` and the action refuses.
        """
        _, payload = self._run_source_json(source, REMOTE_WORKSPACE_ARGS)
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
        if not _usable_remote_path(canonical_path):
            return None
        return SourceWorkspace(
            workspace_id=workspace_id, canonical_path=canonical_path
        )


@dataclass(frozen=True)
class SourceCommandResult:
    """One source command's mechanical outcome and, when it ran, its result."""

    outcome: str
    completed: Optional[subprocess.CompletedProcess] = None

    @property
    def ok(self) -> bool:
        return self.outcome == ANSWER_OK


@dataclass(frozen=True)
class SourceWorkspace:
    """A workspace as the *source host's* registry describes it.

    Only the Git worktree root, which is the workspace authority.  The registry
    project name is intentionally absent so it cannot be mistaken for a project
    scope authority further down the call chain.

    ``canonical_path`` is a path on that host.  It is an argv input for a
    command executed there and must not reach any rendered, stored, or
    journalled surface on the client.
    """

    workspace_id: str
    # A path on another host, kept out of the repr for the same reason it is
    # kept out of every payload (review j#102159 finding_2).
    canonical_path: str = field(repr=False)


__all__ = (
    "ANSWER_OK",
    "MAX_REMOTE_PATH_LENGTH",
    "MAX_SOURCE_OUTPUT_BYTES",
    "UntrustedJsonError",
    "bounded_capture_run",
    "loads_untrusted_json",
    "ANSWER_UNREACHABLE",
    "ANSWER_UNREADABLE",
    "COMMAND_GRACE_SECONDS",
    "REMOTE_BOARD_ARGS",
    "REMOTE_WORKSPACE_ARGS",
    "MultiSourceUnitBoardRuntime",
    "SourceCommandResult",
    "SourceUnitTarget",
    "SourceWorkspace",
)
