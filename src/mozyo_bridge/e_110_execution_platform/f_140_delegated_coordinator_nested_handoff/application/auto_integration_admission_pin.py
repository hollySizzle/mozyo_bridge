"""Exact production action-frame pin for auto-integration mutation capability (#14825).

Composition validates one durable review generation before it opens the writer.  The returned
capability must remain bound to that same full resume frame; otherwise a caller can compose with
the valid frame and register a different action afterward.  This value object owns that invariant
without adding another source of authority: every expected value comes from the already-validated
composition inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_ledger import (  # noqa: E501
    AutoIntegrationLedgerError,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_ports import (  # noqa: E501
    DurableAuthorityReader,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_policy import (  # noqa: E501
    IntegrationActionRecord,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.retirement_cleanup_policy import (  # noqa: E501
    CleanupActionRecord,
)


@dataclass(frozen=True)
class AdmittedActionPin:
    """The one integration frame and its deterministic cleanup namespace."""

    action_frame: Tuple[object, ...]
    cleanup_action_key: str

    @property
    def integration_action_key(self) -> str:
        return str(self.action_frame[0]) if self.action_frame else ""

    @staticmethod
    def frame_of(action: object) -> Tuple[object, ...]:
        """Project a durable action into its persisted identity order."""
        return tuple(
            getattr(action, name, None)
            for name in (
                "action_key",
                "issue",
                "workspace",
                "lane",
                "lane_generation",
                "branch",
                "worktree",
                "repo_root",
                "source_head",
                "target_ref",
                "expected_target_head",
                "review_generation",
            )
        )

    def require_integration_key(self, action_key: str) -> None:
        if action_key != self.integration_action_key:
            raise AutoIntegrationLedgerError(
                "the action key does not equal the exact frame admitted by production composition"
            )

    def require_mutation_key(self, action_key: str) -> None:
        if action_key not in (self.integration_action_key, self.cleanup_action_key):
            raise AutoIntegrationLedgerError(
                "the ledger mutation does not belong to the admitted integration or its cleanup"
            )

    def require_frame(self, action: object) -> None:
        if self.frame_of(action) != self.action_frame:
            raise AutoIntegrationLedgerError(
                "the durable resume frame does not equal the exact frame admitted by composition"
            )

    def require_current_review_generation(
        self, *, action: object, authority: Optional[DurableAuthorityReader]
    ) -> None:
        """Freshly fence the registry write against a superseded review request."""
        if authority is None:
            raise AutoIntegrationLedgerError(
                "the current review generation is unreadable before durable registration"
            )
        record = IntegrationActionRecord(
            issue=str(getattr(action, "issue", "")),
            lane_generation=getattr(action, "lane_generation", 0),
            source_head=str(getattr(action, "source_head", "")),
            target_ref=str(getattr(action, "target_ref", "")),
            expected_target_head=str(getattr(action, "expected_target_head", "")),
            review_generation=str(getattr(action, "review_generation", "")),
        )
        try:
            current = str(authority.current_review_generation(record=record) or "")
        except Exception as exc:  # noqa: BLE001 — unreadable authority authorizes no write
            raise AutoIntegrationLedgerError(
                "the current review generation is unreadable before durable registration"
            ) from exc
        if not current or current != record.review_generation:
            raise AutoIntegrationLedgerError(
                "the review generation is no longer the current approved review_request; "
                "durable registration was refused"
            )

    def require_cleanup(self, record: CleanupActionRecord) -> None:
        if (
            record.integration_action_key != self.integration_action_key
            or record.action_key != self.cleanup_action_key
        ):
            raise AutoIntegrationLedgerError(
                "the cleanup does not derive from the exact integration frame admitted by composition"
            )


__all__ = ("AdmittedActionPin",)
