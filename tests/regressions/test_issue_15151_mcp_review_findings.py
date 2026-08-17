"""Regression pins for the #15151 review findings (two rounds).

One class per finding, each reproducing the reported defect as it was reported and
asserting the fixed behavior. These live in ``tests/regressions`` rather than beside
the feature tests because their job is to keep *these specific defects* from
returning, independently of how the feature's own specs are later reorganized.

- Round 1: review j#102186, verdict j#102195 — ``Finding1``..``Finding5``.
- Round 2: review j#102241, verdict j#102246 — ``R2F1``..``R2F3``, each covering a
  place where the round-1 fix was too shallow (validation that stopped at the
  top-level key, a value range tightened in one direction only, and a "shared"
  entry only one caller actually used).

All eight were accepted after independent reproduction; none was disputed.

Note ``Finding4RequestIdTests.test_a_fractional_number_id_is_accepted``: round 1
pinned the *opposite* behavior, and r2f2 corrected it. The test was rewritten
rather than deleted so the correction stays visible at the place that asserted the
wrong contract.
"""

from __future__ import annotations

import argparse
import contextlib
import inspect
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E402,E501
    cli_workflow,
    herdr_workflow_step,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E402,E501
    workflow_step_plan_resolution as shared_resolution,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.runtime_observation import (  # noqa: E402,E501
    FRESHNESS_FRESH,
    FRESHNESS_UNKNOWN,
    SOURCE_REDMINE,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.mcp_server import (  # noqa: E402,E501
    PHASE_INITIALIZING,
    PHASE_READY,
    PHASE_UNINITIALIZED,
    REQUIRED_CLIENT_INFO_FIELDS,
    REQUIRED_INITIALIZE_PARAMS,
    McpServer,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.read_plan_tools import (  # noqa: E402,E501
    ReadPlanContext,
    run_docs_resolve,
    run_workflow_step_plan,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.unit_state_tool import (  # noqa: E402,E501
    VALUE_LANDED,
    UnitFacts,
    compose_unit_state,
    landing_from_ledger_record,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.blocker_claim import (  # noqa: E402,E501
    latest_blocker_claim,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.jsonrpc import (  # noqa: E402,E501
    ERROR_INVALID_PARAMS,
    ERROR_INVALID_REQUEST,
    ERROR_NOT_INITIALIZED,
    ERROR_PARSE,
    REJECTED_JSON_CONSTANTS,
    FrameError,
    JsonRpcRequest,
    parse_frame,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.repo_path import (  # noqa: E402,E501
    PATH_ABSOLUTE,
    PATH_ESCAPES_REPO,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.unit_selector import (  # noqa: E402,E501
    UnitRecord,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.unit_state import (  # noqa: E402,E501
    VALUE_UNCONFIRMED,
    BlockedClaim,
)

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "regression", "version": "1"},
    },
}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def _forward_outcome():
    """A real, ready forward outcome the safety composition can consume."""
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step import (  # noqa: E501
        EXECUTION_READY,
        WorkflowStepOutcome,
    )

    return WorkflowStepOutcome(
        state="child_dispatch",
        reason="",
        execution=EXECUTION_READY,
        next_action="dispatch the worker",
        next_owner="child",
        primitive="handoff send",
    )


@contextlib.contextmanager
def _inert_safety():
    """Neutralize the shared safety composition for tests about resolution only.

    The store reconcile and the startup gate are exercised by
    :class:`R3F1SafetyCompositionTests`; the backend-selection tests are about
    which rail answers, so they hold both steps inert rather than depending on the
    host's real store.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step_reconcile import (  # noqa: E501
        STORE_ABSENT,
    )

    with patch.object(
        cli_workflow, "_load_store_action", lambda _a, repo_root="": (None, STORE_ABSENT)
    ), patch.object(
        cli_workflow, "_maybe_operator_startup_resume_outcome", lambda _a, _o: None
    ):
        yield


def session(frames, *, repo_root: Path = ROOT):
    """Run one in-process stdio session; return (responses, stderr, server)."""
    out, err = io.StringIO(), io.StringIO()
    server = McpServer(
        context=ReadPlanContext(repo_root=repo_root, redmine_live=False),
        stdin=io.StringIO("\n".join(json.dumps(f) for f in frames) + "\n"),
        stdout=out,
        stderr=err,
    )
    server.serve()
    responses = [json.loads(line) for line in out.getvalue().splitlines() if line]
    return responses, err.getvalue(), server


class Finding1LifecycleTests(unittest.TestCase):
    """MCP lifecycle could be skipped, and `initialize` input was unvalidated."""

    def test_initialized_notification_alone_does_not_open_the_tool_surface(self) -> None:
        """The reported bypass: initialized-before-initialize, then tools/list."""
        responses, _, server = session(
            [INITIALIZED, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}]
        )
        self.assertEqual(responses[0]["error"]["code"], ERROR_NOT_INITIALIZED)
        self.assertEqual(server._phase, PHASE_UNINITIALIZED)

    def test_initialize_with_empty_params_is_refused(self) -> None:
        responses, _, _ = session(
            [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}]
        )
        error = responses[0]["error"]
        self.assertEqual(error["code"], ERROR_INVALID_PARAMS)
        self.assertEqual(sorted(error["data"]["missing"]), sorted(REQUIRED_INITIALIZE_PARAMS))

    def test_each_required_initialize_param_is_individually_required(self) -> None:
        for missing in REQUIRED_INITIALIZE_PARAMS:
            params = {k: v for k, v in INITIALIZE["params"].items() if k != missing}
            responses, _, _ = session(
                [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}]
            )
            self.assertEqual(responses[0]["error"]["code"], ERROR_INVALID_PARAMS, missing)

    def test_wrong_typed_initialize_params_are_refused(self) -> None:
        # `("protocolVersion", "")` left this table in R5 (review j#103251 r4f6):
        # the schema types the member as `string` with no length constraint, so
        # an empty string is conforming and the refusal was this server's own
        # invention. The accepted-empty case is pinned in the R5 suite.
        for key, bad in (
            ("protocolVersion", 5),
            ("capabilities", "none"),
            ("clientInfo", []),
        ):
            params = dict(INITIALIZE["params"])
            params[key] = bad
            responses, _, _ = session(
                [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}]
            )
            self.assertEqual(
                responses[0]["error"]["code"], ERROR_INVALID_PARAMS, f"{key}={bad!r}"
            )

    def test_initialize_sent_as_a_notification_gets_no_response(self) -> None:
        """The spec forbids replying to a notification; the old code replied with id null."""
        responses, stderr, server = session(
            [{"jsonrpc": "2.0", "method": "initialize", "params": INITIALIZE["params"]}]
        )
        self.assertEqual(responses, [])
        self.assertIn("initialize", stderr)
        self.assertEqual(server._phase, PHASE_UNINITIALIZED)

    def test_a_second_initialize_is_refused(self) -> None:
        responses, _, _ = session([INITIALIZE, INITIALIZE])
        self.assertEqual(responses[1]["error"]["code"], ERROR_INVALID_REQUEST)

    def test_the_full_handshake_reaches_ready_and_serves_tools(self) -> None:
        responses, _, server = session(
            [INITIALIZE, INITIALIZED, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        )
        self.assertEqual(server._phase, PHASE_READY)
        self.assertEqual(
            len(responses[1]["result"]["tools"]), 7
        )  # 4 read/plan + 3 declared mutating (#15152)

    def test_requests_are_refused_between_initialize_and_initialized(self) -> None:
        responses, _, server = session(
            [INITIALIZE, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        )
        self.assertEqual(server._phase, PHASE_INITIALIZING)
        self.assertEqual(responses[1]["error"]["code"], ERROR_NOT_INITIALIZED)

    def test_ping_is_answerable_in_every_phase(self) -> None:
        """The one request the spec allows before initialization completes."""
        responses, _, _ = session([{"jsonrpc": "2.0", "id": 9, "method": "ping"}])
        self.assertEqual(responses[0]["result"], {})


class Finding2BackendSelectionTests(unittest.TestCase):
    """`workflow_step_plan` called the tmux rail regardless of the configured backend."""

    def _run(self, *, herdr_active: bool):
        calls = {"require_tmux": 0, "herdr": 0}

        def fake_require_tmux():
            calls["require_tmux"] += 1

        def fake_current_pane():
            return "%1"

        def fake_discover():
            return []

        def fake_herdr(_args):
            calls["herdr"] += 1
            return _forward_outcome()

        with patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.herdr_entrypoint_preflight.herdr_backend_active",
            return_value=herdr_active,
        ), patch.object(cli_workflow, "require_tmux", fake_require_tmux), patch.object(
            cli_workflow, "current_pane", fake_current_pane
        ), patch.object(
            cli_workflow, "_discover_candidates", fake_discover
        ), patch.object(
            herdr_workflow_step, "resolve_herdr_step_outcome", fake_herdr
        ), _inert_safety():
            outcome = run_workflow_step_plan({}, ReadPlanContext(repo_root=ROOT))
        return outcome, calls

    def test_a_herdr_repo_resolves_herdr_natively_and_never_touches_tmux(self) -> None:
        outcome, calls = self._run(herdr_active=True)
        self.assertEqual(calls["herdr"], 1)
        self.assertEqual(calls["require_tmux"], 0)
        self.assertEqual(outcome.payload["backend"], "herdr")

    def test_a_tmux_repo_still_uses_the_tmux_rail(self) -> None:
        outcome, calls = self._run(herdr_active=False)
        self.assertEqual(calls["herdr"], 0)
        self.assertEqual(calls["require_tmux"], 1)
        self.assertEqual(outcome.payload["backend"], "tmux")

    def test_the_plan_is_never_executed_on_either_backend(self) -> None:
        for herdr in (True, False):
            outcome, _ = self._run(herdr_active=herdr)
            self.assertFalse(outcome.payload["executed"], herdr)
            self.assertEqual(outcome.payload["execution"], "plan_only", herdr)

    def test_the_shared_resolver_reads_only_repo_off_its_namespace_shim(self) -> None:
        """Pins the one CLI-shaped seam: if the resolver grows a field, this fails."""
        seen = {}

        def capture(args):
            seen["attrs"] = {k for k in vars(args) if not k.startswith("_")}
            return _forward_outcome()

        with patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.herdr_entrypoint_preflight.herdr_backend_active",
            return_value=True,
        ), patch.object(
            herdr_workflow_step, "resolve_herdr_step_outcome", capture
        ), _inert_safety():
            run_workflow_step_plan({}, ReadPlanContext(repo_root=ROOT))
        self.assertEqual(seen["attrs"], {"repo"})


class Finding3RepoRelativePathTests(unittest.TestCase):
    """`docs_resolve` accepted absolute / escaping paths and leaked the repo root."""

    def _context(self) -> ReadPlanContext:
        return ReadPlanContext(repo_root=ROOT)

    def test_an_absolute_path_is_refused_with_a_fixed_reason(self) -> None:
        outcome = run_docs_resolve({"paths": ["/etc/passwd"]}, self._context())
        self.assertTrue(outcome.is_error)
        self.assertEqual(outcome.payload["error"], "invalid_path")
        self.assertEqual(outcome.payload["rejected"][0]["reason"], PATH_ABSOLUTE)

    def test_a_repo_escaping_path_is_refused(self) -> None:
        outcome = run_docs_resolve({"paths": ["../outside/private.py"]}, self._context())
        self.assertTrue(outcome.is_error)
        self.assertEqual(outcome.payload["rejected"][0]["reason"], PATH_ESCAPES_REPO)

    def test_a_path_that_escapes_after_normalization_is_refused(self) -> None:
        outcome = run_docs_resolve({"paths": ["src/../../x.py"]}, self._context())
        self.assertEqual(outcome.payload["rejected"][0]["reason"], PATH_ESCAPES_REPO)

    def test_a_windows_absolute_path_is_refused(self) -> None:
        outcome = run_docs_resolve({"paths": ["C:\\Users\\x"]}, self._context())
        self.assertEqual(outcome.payload["rejected"][0]["reason"], PATH_ABSOLUTE)

    def test_no_refusal_leaks_the_servers_absolute_repo_root(self) -> None:
        """The reported leak: the resolver's ValueError named the server's own root."""
        root = str(ROOT)
        for bad in ("/etc/passwd", "../outside/private.py", "C:\\x", "src/../../x.py"):
            outcome = run_docs_resolve({"paths": [bad]}, self._context())
            self.assertNotIn(root, repr(outcome.payload), bad)
            self.assertNotIn(root, outcome.summary, bad)

    def test_every_bad_path_is_reported_not_just_the_first(self) -> None:
        outcome = run_docs_resolve(
            {"paths": ["/abs/one", "../two", "src/ok.py"]}, self._context()
        )
        self.assertEqual(len(outcome.payload["rejected"]), 2)

    def test_a_catalog_failure_reports_a_fixed_reason_not_exception_text(self) -> None:
        outcome = run_docs_resolve(
            {"paths": ["src/x.py"]},
            ReadPlanContext(repo_root=ROOT, catalog_path="/nonexistent/catalog.yaml"),
        )
        self.assertTrue(outcome.is_error)
        self.assertIn("reason", outcome.payload)
        self.assertNotIn("/nonexistent/catalog.yaml", repr(outcome.payload))

    def test_a_valid_repo_relative_path_still_resolves(self) -> None:
        outcome = run_docs_resolve(
            {"paths": ["src/mozyo_bridge/application/cli.py"]}, self._context()
        )
        self.assertFalse(outcome.is_error)
        self.assertEqual(len(outcome.payload["resolutions"]), 1)


class Finding4RequestIdTests(unittest.TestCase):
    """Boolean ids were accepted and an explicit null id was read as a notification."""

    def test_a_boolean_id_is_an_invalid_request(self) -> None:
        """`bool` subclasses `int`, so the old isinstance check let `true` through."""
        parsed = parse_frame(json.dumps({"jsonrpc": "2.0", "id": True, "method": "ping"}))
        self.assertIsInstance(parsed, FrameError)
        self.assertEqual(parsed.code, ERROR_INVALID_REQUEST)
        self.assertTrue(parsed.respondable)

    def test_an_explicit_null_id_is_a_request_not_a_notification(self) -> None:
        """The spec: a Notification is a Request *without* an id member."""
        parsed = parse_frame(json.dumps({"jsonrpc": "2.0", "id": None, "method": "ping"}))
        self.assertIsInstance(parsed, JsonRpcRequest)
        self.assertTrue(parsed.has_id)
        self.assertFalse(parsed.is_notification)

    def test_an_absent_id_member_is_a_notification(self) -> None:
        parsed = parse_frame(json.dumps({"jsonrpc": "2.0", "method": "ping"}))
        self.assertIsInstance(parsed, JsonRpcRequest)
        self.assertFalse(parsed.has_id)
        self.assertTrue(parsed.is_notification)

    def test_a_null_id_request_is_answered_with_a_null_id_response(self) -> None:
        responses, _, _ = session(
            [INITIALIZE, INITIALIZED, {"jsonrpc": "2.0", "id": None, "method": "ping"}]
        )
        self.assertEqual(len(responses), 2)
        self.assertIsNone(responses[1]["id"])
        self.assertEqual(responses[1]["result"], {})

    def test_a_notification_ping_is_not_answered(self) -> None:
        responses, _, _ = session(
            [INITIALIZE, INITIALIZED, {"jsonrpc": "2.0", "method": "ping"}]
        )
        self.assertEqual(len(responses), 1)

    def test_an_invalid_request_without_an_id_is_answered(self) -> None:
        """Corrected by review j#102599 r3f2 — see :class:`R3F2InvalidRequestTests`.

        Round 1 asserted the opposite: that a refused frame with no id member stays
        unanswered. But a Notification is a *Request object* without an id, and a
        payload with a bad ``jsonrpc`` is not a Request object at all — so it is an
        Invalid Request, which the spec answers with a null id.
        """
        parsed = parse_frame(json.dumps({"jsonrpc": "1.0", "method": "ping"}))
        self.assertIsInstance(parsed, FrameError)
        self.assertTrue(parsed.respondable)

    def test_a_malformed_null_id_request_is_answered(self) -> None:
        parsed = parse_frame(json.dumps({"jsonrpc": "1.0", "id": None, "method": "ping"}))
        self.assertIsInstance(parsed, FrameError)
        self.assertTrue(parsed.respondable)

    def test_a_fractional_number_id_is_accepted(self) -> None:
        """Corrected by review j#102241 r2f2 — see :class:`R2F2NumberContractTests`.

        This round-1 fix originally refused ``1.5`` as part of tightening the id
        check. The spec allows Number and only SHOULD NOT (not MUST NOT) fractional
        parts, so refusing it rejected a legal id.
        """
        parsed = parse_frame(json.dumps({"jsonrpc": "2.0", "id": 1.5, "method": "ping"}))
        self.assertIsInstance(parsed, JsonRpcRequest)
        self.assertEqual(parsed.id, 1.5)


BLOCKED_NOTE = """## Sublane park

- state: blocked
- durable_anchor: #15151 j#10
- blocked_by: dependency #15149 is not closed
- resume_condition: close #15149 and re-dispatch
- resume_owner: coordinator
"""

RESUMED_NOTE = "## Gate: progress_log\n\n- state: active\n- note: work resumed\n"
UNRELATED_NOTE = "## Gate: implementation_done\n\n- note: no state field here\n"

UNIT = UnitRecord(workspace_id="w", lane_id="l", project_id="p")
CLAIM = BlockedClaim(
    blocker_source=SOURCE_REDMINE,
    reason="dependency #15149 is not closed",
    resume_condition="close #15149 and re-dispatch",
    durable_anchor="#15151 j#10",
    observed_at="2026-08-10T00:00:00Z",
    freshness=FRESHNESS_FRESH,
)


class Finding5StaleBlockedClaimTests(unittest.TestCase):
    """A recorded block never cleared, and its claim carried no observation envelope."""

    def test_a_later_non_blocked_state_declaration_clears_the_claim(self) -> None:
        self.assertIsNone(
            latest_blocker_claim(
                [("10", BLOCKED_NOTE), ("11", RESUMED_NOTE)], issue_id="15151"
            )
        )

    def test_a_later_unrelated_journal_does_not_hide_a_standing_block(self) -> None:
        self.assertIsNotNone(
            latest_blocker_claim(
                [("10", BLOCKED_NOTE), ("11", UNRELATED_NOTE)], issue_id="15151"
            )
        )

    def test_a_conflicting_later_state_declaration_clears_the_claim(self) -> None:
        """A record declaring `state:` twice with different values sustains nothing."""
        conflicted = "- state: blocked\n- state: active\n"
        self.assertIsNone(
            latest_blocker_claim(
                [("10", BLOCKED_NOTE), ("11", conflicted)], issue_id="15151"
            )
        )

    def test_the_claim_carries_the_observation_envelope_it_was_read_with(self) -> None:
        claim = latest_blocker_claim(
            [("10", BLOCKED_NOTE)],
            issue_id="15151",
            observed_at="2026-08-10T12:00:00Z",
            freshness=FRESHNESS_FRESH,
        )
        self.assertEqual(claim.observed_at, "2026-08-10T12:00:00Z")
        self.assertEqual(claim.freshness, FRESHNESS_FRESH)

    def test_a_claim_is_dropped_when_the_current_gate_is_not_blocked(self) -> None:
        """The durable fold is the authority on whether the block is still in force."""
        report = compose_unit_state(
            UNIT,
            UnitFacts(
                workflow_state="implementing",
                workflow_readable=True,
                workflow_observed_at="2026-08-10T00:00:00Z",
                workflow_freshness=FRESHNESS_FRESH,
                workflow_blocked=CLAIM,
            ),
        )
        self.assertIsNone(report.workflow.blocked)
        self.assertEqual(report.workflow.state.value, "implementing")
        self.assertIn("no longer reports blocked", report.workflow.state.note or "")

    def test_a_claim_is_dropped_when_the_current_gate_is_unreadable(self) -> None:
        """Fail-closed: an unconfirmable block is not reported as current."""
        report = compose_unit_state(
            UNIT, UnitFacts(workflow_readable=False, workflow_blocked=CLAIM)
        )
        self.assertIsNone(report.workflow.blocked)

    def test_a_claim_survives_when_the_current_gate_agrees(self) -> None:
        report = compose_unit_state(
            UNIT,
            UnitFacts(
                workflow_state="blocked",
                workflow_readable=True,
                workflow_observed_at="2026-08-10T00:00:00Z",
                workflow_freshness=FRESHNESS_FRESH,
                workflow_blocked=CLAIM,
            ),
        )
        self.assertIsNotNone(report.workflow.blocked)
        self.assertEqual(report.workflow.state.value, "blocked")
        self.assertEqual(report.workflow.blocked.freshness, FRESHNESS_FRESH)
        self.assertIsNotNone(report.workflow.blocked.observed_at)

    def test_a_claim_with_no_envelope_is_still_reported_as_unknown_freshness(self) -> None:
        import dataclasses

        bare = dataclasses.replace(CLAIM, observed_at=None, freshness=FRESHNESS_UNKNOWN)
        report = compose_unit_state(
            UNIT,
            UnitFacts(
                workflow_state="blocked",
                workflow_readable=True,
                workflow_observed_at="2026-08-10T00:00:00Z",
                workflow_freshness=FRESHNESS_FRESH,
                workflow_blocked=bare,
            ),
        )
        self.assertEqual(report.workflow.blocked.freshness, FRESHNESS_UNKNOWN)


# --------------------------------------------------------------------------- #
# Round 2 findings (review j#102241, verdict j#102246)
# --------------------------------------------------------------------------- #


class R2F1ClientInfoTests(unittest.TestCase):
    """`clientInfo` was checked for being a mapping, not for its required members."""

    def _initialize(self, client_info):
        params = dict(INITIALIZE["params"], clientInfo=client_info)
        return session([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}])

    def test_the_required_member_list_matches_the_schema(self) -> None:
        """MCP `Implementation extends BaseMetadata`: name + version, title optional."""
        self.assertEqual(sorted(REQUIRED_CLIENT_INFO_FIELDS), ["name", "version"])

    def test_an_empty_client_info_is_refused(self) -> None:
        responses, _, server = self._initialize({})
        self.assertEqual(responses[0]["error"]["code"], ERROR_INVALID_PARAMS)
        self.assertEqual(server._phase, PHASE_UNINITIALIZED)

    def test_each_required_member_is_individually_required(self) -> None:
        for present, absent in (("name", "version"), ("version", "name")):
            responses, _, _ = self._initialize({present: "x"})
            error = responses[0]["error"]
            self.assertEqual(error["code"], ERROR_INVALID_PARAMS, absent)
            self.assertIn(f"clientInfo.{absent}", error["data"]["invalid"])

    def test_non_string_members_are_refused(self) -> None:
        responses, _, _ = self._initialize({"name": 5, "version": []})
        error = responses[0]["error"]
        self.assertEqual(error["code"], ERROR_INVALID_PARAMS)
        self.assertEqual(
            sorted(error["data"]["invalid"]), ["clientInfo.name", "clientInfo.version"]
        )

    def test_blank_members_are_accepted(self) -> None:
        """Corrected by review j#102599 r3f3 — see :class:`R3F3SchemaExactInitializeTests`.

        Round 2 refused blank members on the argument that they share no
        implementation information. That was this implementation's own rule: the
        schema types `name` / `version` as strings with no length constraint, so a
        blank one conforms. The acceptance boundary is the schema's.
        """
        responses, _, _ = self._initialize({"name": "   ", "version": ""})
        self.assertIn("result", responses[0])

    def test_a_well_formed_client_info_is_accepted(self) -> None:
        responses, _, server = self._initialize({"name": "client", "version": "1.2.3"})
        self.assertIn("result", responses[0])
        self.assertEqual(server._phase, PHASE_INITIALIZING)

    def test_an_optional_title_does_not_break_acceptance(self) -> None:
        responses, _, _ = self._initialize(
            {"name": "client", "version": "1", "title": "Client"}
        )
        self.assertIn("result", responses[0])


class R2F2NumberContractTests(unittest.TestCase):
    """The Number contract was inverted: legal ids refused, invalid JSON accepted."""

    def test_fractional_number_ids_are_accepted_and_echoed_unchanged(self) -> None:
        """The spec requires the response id to be the same value as the request's."""
        for value in (1.5, -2.25, 0.1):
            responses, _, _ = session(
                [INITIALIZE, INITIALIZED, {"jsonrpc": "2.0", "id": value, "method": "ping"}]
            )
            self.assertEqual(responses[1]["id"], value)

    def test_integer_and_string_ids_still_work(self) -> None:
        for value in (7, -7, 0, "abc"):
            parsed = parse_frame(
                json.dumps({"jsonrpc": "2.0", "id": value, "method": "ping"})
            )
            self.assertIsInstance(parsed, JsonRpcRequest, value)
            self.assertEqual(parsed.id, value)

    def test_boolean_ids_are_still_refused(self) -> None:
        """Loosening to Numbers must not re-admit Booleans via the int subclass."""
        for value in (True, False):
            parsed = parse_frame(
                json.dumps({"jsonrpc": "2.0", "id": value, "method": "ping"})
            )
            self.assertIsInstance(parsed, FrameError, value)
            self.assertEqual(parsed.code, ERROR_INVALID_REQUEST)

    def test_structured_ids_are_still_refused(self) -> None:
        for raw in ('{"jsonrpc":"2.0","id":{},"method":"ping"}',
                    '{"jsonrpc":"2.0","id":[],"method":"ping"}'):
            self.assertIsInstance(parse_frame(raw), FrameError, raw)

    def test_the_non_json_numeric_constants_are_parse_errors(self) -> None:
        """`json.loads` accepts NaN/Infinity by default; JSON has no such literal."""
        for constant in REJECTED_JSON_CONSTANTS:
            raw = '{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":%s}}' % constant
            parsed = parse_frame(raw)
            self.assertIsInstance(parsed, FrameError, constant)
            self.assertEqual(parsed.code, ERROR_PARSE, constant)

    def test_a_non_json_constant_as_the_id_is_a_parse_error(self) -> None:
        parsed = parse_frame('{"jsonrpc":"2.0","id":NaN,"method":"ping"}')
        self.assertIsInstance(parsed, FrameError)
        self.assertEqual(parsed.code, ERROR_PARSE)

    def test_an_initialize_carrying_a_non_json_constant_is_refused(self) -> None:
        raw = (
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":'
            '{"protocolVersion":"2025-06-18","capabilities":{"x":NaN},'
            '"clientInfo":{"name":"t","version":"1"}}}'
        )
        out, err = io.StringIO(), io.StringIO()
        server = McpServer(
            context=ReadPlanContext(repo_root=ROOT, redmine_live=False),
            stdin=io.StringIO(raw + "\n"),
            stdout=out,
            stderr=err,
        )
        server.serve()
        response = json.loads(out.getvalue().strip())
        self.assertEqual(response["error"]["code"], ERROR_PARSE)
        self.assertEqual(server._phase, PHASE_UNINITIALIZED)


class R2F3SingleSelectionPointTests(unittest.TestCase):
    """Backend selection was still implemented twice: MCP here, CLI there."""

    def test_the_cli_reaches_the_shared_resolution_entry(self) -> None:
        """The half of the claim that was previously false."""
        calls = {"shared": 0}
        real = shared_resolution.resolve_step_plan

        def spy(*args, **kwargs):
            calls["shared"] += 1
            return real(*args, **kwargs)

        args = argparse.Namespace(
            as_json=True, session=None, dry_run=True, repo=None, issue=None, journal=None
        )
        with patch.object(shared_resolution, "resolve_step_plan", spy), patch.object(
            cli_workflow, "_herdr_step_preflight", lambda _a: None
        ), patch.object(cli_workflow, "require_tmux", lambda: None), patch.object(
            cli_workflow, "current_pane", lambda: "%self"
        ), patch.object(
            cli_workflow, "_discover_candidates", lambda: []
        ), contextlib.redirect_stdout(io.StringIO()):
            cli_workflow.cmd_workflow_step(args)
        self.assertEqual(calls["shared"], 1)

    def test_the_mcp_entry_reaches_the_same_shared_entry(self) -> None:
        calls = {"shared": 0}
        real = shared_resolution.resolve_step_plan

        def spy(*args, **kwargs):
            calls["shared"] += 1
            return real(*args, **kwargs)

        with patch.object(shared_resolution, "resolve_step_plan", spy), patch.object(
            cli_workflow, "_herdr_step_preflight", lambda _a: None
        ), patch.object(cli_workflow, "require_tmux", lambda: None), patch.object(
            cli_workflow, "current_pane", lambda: "%self"
        ), patch.object(cli_workflow, "_discover_candidates", lambda: []):
            run_workflow_step_plan({}, ReadPlanContext(repo_root=ROOT))
        self.assertEqual(calls["shared"], 1)

    def test_the_cli_no_longer_selects_the_backend_itself(self) -> None:
        """Structural: the CLI command body must not re-derive the selection.

        `_herdr_step_preflight` is still the one place the backend check and the
        herdr resolver are wired together — but it is reached *through* the shared
        entry now, so `cmd_workflow_step` must not call it directly.
        """
        source = inspect.getsource(cli_workflow.cmd_workflow_step)
        self.assertNotIn("_herdr_step_preflight(", source)
        self.assertNotIn("resolve_workflow_step(", source)
        self.assertIn("resolve_step_plan(", source)

    def test_both_entries_agree_on_the_backend_for_the_same_repo(self) -> None:
        """The property the single selection point exists to guarantee."""
        outcome = _forward_outcome()
        with patch.object(
            cli_workflow, "_herdr_step_preflight", lambda _a: outcome
        ), _inert_safety():
            mcp = run_workflow_step_plan({}, ReadPlanContext(repo_root=ROOT))
            direct = shared_resolution.resolve_step_plan(ROOT)
        self.assertEqual(mcp.payload["backend"], direct.backend)
        self.assertIs(direct.live_outcome, outcome)

    def test_a_lane_abort_preserves_the_clis_exit_contract(self) -> None:
        """`die` already wrote to stderr; the CLI must re-raise that exact abort."""
        from mozyo_bridge.shared.errors import CommandAbort

        abort = CommandAbort("tmux is not installed or not in PATH")

        def boom():
            raise abort

        args = argparse.Namespace(
            as_json=True, session=None, dry_run=True, repo=None, issue=None, journal=None
        )
        with patch.object(cli_workflow, "_herdr_step_preflight", lambda _a: None), patch.object(
            cli_workflow, "require_tmux", boom
        ):
            with self.assertRaises(CommandAbort) as caught:
                cli_workflow.cmd_workflow_step(args)
        self.assertIs(caught.exception, abort)


# --------------------------------------------------------------------------- #
# Round 3 findings (review j#102599 full-surface adversarial, verdict j#102632)
# --------------------------------------------------------------------------- #


def _gating_action():
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_next_action import (  # noqa: E501
        WorkflowNextAction,
    )

    return WorkflowNextAction(
        action="integrate",
        owner_role="coordinator",
        target_issue="15151",
        route_identity="",
        anchor="redmine:15151:1",
        suggested_command="",
        risk_level="high",
        requires_confirmation=True,
        blocked_reason="",
        reason="pending integration",
        provider="",
    )


class R3F1SafetyCompositionTests(unittest.TestCase):
    """MCP returned the raw rail outcome, skipping the CLI's safety judgement."""

    def _plan(self, *, store_action, store_status, resume=None):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step_reconcile import (  # noqa: E501
            STORE_ABSENT,
        )

        del STORE_ABSENT
        with patch.object(
            cli_workflow, "_herdr_step_preflight", lambda _a: _forward_outcome()
        ), patch.object(
            cli_workflow,
            "_load_store_action",
            lambda _a, repo_root="": (store_action, store_status),
        ), patch.object(
            cli_workflow, "_maybe_operator_startup_resume_outcome", lambda _a, _o: resume
        ):
            return run_workflow_step_plan({}, ReadPlanContext(repo_root=ROOT))

    def test_a_gating_store_action_blocks_the_reported_plan(self) -> None:
        """The reported defect: a forward plan the CLI would refuse to step."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step_reconcile import (  # noqa: E501
            STORE_PRESENT,
        )

        outcome = self._plan(store_action=_gating_action(), store_status=STORE_PRESENT)
        self.assertEqual(outcome.payload["plan"]["execution"], "blocked")
        self.assertFalse(outcome.payload["plan"]["ok"])
        self.assertTrue(outcome.payload["safety_gated"])
        self.assertTrue(outcome.is_error)

    def test_an_absent_store_leaves_the_forward_plan_intact(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step_reconcile import (  # noqa: E501
            STORE_ABSENT,
        )

        outcome = self._plan(store_action=None, store_status=STORE_ABSENT)
        self.assertEqual(outcome.payload["plan"]["execution"], "ready")
        self.assertFalse(outcome.payload["safety_gated"])

    def test_an_outstanding_startup_gate_overrides_the_plan(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step_reconcile import (  # noqa: E501
            STORE_ABSENT,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step import (  # noqa: E501
            WorkflowStepOutcome,
        )

        gated = WorkflowStepOutcome(
            state="startup_resume",
            reason="operator_reported_done",
            execution="blocked",
            next_action="resume the operator startup gate",
            next_owner="coordinator",
            primitive="operator_startup_resume",
        )
        outcome = self._plan(
            store_action=None, store_status=STORE_ABSENT, resume=gated
        )
        self.assertEqual(outcome.payload["plan"]["primitive"], "operator_startup_resume")
        self.assertTrue(outcome.payload["safety_gated"])

    def test_the_reconcile_disposition_reaches_the_tool_payload(self) -> None:
        """A caller must be able to see that the store, not the lane, decided."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step_reconcile import (  # noqa: E501
            STORE_PRESENT,
        )

        outcome = self._plan(store_action=_gating_action(), store_status=STORE_PRESENT)
        self.assertIn("reconcile_disposition", outcome.payload["plan"])

    def test_both_entries_apply_the_same_composition(self) -> None:
        """Structural: the CLI no longer owns the safety steps privately."""
        cli_source = inspect.getsource(cli_workflow.cmd_workflow_step)
        shared_source = inspect.getsource(shared_resolution)
        for step in ("_load_store_action", "_maybe_operator_startup_resume_outcome"):
            self.assertNotIn(f"{step}(args", cli_source, step)
            self.assertIn(step, shared_source, step)


class R3F2InvalidRequestTests(unittest.TestCase):
    """An Invalid Request without an id was silently dropped as a notification."""

    def test_a_bad_jsonrpc_version_without_an_id_is_answered(self) -> None:
        parsed = parse_frame('{"jsonrpc":"1.0","method":"ping"}')
        self.assertIsInstance(parsed, FrameError)
        self.assertEqual(parsed.code, ERROR_INVALID_REQUEST)
        self.assertTrue(parsed.respondable)

    def test_a_non_string_method_without_an_id_is_answered(self) -> None:
        parsed = parse_frame('{"jsonrpc":"2.0","method":5}')
        self.assertIsInstance(parsed, FrameError)
        self.assertTrue(parsed.respondable)

    def test_a_missing_method_without_an_id_is_answered(self) -> None:
        parsed = parse_frame('{"jsonrpc":"2.0"}')
        self.assertIsInstance(parsed, FrameError)
        self.assertTrue(parsed.respondable)

    def test_the_server_answers_such_a_frame_with_a_null_id(self) -> None:
        responses, _, _ = session(
            [INITIALIZE, INITIALIZED, {"jsonrpc": "1.0", "method": "ping"}]
        )
        self.assertEqual(len(responses), 2)
        self.assertIsNone(responses[1]["id"])
        self.assertEqual(responses[1]["error"]["code"], ERROR_INVALID_REQUEST)

    def test_array_params_without_an_id_stay_a_silent_notification(self) -> None:
        """Deliberately unchanged: array params ARE a valid Request object.

        JSON-RPC allows params "by-position through an Array"; MCP simply defines
        no positional method. That makes it an application-level invalid-params on
        a well-formed Request object, and such an object without an id is a
        notification the server MUST NOT reply to.
        """
        parsed = parse_frame('{"jsonrpc":"2.0","method":"ping","params":[1]}')
        self.assertIsInstance(parsed, FrameError)
        self.assertFalse(parsed.respondable)

    def test_a_genuine_notification_is_still_silent(self) -> None:
        responses, _, _ = session(
            [INITIALIZE, INITIALIZED, {"jsonrpc": "2.0", "method": "notifications/foo"}]
        )
        self.assertEqual(len(responses), 1)


class R3F3SchemaExactInitializeTests(unittest.TestCase):
    """The initialize acceptance boundary was both looser and tighter than the schema."""

    def _init(self, **overrides):
        params = dict(INITIALIZE["params"])
        params.update(overrides)
        responses, _, server = session(
            [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}]
        )
        return responses[0], server

    def test_a_non_string_client_info_title_is_refused(self) -> None:
        response, _ = self._init(clientInfo={"name": "t", "version": "1", "title": []})
        self.assertEqual(response["error"]["code"], ERROR_INVALID_PARAMS)
        self.assertIn("clientInfo.title", response["error"]["data"]["invalid"])

    def test_a_non_object_roots_capability_is_refused(self) -> None:
        response, _ = self._init(capabilities={"roots": 1})
        self.assertIn("capabilities.roots", response["error"]["data"]["invalid"])

    def test_a_non_boolean_roots_list_changed_is_refused(self) -> None:
        response, _ = self._init(capabilities={"roots": {"listChanged": "yes"}})
        self.assertIn(
            "capabilities.roots.listChanged", response["error"]["data"]["invalid"]
        )

    def test_an_empty_name_and_version_are_accepted(self) -> None:
        """The schema states no length constraint; the non-empty rule was ours."""
        response, server = self._init(clientInfo={"name": "", "version": ""})
        self.assertIn("result", response)
        self.assertEqual(server._phase, PHASE_INITIALIZING)

    def test_required_members_are_still_required_and_typed(self) -> None:
        for bad in ({"version": "1"}, {"name": "t"}, {"name": 5, "version": "1"}):
            response, _ = self._init(clientInfo=bad)
            self.assertEqual(response["error"]["code"], ERROR_INVALID_PARAMS, bad)

    def test_a_fully_conforming_initialize_with_optionals_is_accepted(self) -> None:
        response, _ = self._init(
            clientInfo={"name": "c", "version": "1", "title": "C"},
            capabilities={"roots": {"listChanged": True}, "sampling": {}},
        )
        self.assertIn("result", response)


class R3F4DeliveryLandingTests(unittest.TestCase):
    """`no known anomaly` was read as `landed` — an absence turned into a fact."""

    class _Record:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    def test_an_uninterpretable_ledger_row_is_unconfirmed(self) -> None:
        self.assertEqual(
            landing_from_ledger_record(self._Record(status="wat", reason="")),
            VALUE_UNCONFIRMED,
        )

    def test_an_empty_ledger_row_is_unconfirmed(self) -> None:
        self.assertEqual(landing_from_ledger_record(self._Record()), VALUE_UNCONFIRMED)

    def test_no_record_at_all_is_unconfirmed(self) -> None:
        self.assertEqual(landing_from_ledger_record(None), VALUE_UNCONFIRMED)

    def test_only_a_confirmed_submission_is_landed(self) -> None:
        # `rail`, not `mode` (review j#103251 r4f2): a ledger record spells the
        # rail in its own vocabulary, and the pre-R5 fixture carried a `mode`
        # attribute no real ledger record has — which is exactly how the
        # attribute-name mismatch this fixture should have caught went unseen.
        record = self._Record(status="sent", reason="ok", rail="event_rail")
        self.assertEqual(landing_from_ledger_record(record), VALUE_LANDED)

    def test_the_shared_injection_stage_authority_is_what_decides(self) -> None:
        """No second delivery verdict: the same authority `delivered` uses."""
        source = inspect.getsource(landing_from_ledger_record)
        self.assertIn("injection_stage_for_outcome", source)
        self.assertIn("STAGE_SUBMITTED_CONFIRMED", source)

    def test_an_unread_delivery_source_stays_unknown_not_unconfirmed(self) -> None:
        report = compose_unit_state(UNIT, UnitFacts(delivery_readable=False))
        self.assertEqual(report.delivery.outcome.value, "unknown")


class R3F5PerRoleRuntimeTests(unittest.TestCase):
    """The runtime axis copied one lane value onto every role."""

    def _facts(self, observed_rows):
        from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.unit_state_tool import (  # noqa: E501
            LiveUnitStateSource,
        )

        source = LiveUnitStateSource(ReadPlanContext(repo_root=ROOT))
        with patch(
            "mozyo_bridge.e_120_operations_cockpit.f_120_cockpit_web_ui."
            "application.cockpit_payload.herdr_observed_units",
            return_value=(observed_rows, []),
        ):
            return source._runtime_observation(
                UnitRecord(
                    workspace_id="ws1",
                    lane_id="lane1",
                    project_id="p",
                    roles=("gateway", "worker"),
                )
            )

    class _Row:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    def test_distinct_per_role_states_are_reported_distinctly(self) -> None:
        row = self._Row(
            workspace_id="ws1",
            lane_id="lane1",
            backend="herdr",
            role_runtime_states=(("gateway", "awaiting_input"), ("worker", "busy")),
        )
        observation = self._facts([row])
        self.assertEqual(
            dict(observation.roles), {"gateway": "awaiting_input", "worker": "busy"}
        )
        self.assertTrue(observation.readable)

    def test_the_backend_field_holds_a_backend_not_a_runtime_state(self) -> None:
        row = self._Row(
            workspace_id="ws1",
            lane_id="lane1",
            backend="herdr",
            role_runtime_states=(("gateway", "busy"),),
        )
        self.assertEqual(self._facts([row]).backend, "herdr")

    def test_a_role_the_fold_does_not_cover_is_unknown_not_copied(self) -> None:
        """The reported defect: a lane-level value standing in for a role read."""
        row = self._Row(
            workspace_id="ws1",
            lane_id="lane1",
            backend="herdr",
            role_runtime_states=(("gateway", "busy"),),
        )
        observation = self._facts([row])
        self.assertEqual(dict(observation.roles)["worker"], "unknown")

    def test_no_matching_unit_reports_every_role_unknown(self) -> None:
        row = self._Row(
            workspace_id="other", lane_id="lane1", backend="herdr", role_runtime_states=()
        )
        observation = self._facts([row])
        self.assertEqual(
            dict(observation.roles), {"gateway": "unknown", "worker": "unknown"}
        )
        self.assertFalse(observation.readable)

    def test_an_unavailable_fold_reports_unknown_rather_than_raising(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.unit_state_tool import (  # noqa: E501
            LiveUnitStateSource,
        )

        source = LiveUnitStateSource(ReadPlanContext(repo_root=ROOT))
        with patch(
            "mozyo_bridge.e_120_operations_cockpit.f_120_cockpit_web_ui."
            "application.cockpit_payload.herdr_observed_units",
            side_effect=RuntimeError("herdr unavailable"),
        ):
            observation = source._runtime_observation(
                UnitRecord(
                    workspace_id="ws1", lane_id="lane1", project_id="p", roles=("worker",)
                )
            )
        self.assertEqual(dict(observation.roles), {"worker": "unknown"})
        self.assertFalse(observation.readable)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
