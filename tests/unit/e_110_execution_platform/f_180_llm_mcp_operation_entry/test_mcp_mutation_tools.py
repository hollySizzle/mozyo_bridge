"""Mutating tool handlers + the mutating-declaration guard (Redmine #15152).

Two claims, each pinned structurally:

1. **Projection discipline.** The mutating handlers republish closed tokens and
   caller-supplied identities ONLY. Producer free text (the CLI ``die``
   message, an outcome's ``next_action`` prose) and pane / path evidence
   (``target``, ``worktree_path``, ``gateway_pane``, ``steps``) are dropped —
   the #15151 r4f3 / r5f1 allowlist discipline, applied to the mutating
   surface.
2. **The guard still guards.** ``read_only=False`` is publishable only for the
   closed ``MUTATING_TOOL_NAMES`` declaration, the declaration must not claim
   read-only, and the forbidden-token check applies to a mutating tool's input
   schema exactly as it does to a read tool's.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.mutation_tools import (  # noqa: E402,E501
    HANDOFF_REFUSAL_SENTENCE,
    run_handoff_reply,
    run_handoff_send,
    run_sublane_start_tool,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.read_plan_tools import (  # noqa: E402,E501
    ReadPlanContext,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.tool_catalog import (  # noqa: E402,E501
    MUTATING_TOOL_NAMES,
    TOOL_CATALOG,
    ToolDefinition,
    catalog_surface_violations,
)


def _context() -> ReadPlanContext:
    return ReadPlanContext(repo_root=Path("/nonexistent-repo"))


def _delivery_outcome(**overrides):
    from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (  # noqa: E501
        DeliveryOutcome,
    )

    fields = dict(
        status="blocked",
        reason="invalid_anchor",
        receiver="codex",
        target="%7",  # pane evidence: must never be republished
        source="redmine",
        anchor={"source": "redmine", "issue": "15152", "journal": "1"},
        mode="queue-enter",
        kind="reply",
        next_action_owner="sender",
        next_action="repair the anchor then re-run from pane %7",  # producer prose
        notification_marker=None,
    )
    fields.update(overrides)
    return DeliveryOutcome(**fields)


class HandoffProjectionTests(unittest.TestCase):
    # A coherent caller input. #15152 R9 (review j#107091 finding_effectiveinputprojection):
    # the projection sources receiver/kind/anchor/marker from the EFFECTIVE,
    # domain-validated DeliveryOutcome the shared layer returns — never this raw
    # input (which precedes the entry policy). The input only reaches the shared
    # `run_handoff`, patched away in these tests.
    _INPUT_ARGS = {
        "to": "codex",
        "source": "redmine",
        "issue": "15152",
        "journal": "1",
        "kind": "review_request",
    }

    def _run_with_result(self, result, args=None):
        import mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_application_service as api  # noqa: E501

        with patch.object(api, "run_handoff", return_value=result):
            return run_handoff_send(dict(args or self._INPUT_ARGS), _context())

    def _fail_closed_result(self, outcome):
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_application_service import (  # noqa: E501
            HandoffResult,
        )

        return HandoffResult(
            operation="send",
            status="fail_closed",
            exit_code=2,
            outcome=outcome,
            delivered=False,
            error_message="died: pane %7 at /private/runtime/checkout is busy",
        )

    def test_the_gate_message_is_never_forwarded(self) -> None:
        outcome = self._run_with_result(self._fail_closed_result(_delivery_outcome()))

        body = json.dumps(outcome.payload)
        self.assertTrue(outcome.is_error)
        self.assertNotIn("/private/runtime", body)
        self.assertNotIn("%7", body)
        self.assertEqual(HANDOFF_REFUSAL_SENTENCE, outcome.payload["refusal"])

    def test_the_outcome_projection_is_the_allowlist(self) -> None:
        # #15152 R9: every published member is the EFFECTIVE outcome fact, not the
        # raw input. The blocked fixture carries kind="reply" and no marker (a
        # blocked terminal never formed an envelope), so THOSE — not the input's
        # kind="review_request" — are what the projection publishes.
        outcome = self._run_with_result(self._fail_closed_result(_delivery_outcome()))

        projected = outcome.payload["outcome"]
        # Closed producer tokens (from the outcome) survive...
        self.assertEqual("blocked", projected["status"])
        self.assertEqual("invalid_anchor", projected["reason"])
        # ...effective caller-echo comes from the OUTCOME, not the input...
        self.assertEqual("codex", projected["receiver"])
        self.assertEqual("reply", projected["kind"])  # the outcome's effective kind
        self.assertEqual(
            {"source": "redmine", "issue": "15152", "journal": "1"},
            projected["anchor"],
        )
        # ...a blocked terminal formed no envelope, so no marker is published...
        self.assertNotIn("notification_marker", projected)
        # ...pane evidence and producer prose do not appear.
        self.assertNotIn("target", projected)
        self.assertNotIn("next_action", projected)
        self.assertNotIn("execution_root", projected)

    def test_a_delivered_completion_reports_delivered_true(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_application_service import (  # noqa: E501
            HandoffResult,
        )

        outcome = self._run_with_result(
            HandoffResult(
                operation="send",
                status="completed",
                exit_code=0,
                outcome=_delivery_outcome(status="sent", reason="ok"),
                delivered=True,
            )
        )

        self.assertFalse(outcome.is_error)
        self.assertTrue(outcome.payload["delivered"])
        self.assertEqual("", outcome.payload["refusal"])

    def test_hostile_producer_drift_never_reaches_the_public_surface(self) -> None:
        # #15152 R7 (review j#107015 finding_handoffprojectionopen): DeliveryOutcome
        # `Literal` types are NOT runtime guards; a producer contract drift can put a
        # private path in `reason` or an off-set `status`/`mode`. None may reach the
        # structured payload OR the text summary; each maps to its closed set / token.
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_application_service import (  # noqa: E501
            HandoffResult,
        )

        hostile = _delivery_outcome(
            status="/private/runtime/pane-%7",
            reason="workspace anchor unreadable (/private/runtime/secret at %5)",
            mode="internal_mode_alpha",
            next_action_owner="operator_beta",
            receiver="codex",
        )
        outcome = self._run_with_result(
            HandoffResult(
                operation="send",
                status="internal_state_gamma",  # off-set top-level status
                exit_code=0,
                outcome=hostile,
                delivered="false",  # string, must not read as delivered
            )
        )

        body = json.dumps(outcome.payload)
        for sentinel in (
            "/private/runtime",
            "%5",
            "%7",
            "internal_mode_alpha",
            "operator_beta",
            "internal_state_gamma",
            "unreadable",
        ):
            self.assertNotIn(sentinel, body, sentinel)
            self.assertNotIn(sentinel, outcome.summary, sentinel)
        projected = outcome.payload["outcome"]
        self.assertEqual("unknown_status", projected["status"])
        self.assertEqual("unknown_reason", projected["reason"])
        self.assertEqual("unknown_mode", projected["mode"])
        self.assertEqual("unknown_next_action_owner", projected["next_action_owner"])
        # Top-level result status and delivered are closed / exact-bool too.
        self.assertEqual("unknown_status", outcome.payload["status"])
        self.assertIs(False, outcome.payload["delivered"])

    # --- #15152 R9 (review j#107091 finding_effectiveinputprojection) ---------- #
    # The projection MUST use the EFFECTIVE, domain-validated public facts the
    # shared application layer adopted — the returned DeliveryOutcome — as its sole
    # authority. It must publish the effective kind and the canonical envelope
    # marker on success, and NO marker on a domain refusal (the R9 bug rebuilt a
    # bogus marker from raw args for a refused op, and dropped the effective kind
    # of a kind-omitted reply). Every assertion covers BOTH structuredContent and
    # the text summary.

    _CANONICAL_REPLY_MARKER = (
        "[mozyo:handoff:source=redmine:issue=15152:journal=1:kind=reply:to=codex]"
    )

    def _completed_result(self, outcome):
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_application_service import (  # noqa: E501
            HandoffResult,
        )

        return HandoffResult(
            operation="reply",
            status="completed",
            exit_code=0,
            outcome=outcome,
            delivered=True,
        )

    def _run_reply_with_result(self, result, args=None):
        import mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_application_service as api  # noqa: E501

        with patch.object(api, "run_handoff", return_value=result):
            return run_handoff_reply(dict(args or self._INPUT_ARGS), _context())

    def test_kind_omitted_reply_publishes_effective_kind_and_canonical_marker(self) -> None:
        # (a) A `handoff reply` with `--kind` OMITTED is executed as a reply
        # (effective kind "reply", via the operation's default_kind) and the run
        # forms the canonical envelope. The projection must publish kind="reply"
        # AND that canonical marker — the R9 raw-input binding published neither
        # (raw kind was None). The caller omits `kind` entirely here.
        effective = _delivery_outcome(
            status="sent",
            reason="ok",
            kind="reply",  # the EFFECTIVE kind the shared layer adopted
            notification_marker=self._CANONICAL_REPLY_MARKER,
        )
        args = {"to": "codex", "source": "redmine", "issue": "15152", "journal": "1"}
        outcome = self._run_reply_with_result(self._completed_result(effective), args)

        projected = outcome.payload["outcome"]
        self.assertEqual("reply", projected["kind"])
        self.assertEqual(self._CANONICAL_REPLY_MARKER, projected["notification_marker"])
        # The canonical marker also survives into the serialized body.
        self.assertIn(self._CANONICAL_REPLY_MARKER, json.dumps(outcome.payload))

    def test_invalid_delimiter_receiver_refusal_emits_no_marker(self) -> None:
        # (b) `--to codex:evil` is schema-valid (minLength only) but the DOMAIN
        # refuses it with invalid_args before any envelope is formed. The blocked
        # terminal carries notification_marker=None. NO marker — and no
        # marker-shaped string at all — may appear anywhere. The R9 code applied
        # build_marker to the raw `to` and returned a bogus marker for this
        # REFUSED operation.
        refused = _delivery_outcome(
            status="blocked",
            reason="invalid_args",
            receiver="codex:evil",  # the domain-refused receiver, echoed as-is
            anchor=None,
            notification_marker=None,
        )
        args = {
            "to": "codex:evil",
            "source": "redmine",
            "issue": "15152",
            "journal": "1",
            "kind": "reply",
        }
        outcome = self._run_reply_with_result(self._fail_closed_result(refused), args)

        projected = outcome.payload["outcome"]
        self.assertNotIn("notification_marker", projected)
        body = json.dumps(outcome.payload)
        self.assertNotIn("[mozyo:handoff:", body)
        self.assertNotIn("[mozyo:handoff:", outcome.summary)

    def test_delimiter_or_duplicate_field_input_yields_no_bogus_marker(self) -> None:
        # (c) A delimiter / duplicate-field-shaped input (an anchor field carrying
        # `:` / `=` / a `to=`-shaped fragment) is refused before the envelope; the
        # published outcome carries no marker. The R9 code fed these raw args to
        # build_marker and produced a duplicate/delimiter-laden marker-shaped
        # string. Nothing marker-shaped may appear.
        refused = _delivery_outcome(
            status="blocked",
            reason="invalid_args",
            receiver="codex",
            anchor=None,
            notification_marker=None,
        )
        args = {
            "to": "codex",
            "source": "redmine",
            "issue": "15152:to=mallory",  # delimiter / duplicate-field injection
            "journal": "1=x",
            "kind": "reply",
        }
        outcome = self._run_reply_with_result(self._fail_closed_result(refused), args)

        body = json.dumps(outcome.payload)
        self.assertNotIn("[mozyo:handoff:", body)
        self.assertNotIn("[mozyo:handoff:", outcome.summary)
        self.assertNotIn("to=mallory", body)
        self.assertNotIn("to=mallory", outcome.summary)
        self.assertNotIn("notification_marker", outcome.payload["outcome"])

    def test_pre_envelope_refusal_publishes_no_marker(self) -> None:
        # (d) A gate refusing before any DeliveryOutcome is published leaves
        # result.outcome None; the projection is empty and carries no marker.
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_application_service import (  # noqa: E501
            HandoffResult,
        )

        result = HandoffResult(
            operation="reply",
            status="fail_closed",
            exit_code=2,
            outcome=None,
            delivered=False,
            error_message="died before the envelope at /private/runtime pane %7",
        )
        outcome = self._run_reply_with_result(result)

        self.assertEqual({}, outcome.payload["outcome"])
        body = json.dumps(outcome.payload)
        self.assertNotIn("[mozyo:handoff:", body)
        self.assertNotIn("[mozyo:handoff:", outcome.summary)

    def test_caller_facing_fields_are_sealed_against_producer_drift(self) -> None:
        # #15152 R11 (review j#107115 finding_outcomeprojectionunsealed): the
        # DeliveryOutcome is a plain dataclass with NO runtime validator, so an
        # ordinary producer contract drift can put a private path in receiver /
        # kind / an anchor value / a marker-shaped notification. The projection
        # SEALS each caller-facing field against a closed vocabulary / grammar
        # (source-independently — neither raw input nor raw outcome is trusted) and
        # correlates the marker with the canonical envelope; every drifted value is
        # dropped from BOTH the structured payload and the text summary.
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_application_service import (  # noqa: E501
            HandoffResult,
        )

        hostile = _delivery_outcome(
            status="sent",
            reason="ok",
            receiver="/private/runtime/evilrcv",
            kind="/private/runtime/evilkind",
            anchor={
                "source": "/private/src",
                "issue": "/private/runtime/evilissue",
                "journal": "%9",
            },
            notification_marker="[mozyo:handoff:leaked /private/runtime/pane-%9 to=mallory]",
        )
        outcome = self._run_with_result(
            HandoffResult(
                operation="send", status="completed", exit_code=0, outcome=hostile, delivered=True
            )
        )

        body = json.dumps(outcome.payload)
        for sentinel in (
            "/private/runtime",
            "/private/src",
            "%9",
            "evilrcv",
            "evilkind",
            "evilissue",
            "mallory",
            "leaked",
        ):
            self.assertNotIn(sentinel, body, sentinel)
            self.assertNotIn(sentinel, outcome.summary, sentinel)
        projected = outcome.payload["outcome"]
        self.assertNotIn("receiver", projected)
        self.assertNotIn("kind", projected)
        self.assertNotIn("anchor", projected)
        self.assertNotIn("notification_marker", projected)

    def test_a_marker_that_does_not_correlate_is_dropped(self) -> None:
        # #15152 R11: even when receiver / kind / anchor are individually valid, the
        # marker is republished ONLY if it byte-equals the canonical envelope rebuilt
        # from those validated parts. A marker that drifted (an injected field, an
        # extra segment) does not correlate and is dropped — so a private value
        # smuggled into an otherwise-valid-looking marker never reaches the surface.
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_application_service import (  # noqa: E501
            HandoffResult,
        )

        drifted_marker = self._CANONICAL_REPLY_MARKER[:-1] + ":injected=/private/x]"
        outcome = self._run_reply_with_result(
            self._completed_result(
                _delivery_outcome(
                    status="sent",
                    reason="ok",
                    receiver="codex",
                    kind="reply",
                    notification_marker=drifted_marker,
                )
            ),
            {"to": "codex", "source": "redmine", "issue": "15152", "journal": "1"},
        )

        projected = outcome.payload["outcome"]
        # The valid caller-facing fields still publish...
        self.assertEqual("codex", projected["receiver"])
        self.assertEqual("reply", projected["kind"])
        # ...but the non-correlating marker (and its smuggled value) do not.
        self.assertNotIn("notification_marker", projected)
        body = json.dumps(outcome.payload)
        self.assertNotIn("/private/x", body)
        self.assertNotIn("/private/x", outcome.summary)
        self.assertNotIn("injected=", body)

    def test_kind_and_anchor_are_sealed_to_the_mcp_closed_contract(self) -> None:
        # #15152 R12 (review j#107173 finding_projectiongrammaropen): the seal is THIS
        # MCP surface's exact closed contract, not a generic grammar. A kind matching
        # the old [a-z_]+ grammar but OUTSIDE KIND_LABELS, a trailing-newline kind, an
        # asana anchor, a colon-bearing / non-decimal id, and a partial anchor are all
        # rejected atomically.
        from types import SimpleNamespace as NS

        from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.mutation_tools import (  # noqa: E501
            _sealed_anchor,
            _sealed_kind,
        )

        self.assertEqual("reply", _sealed_kind(NS(kind="reply")))
        self.assertEqual("review_request", _sealed_kind(NS(kind="review_request")))
        self.assertIsNone(_sealed_kind(NS(kind="arbitrarykind")))  # matched the old grammar
        self.assertIsNone(_sealed_kind(NS(kind="reply\n")))  # a `$` anchor let this through
        self.assertIsNone(_sealed_kind(NS(kind="Reply")))
        self.assertEqual(
            {"source": "redmine", "issue": "15152", "journal": "1"},
            _sealed_anchor(NS(anchor={"source": "redmine", "issue": "15152", "journal": "1"})),
        )
        for bad in (
            {"source": "asana", "task_id": "T1", "comment_id": "C1"},  # not this surface
            {"source": "redmine", "issue": "15152", "journal": "1", "task_id": "T1"},  # asana field
            {"source": "redmine", "issue": "15152:to=mallory", "journal": "1"},  # colon
            {"source": "redmine", "issue": "15152", "journal": "1\n"},  # trailing newline
            {"source": "redmine", "issue": "15152"},  # partial
            {"source": "redmine", "issue": 15152, "journal": "1"},  # wrong type (non-coercing)
        ):
            self.assertEqual({}, _sealed_anchor(NS(anchor=bad)), bad)

    def test_a_contradictory_blocked_outcome_publishes_no_marker(self) -> None:
        # #15152 R12 (finding_markercorrelationselfattested): a fail-closed result whose
        # outcome carries a self-consistent marker (built from valid parts) must NOT
        # publish the marker — the phase authority is the shared layer's result status
        # (fail_closed), read here, not the outcome's own self-correlation.
        contradictory = _delivery_outcome(
            status="blocked",
            reason="invalid_anchor",
            receiver="codex",
            kind="reply",
            notification_marker=self._CANONICAL_REPLY_MARKER,  # self-consistent, but the run refused
        )
        outcome = self._run_reply_with_result(
            self._fail_closed_result(contradictory),
            {"to": "codex", "source": "redmine", "issue": "15152", "journal": "1"},
        )

        projected = outcome.payload["outcome"]
        self.assertNotIn("notification_marker", projected)
        body = json.dumps(outcome.payload)
        self.assertNotIn("[mozyo:handoff:", body)
        self.assertNotIn("[mozyo:handoff:", outcome.summary)
        self.assertNotIn("/private/runtime", body)
        self.assertNotIn("%7", body)


class RealOrchestrationHandoffTests(unittest.TestCase):
    """#15152 R10 (review j#107115 finding_outcomeprojectionunsealed): the
    ``HandoffProjectionTests`` above MOCK ``run_handoff`` and hand back a hand-built
    ``DeliveryOutcome``, so they pin the PROJECTION but not that the REAL shared
    orchestration supplies the effective kind + canonical marker. R10 required a
    regression that drives the REAL ``run_handoff`` from the MCP tool entrypoints
    (``run_handoff_reply`` / ``run_handoff_send``) with ONLY the low-level terminal
    transport stubbed — no ``patch.object(api, "run_handoff", ...)``.

    The stub seam is the one the CLI's own fake-tmux orchestrator tests use
    (``tests/integration/e_110_execution_platform/f_130_handoff_routing/
    test_handoff_orchestrator.py`` + its package ``__init__`` transport-isolation
    fixture): the tmux send/capture primitives in ``mozyo_bridge.application.commands``
    (``require_tmux`` / ``capture_pane`` / ``run_tmux`` / ``time.sleep`` /
    ``current_session_name``) and the ``pane_resolver`` snapshot are patched, the
    ``handoff_transport_wiring`` resolvers are pinned to the tmux default so the
    committed herdr cutover config does not drive the send, and the live Redmine
    anchor source is replaced by the hermetic ``matching_redmine_anchor_source``
    ownership fixture. Everything BETWEEN the MCP entrypoint and those primitives —
    the entry policy (effective ``default_kind``), receiver/anchor gates, target
    resolution, admission pipeline, envelope planning, and ``build_marker`` — runs
    for real. The single real terminal that types the marker+body is the patched
    ``run_tmux``; the assertion is on the MCP tool's PROJECTED payload.
    """

    @classmethod
    def setUpClass(cls) -> None:
        # A real identity-bearing workspace: the MCP `handoff_reply` tool sends the
        # default `target_repo=auto`, but `auto` requires an explicit `%pane` (which
        # this surface deliberately has no property for), so a real end-to-end send
        # is reached by naming `target_repo` at a workspace whose resolved receiver
        # pane cwd matches. A git repo is the cheapest such identity marker.
        cls._repo_dir = tempfile.mkdtemp(prefix="mcp_real_handoff_")
        subprocess.run(
            ["git", "init", "-q", cls._repo_dir],
            check=True,
            capture_output=True,
        )
        cls.REPO = str(Path(cls._repo_dir).resolve())

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls._repo_dir, ignore_errors=True)

    #: The resolved receiver pane the stubbed pane snapshot returns. `pane_active=1`
    #: makes it the active split so the queue-enter rail (the only mode this surface
    #: uses) does not fail closed on an inactive split; `window_name=codex` is what
    #: the receiver-label resolver binds `to=codex` against; `cwd=REPO` is what the
    #: `--target-repo` identity gate observes.
    def _pane(self) -> dict:
        return {
            "id": "%2",
            "location": "agents:0.1",
            "command": "node",
            "cwd": self.REPO,
            "window_name": "codex",
            "pane_active": "1",
        }

    def _run_real(self, fn, arguments):
        """Drive ``fn`` (an MCP handoff tool) through the REAL orchestration.

        Only the terminal transport + environment probes are stubbed. Returns the
        tool's :class:`ToolOutcome` plus the recorded tmux ``send-keys`` calls so a
        caller can prove the real rail typed the marker.
        """
        from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.read_plan_tools import (  # noqa: E501
            ReadPlanContext,
        )
        from tests.support.redmine_anchor_authority import (
            matching_redmine_anchor_source_patch,
        )

        pane_value = self._pane()
        sent: list = []
        pane_text = {"buf": ""}

        def fake_capture(_target: str, _lines: int) -> str:
            return pane_text["buf"]

        def fake_run_tmux(*tmux_args: str, check: bool = True):
            # The ONE real terminal, stubbed: record the send-keys, and model a
            # well-behaved receiver whose pane advances on Enter (the queue-enter
            # turn-start observation confirms). No keystroke leaves the process.
            if tmux_args[:4] == ("send-keys", "-t", "%2", "-l"):
                pane_text["buf"] += tmux_args[-1]
                sent.append(tmux_args)
                return argparse.Namespace(returncode=0, stdout="", stderr="")
            if tmux_args[:3] == ("send-keys", "-t", "%2"):
                if tmux_args[-1] == "Enter":
                    pane_text["buf"] += "\n<codex-turn-started>"
                sent.append(tmux_args)
                return argparse.Namespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected tmux call: {tmux_args}")

        pane_resolver = (
            "mozyo_bridge.e_110_execution_platform."
            "f_120_agent_discovery_pane_resolution.domain.pane_resolver."
        )
        patches = [
            # Transport isolation (Redmine #13254): pin the resolver to the tmux
            # default so the committed herdr cutover config does not route the send.
            mock.patch(
                "mozyo_bridge.application.handoff_transport_wiring."
                "resolve_handoff_transport_binding",
                return_value=None,
            ),
            mock.patch(
                "mozyo_bridge.application.handoff_transport_wiring."
                "resolve_handoff_transport_runtime",
                return_value=(None, None),
            ),
            mock.patch(
                "mozyo_bridge.application.commands.herdr_effective_backend_selected",
                return_value=False,
            ),
            # Hermetic Redmine ownership: the production anchor-authority gate still
            # executes; only its live source constructor is replaced.
            matching_redmine_anchor_source_patch(),
            # The low-level tmux terminal + environment probes.
            patch("mozyo_bridge.application.commands.require_tmux"),
            patch(
                "mozyo_bridge.application.commands.capture_pane",
                side_effect=fake_capture,
            ),
            patch(
                "mozyo_bridge.application.commands.run_tmux",
                side_effect=fake_run_tmux,
            ),
            patch("mozyo_bridge.application.commands.time.sleep"),
            patch(
                "mozyo_bridge.application.commands.current_session_name",
                return_value="agents",
            ),
            patch(pane_resolver + "current_session_name", return_value="agents"),
            patch(pane_resolver + "validate_target"),
            patch(pane_resolver + "pane_lines", return_value=[pane_value]),
        ]
        for started in patches:
            started.start()
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                outcome = fn(dict(arguments), ReadPlanContext(repo_root=Path(self.REPO)))
        finally:
            for started in reversed(patches):
                started.stop()
        return outcome, sent

    def test_real_run_handoff_kind_omitted_reply_supplies_effective_kind_and_marker(
        self,
    ) -> None:
        # (1) A `handoff reply` with `kind` OMITTED, driven through the REAL
        # `run_handoff`: the operation's entry policy supplies the effective
        # `default_kind` ("reply"), the run forms the canonical envelope, and the
        # real `build_marker` produces the canonical marker. The projection must
        # publish kind="reply" AND that exact canonical marker — proven here by the
        # real path, not a hand-built DeliveryOutcome.
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (  # noqa: E501
            build_marker,
            normalize_anchor,
        )
        from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.mutation_tools import (  # noqa: E501
            run_handoff_reply,
        )

        arguments = {
            "to": "codex",
            "source": "redmine",
            "issue": "15152",
            "journal": "1",
            "target_repo": self.REPO,
            # `kind` deliberately omitted — the effective kind must come from the
            # reply operation's default_kind, supplied by the real entry policy.
        }
        outcome, sent = self._run_real(run_handoff_reply, arguments)

        self.assertFalse(outcome.is_error, outcome.payload)
        self.assertEqual("completed", outcome.payload["status"])
        projected = outcome.payload["outcome"]
        # The effective kind the shared layer adopted for an omitted --kind reply.
        self.assertEqual("reply", projected["kind"])
        # The canonical marker, correlated with build_marker over the effective
        # anchor / kind / receiver (the projection republishes it only when it
        # byte-equals this reconstruction), AND stated as the exact expected string.
        expected_marker = build_marker(
            normalize_anchor("redmine", issue="15152", journal="1"),
            "reply",
            "codex",
        )
        self.assertEqual(
            "[mozyo:handoff:source=redmine:issue=15152:journal=1:kind=reply:to=codex]",
            expected_marker,
        )
        self.assertEqual(expected_marker, projected["notification_marker"])
        self.assertIn(expected_marker, json.dumps(outcome.payload))
        # The one real terminal actually typed the canonical marker+body then Enter.
        self.assertTrue(
            any(call[:4] == ("send-keys", "-t", "%2", "-l") for call in sent), sent
        )
        self.assertIn(("send-keys", "-t", "%2", "Enter"), sent)

    def test_real_run_handoff_pre_envelope_refusal_publishes_no_marker(self) -> None:
        # (2) The REAL `run_handoff` driven into a domain refusal BEFORE an envelope
        # forms: a redmine anchor missing its journal is rejected as invalid_anchor
        # by the real anchor planner, which publishes a blocked DeliveryOutcome whose
        # notification_marker is None. No marker — and no `[mozyo:handoff:` string at
        # all — may appear in structuredContent or the text summary. Proven by the
        # real refusal path, not a hand-built outcome.
        from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.mutation_tools import (  # noqa: E501
            run_handoff_reply,
        )

        arguments = {
            "to": "codex",
            "source": "redmine",
            "issue": "15152",
            # `journal` omitted: the redmine anchor is structurally invalid, so the
            # real orchestration fails closed before any envelope / marker is built.
            "target_repo": self.REPO,
        }
        outcome, sent = self._run_real(run_handoff_reply, arguments)

        self.assertTrue(outcome.is_error)
        self.assertEqual("fail_closed", outcome.payload["status"])
        projected = outcome.payload["outcome"]
        # A blocked terminal was published (the refusal is a real domain outcome),
        # but it formed no envelope, so it carries no marker.
        self.assertEqual("invalid_anchor", projected["reason"])
        self.assertNotIn("notification_marker", projected)
        body = json.dumps(outcome.payload)
        self.assertNotIn("[mozyo:handoff:", body)
        self.assertNotIn("[mozyo:handoff:", outcome.summary)
        # The real refusal happened before delivery: no keystroke was ever sent.
        self.assertEqual([], sent)


class SublaneProjectionTests(unittest.TestCase):
    def _actuation_outcome(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
            ActuationStep,
            SublaneActuationOutcome,
        )

        return SublaneActuationOutcome(
            status="ready",
            execute=False,
            reason="plan",
            issue="15152",
            lane_label="issue_15152_probe",
            branch="issue_15152_probe",
            worktree_path="/private/runtime/.worktrees/issue_15152_probe",
            gateway_pane="%3",
            worker_pane="%4",
            dispatch_target="%3",
            steps=(
                ActuationStep(
                    order=1,
                    title="worktree",
                    status="ready",
                    detail="would run",
                    command="git worktree add /private/runtime/.worktrees/x b",
                ),
            ),
        )

    def test_the_outcome_projection_drops_panes_paths_and_steps(self) -> None:
        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_start_service as svc  # noqa: E501

        result = svc.SublaneStartResult(
            status=svc.STATUS_COMPLETED, exit_code=0, outcome=self._actuation_outcome()
        )
        with patch.object(svc, "run_sublane_start", return_value=result):
            outcome = run_sublane_start_tool(
                {"issue": "15152", "lane_label": "issue_15152_probe"}, _context()
            )

        body = json.dumps(outcome.payload)
        self.assertNotIn("/private/runtime", body)
        self.assertNotIn("%3", body)
        self.assertNotIn("git worktree add", body)
        projected = outcome.payload["outcome"]
        self.assertEqual("ready", projected["status"])
        self.assertEqual("issue_15152_probe", projected["lane_label"])
        self.assertNotIn("worktree_path", projected)
        self.assertNotIn("gateway_pane", projected)
        self.assertNotIn("steps", projected)

    def test_a_hostile_actuation_reason_is_reconstructed_not_leaked(self) -> None:
        # #15152 R4 (review j#106903 finding_reasonproseleak): the ACTUATION
        # producer's `reason` concatenates a gate's free-text detail (a sender
        # preflight can emit "workspace anchor unreadable (/private/runtime/
        # ...)"), so the raw reason must never reach the public surface — neither
        # structuredContent nor the text summary. The public reason is
        # reconstructed from the closed status + blocked_reasons tokens.
        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_start_service as svc  # noqa: E501
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
            SublaneActuationOutcome,
        )

        hostile = SublaneActuationOutcome(
            status="blocked",
            execute=True,
            reason=(
                "dispatch sender attestation failed before actuation; "
                "workspace anchor unreadable (/private/runtime/secret at %5)"
            ),
            issue="15152",
            lane_label="issue_15152_probe",
            blocked_reasons=("missing_identity", "sender_attestation"),
        )
        result = svc.SublaneStartResult(
            status=svc.STATUS_COMPLETED, exit_code=1, outcome=hostile
        )
        with patch.object(svc, "run_sublane_start", return_value=result):
            outcome = run_sublane_start_tool(
                {"issue": "15152", "lane_label": "issue_15152_probe", "actuate": True},
                _context(),
            )

        # Neither the structured payload nor the text summary carries the raw
        # reason's private path / pane id.
        body = json.dumps(outcome.payload)
        self.assertNotIn("/private/runtime", body)
        self.assertNotIn("%5", body)
        self.assertNotIn("/private/runtime", outcome.summary)
        self.assertNotIn("%5", outcome.summary)
        # The public reason is reconstructed from the closed tokens.
        projected_reason = outcome.payload["outcome"]["reason"]
        self.assertIn("blocked", projected_reason)
        self.assertIn("sender_attestation", projected_reason)
        self.assertNotIn("unreadable", projected_reason)

    def test_a_service_refusal_is_reconstructed_not_forwarded(self) -> None:
        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_start_service as svc  # noqa: E501

        result = svc.SublaneStartResult(
            status=svc.STATUS_REFUSED,
            exit_code=1,
            refusal=svc.SublaneStartRefusal(
                reason="parent_gateway_binding_missing",
                message="producer prose mentioning /private/runtime and %5",
            ),
        )
        with patch.object(svc, "run_sublane_start", return_value=result):
            outcome = run_sublane_start_tool(
                {"issue": "1", "lane_label": "x", "lane_kind": "delegated_coordinator"},
                _context(),
            )

        self.assertTrue(outcome.is_error)
        self.assertEqual(
            "parent_gateway_binding_missing", outcome.payload["refusal_reason"]
        )
        body = json.dumps(outcome.payload)
        self.assertNotIn("/private/runtime", body)
        self.assertNotIn("%5", body)
        # The fixed sentence names the recovery surface, not the producer text.
        self.assertIn("declare-project-gateway", outcome.payload["refusal"])


class R5BlockedReasonGrammarTests(unittest.TestCase):
    """#15152 R5 (review j#106995 finding_blockedreasonleak): blocked_reasons is
    only a `Tuple[str, ...]`, and producers append dynamic / free-text values
    (a launcher capability verdict's `gate_reason`, `missing_field:<field>`,
    `unattested:<role>`). R4 published it verbatim, moving the leak. Now every
    published blocker token must match the closed public grammar; anything else
    (a path, a pane id, exception prose) maps to a fixed fallback, in all three
    surfaces (structured blocked_reasons, reconstructed reason, summary)."""

    def _run_with_blocked(self, blocked):
        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_start_service as svc  # noqa: E501
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
            SublaneActuationOutcome,
        )

        outcome = SublaneActuationOutcome(
            status="blocked",
            execute=True,
            reason="clean token reason",
            issue="15152",
            lane_label="issue_15152_probe",
            blocked_reasons=tuple(blocked),
        )
        result = svc.SublaneStartResult(
            status=svc.STATUS_COMPLETED, exit_code=1, outcome=outcome
        )
        with patch.object(svc, "run_sublane_start", return_value=result):
            return run_sublane_start_tool(
                {"issue": "15152", "lane_label": "issue_15152_probe", "actuate": True},
                _context(),
            )

    def test_a_path_or_pane_blocked_reason_maps_to_fallback_everywhere(self) -> None:
        outcome = self._run_with_blocked(
            [
                "missing_identity",  # legit closed token — kept
                "launcher failed at /private/runtime/x",  # free text — fallback
                "%7",  # pane id — fallback
            ]
        )
        projected = outcome.payload["outcome"]
        self.assertEqual(
            ["missing_identity", "unclassified_blocker", "unclassified_blocker"],
            projected["blocked_reasons"],
        )
        body = json.dumps(outcome.payload)
        self.assertNotIn("/private/runtime", body)
        self.assertNotIn("%7", body)
        # The reconstructed reason and the summary use the validated tokens only.
        self.assertNotIn("/private/runtime", outcome.summary)
        self.assertNotIn("%7", outcome.summary)
        self.assertIn("unclassified_blocker", projected["reason"])
        self.assertNotIn("/private/runtime", projected["reason"])

    def test_the_namespaced_forms_are_kept(self) -> None:
        # The `<prefix>:<identifier>` producer forms are safe and preserved.
        outcome = self._run_with_blocked(["missing_field:issue", "unattested:codex"])
        self.assertEqual(
            ["missing_field:issue", "unattested:codex"],
            outcome.payload["outcome"]["blocked_reasons"],
        )


class R5HandoffAnchorSourceTests(unittest.TestCase):
    """#15152 R5 (review j#106995 finding_handoffauthoritygap a): the shared
    anchor gate returns any non-Redmine anchor UNVERIFIED, so `source=asana` and
    its task/comment anchor fields are made unrepresentable on the MCP mutating
    surface — a schema violation (protocol error), not a silent unverified send."""

    def _dispatch(self, name, arguments):
        from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.tool_dispatch import (  # noqa: E501
            dispatch_tool_call,
        )

        return dispatch_tool_call(name, arguments, _context())

    def test_source_asana_is_a_protocol_error(self) -> None:
        for name in ("handoff_send", "handoff_reply"):
            dispatched = self._dispatch(name, {"to": "codex", "source": "asana"})
            self.assertTrue(dispatched.is_protocol_error, name)

    def test_task_id_and_comment_id_are_unknown_properties(self) -> None:
        for field in ("task_id", "comment_id"):
            dispatched = self._dispatch(
                "handoff_send", {"to": "codex", field: "123"}
            )
            self.assertTrue(dispatched.is_protocol_error, field)
            self.assertTrue(
                any(
                    "unknown property" in v
                    for v in dispatched.protocol_error.data["violations"]
                ),
                field,
            )


class R6CallerAuthDisclaimerTests(unittest.TestCase):
    """#15152 R6 (review j#107004 finding_overclaimguardopen): the R5 guard was a
    4-phrase denylist a synonym bypassed. The trust boundary is now stated by ONE
    canonical disclaimer that every mutating surface CONTAINS (positive structural
    assertion), and NO surface states caller-auth anywhere else — so a synonym
    over-claim added alongside the disclaimer is still caught."""

    def _mutating_texts(self):
        from collections.abc import Mapping

        texts = []
        for name in sorted(MUTATING_TOOL_NAMES):
            definition = TOOL_CATALOG[name]
            texts.append(("description", name, definition.description))
            # Frozen schema properties are MappingProxyType — match on Mapping,
            # not dict (the R5 hole).
            props = definition.input_schema.get("properties", {})
            if isinstance(props, Mapping):
                for prop_name, prop in props.items():
                    if isinstance(prop, Mapping) and isinstance(
                        prop.get("description"), str
                    ):
                        texts.append((f"property:{prop_name}", name, prop["description"]))
        return texts

    def test_every_mutating_surface_contains_the_canonical_disclaimer(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.mcp_server import (  # noqa: E501
            SERVER_INSTRUCTIONS,
        )
        from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.tool_catalog import (  # noqa: E501
            CALLER_AUTH_DISCLAIMER,
        )

        # Positive assertion: the tool description of every mutating tool and the
        # server instructions carry the ONE canonical disclaimer.
        self.assertIn(CALLER_AUTH_DISCLAIMER, SERVER_INSTRUCTIONS)
        for name in sorted(MUTATING_TOOL_NAMES):
            self.assertIn(
                CALLER_AUTH_DISCLAIMER, TOOL_CATALOG[name].description, name
            )

    _SUBJECTS = ("caller", "sender", "coordinator identity", "caller identity")
    _CLAIM_VERBS = ("authenticat", "authority", "verified as", "verifies")

    @classmethod
    def _caller_auth_claim(cls, text, disclaimer):
        """Return (subject, verb) of a caller-auth claim outside the disclaimer.

        #15152 R7 (finding_overclaimguardorder): the R6 window scanned only
        FORWARD from the subject, so a claim verb BEFORE the subject
        ("Authenticated coordinator identity is accepted") slipped through. The
        window is now symmetric around each subject occurrence, so the check is
        independent of subject/claim order.
        """
        remainder = text.replace(disclaimer, " ").lower()
        for subject in cls._SUBJECTS:
            idx = remainder.find(subject)
            while idx != -1:
                window = remainder[max(0, idx - 60) : idx + len(subject) + 60]
                for verb in cls._CLAIM_VERBS:
                    if verb in window:
                        return (subject, verb)
                idx = remainder.find(subject, idx + 1)
        return None

    def test_no_caller_auth_claim_outside_the_canonical_disclaimer(self) -> None:
        # Robust negative: strip the ONE canonical disclaimer, then no remaining
        # text may pair an authentication/authority verb with a caller/sender/
        # identity subject in EITHER order. Broader than a fixed phrase list; it
        # catches the synonym "verified as coordinator authority" (R5) and the
        # reverse-order "Authenticated coordinator identity" (R6).
        from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.mcp_server import (  # noqa: E501
            SERVER_INSTRUCTIONS,
        )
        from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.tool_catalog import (  # noqa: E501
            CALLER_AUTH_DISCLAIMER,
        )

        surfaces = [("SERVER_INSTRUCTIONS", "", SERVER_INSTRUCTIONS)] + list(
            self._mutating_texts()
        )
        for where, name, text in surfaces:
            claim = self._caller_auth_claim(text, CALLER_AUTH_DISCLAIMER)
            self.assertIsNone(
                claim, f"{where} {name}: caller-auth claim {claim} outside disclaimer"
            )

    def test_a_reverse_order_overclaim_is_caught_beside_the_disclaimer(self) -> None:
        # #15152 R7 (finding_overclaimguardorder): pin the reverse-order mutant
        # red. Even WITH the canonical disclaimer present, a claim whose verb
        # precedes the subject must be detected.
        from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.tool_catalog import (  # noqa: E501
            CALLER_AUTH_DISCLAIMER,
        )

        forward = CALLER_AUTH_DISCLAIMER + " The caller is authenticated before mutation."
        reverse = (
            CALLER_AUTH_DISCLAIMER
            + " Authenticated coordinator identity is accepted before mutation."
        )
        self.assertIsNotNone(self._caller_auth_claim(forward, CALLER_AUTH_DISCLAIMER))
        self.assertIsNotNone(self._caller_auth_claim(reverse, CALLER_AUTH_DISCLAIMER))
        # The bare disclaimer itself is clean once removed.
        self.assertIsNone(
            self._caller_auth_claim(CALLER_AUTH_DISCLAIMER, CALLER_AUTH_DISCLAIMER)
        )


class R6PublicVocabularyDriftTests(unittest.TestCase):
    """#15152 R6 (finding_projectionvocabopen): the closed public vocabularies
    used to sanitize blocked_reasons are IMPORTED from the producing registries,
    and these tests pin the two hand-listed sets to their producers so a new
    producer token cannot silently start mapping to the unclassified fallback."""

    def test_missing_field_names_match_the_request_producer(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.mutation_tools import (  # noqa: E501
            _MISSING_FIELD_NAMES,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E501
            SublaneCreateRequest,
        )

        produced = set(
            SublaneCreateRequest(
                issue="", lane_label="", branch="", worktree_path=""
            ).missing_fields(is_git=True)
        )
        self.assertEqual(produced, set(_MISSING_FIELD_NAMES))

    def test_a_launcher_verdict_reason_maps_to_fallback(self) -> None:
        # A gate_reason string (launcher capability verdict) is NOT in any closed
        # registry, so it must fall to the unclassified token even when it is
        # identifier-shaped (the R5 grammar would have let it through).
        from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.mutation_tools import (  # noqa: E501
            _UNCLASSIFIED_BLOCKER,
            _public_blocker_token,
        )

        self.assertEqual(
            _UNCLASSIFIED_BLOCKER,
            _public_blocker_token("attestation_store_schema_mismatch_v3"),
        )
        # A hostile identifier-shaped sentinel also falls to the fallback.
        self.assertEqual(
            _UNCLASSIFIED_BLOCKER, _public_blocker_token("internal_identifier_alpha")
        )
        # Legit registry tokens and namespaced forms survive.
        self.assertEqual("missing_identity", _public_blocker_token("missing_identity"))
        self.assertEqual(
            "missing_field:issue", _public_blocker_token("missing_field:issue")
        )
        # A prefixed form with an out-of-registry value falls to the fallback.
        self.assertEqual(
            _UNCLASSIFIED_BLOCKER, _public_blocker_token("missing_field:secret_path")
        )


class R7ProducerFieldClosureTests(unittest.TestCase):
    """#15152 R7 (review j#107015 finding_projectiontokensopen): R6 closed only
    `status` and `blocked_reasons`; the sibling producer-owned string fields were
    still copied verbatim. The projection is now closed AS A CLASS — every field
    is routed through a declared category and producer tokens are validated
    against their producing registry."""

    def test_every_projected_field_is_categorized_exactly_once(self) -> None:
        # Exhaustiveness drift guard: a new field added to the public projection
        # without a category fails here instead of silently leaking.
        from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.mutation_tools import (  # noqa: E501
            SUBLANE_OUTCOME_PUBLIC_FIELDS,
            _BOOL_PUBLIC_FIELDS,
            _CALLER_ECHO_PUBLIC_FIELDS,
            _PRESENCE_ONLY_PUBLIC_FIELDS,
            _PRODUCER_TOKEN_REGISTRIES,
            _STATUS_PUBLIC_FIELDS,
        )

        categories = [
            _STATUS_PUBLIC_FIELDS,
            _BOOL_PUBLIC_FIELDS,
            _CALLER_ECHO_PUBLIC_FIELDS,
            frozenset(_PRODUCER_TOKEN_REGISTRIES),
            _PRESENCE_ONLY_PUBLIC_FIELDS,
        ]
        declared = set(SUBLANE_OUTCOME_PUBLIC_FIELDS)
        union = set().union(*categories)
        self.assertEqual(declared, union, "a projected field is uncategorized")
        # Exactly once: no field appears in two categories.
        for i, a in enumerate(categories):
            for b in categories[i + 1 :]:
                self.assertEqual(frozenset(), a & b, f"field in two categories: {a & b}")

    def test_producer_registries_match_their_producers(self) -> None:
        # Pin each producer-token registry to its producing module so a new token
        # cannot silently start mapping to the unclassified fallback.
        from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.mutation_tools import (  # noqa: E501
            _PRODUCER_TOKEN_REGISTRIES,
        )
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.injection_stage import (  # noqa: E501
            INJECTION_STAGES,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
            DISPATCH_RESULTS,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_integration_policy import (  # noqa: E501
            LAUNCH_ACTIONS,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_fill_decision import (  # noqa: E501
            FILL_DECISIONS,
        )

        self.assertEqual(
            set(_PRODUCER_TOKEN_REGISTRIES["launch_action"]), set(LAUNCH_ACTIONS)
        )
        self.assertEqual(
            set(_PRODUCER_TOKEN_REGISTRIES["dispatch_result"]), set(DISPATCH_RESULTS)
        )
        self.assertEqual(
            set(_PRODUCER_TOKEN_REGISTRIES["dispatch_injection_stage"]),
            set(INJECTION_STAGES),
        )
        self.assertEqual(
            set(_PRODUCER_TOKEN_REGISTRIES["fill_decision"]), set(FILL_DECISIONS)
        )

    def test_hostile_producer_field_values_never_reach_the_public_surface(self) -> None:
        # Inject a private path / pane id / operator token into EACH producer-owned
        # field; none may appear in structuredContent or the text summary, and each
        # maps to its fixed unclassified token (durable_anchor -> presence bool).
        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_start_service as svc  # noqa: E501
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
            SublaneActuationOutcome,
        )

        hostile = SublaneActuationOutcome(
            status="ready",
            execute=True,
            reason="planned",
            issue="15152",
            lane_label="issue_15152_probe",
            launch_action="internal_identifier_alpha",
            dispatch_result="/private/runtime/pane-%7",
            dispatch_injection_stage="private_stage_beta",
            fill_decision="internal_customer_gamma",
            durable_anchor="/private/runtime/anchor-note",
        )
        result = svc.SublaneStartResult(
            status=svc.STATUS_COMPLETED, exit_code=0, outcome=hostile
        )
        with patch.object(svc, "run_sublane_start", return_value=result):
            outcome = run_sublane_start_tool(
                {"issue": "15152", "lane_label": "issue_15152_probe", "actuate": True},
                _context(),
            )

        body = json.dumps(outcome.payload)
        for sentinel in (
            "internal_identifier_alpha",
            "/private/runtime",
            "%7",
            "private_stage_beta",
            "internal_customer_gamma",
            "anchor-note",
        ):
            self.assertNotIn(sentinel, body, sentinel)
            self.assertNotIn(sentinel, outcome.summary, sentinel)
        projected = outcome.payload["outcome"]
        self.assertEqual("unclassified_launch_action", projected["launch_action"])
        self.assertEqual("unclassified_dispatch_result", projected["dispatch_result"])
        self.assertEqual(
            "unclassified_dispatch_injection_stage",
            projected["dispatch_injection_stage"],
        )
        self.assertEqual("unclassified_fill_decision", projected["fill_decision"])
        self.assertIs(True, projected["durable_anchor_present"])
        self.assertNotIn("durable_anchor", projected)

    def test_sublane_caller_echo_ignores_a_hostile_producer_outcome(self) -> None:
        # #15152 R8 (finding_callerechobindingopen): issue / lane_label / branch are
        # sourced from the validated command, never the producer outcome.
        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_start_service as svc  # noqa: E501
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
            SublaneActuationOutcome,
        )

        hostile = SublaneActuationOutcome(
            status="ready",
            execute=True,
            reason="planned",
            issue="/private/runtime/evilissue",
            lane_label="/private/runtime/evillane",
            branch="%9-evilbranch",
        )
        result = svc.SublaneStartResult(
            status=svc.STATUS_COMPLETED, exit_code=0, outcome=hostile
        )
        with patch.object(svc, "run_sublane_start", return_value=result):
            outcome = run_sublane_start_tool(
                {
                    "issue": "15152",
                    "lane_label": "issue_15152_probe",
                    "branch": "issue_15152_probe",
                    "actuate": True,
                },
                _context(),
            )

        body = json.dumps(outcome.payload)
        for sentinel in ("/private/runtime", "%9", "evilissue", "evillane", "evilbranch"):
            self.assertNotIn(sentinel, body, sentinel)
            self.assertNotIn(sentinel, outcome.summary, sentinel)
        projected = outcome.payload["outcome"]
        self.assertEqual("15152", projected["issue"])
        self.assertEqual("issue_15152_probe", projected["lane_label"])
        self.assertEqual("issue_15152_probe", projected["branch"])

    def test_valid_producer_tokens_survive(self) -> None:
        # A legitimate registry member is republished unchanged.
        from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.mutation_tools import (  # noqa: E501
            _public_producer_token,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_integration_policy import (  # noqa: E501
            LAUNCH_ACTIONS,
        )

        member = sorted(LAUNCH_ACTIONS)[0]
        self.assertEqual(member, _public_producer_token("launch_action", member))

    def test_non_bool_boolean_fields_never_coerce_to_an_affirmative(self) -> None:
        # #15152 R7 (review j#107015 finding_booleantruthinessoverclaim): a string
        # `false`, an int, or a container in a boolean field must NOT be published
        # as `true`. The field is omitted rather than coerced.
        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_start_service as svc  # noqa: E501
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
            SublaneActuationOutcome,
        )
        from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.mutation_tools import (  # noqa: E501
            _public_bool,
        )

        # The pure helper: only exact bools pass, everything else is None (omit).
        for hostile in ("false", "true", "0", 0, 1, [], {}, "False"):
            self.assertIsNone(_public_bool(hostile), repr(hostile))
        self.assertIs(True, _public_bool(True))
        self.assertIs(False, _public_bool(False))

        # End-to-end: a string `false` in `execute`/`adopted` is omitted, not `true`.
        hostile = SublaneActuationOutcome(
            status="ready",
            execute="false",  # type: ignore[arg-type]
            reason="planned",
            issue="15152",
            lane_label="issue_15152_probe",
            adopted="false",  # type: ignore[arg-type]
        )
        result = svc.SublaneStartResult(
            status=svc.STATUS_COMPLETED, exit_code=0, outcome=hostile
        )
        with patch.object(svc, "run_sublane_start", return_value=result):
            outcome = run_sublane_start_tool(
                {"issue": "15152", "lane_label": "issue_15152_probe", "actuate": True},
                _context(),
            )
        projected = outcome.payload["outcome"]
        self.assertNotIn("execute", projected)
        self.assertNotIn("adopted", projected)

    def test_every_handoff_field_is_categorized_exactly_once(self) -> None:
        # #15152 R7/R8/R9: the handoff projection is closed AS A CLASS — every
        # published field is either a producer-owned closed token (validated against
        # its registry) or an effective caller-echo (receiver/kind/marker, sourced
        # from the domain-validated outcome since R9). A new field without a
        # category fails here.
        from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.mutation_tools import (  # noqa: E501
            HANDOFF_OUTCOME_PUBLIC_FIELDS,
            _HANDOFF_PRODUCER_REGISTRIES,
        )

        producer = set(_HANDOFF_PRODUCER_REGISTRIES)  # status/reason/next_action_owner/mode
        caller_echo = {"receiver", "kind", "notification_marker"}  # effective, from outcome
        self.assertEqual(
            set(HANDOFF_OUTCOME_PUBLIC_FIELDS), producer | caller_echo, "handoff field uncategorized"
        )
        self.assertEqual(set(), producer & caller_echo, "handoff field in two categories")

    def test_handoff_producer_registries_match_their_types(self) -> None:
        # Pin each handoff producer registry to its source Literal / canonical set
        # so a new member cannot silently start mapping to unknown_<field>. #15152 R8
        # (finding_handoffmodevocabularypartial): mode MUST equal the canonical MODES
        # (which includes `standard`), never a hand-picked subset.
        from typing import get_args

        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (  # noqa: E501
            MODES,
            NextActionOwner,
            Reason,
            Status,
        )
        from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.mutation_tools import (  # noqa: E501
            _HANDOFF_PRODUCER_REGISTRIES,
        )

        self.assertEqual(_HANDOFF_PRODUCER_REGISTRIES["status"], frozenset(get_args(Status)))
        self.assertEqual(_HANDOFF_PRODUCER_REGISTRIES["reason"], frozenset(get_args(Reason)))
        self.assertEqual(
            _HANDOFF_PRODUCER_REGISTRIES["next_action_owner"],
            frozenset(get_args(NextActionOwner)),
        )
        self.assertEqual(_HANDOFF_PRODUCER_REGISTRIES["mode"], frozenset(MODES))
        # `standard` is a canonical member and must be preserved, not unknown'd.
        self.assertIn("standard", _HANDOFF_PRODUCER_REGISTRIES["mode"])


class MutatingDeclarationGuardTests(unittest.TestCase):
    def _tool(self, name: str, schema: dict, *, read_only: bool) -> dict:
        return {
            name: ToolDefinition(
                name=name,
                title=name,
                description="synthetic",
                input_schema=schema,
                output_schema={"type": "object"},
                read_only=read_only,
            )
        }

    def test_an_undeclared_mutating_tool_is_caught(self) -> None:
        violations = catalog_surface_violations(
            self._tool("probe", {"type": "object"}, read_only=False)
        )
        self.assertTrue(any("undeclared mutating" in v for v in violations))

    def test_a_declared_mutating_tool_claiming_read_only_is_caught(self) -> None:
        violations = catalog_surface_violations(
            self._tool("handoff_send", {"type": "object"}, read_only=True)
        )
        self.assertTrue(any("claims to be read-only" in v for v in violations))

    def test_the_token_guard_applies_to_declared_mutating_tools_too(self) -> None:
        violations = catalog_surface_violations(
            self._tool(
                "probe",
                {"type": "object", "properties": {"target_pane": {"type": "string"}}},
                read_only=False,
            ),
            mutating_names=frozenset({"probe"}),
        )
        self.assertTrue(any("pane" in v for v in violations))

    def test_no_shipped_mutating_input_can_name_a_pane_or_command(self) -> None:
        # Belt-and-suspenders over the guard: the shipped mutating schemas
        # carry no property that could address a raw pane, tmux target, or
        # command string — the structural reason an unmanaged (identity-less)
        # row cannot be reached from this surface (#15152 j#102930).
        for name in MUTATING_TOOL_NAMES:
            properties = TOOL_CATALOG[name].input_schema["properties"]
            for prop in properties:
                for token in ("pane", "tmux", "command", "argv", "target_session"):
                    self.assertNotIn(token, prop, f"{name}.{prop}")
            self.assertNotIn("target", properties)  # a bare pane locator field


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
