"""Fake-port / pure-policy specifications for the doctor CLI boundary (#12845).

These exercise the ``doctor_cli`` drift verdict authority and CLI-install read
port directly, with a synthetic CLI read-view (including a synthetic source-drift
record) — without a real checkout / stale-install topology and without parsing a
real ``src/mozyo_bridge/__init__.py``. They are the checkout-topology ->
fake-port / fake-policy migration for the ``cli`` section slice.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from mozyo_bridge.application import doctor
from mozyo_bridge.application.doctor_cli import (
    CliReads,
    CliSectionUseCase,
    CliSectionVerdict,
    LiveCliReads,
    evaluate_cli_section,
)


def _drift(
    *,
    relation: str = "version-differs",
    source_version: str = "0.8.0",
    running_version: str = "0.9.2",
    repo_local_invocation: str = "PYTHONPATH=src python3 -m mozyo_bridge",
) -> dict[str, Any]:
    return {
        "source_package": "/checkout/src/mozyo_bridge",
        "source_version": source_version,
        "running_version": running_version,
        "running_package": "/opt/site-packages/mozyo_bridge",
        "relation": relation,
        "repo_local_invocation": repo_local_invocation,
    }


def _cli_view(
    *,
    drift: dict[str, Any] | None,
    version: str = "0.9.2",
    executable: str = "/usr/local/bin/mozyo-bridge",
    package_path: str = "/opt/site-packages/mozyo_bridge",
    python: str = "/usr/bin/python3",
    subcommands: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "version": version,
        "executable": executable,
        "package_path": package_path,
        "python": python,
        "subcommands": subcommands if subcommands is not None else ["doctor", "rules", "scaffold"],
        "drift": drift,
    }


class FakeCliReads:
    """In-memory fake of the ``CliReads`` port."""

    def __init__(self, view: dict[str, Any]) -> None:
        self._view = view
        self.calls = 0

    def describe(self) -> dict[str, Any]:
        self.calls += 1
        return self._view


class EvaluateCliSectionPolicyTest(unittest.TestCase):
    def test_no_drift_is_ok_with_no_next_action(self) -> None:
        verdict = evaluate_cli_section(_cli_view(drift=None))
        self.assertEqual(CliSectionVerdict(status="ok"), verdict)

    def test_version_differs_drift_warns_with_repo_local_guidance(self) -> None:
        verdict = evaluate_cli_section(
            _cli_view(drift=_drift(relation="version-differs", source_version="0.8.0"))
        )
        self.assertEqual("warning", verdict.status)
        self.assertEqual(1, len(verdict.next_action))
        message = verdict.next_action[0]
        self.assertIn("running mozyo-bridge is the installed CLI (version 0.9.2)", message)
        self.assertIn("src/mozyo_bridge 0.8.0", message)
        self.assertIn("PYTHONPATH=src python3 -m mozyo_bridge <args>", message)
        # The same-version clause must NOT appear for a version-differs drift.
        self.assertNotIn("same version string does not guarantee", message)

    def test_same_version_drift_appends_dogfooding_clause(self) -> None:
        verdict = evaluate_cli_section(
            _cli_view(drift=_drift(relation="same-version", source_version="0.9.2"))
        )
        self.assertEqual("warning", verdict.status)
        message = verdict.next_action[0]
        self.assertIn(
            "(same version string does not guarantee the same commits "
            "during dogfooding; the install can lack newer subcommands)",
            message,
        )

    def test_unknown_relation_uses_version_unknown_label(self) -> None:
        verdict = evaluate_cli_section(
            _cli_view(drift=_drift(relation="unknown", source_version=""))
        )
        self.assertEqual("warning", verdict.status)
        message = verdict.next_action[0]
        self.assertIn("src/mozyo_bridge version unknown", message)
        self.assertNotIn("same version string does not guarantee", message)

    def test_message_is_byte_identical_to_legacy_collector_text(self) -> None:
        # Pin the full version-differs message so the verdict authority cannot
        # drift from the historical operator guidance.
        verdict = evaluate_cli_section(
            _cli_view(drift=_drift(relation="version-differs", source_version="0.8.0"))
        )
        self.assertEqual(
            (
                "running mozyo-bridge is the installed CLI (version 0.9.2) but "
                "this checkout has repo-local source (src/mozyo_bridge 0.8.0); "
                "during active development run the repo-local CLI instead: "
                "PYTHONPATH=src python3 -m mozyo_bridge <args>",
            ),
            verdict.next_action,
        )


class CliSectionUseCaseTest(unittest.TestCase):
    def test_use_case_assembles_legacy_ok_section_dict_without_source_drift(self) -> None:
        view = _cli_view(drift=None)
        reads = FakeCliReads(view)

        section = CliSectionUseCase(reads).execute()

        self.assertEqual(
            {
                "status": "ok",
                "version": "0.9.2",
                "executable": "/usr/local/bin/mozyo-bridge",
                "package_path": "/opt/site-packages/mozyo_bridge",
                "python": "/usr/bin/python3",
                "subcommands": ["doctor", "rules", "scaffold"],
                "next_action": [],
            },
            section,
        )
        # The legacy collector omits source_drift entirely when there is none.
        self.assertNotIn("source_drift", section)
        self.assertEqual(1, reads.calls)

    def test_use_case_attaches_source_drift_and_warning_next_action(self) -> None:
        drift = _drift(relation="version-differs", source_version="0.8.0")
        view = _cli_view(drift=drift)

        section = CliSectionUseCase(FakeCliReads(view)).execute()

        self.assertEqual("warning", section["status"])
        self.assertIs(drift, section["source_drift"])
        self.assertEqual(1, len(section["next_action"]))
        self.assertIsInstance(section["next_action"], list)
        # Key insertion order: source_drift is appended last (after next_action).
        self.assertEqual(
            [
                "status",
                "version",
                "executable",
                "package_path",
                "python",
                "subcommands",
                "next_action",
                "source_drift",
            ],
            list(section.keys()),
        )

    def test_use_case_copies_subcommands_into_a_fresh_list(self) -> None:
        view = _cli_view(drift=None)
        section = CliSectionUseCase(FakeCliReads(view)).execute()
        self.assertEqual(view["subcommands"], section["subcommands"])
        self.assertIsNot(view["subcommands"], section["subcommands"])

    def test_live_adapter_satisfies_the_port_protocol(self) -> None:
        self.assertIsInstance(LiveCliReads(None), CliReads)


class LiveCliReadsTest(unittest.TestCase):
    """The live adapter introspects the running package and resolves
    ``repo_local_source_drift`` at call time; drift is detected only when a
    ``target`` checkout is diagnosed."""

    def test_no_target_skips_drift_detection(self) -> None:
        def fail_drift(*args: Any, **kwargs: Any) -> None:  # pragma: no cover
            raise AssertionError("repo_local_source_drift must not run without a target")

        original = doctor.repo_local_source_drift
        doctor.repo_local_source_drift = fail_drift
        try:
            view = LiveCliReads(None).describe()
        finally:
            doctor.repo_local_source_drift = original

        from mozyo_bridge import __version__

        self.assertIsNone(view["drift"])
        self.assertEqual(list(doctor.EXPECTED_SUBCOMMANDS), view["subcommands"])
        self.assertEqual(__version__, view["version"])

    def test_target_routes_through_repo_local_source_drift_at_call_time(self) -> None:
        sentinel = _drift(relation="same-version")
        captured: dict[str, Any] = {}

        def fake_drift(target: Path, running_package_path: Path, running_version: str) -> dict[str, Any]:
            captured["target"] = target
            captured["running_package_path"] = running_package_path
            captured["running_version"] = running_version
            return sentinel

        target = Path("/checkout")
        original = doctor.repo_local_source_drift
        doctor.repo_local_source_drift = fake_drift
        try:
            view = LiveCliReads(target).describe()
        finally:
            doctor.repo_local_source_drift = original

        self.assertIs(sentinel, view["drift"])
        self.assertEqual(target, captured["target"])
        # The drift detector is fed the running version + package path the
        # adapter just introspected.
        self.assertEqual(view["version"], captured["running_version"])
        self.assertEqual(view["package_path"], str(captured["running_package_path"]))


class DoctorCollectorDelegationTest(unittest.TestCase):
    """``doctor_cli_section`` is now a thin handler over the use case; it still
    routes through the live CLI read."""

    def test_collector_matches_use_case_over_live_read_no_target(self) -> None:
        section = doctor.doctor_cli_section()
        expected = CliSectionUseCase(LiveCliReads(None)).execute()
        self.assertEqual(expected, section)
        self.assertNotIn("source_drift", section)

    def test_collector_matches_use_case_over_live_read_with_target(self) -> None:
        target = Path("/tmp/doctor-cli-delegation-no-checkout")
        section = doctor.doctor_cli_section(target)
        expected = CliSectionUseCase(LiveCliReads(target)).execute()
        self.assertEqual(expected, section)


if __name__ == "__main__":
    unittest.main()
