"""Redmine #15151 R5 — the six accepted R4 findings, each pinned (verdict j#103251).

One class per finding. Every case is written against the *defect*, not the fix: a
mutation that reintroduces the R4 behaviour must turn the corresponding class red.

- r4f1: the Unit's consumer role vocabulary (gateway/worker) meets producer rows
  keyed by provider (codex/claude); the adapter translates via the dispatch rail's
  own resolvers instead of reporting healthy rows `unknown`.
- r4f2: the delivery ledger spells its fields differently from a DeliveryOutcome
  (`rail`, `queue_enter_observation`); the landing read re-spells them losslessly
  so the shared authority's queue-enter carve-out actually applies.
- r4f3: `workflow_step_plan` publishes an allowlisted plan projection — no pane
  identity, no private filesystem path, no resolver free text.
- r4f4: `structuredContent` conforms to the declared `outputSchema` on every
  path; a nonconforming result is withheld, never emitted.
- r4f5: the derived health members carry the same observation envelope as every
  other reported field, with their value types preserved.
- r4f6: `initialize` accepts any string `protocolVersion` (the schema's own
  boundary) and validates that each `capabilities.experimental` value is an
  object.
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.mcp_server import (  # noqa: E402,E501
    McpServer,
    PROTOCOL_VERSION,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.read_plan_tools import (  # noqa: E402,E501
    PLAN_PUBLIC_FIELDS,
    ReadPlanContext,
    run_workflow_step_plan,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.tool_dispatch import (  # noqa: E402,E501
    dispatch_tool_call,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.unit_state_tool import (  # noqa: E402,E501
    LiveUnitStateSource,
    landing_from_ledger_record,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.tool_catalog import (  # noqa: E402,E501
    TOOL_CATALOG,
    conforming_skeleton,
    tool_definition,
    validate_output,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.unit_selector import (  # noqa: E402,E501
    UnitRecord,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.unit_state import (  # noqa: E402,E501
    SOURCE_DERIVED,
    derive_health,
)


class _Row:
    """A producer-shaped ObservedUnit row, as the live herdr fold emits it."""

    def __init__(self, workspace_id, lane_id, states):
        self.workspace_id = workspace_id
        self.lane_id = lane_id
        self.backend = "herdr"
        self.role_runtime_states = states


class R4F1ProducerRoleVocabularyTests(unittest.TestCase):
    """The fold keys rows by provider; the Unit asks by consumer role."""

    def _observe(self, states, mapping=None):
        source = LiveUnitStateSource(ReadPlanContext(repo_root=ROOT))
        unit = UnitRecord(
            workspace_id="ws1",
            lane_id="lane1",
            project_id="p",
            roles=("gateway", "worker"),
        )
        # The mapping is fixed here so these cases test the TRANSLATION; the
        # resolver wiring has its own case below, without assuming which provider
        # this repo's config binds to which role.
        resolved = mapping if mapping is not None else {
            "gateway": "codex", "worker": "claude"
        }
        with patch.object(
            LiveUnitStateSource, "_provider_for_consumer_role", return_value=resolved
        ):
            with patch(
                "mozyo_bridge.e_120_operations_cockpit.f_120_cockpit_web_ui."
                "application.cockpit_payload.herdr_observed_units",
                return_value=([_Row("ws1", "lane1", states)], []),
            ):
                return source._runtime_observation(unit)

    def test_a_real_producer_row_reports_both_roles(self) -> None:
        # The R4 defect: these provider-keyed states resolved to `unknown` for
        # both consumer roles, so a perfectly healthy pair read as unreadable.
        observed = self._observe((("codex", "awaiting_input"), ("claude", "busy")))

        states = dict(observed.roles)
        self.assertEqual("awaiting_input", states["gateway"])
        self.assertEqual("busy", states["worker"])
        self.assertTrue(observed.readable)

    def test_an_exact_consumer_role_key_still_wins(self) -> None:
        # A future producer that emits the consumer vocabulary directly must not
        # be double-translated.
        observed = self._observe((("gateway", "busy"), ("claude", "awaiting_input")))

        self.assertEqual("busy", dict(observed.roles)["gateway"])

    def test_an_unresolvable_binding_degrades_to_unknown(self) -> None:
        observed = self._observe((("codex", "busy"),), mapping={})

        self.assertEqual("unknown", dict(observed.roles)["gateway"])

    def test_the_mapping_comes_from_the_dispatch_rails_own_resolvers(self) -> None:
        # No assumption about WHICH provider this repo binds to which role — the
        # claim is only that this read and the dispatch rail resolve identically.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
            resolve_gateway_provider,
            resolve_worker_provider,
        )

        source = LiveUnitStateSource(ReadPlanContext(repo_root=ROOT))
        mapping = source._provider_for_consumer_role()

        self.assertEqual(resolve_gateway_provider(str(ROOT)), mapping["gateway"])
        self.assertEqual(resolve_worker_provider(str(ROOT)), mapping["worker"])


class _Record:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class R4F2LedgerObservationConveyanceTests(unittest.TestCase):
    """The queue-enter carve-out must see the ledger's own telemetry fields."""

    def test_queue_enter_ok_without_evidence_is_not_landed(self) -> None:
        # The R4 defect in one line: rail + observation were dropped by attribute
        # name, so this record was read `landed` from status/reason alone.
        record = _Record(
            status="sent",
            reason="ok",
            rail="queue_enter_rail",
            queue_enter_observation={"runtime_state": "awaiting_input"},
            turn_start_outcome=None,
        )

        self.assertEqual("unconfirmed", landing_from_ledger_record(record))

    def test_queue_enter_ok_with_positive_evidence_lands(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.injection_stage import (  # noqa: E501
            injection_stage_for,
            STAGE_SUBMITTED_CONFIRMED,
        )

        # Build an observation the shared authority itself accepts, so this case
        # can never drift from the authority's evidence rule.
        # The event rail's armed-wait outcome is the simpler of the two causal
        # signals the authority accepts; assert the authority accepts it so this
        # fixture can never drift from the authority's evidence rule.
        candidate = None
        turn_start = {"outcome": "started"}
        stage = injection_stage_for(
            "sent", "ok", mode="queue-enter",
            queue_enter_turn_start_observation=candidate,
            turn_start_outcome=turn_start,
        )
        self.assertEqual(STAGE_SUBMITTED_CONFIRMED, stage)

        record = _Record(
            status="sent",
            reason="ok",
            rail="queue_enter_rail",
            queue_enter_observation=candidate,
            turn_start_outcome=turn_start,
        )
        self.assertEqual("landed", landing_from_ledger_record(record))

    def test_the_queue_observation_alone_can_land_and_its_conveyance_is_load_bearing(
        self,
    ) -> None:
        """Adversarial-verification finding 1: the conveyed attribute, landed-direction.

        The positive case above lands via `turn_start_outcome`, so a regression
        that drops ONLY `queue_enter_observation` (the exact attribute-name
        mismatch r4f2 was about) stayed green. Here the causal v2 queue
        observation is the ONLY evidence: severing its conveyance turns this
        landed into unconfirmed.
        """
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.injection_stage import (  # noqa: E501
            injection_stage_for,
            STAGE_SUBMITTED_CONFIRMED,
        )

        observation = {
            "observation_version": 2,
            "event_wait_kind": "changed",
            "baseline_runtime_state": "awaiting_input",
            "gateway_binding": {
                "provider": "codex",
                "assigned_name": "mzb1_ws_codex_default",
                "locator": "w1:p2",
                "row_revision": "3",
                "attestation_observed_at": "2026-08-16T00:00:00+00:00",
                "startup_action_id": "startup-abc",
            },
        }
        # The authority itself must accept this shape, so the fixture cannot
        # drift from the evidence rule.
        self.assertEqual(
            STAGE_SUBMITTED_CONFIRMED,
            injection_stage_for(
                "sent", "ok", mode="queue-enter",
                queue_enter_turn_start_observation=observation,
            ),
        )

        record = _Record(
            status="sent",
            reason="ok",
            rail="queue_enter_rail",
            queue_enter_observation=observation,
            turn_start_outcome=None,
        )
        self.assertEqual("landed", landing_from_ledger_record(record))
        # Sever the conveyance the way the original defect did — the attribute
        # simply is not there — and the same record must stop landing.
        severed = _Record(
            status="sent",
            reason="ok",
            rail="queue_enter_rail",
            turn_start_outcome=None,
        )
        self.assertEqual("unconfirmed", landing_from_ledger_record(severed))

    def test_an_unrecognised_rail_never_lands_on_status_alone(self) -> None:
        for rail in ("", "other", None):
            record = _Record(status="sent", reason="ok", rail=rail)
            self.assertEqual(
                "unconfirmed", landing_from_ledger_record(record), f"rail={rail!r}"
            )


class R4F3PlanProjectionTests(unittest.TestCase):
    """The published plan carries the contract surface, never the wiring."""

    class _Outcome:
        ok = True

        def as_payload(self):
            return {
                "state": "review_requested",
                # Exactly the real producer shape (review j#106183 r5f1): the
                # role-authority branch interpolates project_scope into this
                # prose, and the blocked branch appends the resolver detail.
                "next_action": (
                    "authority resolves (project_scope='SCOPE_MARKER') "
                    "(DETAIL_MARKER at /home/someone w9:p9)"
                ),
                "execution": "plan",
                "reason": "ok",
                "next_owner": "auditor",
                "primitive": "none",
                "durable_anchor": "#15151 j#103253",
                "caller_role": "claude",
                "callback_classification": "",
                "callback_to_role": "",
                "ok": True,
                "detail": "resolved via pane %s under /home/someone/checkout" % "w9:p9",
                "target_pane": "w9:p9",
                "self_pane": "w9:p8",
                "repo_root": "/home/someone/checkout",
                "project_scope": "secret-project",
                # Adversarial-verification finding 5: a field NOBODY has named.
                # A denylist of the known-private keys passes it through; only a
                # true allowlist keeps it out.
                "future_private_thing": "w9:p7 at /home/someone/other",
            }

    class _Resolution:
        backend = "herdr"
        gated = False
        reconciled = None

        def __init__(self, outcome):
            self.outcome = outcome

    def _payload(self):
        resolution = self._Resolution(self._Outcome())
        with patch(
            "mozyo_bridge.e_110_execution_platform."
            "f_140_delegated_coordinator_nested_handoff.application."
            "workflow_step_plan_resolution.resolve_step_plan",
            return_value=resolution,
        ):
            outcome = run_workflow_step_plan({}, ReadPlanContext(repo_root=ROOT))
        return outcome.payload

    def test_no_pane_path_or_free_text_reaches_the_payload(self) -> None:
        rendered = json.dumps(self._payload(), ensure_ascii=False)

        for leaked in (
            "w9:p9",
            "w9:p8",
            "/home/someone/checkout",
            "secret-project",
            "future_private_thing",
            "SCOPE_MARKER",
            "DETAIL_MARKER",
        ):
            self.assertNotIn(leaked, rendered)

    def test_the_contract_surface_survives_the_projection(self) -> None:
        plan = self._payload()["plan"]

        self.assertEqual("review_requested", plan["state"])
        self.assertEqual("#15151 j#103253", plan["durable_anchor"])
        # The allowlist plus the ONE reconstructed member (review j#106183
        # r5f1): `next_action` is never the producer's prose.
        self.assertTrue(
            set(plan).issubset(set(PLAN_PUBLIC_FIELDS) | {"next_action"})
        )

    def test_next_action_is_reconstructed_from_closed_tokens_only(self) -> None:
        """Review j#106183 r5f1: the producer's next_action prose interpolates
        project_scope (and, blocked, the resolver detail) — so the public value
        is a fixed template over closed tokens, never the outcome's own text."""
        plan = self._payload()["plan"]

        self.assertNotIn("SCOPE_MARKER", plan["next_action"])
        self.assertNotIn("DETAIL_MARKER", plan["next_action"])
        self.assertIn("review_requested", plan["next_action"])
        self.assertIn("auditor", plan["next_action"])


class R5F1LaneRefusalStaysValueFreeTests(unittest.TestCase):
    """Review j#106183 r5f1, third channel: the refusal must not copy str(exc)."""

    def test_the_refusal_carries_fixed_wording_not_the_exception_body(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_step_plan_resolution import (  # noqa: E501
            LaneUnavailable,
        )

        marker = "PANE_MARKER w7:p7 under /home/secret/checkout"
        with patch(
            "mozyo_bridge.e_110_execution_platform."
            "f_140_delegated_coordinator_nested_handoff.application."
            "workflow_step_plan_resolution.resolve_step_plan",
            side_effect=LaneUnavailable(marker),
        ):
            outcome = run_workflow_step_plan({}, _context())

        self.assertTrue(outcome.is_error)
        rendered = json.dumps(outcome.payload, ensure_ascii=False) + (
            outcome.summary or ""
        )
        for leaked in ("PANE_MARKER", "w7:p7", "/home/secret/checkout"):
            self.assertNotIn(leaked, rendered)
        self.assertIn("could not be resolved", outcome.payload["message"])
        self.assertIn(
            "could not be resolved", outcome.payload["source_health"]["notes"][0]
        )


class R4F4OutputSchemaConformanceTests(unittest.TestCase):
    """`structuredContent` conforms to the declared schema, or is withheld."""

    def test_every_error_path_yields_conformant_structured_content(self) -> None:
        # The generic handler-failure path, driven through the real dispatcher,
        # for every tool in the catalog: the R4 defect was that none of these
        # conformed.
        for name in TOOL_CATALOG:
            definition = tool_definition(name)
            arguments = {
                "docs_resolve": {"paths": ["README.md"]},
                "workflow_glance": {},
                "workflow_step_plan": {},
                "unit_state": {
                    "unit": {"workspace_id": "w", "lane_id": "l", "project_id": "p"}
                },
            }[name]
            with patch.dict(
                "mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry."
                "application.tool_dispatch._HANDLERS",
                {name: _raise},
            ):
                dispatched = dispatch_tool_call(name, arguments, _context())
            result = dispatched.result
            self.assertTrue(result["isError"], name)
            structured = result.get("structuredContent")
            self.assertIsNotNone(structured, name)
            self.assertEqual(
                (), validate_output(definition, structured), name
            )
            # The typed error fields survive the projection.
            self.assertEqual("tool_failed", structured["error"], name)

    def test_a_nonconforming_result_is_withheld_not_emitted(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.tool_dispatch import (  # noqa: E501
            _tool_result,
        )
        from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.read_plan_tools import (  # noqa: E501
            ToolOutcome,
        )

        # A SUCCESS payload that breaks its schema is this server's own bug; the
        # fail-closed answer is no structuredContent at all.
        broken = ToolOutcome(payload={"nonsense": True}, is_error=False, summary="x")
        result = _tool_result(broken, tool_definition("docs_resolve"))

        self.assertTrue(result["isError"])
        self.assertNotIn("structuredContent", result)
        self.assertIn("output schema", result["content"][0]["text"])

    def test_the_skeleton_satisfies_every_declared_schema(self) -> None:
        # Recursive by construction: source_health carries its own required list.
        for name, definition in TOOL_CATALOG.items():
            skeleton = conforming_skeleton(definition.output_schema)
            self.assertEqual(
                (), validate_output(definition, skeleton), name
            )

    def test_the_skeleton_never_claims_healthy_sources_on_an_error(self) -> None:
        """Adversarial-verification finding 2: the boolean zero said 'healthy'."""
        for name in ("workflow_glance", "workflow_step_plan"):
            skeleton = conforming_skeleton(tool_definition(name).output_schema)
            self.assertIs(
                True,
                skeleton["source_health"]["degraded"],
                f"{name}: an error skeleton claimed its sources were healthy",
            )

    def test_the_step_plan_skeleton_keeps_the_plan_only_token(self) -> None:
        """Adversarial-verification finding 3, with the anti-drift pin."""
        from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.read_plan_tools import (  # noqa: E501
            EXECUTION_PLAN_ONLY,
        )

        skeleton = conforming_skeleton(
            tool_definition("workflow_step_plan").output_schema
        )
        # The domain schema cannot import the application constant, so the two
        # literals are pinned equal here instead.
        self.assertEqual(EXECUTION_PLAN_ONLY, skeleton["execution"])

    def test_output_schemas_carry_only_enforced_keywords(self) -> None:
        """Adversarial-verification finding 7: validate_output now enforces the
        output schemas, so an unsupported keyword there is a silently skipped
        check — the surface guard must walk the output side too."""
        from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.tool_catalog import (  # noqa: E501
            ToolDefinition,
            catalog_surface_violations,
        )

        self.assertEqual((), catalog_surface_violations())
        bad = ToolDefinition(
            name="bad",
            title="t",
            description="d",
            input_schema={"type": "object"},
            output_schema={"type": "object", "patternProperties": {}},
        )
        violations = catalog_surface_violations({"bad": bad})
        self.assertTrue(
            any("patternProperties" in violation for violation in violations)
        )

    def test_the_unit_state_skeleton_stays_read_only(self) -> None:
        skeleton = conforming_skeleton(tool_definition("unit_state").output_schema)
        # The declared default wins over the boolean zero: even a refusal comes
        # from a read-only tool.
        self.assertIs(True, skeleton["read_only"])


class R4F5HealthProvenanceTests(unittest.TestCase):
    """The derived health members carry the shared observation envelope."""

    ENVELOPE_KEYS = {"value", "source", "observed_at", "freshness", "readability", "note"}

    def test_every_health_member_is_an_envelope_with_its_type_kept(self) -> None:
        axis = derive_health((), observed_at="2026-08-16T00:00:00Z")
        payload = axis.as_payload()

        for member, kind in (("degraded", bool), ("freshness", str), ("notes", list)):
            envelope = payload[member]
            self.assertEqual(self.ENVELOPE_KEYS, set(envelope), member)
            self.assertIsInstance(envelope["value"], kind, member)
            self.assertEqual(SOURCE_DERIVED, envelope["source"], member)
            self.assertEqual("2026-08-16T00:00:00Z", envelope["observed_at"], member)

    def test_the_composer_stamps_the_read_instant_onto_health(self) -> None:
        """Adversarial-verification finding 4: the plumbing, not just the axis."""
        from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.unit_state_tool import (  # noqa: E501
            UnitFacts,
            compose_unit_state,
        )

        report = compose_unit_state(
            UnitRecord(workspace_id="w", lane_id="l", project_id="p"),
            UnitFacts(
                issue_id="1",
                workflow_readable=True,
                workflow_observed_at="2026-08-16T01:02:03Z",
                workflow_freshness="fresh",
            ),
        )

        payload = report.health.as_payload()
        self.assertEqual(
            "2026-08-16T01:02:03Z", payload["degraded"]["observed_at"]
        )

    def test_no_bare_scalar_survives_recursively(self) -> None:
        # The model's own contract: "there is no bare-scalar path". Walk the
        # whole health payload; every scalar must sit inside an envelope.
        payload = derive_health((), observed_at="t0").as_payload()

        def walk(node, path, inside_envelope):
            if isinstance(node, dict):
                is_envelope = set(node) == self.ENVELOPE_KEYS
                for key, value in node.items():
                    walk(value, f"{path}.{key}", inside_envelope or is_envelope)
                return
            if isinstance(node, list):
                for index, item in enumerate(node):
                    walk(item, f"{path}[{index}]", inside_envelope)
                return
            self.assertTrue(
                inside_envelope, f"bare scalar outside any envelope at {path}"
            )

        walk(payload, "health", False)


class R4F6InitializeSchemaBoundaryTests(unittest.TestCase):
    """The acceptance boundary is the MCP schema — no looser, no tighter."""

    def _session(self, params):
        stdout = io.StringIO()
        server = McpServer(
            context=_context(),
            stdin=io.StringIO(
                json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}
                )
                + "\n"
            ),
            stdout=stdout,
            stderr=io.StringIO(),
        )
        server.serve()
        return json.loads(stdout.getvalue().splitlines()[0])

    def _params(self, **over):
        params = {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        }
        params.update(over)
        return params

    def test_an_empty_protocol_version_is_accepted_and_answered_with_ours(self) -> None:
        # r4f6 half one: the schema says `string`, full stop. The pre-R5 server
        # invented a non-empty rule and refused a conforming client.
        response = self._session(self._params(protocolVersion=""))

        self.assertIn("result", response)
        self.assertEqual(PROTOCOL_VERSION, response["result"]["protocolVersion"])

    def test_a_whitespace_protocol_version_is_accepted_too(self) -> None:
        response = self._session(self._params(protocolVersion="   "))

        self.assertIn("result", response)

    def test_a_non_string_protocol_version_is_still_refused(self) -> None:
        response = self._session(self._params(protocolVersion=7))

        self.assertIn("error", response)

    def test_an_experimental_value_that_is_not_an_object_is_refused(self) -> None:
        # r4f6 half two: `experimental` is `{ [key: string]: object }`; the member
        # itself was typed but its values went unvalidated.
        response = self._session(
            self._params(capabilities={"experimental": {"feature": "yes"}})
        )

        self.assertIn("error", response)
        self.assertIn(
            "capabilities.experimental.feature", response["error"]["data"]["invalid"]
        )

    def test_object_experimental_values_are_accepted(self) -> None:
        response = self._session(
            self._params(capabilities={"experimental": {"feature": {}}})
        )

        self.assertIn("result", response)


def _context() -> ReadPlanContext:
    return ReadPlanContext(repo_root=ROOT)


def _raise(arguments, context):
    raise RuntimeError("boom")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
