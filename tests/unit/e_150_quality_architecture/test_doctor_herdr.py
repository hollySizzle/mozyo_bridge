"""Specs for the doctor herdr backend probe boundary (#13355).

Pure-policy / fake-port tests: the verdict authority is exercised with
synthetic :class:`HerdrInventoryView` values — no live herdr binary, no config
read. The conditional-section contract (tmux byte-invariance: no ``herdr`` key
when the backend is not selected) is pinned both at the policy level and at the
``LiveDoctorSections`` collection level.
"""

from __future__ import annotations

import argparse
import unittest

from mozyo_bridge.application import doctor
from mozyo_bridge.application.doctor_herdr import (
    HerdrDoctorReads,
    HerdrSectionUseCase,
    LiveHerdrDoctorReads,
    build_attestation_joins,
    evaluate_herdr_section,
)
from mozyo_bridge.core.state.herdr_identity_attestation import (
    IdentityAttestationRecord,
    VERDICT_MISSING,
    VERDICT_PRESENT,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_observability import (  # noqa: E501
    HerdrInventoryView,
    project_observed_agents,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    REASON_BINARY_UNCONFIGURED,
    REASON_TRANSPORT_ERROR,
)


def _ok_view(segment: str = "ws-a", agents=()) -> HerdrInventoryView:
    return HerdrInventoryView(
        backend_selected=True, ok=True, workspace_segment=segment, agents=agents
    )


class FakeHerdrDoctorReads:
    def __init__(self, view: HerdrInventoryView) -> None:
        self._view = view

    def describe(self) -> HerdrInventoryView:
        return self._view


class EvaluateHerdrSectionTest(unittest.TestCase):
    def test_unselected_backend_yields_no_section(self) -> None:
        self.assertIsNone(
            evaluate_herdr_section(HerdrInventoryView(backend_selected=False))
        )

    def test_unreachable_server_is_a_fail_closed_error(self) -> None:
        section = evaluate_herdr_section(
            HerdrInventoryView(
                backend_selected=True,
                ok=False,
                reason=REASON_TRANSPORT_ERROR,
                detail="herdr agent list timed out",
                workspace_segment="ws-a",
            )
        )
        self.assertEqual("error", section["status"])
        self.assertFalse(section["server"]["reachable"])
        self.assertEqual(REASON_TRANSPORT_ERROR, section["server"]["reason"])
        self.assertTrue(
            any("server" in action for action in section["next_action"])
        )

    def test_unconfigured_binary_gets_the_binary_guidance(self) -> None:
        section = evaluate_herdr_section(
            HerdrInventoryView(
                backend_selected=True,
                ok=False,
                reason=REASON_BINARY_UNCONFIGURED,
                detail="no herdr binary configured",
            )
        )
        self.assertEqual("error", section["status"])
        self.assertTrue(
            any("MOZYO_HERDR_BINARY" in action for action in section["next_action"])
        )

    def test_unresolved_workspace_segment_is_a_warning(self) -> None:
        section = evaluate_herdr_section(_ok_view(segment=""))
        self.assertEqual("warning", section["status"])
        self.assertTrue(section["next_action"])

    def test_healthy_inventory_is_ok_with_agent_records_and_counts(self) -> None:
        ours = encode_assigned_name("ws-a", "claude", "")
        agents = project_observed_agents(
            [
                {"name": ours, "agent_status": "working", "pane_id": "%5"},
                {"name": "foreign", "agent_status": "idle"},
            ]
        )
        section = evaluate_herdr_section(_ok_view(agents=agents))

        self.assertEqual("ok", section["status"])
        self.assertTrue(section["server"]["reachable"])
        self.assertEqual(
            {"total": 2, "managed": 1, "workspace": 1, "unmanaged": 1},
            section["counts"],
        )
        managed = [a for a in section["agents"] if a["managed"]]
        self.assertEqual("busy", managed[0]["runtime_state"])
        self.assertEqual([], section["notes"])

    def test_empty_workspace_is_ok_with_a_note(self) -> None:
        section = evaluate_herdr_section(_ok_view(agents=()))
        self.assertEqual("ok", section["status"])
        self.assertTrue(section["notes"])


class HerdrSectionAttestationTest(unittest.TestCase):
    """Startup self-attestation join (Redmine #13637): a managed workspace agent with
    an absent / stale / missing / conflicting self-attestation drags doctor non-green,
    worded honestly (self-attestation, never a live-env read) and value-free."""

    def _view_with_agent(self, locator="%5"):
        ours = encode_assigned_name("ws-a", "claude", "")
        agents = project_observed_agents(
            [{"name": ours, "agent_status": "working", "pane_id": locator}]
        )
        return ours, _ok_view(agents=agents)

    def _rec(self, name, *, locator="%5", verdict=VERDICT_PRESENT):
        return IdentityAttestationRecord(
            assigned_name=name,
            workspace_id="ws-a",
            role="claude",
            lane_id="default",
            locator=locator,
            verdict=verdict,
        )

    def test_absent_record_is_warning_and_notes_self_attestation(self) -> None:
        ours, view = self._view_with_agent()
        joins = build_attestation_joins(view, lambda n: None)  # legacy / no record
        section = evaluate_herdr_section(view, attestations=joins)
        self.assertEqual("warning", section["status"])
        self.assertTrue(
            any("self-attestation" in note for note in section["notes"])
        )
        self.assertTrue(section["next_action"])

    def test_present_generation_matched_is_ok(self) -> None:
        ours, view = self._view_with_agent(locator="%5")
        joins = build_attestation_joins(
            view, lambda n: self._rec(n, locator="%5") if n == ours else None
        )
        section = evaluate_herdr_section(view, attestations=joins)
        self.assertEqual("ok", section["status"])

    def test_stale_locator_is_warning(self) -> None:
        ours, view = self._view_with_agent(locator="%5")
        joins = build_attestation_joins(
            view, lambda n: self._rec(n, locator="%OLD") if n == ours else None
        )
        section = evaluate_herdr_section(view, attestations=joins)
        self.assertEqual("warning", section["status"])
        self.assertTrue(any("stale" in note for note in section["notes"]))

    def test_missing_verdict_is_warning_without_leaking_values(self) -> None:
        ours, view = self._view_with_agent(locator="%5")
        joins = build_attestation_joins(
            view,
            lambda n: self._rec(n, locator="%5", verdict=VERDICT_MISSING)
            if n == ours
            else None,
        )
        section = evaluate_herdr_section(view, attestations=joins)
        self.assertEqual("warning", section["status"])
        # Value-free: notes carry states / identity, never an env value.
        joined_notes = " ".join(section["notes"])
        self.assertIn("missing", joined_notes)

    def test_use_case_reads_store_and_goes_warning_on_absent(self) -> None:
        ours, view = self._view_with_agent()
        section = HerdrSectionUseCase(
            FakeHerdrDoctorReads(view), attestation_reader=lambda n: None
        ).execute()
        self.assertEqual("warning", section["status"])

    def test_no_attestation_argument_is_byte_invariant_ok(self) -> None:
        # A caller that does not join (or the tmux / server-down paths) stays ok.
        _, view = self._view_with_agent()
        section = evaluate_herdr_section(view)  # no attestations kwarg
        self.assertEqual("ok", section["status"])


class HerdrSectionUseCaseTest(unittest.TestCase):
    def test_use_case_returns_none_when_unselected(self) -> None:
        use_case = HerdrSectionUseCase(
            FakeHerdrDoctorReads(HerdrInventoryView(backend_selected=False))
        )
        self.assertIsNone(use_case.execute())

    def test_use_case_returns_the_section_dict(self) -> None:
        section = HerdrSectionUseCase(FakeHerdrDoctorReads(_ok_view())).execute()
        self.assertEqual("ok", section["status"])
        self.assertEqual("herdr", section["backend"])

    def test_live_adapter_satisfies_the_port_protocol(self) -> None:
        adapter = LiveHerdrDoctorReads(argparse.Namespace(repo="/repo"))
        self.assertIsInstance(adapter, HerdrDoctorReads)


class LiveDoctorSectionsConditionalHerdrTest(unittest.TestCase):
    """The section map carries a ``herdr`` key iff the collector returns one."""

    _STUB_COLLECTORS = (
        "doctor_cli_section",
        "doctor_rules_section",
        "doctor_codex_skill_section",
        "doctor_claude_skill_section",
        "doctor_scaffold_section",
        "doctor_workspace_registry_section",
        "doctor_state_store_section",
        "doctor_claude_nagger_section",
        "doctor_claude_launch_policy_section",
        "doctor_tmux_section",
        "doctor_otel_section",
        "doctor_delivery_env_section",
    )

    def _collect(self, herdr_result):
        from mozyo_bridge.application.doctor_health import LiveDoctorSections

        stub = {"status": "ok", "next_action": []}
        originals = {
            name: getattr(doctor, name)
            for name in self._STUB_COLLECTORS + ("doctor_herdr_section",)
        }
        originals["doctor_tmux_ui_artifact_info"] = doctor.doctor_tmux_ui_artifact_info
        try:
            for name in self._STUB_COLLECTORS:
                setattr(doctor, name, lambda *a, **k: dict(stub))
            doctor.doctor_tmux_ui_artifact_info = lambda *a, **k: {"status": "ok"}
            doctor.doctor_herdr_section = lambda *a, **k: herdr_result
            return LiveDoctorSections(
                argparse.Namespace(repo="/repo", home=None)
            ).collect_sections()
        finally:
            for name, value in originals.items():
                setattr(doctor, name, value)

    def test_tmux_backend_has_no_herdr_key(self) -> None:
        sections = self._collect(None)
        self.assertNotIn("herdr", sections)

    def test_herdr_backend_carries_the_section(self) -> None:
        sections = self._collect({"status": "ok", "backend": "herdr"})
        self.assertIn("herdr", sections)
        self.assertEqual("ok", sections["herdr"]["status"])


class FormatDoctorTextHerdrTest(unittest.TestCase):
    def _base_sections(self) -> dict:
        return {
            "cli": {"status": "ok", "next_action": []},
            "rules": {"status": "ok", "next_action": []},
            "codex_skill": {"status": "ok", "next_action": []},
            "claude_skill": {"status": "ok", "next_action": []},
            "scaffold": {"status": "ok", "next_action": []},
            "tmux": {"status": "ok", "next_action": []},
        }

    def test_absent_section_renders_no_herdr_line(self) -> None:
        text = doctor.format_doctor_text(
            {"ok": True, "sections": self._base_sections()}
        )
        self.assertNotIn("herdr:", text)

    def test_present_section_renders_status_server_and_agents(self) -> None:
        sections = self._base_sections()
        sections["herdr"] = {
            "status": "ok",
            "backend": "herdr",
            "workspace_segment": "ws-a",
            "server": {"reachable": True, "reason": None, "detail": ""},
            "agents": [
                {
                    "name": "mzb1_wsZ2Da_claude_default",
                    "managed": True,
                    "workspace_id": "ws-a",
                    "lane_id": "default",
                    "role": "claude",
                    "runtime_state": "busy",
                    "raw_status": "working",
                    "locator": "%5",
                    "decode_reason": None,
                },
                {
                    "name": "foreign",
                    "managed": False,
                    "runtime_state": "unknown",
                    "raw_status": "",
                    "locator": "",
                    "decode_reason": "bad_prefix",
                },
            ],
            "counts": {"total": 2, "managed": 1, "workspace": 1, "unmanaged": 1},
            "notes": ["a note"],
            "next_action": ["an action"],
        }
        text = doctor.format_doctor_text({"ok": True, "sections": sections})

        self.assertIn("herdr: ok workspace=ws-a agents=1 managed/1 unmanaged", text)
        self.assertIn("server: reachable", text)
        self.assertIn(
            "mzb1_wsZ2Da_claude_default: state=busy raw=working "
            "slot=ws-a/default/claude locator=%5",
            text,
        )
        self.assertIn("foreign: unmanaged (bad_prefix) state=unknown", text)
        self.assertIn("note: a note", text)
        self.assertIn("-> an action", text)

    def test_unreachable_server_renders_the_fail_line(self) -> None:
        sections = self._base_sections()
        sections["herdr"] = {
            "status": "error",
            "backend": "herdr",
            "workspace_segment": "ws-a",
            "server": {
                "reachable": False,
                "reason": "transport_error",
                "detail": "herdr agent list timed out",
            },
            "agents": [],
            "counts": {"total": 0, "managed": 0, "workspace": 0, "unmanaged": 0},
            "notes": [],
            "next_action": ["restart the herdr server"],
        }
        text = doctor.format_doctor_text({"ok": False, "sections": sections})

        self.assertIn("herdr: error", text)
        self.assertIn(
            "server: UNREACHABLE (transport_error) herdr agent list timed out", text
        )
        self.assertIn("-> restart the herdr server", text)


if __name__ == "__main__":
    unittest.main()
