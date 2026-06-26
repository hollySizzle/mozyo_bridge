"""AttentionRecord derivation read model tests (Redmine #11951 / #11935).

Pins the derivation priority, the fail-safe to ``unknown``, and the
non-routing / read-model boundary from
``vibes/docs/logics/cockpit-attention-state.md``.
"""

from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.domain import attention
from mozyo_bridge.domain.attention import (
    REASON_CONTRADICTORY,
    REASON_SOURCE_UNREADABLE,
    STATE_BLOCKED,
    STATE_DONE,
    STATE_HEALTHY,
    STATE_OWNER_WAITING,
    STATE_RETIRED_CANDIDATE,
    STATE_REVIEW_WAITING,
    STATE_STALLED,
    STATE_UNKNOWN,
    AttentionInputs,
    derive_attention,
)


def _inputs(**kwargs) -> AttentionInputs:
    base = {
        "unit_id": "unit:local:ws1:default",
        "observed_at": "2026-06-15T00:00:00Z",
        "workspace_id": "ws1",
        "role": "codex",
        "target_key": "tmux:local:%953",
        "source_refs": ("redmine:#11935#journal",),
    }
    base.update(kwargs)
    return AttentionInputs(**base)


class DerivationPriorityTest(unittest.TestCase):
    def test_owner_waiting_beats_review_and_stalled(self) -> None:
        # Design verification: owner_waiting / review_waiting outrank stalled,
        # and owner_waiting outranks review_waiting.
        rec = derive_attention(
            _inputs(owner_waiting=True, review_waiting=True, stalled=True)
        )
        self.assertEqual(rec.attention_state, STATE_OWNER_WAITING)

    def test_blocked_beats_stalled_and_healthy(self) -> None:
        rec = derive_attention(_inputs(blocked=True, stalled=True))
        self.assertEqual(rec.attention_state, STATE_BLOCKED)

    def test_review_waiting_beats_stalled(self) -> None:
        rec = derive_attention(_inputs(review_waiting=True, stalled=True))
        self.assertEqual(rec.attention_state, STATE_REVIEW_WAITING)

    def test_owner_outranks_blocked(self) -> None:
        rec = derive_attention(_inputs(owner_waiting=True, blocked=True))
        self.assertEqual(rec.attention_state, STATE_OWNER_WAITING)

    def test_blocked_outranks_review(self) -> None:
        rec = derive_attention(_inputs(blocked=True, review_waiting=True))
        self.assertEqual(rec.attention_state, STATE_BLOCKED)

    def test_stalled_beats_healthy(self) -> None:
        rec = derive_attention(_inputs(stalled=True))
        self.assertEqual(rec.attention_state, STATE_STALLED)

    def test_active_signal_outranks_done(self) -> None:
        # An owner/review gate must never be hidden behind a "done" projection.
        rec = derive_attention(_inputs(owner_waiting=True, done=True))
        self.assertEqual(rec.attention_state, STATE_OWNER_WAITING)

    def test_retired_candidate_beats_done(self) -> None:
        rec = derive_attention(_inputs(done=True, retired_candidate=True))
        self.assertEqual(rec.attention_state, STATE_RETIRED_CANDIDATE)

    def test_done_when_close_gate_only(self) -> None:
        rec = derive_attention(_inputs(done=True))
        self.assertEqual(rec.attention_state, STATE_DONE)

    def test_healthy_when_no_signal(self) -> None:
        rec = derive_attention(_inputs())
        self.assertEqual(rec.attention_state, STATE_HEALTHY)


class UnknownFailSafeTest(unittest.TestCase):
    def test_unreadable_source_is_unknown_not_healthy(self) -> None:
        rec = derive_attention(_inputs(source_readable=False))
        self.assertEqual(rec.attention_state, STATE_UNKNOWN)
        self.assertEqual(rec.reason_code, REASON_SOURCE_UNREADABLE)

    def test_contradictory_source_is_unknown_not_healthy(self) -> None:
        rec = derive_attention(_inputs(contradictory=True))
        self.assertEqual(rec.attention_state, STATE_UNKNOWN)
        self.assertEqual(rec.reason_code, REASON_CONTRADICTORY)

    def test_unreadable_overrides_every_active_signal(self) -> None:
        # Even with strong signals present, an unreadable source fails safe.
        rec = derive_attention(
            _inputs(source_readable=False, owner_waiting=True, blocked=True)
        )
        self.assertEqual(rec.attention_state, STATE_UNKNOWN)


class RecordShapeTest(unittest.TestCase):
    def test_record_carries_identity_and_provenance(self) -> None:
        rec = derive_attention(_inputs(review_waiting=True))
        self.assertEqual(rec.unit_id, "unit:local:ws1:default")
        self.assertEqual(rec.host_id, "local")
        self.assertEqual(rec.target_key, "tmux:local:%953")
        self.assertEqual(rec.source_refs, ("redmine:#11935#journal",))
        self.assertEqual(rec.observed_at, "2026-06-15T00:00:00Z")

    def test_caller_reason_override_wins(self) -> None:
        rec = derive_attention(
            _inputs(review_waiting=True, reason_code="target_dead")
        )
        self.assertEqual(rec.attention_state, STATE_REVIEW_WAITING)
        self.assertEqual(rec.reason_code, "target_dead")

    def test_payload_key_stability(self) -> None:
        rec = derive_attention(_inputs())
        self.assertEqual(
            sorted(rec.as_payload()),
            [
                "attention_state",
                "expires_at",
                "host_id",
                "lane_id",
                "observed_at",
                "reason_code",
                "role",
                "severity",
                "source_refs",
                "target_key",
                "unit_id",
                "workspace_id",
            ],
        )

    def test_derivation_is_deterministic(self) -> None:
        a = derive_attention(_inputs(blocked=True))
        b = derive_attention(_inputs(blocked=True))
        self.assertEqual(a, b)


class NonRoutingBoundaryTest(unittest.TestCase):
    """The read model must not become a routing / target-resolution surface."""

    def test_module_does_not_import_routing_surfaces(self) -> None:
        src = inspect.getsource(attention)
        for forbidden in (
            "handoff",
            "pane_resolver",
            "target",  # no target resolver import (target_key is a plain str field)
            "tmux_client",
            "infrastructure",
            "agent_discovery",
        ):
            self.assertNotIn(
                f"import {forbidden}",
                src,
                f"attention read model must not import {forbidden}",
            )
            self.assertNotIn(
                f"from mozyo_bridge.domain.{forbidden}",
                src,
                f"attention read model must not import {forbidden}",
            )
            self.assertNotIn(
                f"from mozyo_bridge.infrastructure",
                src,
            )

    def test_derive_signature_takes_only_inputs(self) -> None:
        # Pure read model: derivation takes the extracted facts, not a tmux /
        # Redmine handle, so it can never perform routing or I/O.
        params = list(inspect.signature(derive_attention).parameters)
        self.assertEqual(params, ["inputs"])

    def test_importing_module_does_not_pull_in_routing_modules(self) -> None:
        # Importing the read model must not transitively load routing / I/O
        # modules (a structural guard that the boundary holds at import time).
        before = set(sys.modules)
        import importlib

        importlib.reload(attention)
        newly = set(sys.modules) - before
        self.assertFalse(
            {m for m in newly if "handoff" in m or "pane_resolver" in m},
            "attention import must not load routing modules",
        )


class ConservativeAttentionTest(unittest.TestCase):
    """The shared conservative projection (#11952 / #12007).

    Pins the single convention behind ``agents targets --json`` and the cockpit
    ``/api/units`` join so the two read-only attention surfaces cannot drift.
    """

    def test_readable_identity_derives_healthy_no_source(self) -> None:
        from mozyo_bridge.domain.attention import (
            NO_ATTENTION_SOURCE_REASON,
            conservative_attention,
        )

        record = conservative_attention(
            observed_at="2026-06-15T00:00:00Z",
            role="claude",
            identity_readable=True,
            contradictory=False,
            workspace_id="ws1",
            pane_id="%1",
        )
        self.assertEqual(STATE_HEALTHY, record.attention_state)
        self.assertEqual(NO_ATTENTION_SOURCE_REASON, record.reason_code)
        self.assertEqual("claude", record.role)
        # Provenance conventions from unit-target-model.md, public-safe.
        self.assertEqual("unit:local:ws1:default", record.unit_id)
        self.assertEqual("tmux:local:%1", record.target_key)
        self.assertEqual(["tmux:%1"], list(record.source_refs))

    def test_unreadable_or_contradictory_identity_fails_safe(self) -> None:
        from mozyo_bridge.domain.attention import conservative_attention

        unreadable = conservative_attention(
            observed_at="2026-06-15T00:00:00Z",
            role="claude",
            identity_readable=False,
            contradictory=False,
        )
        self.assertEqual(STATE_UNKNOWN, unreadable.attention_state)
        self.assertEqual(REASON_SOURCE_UNREADABLE, unreadable.reason_code)

        contradictory = conservative_attention(
            observed_at="2026-06-15T00:00:00Z",
            role="codex",
            identity_readable=True,
            contradictory=True,
        )
        self.assertEqual(STATE_UNKNOWN, contradictory.attention_state)
        self.assertEqual(REASON_CONTRADICTORY, contradictory.reason_code)

    def test_non_agent_role_normalizes_to_other(self) -> None:
        from mozyo_bridge.domain.attention import ROLE_OTHER, conservative_attention

        record = conservative_attention(
            observed_at="2026-06-15T00:00:00Z",
            role="unknown",
            identity_readable=False,
            contradictory=False,
        )
        self.assertEqual(ROLE_OTHER, record.role)
        # No pane id → no routing target key fabricated.
        self.assertIsNone(record.target_key)
        self.assertEqual([], list(record.source_refs))


if __name__ == "__main__":
    unittest.main()
