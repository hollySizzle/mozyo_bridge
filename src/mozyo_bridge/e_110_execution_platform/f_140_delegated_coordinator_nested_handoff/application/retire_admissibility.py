"""Terminal-retire integration admissibility, resolved from durable observations.

The ``sublane retire`` integration decision is fenced on ``latest_generation_admissible``: a
lane may not retire on a STALE last-write-wins approval (#13518 review R2-F7 / R3-F2). This
module owns how that single boolean is RESOLVED for a CLI invocation, from two independent
kinds of durable evidence measured at action time, plus the operator's fallback assertion:

- :func:`_resolve_review_generation_admissible` — the review-generation fence (#13518): the
  LATEST generation is approved and carries no unresolved blocking finding;
- :func:`_resolve_review_exemption_admissible` — the review-EXEMPTION fence (#14539): a lane
  covered by a valid ``codex_direct_edit`` gate with ``follow_up_review: false`` has no review
  generation at all, so it is admitted on exemption + Close + complete integration instead of
  on a review that never happened.

Carved out of :mod:`.sublane_lifecycle_command` (Redmine #14539) when adding the second route
pushed that module past the oversized-module gate. This is a pure move plus the new route: the
CLI module re-exports :func:`_resolve_latest_generation_admissible`, so its import site and the
existing #13518 tests are unchanged.

Boundary: reads a caller-named JSON observation file and delegates every decision to the pure
domain fences. Every failure mode — absent flag, unreadable / malformed file, inadmissible
evidence — resolves to ``False`` (fail-closed).
"""

from __future__ import annotations

import argparse
from pathlib import Path


def _resolve_review_exemption_admissible(args: argparse.Namespace) -> bool:
    """Re-verify a review-EXEMPT lane's terminal-retire admissibility at action time (#14539).

    ``--review-exemption-json`` supplies the issue's durable journals
    (``{issue, journals: [{journal_id, notes}]}``). They are folded with the SAME grammar the
    glance projection uses — no second reader — and admitted only when all three durable facts
    hold together: a valid ``codex_direct_edit`` gate with ``follow_up_review: false``, a recorded
    Close gate, and a COMPLETE integration disposition
    (:func:`...review_exemption.evaluate_exemption_integration_admissible`).

    This is what removes the false assert the issue names: an exempt lane has no review generation,
    so ``--latest-generation-admissible`` ("the latest generation is approved with no unresolved
    blocking finding") could only ever be asserted untruthfully for it. Every failure mode —
    unreadable file, malformed journals, invalid gate, ``follow_up_review: true``, missing Close,
    incomplete integration — is fail-closed to ``False``.
    """
    path = (getattr(args, "review_exemption_json", None) or "").strip()
    if not path:
        return False
    try:
        import json

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.glance_integration_disposition import (  # noqa: E501
            fold_integration_disposition,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.glance_journal_grammar import (  # noqa: E501
            fold_issue_gate_facts,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_exemption import (  # noqa: E501
            evaluate_exemption_integration_admissible,
            fold_review_exemption,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (  # noqa: E501
            GATE_CLOSE,
        )

        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        journals = [
            (str(entry.get("journal_id", "")), str(entry.get("notes", "")))
            for entry in (raw.get("journals") or [])
        ]
        gate_facts = fold_issue_gate_facts(journals)
        # No recognized gate at all -> no Close evidence. Never "assume closed".
        close_recorded = gate_facts is not None and gate_facts.latest_gate == GATE_CLOSE
        return bool(
            evaluate_exemption_integration_admissible(
                fold_review_exemption(journals),
                close_recorded=close_recorded,
                integration_complete=fold_integration_disposition(journals).complete,
            ).admissible
        )
    except Exception:  # noqa: BLE001 - unreadable / malformed durable observation -> fail closed
        return False


def _resolve_latest_generation_admissible(args: argparse.Namespace) -> bool:
    """Resolve the latest-generation integration admissibility for a retire (#13518 R3-F2).

    Priority: (1) a coordinator-supplied durable review observation (``--review-generation-json``)
    is MEASURED at action-time through the pure review-generation fence
    (:func:`...review_generation.evaluate_integration_admissible`) — an unreadable / malformed file
    or an inadmissible latest generation fails closed. (2) A durable review EXEMPTION observation
    (``--review-exemption-json``) is MEASURED the same way (#14539). (3) Otherwise the operator's
    durable-record assertion (``--latest-generation-admissible``). (4) Absent all, ``False``
    (fail-closed) — the actual integration decision never default-admits a stale last-write-wins
    approval.

    When EITHER measured input is supplied, the measurement decides and the operator assertion is
    NOT consulted: a supplied-but-failing measurement must never fall back to a hand assert. The
    two measured routes are independent evidence for the same fence (a lane either passed a review
    generation or was exempt from one), so either one admitting is sufficient.
    """
    exemption_path = (getattr(args, "review_exemption_json", None) or "").strip()
    path = (getattr(args, "review_generation_json", None) or "").strip()
    if path or exemption_path:
        return _resolve_review_generation_admissible(args) or (
            _resolve_review_exemption_admissible(args)
        )
    return bool(getattr(args, "latest_generation_admissible", False))


def _resolve_review_generation_admissible(args: argparse.Namespace) -> bool:
    """Measure latest-generation admissibility from ``--review-generation-json`` (#13518 R3-F2).

    Returns ``False`` when the flag is absent, when the file is unreadable / malformed, or when
    the fence finds the latest generation inadmissible.
    """
    path = (getattr(args, "review_generation_json", None) or "").strip()
    if path:
        try:
            import json

            from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_generation import (  # noqa: E501
                ReviewDecision,
                ReviewGeneration,
                evaluate_integration_admissible,
            )

            raw = json.loads(Path(path).read_text(encoding="utf-8"))
            gen = ReviewGeneration(
                issue=str(raw.get("issue", "")),
                review_request_journal=str(raw.get("review_request_journal", "")),
                target_head=str(raw.get("target_head", "")),
            )
            decisions = [
                ReviewDecision(
                    generation=ReviewGeneration(
                        issue=str(d.get("issue", raw.get("issue", ""))),
                        review_request_journal=str(
                            d.get("review_request_journal", raw.get("review_request_journal", ""))
                        ),
                        target_head=str(d.get("target_head", raw.get("target_head", ""))),
                    ),
                    kind=str(d.get("kind", "")),
                    seq=int(d.get("seq", 0)),
                    blocking=bool(d.get("blocking", False)),
                    disposition=str(d.get("disposition", "unresolved")),
                    journal_id=str(d.get("journal_id", "")),
                )
                for d in (raw.get("decisions") or [])
            ]
            return bool(evaluate_integration_admissible(gen, decisions).admissible)
        except Exception:  # noqa: BLE001 - unreadable / malformed durable observation -> fail closed
            return False
    return False


__all__ = (
    "_resolve_latest_generation_admissible",
    "_resolve_review_exemption_admissible",
    "_resolve_review_generation_admissible",
)
