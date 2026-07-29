"""Redmine #14667 — the proxy-send decision is read from an UNCOLLAPSED marker body.

Cause: ``3c101ca5`` introduced ``canonical_decision_in_journal`` with an injected ``parse`` seam
bound to the lenient fold (#14546); ``94207e26`` made the SCAN quote-aware (#14585) without
touching how the marker's BODY was judged, so the body stayed lenient behind a reader whose
docstring was by then about strictness. Reproduced independently during the #14539 R34 audit
(j#92652 / head ``160a1e2e5c3202c8acb3185ad8d1146bdfa5575b``) and routed here rather than patched
there; fixed on base ``origin/main-next@4f0d765b``.

``coordinator_proxy_send.canonical_decision_in_journal`` read the named journal's decision through
``marker_fields_in_note``, the LENIENT fold. That fold collapses a repeated key by last-write-wins
and strips whitespace around every key and value, so a marker body **no canonical producer could
render** arrived at the authority check looking clean — and this reader's result decides a proxy
SEND.

Three bodies were measured deciding one on that head:

    gate=some_other:gate=implementation_request           (repeated key, last-write-wins)
    proxy_action=dispatch_next:proxy_action=bootstrap_lane (the same, on the action field)
    gate = implementation_request:proxy_action = …         (whitespace-contaminated fields)

Every assertion below is about the **number of sends that actually happened**, not about a returned
object's shape. A grammar test that stops at the reader cannot tell a refusal from a refusal that a
later link happened to produce — which is exactly how the quoted-marker defect (#14577 j#90392)
reached live acceptance with ``links.anchor=verified``. So each case runs the whole choreography
against a counting port and asserts both the fixed zero-send reason AND ``port.calls == []``.

Two properties carry the fix, and both have a dedicated case:

- **the strictness is the SHARED reader's, not this rail's.** Every rule applied to a marker body
  comes from ``redmine_journal_source.strict_marker_fields`` / ``marker_logical_gates``, the ones
  every other authority consumer calls. A private second opinion about what a producer can render
  is the same drift class that let two notions of "quoted" coexist (#14585);
- **an unreadable claim is not dropped.** The naive strict fix — parse strictly, skip what does not
  parse — is a LOOSENING here: it turns the exactly-one-decision rule's duplicate refusal into an
  acceptance, so a note carrying one forged marker beside one clean marker reads exactly like a
  clean note. ``test_a_clean_sibling_does_not_rescue_a_forged_claim`` is the case that distinguishes
  the two implementations; a forged marker on its own would zero-send either way.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.coordinator_proxy_fence import (  # noqa: E402
    CoordinatorProxyFence,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.coordinator_proxy_send import (  # noqa: E402,E501
    DECISION_ACTION_FIELD,
    SEND_DELIVERED,
    ProxySendOutcome,
    canonical_decision_in_journal,
    execute_proxy_delegation,
    render_bootstrap_decision_marker,
    resolve_proxy_context,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.coordinator_proxy import (  # noqa: E402,E501
    ACTION_BOOTSTRAP_LANE,
    ACTION_DISPATCH_NEXT,
    ANCHOR_ACTION_MISMATCH,
    ANCHOR_DECISION_AMBIGUOUS,
    ANCHOR_DECISION_UNREADABLE,
    ANCHOR_UNVERIFIED,
    ANCHOR_VERIFIED,
    DELIVER,
    REASON_ANCHOR_ACTION_MISMATCH,
    REASON_ANCHOR_DECISION_AMBIGUOUS,
    REASON_ANCHOR_DECISION_UNREADABLE,
    REASON_ANCHOR_UNVERIFIED,
    ZERO_SEND,
    IssueExpectation,
    LaneExpectation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E402,E501
    marker_components_in_note,
    marker_logical_gates,
    strict_marker_fields,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_role_authority import (  # noqa: E402,E501
    SCHEMA_NAME,
    SCHEMA_VERSION,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_runtime import (  # noqa: E402,E501
    ROLE_COORDINATOR,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402,E501
    AGENT_KEY_LOCATOR,
    AGENT_KEY_NAME,
    encode_assigned_name,
)

WS = "e1487dcb1f2d4412b28e825fdeccf9e8"
SCOPE = "bare_mozyo_workspace"
ISSUE = "14667"
JOURNAL = "92675"
LANE = "issue_14667_proxy_send_strict_marker_fold"

#: What the canonical producer actually renders — the only shapes a decision may have.
BOOTSTRAP_MARKER = render_bootstrap_decision_marker()
DISPATCH_MARKER = render_bootstrap_decision_marker(lane=LANE, lane_generation="2")


def _marker(body: str) -> str:
    return f"[mozyo:workflow-event:{body}]"


#: The bodies measured deciding a send on ``origin/main-next@4f0d765b``, plus the rest of the
#: producer-impossible component shapes the central `### Hibernate Evidence Marker Contract`
#: enumerates ("空 component・``=`` を欠く fragment・空 key・whitespace 混入").
PRODUCER_IMPOSSIBLE_BODIES = {
    "repeated gate (last-write-wins)":
        f"gate=some_other:gate=implementation_request:{DECISION_ACTION_FIELD}=bootstrap_lane",
    "repeated proxy_action (last-write-wins)":
        f"gate=implementation_request:{DECISION_ACTION_FIELD}=dispatch_next:"
        f"{DECISION_ACTION_FIELD}=bootstrap_lane",
    "whitespace-contaminated fields":
        f"gate = implementation_request:{DECISION_ACTION_FIELD} = bootstrap_lane",
    "empty component":
        f"gate=implementation_request::{DECISION_ACTION_FIELD}=bootstrap_lane",
    "fragment carrying no '='":
        f"gate=implementation_request:junk:{DECISION_ACTION_FIELD}=bootstrap_lane",
    "empty key":
        f"gate=implementation_request:=orphan:{DECISION_ACTION_FIELD}=bootstrap_lane",
    # Not malformed — it parses cleanly and names TWO gates, which by ruling #14219 j#86718 proves
    # neither. As this action's decision it is exactly as unusable as a malformed body, and the
    # aliases are read as a SET rather than first-non-empty for that reason (#14539 j#91847 F3).
    "a second gate in the other alias":
        f"gate=implementation_request:kind=some_other_gate:"
        f"{DECISION_ACTION_FIELD}=bootstrap_lane",
}


class _CountingPort:
    """Counts sends, so every claim below is about what actually happened."""

    def __init__(self) -> None:
        self.calls: list = []

    def send(self, context, action_id, *, args):
        self.calls.append((context.issue, context.journal, action_id))
        return ProxySendOutcome(result=SEND_DELIVERED, rc=0)


def _row(workspace_id: str, provider: str, locator: str = "w3:p1") -> dict:
    return {
        AGENT_KEY_NAME: encode_assigned_name(workspace_id, provider, ""),
        AGENT_KEY_LOCATOR: locator,
    }


def _attested(_name, *, locator, workspace_id, provider):
    return True, "ok", "startup self-attestation present and generation-matched"


class StrictDecisionBodyTestBase(unittest.TestCase):
    """Drives the whole rail — resolution through fenced send — over an injected journal note."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        (self.repo / ".mozyo-bridge").mkdir(parents=True, exist_ok=True)
        (self.repo / ".mozyo-bridge" / "workflow-role-bindings.json").write_text(
            json.dumps(
                {
                    "schema": SCHEMA_NAME,
                    "version": SCHEMA_VERSION,
                    "bindings": [{"role": ROLE_COORDINATOR, "project_scope": SCOPE}],
                }
            ),
            encoding="utf-8",
        )
        self.fence = CoordinatorProxyFence(Path(self._tmp.name) / "proxy.sqlite")
        self.fence.bootstrap()

    def _context(self, notes, *, action=ACTION_BOOTSTRAP_LANE):
        return resolve_proxy_context(
            argparse.Namespace(repo=str(self.repo)),
            action=action,
            issue=ISSUE,
            journal=JOURNAL,
            repo_root=self.repo,
            env={},
            rows_provider=lambda _env: [_row(WS, "codex")],
            named_journal_provider=lambda _issue, _journal: (notes, True),
            workspace_provider=lambda _root: WS,
            attestation_join=_attested,
            # The issue owns no active lane (the bootstrap precondition) and the lane a dispatch
            # decision names is live at generation 2 — so every link EXCEPT the decision body is
            # deliverable, and the send count answers only the question this issue is about.
            issue_expectation_provider=lambda _root, issue, _decisions, action="": IssueExpectation(
                issue=issue, owns_active_lane=False, latest_decision_journal=""
            ),
            lane_expectation_provider=lambda _root, lane: (
                LaneExpectation(lane=LANE, generation=2, decision_journal=JOURNAL)
                if lane == LANE
                else None
            ),
        )

    def _run(self, notes, *, action=ACTION_BOOTSTRAP_LANE):
        context = self._context(notes, action=action)
        port = _CountingPort()
        result = execute_proxy_delegation(
            context,
            args=argparse.Namespace(repo=str(self.repo), action=action),
            action=action,
            fence=self.fence,
            send_port=port,
        )
        return context, result, port


class CleanProducerOutputStillDelegatesTest(StrictDecisionBodyTestBase):
    """The control. A refusal-only regression is indistinguishable from a rail that refuses all."""

    def test_the_canonical_bootstrap_marker_delegates_exactly_once(self):
        context, result, port = self._run(f"この issue を bootstrap する。\n{BOOTSTRAP_MARKER}\n")
        self.assertEqual(context.links.anchor, ANCHOR_VERIFIED)
        self.assertEqual(result.decision, DELIVER)
        self.assertTrue(result.sent)
        self.assertEqual(len(port.calls), 1)

    def test_the_canonical_dispatch_marker_delegates_exactly_once(self):
        context, result, port = self._run(
            f"次を dispatch する。\n{DISPATCH_MARKER}\n", action=ACTION_DISPATCH_NEXT
        )
        self.assertEqual(context.links.anchor, ANCHOR_VERIFIED)
        self.assertEqual(result.decision, DELIVER)
        self.assertEqual(len(port.calls), 1)
        # The lane-scoped fields survive the strict read verbatim — the reader no longer strips
        # them, because the strict reader has already refused every body that carried whitespace.
        (decision,) = context.decisions
        self.assertEqual((decision.lane, decision.lane_generation), (LANE, "2"))


class ProducerImpossibleBodyIsZeroSendTest(StrictDecisionBodyTestBase):
    """Each measured forgery, taken to the effect entry."""

    def test_every_producer_impossible_body_sends_nothing(self):
        for label, body in PRODUCER_IMPOSSIBLE_BODIES.items():
            with self.subTest(label):
                context, result, port = self._run(f"decision:\n{_marker(body)}\n")
                self.assertEqual(context.links.anchor, ANCHOR_DECISION_UNREADABLE, label)
                self.assertEqual(result.decision, ZERO_SEND, label)
                self.assertEqual(result.reason, REASON_ANCHOR_DECISION_UNREADABLE, label)
                self.assertEqual(port.calls, [], label)

    def test_a_clean_sibling_does_not_rescue_a_forged_claim(self):
        # THE case that separates this fix from the naive one. "Parse strictly and skip what does
        # not parse" reads this note as a single clean decision and DELIVERS: the forged marker is
        # dropped before the exactly-one count, so the note that should have been at least
        # ambiguous becomes unambiguous. The claim is refused whole instead — asked of the RAW
        # components, because "does this marker claim this gate" and "is its body readable" are
        # different questions.
        forged = _marker(PRODUCER_IMPOSSIBLE_BODIES["repeated gate (last-write-wins)"])
        for label, notes in {
            "forged first": f"{forged}\n{BOOTSTRAP_MARKER}\n",
            "forged second": f"{BOOTSTRAP_MARKER}\n{forged}\n",
        }.items():
            with self.subTest(label):
                context, result, port = self._run(notes)
                self.assertEqual(context.links.anchor, ANCHOR_DECISION_UNREADABLE, label)
                self.assertEqual(result.reason, REASON_ANCHOR_DECISION_UNREADABLE, label)
                self.assertEqual(port.calls, [], label)

    def test_a_forged_claim_the_reader_could_have_dropped_is_still_refused(self):
        # The same forgery alone. It zero-sends under either implementation, so it proves nothing
        # on its own — it is here because the reason must be the UNREADABLE one rather than
        # "this journal carries no decision": an operator told the latter would add a marker,
        # and adding one to this note is precisely what must not restore authority.
        forged = _marker(PRODUCER_IMPOSSIBLE_BODIES["whitespace-contaminated fields"])
        context, result, port = self._run(f"{forged}\n")
        self.assertEqual(context.links.anchor, ANCHOR_DECISION_UNREADABLE)
        self.assertEqual(result.reason, REASON_ANCHOR_DECISION_UNREADABLE)
        self.assertEqual(port.calls, [])


class DecisionCardinalityIsPreservedTest(StrictDecisionBodyTestBase):
    """0 / 1 / 2+ and the quotation contract survive the strict read unchanged."""

    def test_zero_decisions_send_nothing(self):
        context, result, port = self._run("進捗のみ。marker は無い。\n")
        self.assertEqual(context.links.anchor, ANCHOR_UNVERIFIED)
        self.assertEqual(result.reason, REASON_ANCHOR_UNVERIFIED)
        self.assertEqual(port.calls, [])

    def test_two_decisions_send_nothing(self):
        context, result, port = self._run(f"{BOOTSTRAP_MARKER}\n{BOOTSTRAP_MARKER}\n")
        self.assertEqual(context.links.anchor, ANCHOR_DECISION_AMBIGUOUS)
        self.assertEqual(result.reason, REASON_ANCHOR_DECISION_AMBIGUOUS)
        self.assertEqual(port.calls, [])

    def test_a_quoted_marker_is_neither_authority_nor_poison(self):
        # Both halves of the #14585 contract, in one note: the quoted forgery must not become a
        # decision AND must not poison the real one beside it. A strict reader that scanned the
        # raw note would refuse this journal, which is the failure direction that made an issue
        # permanently unusable (Design Answer j#90329 contract 5).
        forged = _marker(PRODUCER_IMPOSSIBLE_BODIES["repeated gate (last-write-wins)"])
        context, result, port = self._run(
            f"例 (引用):\n\n> {forged}\n\n実際の decision:\n\n{BOOTSTRAP_MARKER}\n"
        )
        self.assertEqual(context.links.anchor, ANCHOR_VERIFIED)
        self.assertEqual(result.decision, DELIVER)
        self.assertEqual(len(port.calls), 1)

    def test_a_marker_spliced_across_a_quotation_is_not_a_decision(self):
        """The per-line property, pinned on a note that actually depends on it.

        The marker body grammar is ``[^\\]]*``, which spans newlines, so a scan of the blanked note
        as ONE string lets an unclosed ``[mozyo:`` close on a ``]`` further down and parse as a
        marker no single line contains. ``test_a_marker_cannot_be_spliced_together_across_a_quoted
        _region`` (the #14546 integration suite) is written with the closing bracket on the line
        directly under a blockquote — where it is itself swallowed as a lazy continuation, so no
        marker exists under EITHER scanning strategy and the case passes without exercising the
        property it names. Measured: replacing the per-line scan with a joined-text scan leaves
        that case green.

        Here the bracket sits after a blank line, outside the quotation. A joined scan produces a
        marker whose last value carries the swallowed newlines; the strict component rule refuses
        it, so authority is defended twice over — but the two strategies then disagree about the
        REASON (a spliced non-marker vs a same-kind claim that poisons the journal), and only the
        per-line scan gives the true one. Nothing was spliced, so nothing was claimed.
        """
        context, result, port = self._run(
            f"{BOOTSTRAP_MARKER[:-1]}\n> quoted noise\n\n]\n"
        )
        self.assertEqual(context.links.anchor, ANCHOR_UNVERIFIED)
        self.assertEqual(result.reason, REASON_ANCHOR_UNVERIFIED)
        self.assertEqual(port.calls, [])

    def test_a_quoted_decision_alone_sends_nothing(self):
        for label, notes in {
            "blockquote": f"> {BOOTSTRAP_MARKER}",
            "inline span": f"grammar は `{BOOTSTRAP_MARKER}` である",
            "fenced": f"```\n{BOOTSTRAP_MARKER}\n```",
        }.items():
            with self.subTest(label):
                context, result, port = self._run(notes)
                self.assertEqual(context.links.anchor, ANCHOR_UNVERIFIED, label)
                self.assertEqual(port.calls, [], label)


class ForeignActionIsZeroSendTest(StrictDecisionBodyTestBase):
    """``proxy_action`` binds a decision to ONE action; a readable body naming another authorizes
    nothing (Design Answer j#90329 contract 5)."""

    def test_a_decision_for_the_other_action_sends_nothing(self):
        context, result, port = self._run(f"{DISPATCH_MARKER}\n", action=ACTION_BOOTSTRAP_LANE)
        self.assertEqual(context.links.anchor, ANCHOR_ACTION_MISMATCH)
        self.assertEqual(result.reason, REASON_ANCHOR_ACTION_MISMATCH)
        self.assertEqual(port.calls, [])

    def test_a_decision_omitting_the_action_field_sends_nothing(self):
        context, result, port = self._run(f"{_marker('gate=implementation_request')}\n")
        self.assertEqual(context.links.anchor, ANCHOR_ACTION_MISMATCH)
        self.assertEqual(port.calls, [])

    def test_an_out_of_vocabulary_action_value_sends_nothing(self):
        body = f"gate=implementation_request:{DECISION_ACTION_FIELD}=retire_everything"
        context, result, port = self._run(f"{_marker(body)}\n")
        self.assertEqual(context.links.anchor, ANCHOR_ACTION_MISMATCH)
        self.assertEqual(port.calls, [])


class TheStrictnessIsTheSharedReadersTest(unittest.TestCase):
    """No private strictness axis lives in this rail — the grammar verdict is the shared one.

    Stated as a property rather than as a list of bodies: for every body, "does this rail read a
    decision here" agrees with "does the SHARED strict reader read exactly this action's gate
    here". A rule added on either side alone breaks it. That is the guard the module comment can
    only promise: the previous reader also documented that the quotation rules were shared, and it
    was the parse it accepted from its caller that was not.
    """

    def _shared_verdict(self, notes: str) -> bool:
        """Whether the shared authority alone counts exactly one ``implementation_request``."""
        counted = 0
        for channel, components in marker_components_in_note(notes):
            if channel != "workflow-event":
                continue
            if marker_logical_gates(strict_marker_fields(components)) == {
                "implementation_request"
            }:
                counted += 1
        return counted == 1

    def test_the_rails_verdict_equals_the_shared_readers_for_every_body(self):
        bodies = dict(PRODUCER_IMPOSSIBLE_BODIES)
        bodies["canonical producer output"] = (
            f"gate=implementation_request:{DECISION_ACTION_FIELD}=bootstrap_lane"
        )
        for label, body in bodies.items():
            with self.subTest(label):
                notes = f"{_marker(body)}\n"
                decision, _refusal = canonical_decision_in_journal(
                    notes, action=ACTION_BOOTSTRAP_LANE
                )
                self.assertEqual(decision is not None, self._shared_verdict(notes), label)

    def test_a_field_repeated_with_the_SAME_value_is_one_declaration(self):
        """The shared reader's rule, named here so it is a decision rather than an accident.

        ``strict_marker_fields`` collapses a key repeated with an identical value and refuses one
        repeated with a different one — the central `### Hibernate Evidence Marker Contract`'s
        "完全に同一の重複は 1 件に畳んでよい". No canonical producer emits the duplicate either,
        so a stricter "any repetition at all" rule is defensible; it is not adopted HERE because
        it would make this rail disagree with every sibling consumer of the same reader about what
        a producer-impossible body is, which is the drift that produced this issue. If the rule
        should change, it changes in the shared reader and this case moves with it.
        """
        body = (
            "gate=implementation_request:gate=implementation_request:"
            f"{DECISION_ACTION_FIELD}=bootstrap_lane"
        )
        decision, refusal = canonical_decision_in_journal(
            f"{_marker(body)}\n", action=ACTION_BOOTSTRAP_LANE
        )
        self.assertEqual(refusal, "")
        self.assertIsNotNone(decision)

    def test_the_reader_takes_no_injectable_parser(self):
        """The seam that let a test choose the grammar is gone (Redmine #14667).

        While it existed, both callers in the committed tests passed the LENIENT fold, so a strict
        reader could have been written and never exercised. Pinned as a signature property because
        re-adding the parameter is exactly how the leniency would come back.
        """
        import inspect

        parameters = inspect.signature(canonical_decision_in_journal).parameters
        self.assertEqual(list(parameters), ["notes", "action"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
