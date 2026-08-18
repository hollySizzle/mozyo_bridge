"""Dead-letter redrive use case (Redmine #15707 c; review j#108062 finding_redriveboundary).

The application service between the ``workflow callback-redrive`` CLI and the store: the CLI
converts arguments and maps typed results to exit codes, this use case owns the operation's
decisions (dry-run listing vs the one gated apply, input completeness, filtering), and the
store object performs the actual strictly-read-only read / compare-and-swap. The store is
consumed through :class:`RedriveStorePort` (a ``Protocol``), so a unit test expresses the
contract with a fake port instead of monkeypatching sqlite internals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from mozyo_bridge.core.state.callback_outbox import CallbackOutboxKey, CallbackOutboxRow

#: The use case's own typed apply refusal: the caller did not name exactly one row plus the
#: observed fingerprint. Disjoint from the store's dispositions (which all presuppose a key).
REDRIVE_INVALID_ARGS = "invalid_args"


class RedriveStorePort(Protocol):
    """What the redrive use case needs from the store (the #15707 redrive companion object)."""

    def dead_letter_fingerprints(
        self, *, workspace_id: Optional[str] = None
    ) -> tuple["tuple[CallbackOutboxRow, str]", ...]: ...

    def requeue_dead_letter(
        self, key: CallbackOutboxKey, *, expect_fingerprint: str
    ) -> str: ...


@dataclass(frozen=True)
class RedriveDryRun:
    """The dry-run listing: the workspace's dead-letter rows with their redrive fingerprints."""

    workspace_id: str
    rows: tuple["tuple[CallbackOutboxRow, str]", ...]


@dataclass(frozen=True)
class RedriveApplyRequest:
    """The ONE row an apply names, plus the fingerprint a prior dry-run reported."""

    workspace_id: str
    source: str
    issue: str
    journal: str
    normalized_gate: str
    callback_route: str
    expect_fingerprint: str

    @property
    def complete(self) -> bool:
        return all(
            str(value or "").strip()
            for value in (
                self.source,
                self.issue,
                self.journal,
                self.normalized_gate,
                self.callback_route,
                self.expect_fingerprint,
            )
        )

    @property
    def key(self) -> CallbackOutboxKey:
        return CallbackOutboxKey(
            source=self.source.strip(),
            issue=self.issue.strip(),
            journal=self.journal.strip(),
            normalized_gate=self.normalized_gate.strip(),
            callback_route=self.callback_route.strip(),
            workspace_id=self.workspace_id,
        )


@dataclass(frozen=True)
class RedriveApplyResult:
    """The apply outcome: a store disposition, or the use case's own ``invalid_args``."""

    disposition: str
    request: RedriveApplyRequest


class CallbackRedriveUseCase:
    """Coordinates the explicit dead-letter redrive over the injected store port."""

    def __init__(self, store: RedriveStorePort) -> None:
        self._store = store

    def dry_run(self, *, workspace_id: str, issue_filter: str = "") -> RedriveDryRun:
        """List the partition's dead-letter backlog with fingerprints (writes nothing)."""
        rows = self._store.dead_letter_fingerprints(workspace_id=workspace_id)
        wanted = str(issue_filter or "").strip()
        if wanted:
            rows = tuple(pair for pair in rows if pair[0].issue == wanted)
        return RedriveDryRun(workspace_id=workspace_id, rows=rows)

    def apply(self, request: RedriveApplyRequest) -> RedriveApplyResult:
        """Requeue the ONE named row, or refuse typed (incomplete naming / store CAS refusal)."""
        if not request.complete:
            return RedriveApplyResult(disposition=REDRIVE_INVALID_ARGS, request=request)
        disposition = self._store.requeue_dead_letter(
            request.key, expect_fingerprint=request.expect_fingerprint.strip()
        )
        return RedriveApplyResult(disposition=disposition, request=request)


__all__ = (
    "REDRIVE_INVALID_ARGS",
    "CallbackRedriveUseCase",
    "RedriveApplyRequest",
    "RedriveApplyResult",
    "RedriveDryRun",
    "RedriveStorePort",
)
