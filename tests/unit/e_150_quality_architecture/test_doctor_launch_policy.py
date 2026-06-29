"""Fake-port / pure-policy specifications for the doctor launch-policy boundary (#12835).

These exercise the ``doctor_launch_policy`` verdict authority and launch-policy
read port directly, with a synthetic policy view — without patching
``os.environ`` and without monkeypatching the ``doctor.*`` collectors or any
``commands.*`` doctor helper. They are the env-patch -> fake-port / fake-policy
migration for the ``claude_launch_policy`` section slice.
"""

from __future__ import annotations

import unittest
from typing import Any

from mozyo_bridge.application import doctor
from mozyo_bridge.application.doctor_launch_policy import (
    SECTION_SCOPE,
    LaunchPolicyReads,
    LaunchPolicySectionUseCase,
    LaunchPolicySectionVerdict,
    LiveLaunchPolicyReads,
    evaluate_launch_policy_section,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.claude_permission_policy import (
    CLAUDE_PERMISSION_MODE_ENV,
    SOURCE_ENV_INVALID,
    SOURCE_ENV_OVERRIDE,
    SOURCE_NONE,
    SOURCE_POLICY_DEFAULT,
)


def _policy_view(
    *,
    source: str,
    effective_mode: str | None,
    reproducible_auto: bool,
    env_present: bool = False,
    env_value: str | None = None,
    policy_default: str | None = "auto",
) -> dict[str, Any]:
    return {
        "env_var": CLAUDE_PERMISSION_MODE_ENV,
        "env_present": env_present,
        "env_value": env_value,
        "env_valid": source != SOURCE_ENV_INVALID,
        "policy_default": policy_default,
        "effective_mode": effective_mode,
        "source": source,
        "reproducible_auto": reproducible_auto,
    }


class FakeLaunchPolicyReads:
    """In-memory fake of the ``LaunchPolicyReads`` port."""

    def __init__(self, policy: dict[str, Any]) -> None:
        self._policy = policy
        self.calls = 0

    def describe(self) -> dict[str, Any]:
        self.calls += 1
        return self._policy


class EvaluateLaunchPolicySectionPolicyTest(unittest.TestCase):
    def test_reproducible_auto_is_ok_with_no_next_action(self) -> None:
        verdict = evaluate_launch_policy_section(
            _policy_view(
                source=SOURCE_POLICY_DEFAULT,
                effective_mode="auto",
                reproducible_auto=True,
            )
        )
        self.assertEqual(LaunchPolicySectionVerdict(status="ok"), verdict)

    def test_env_override_to_auto_is_ok(self) -> None:
        # An explicit env override to ``auto`` is still reproducible-auto, so it
        # is ok ahead of the override branch (mirrors the legacy ordering).
        verdict = evaluate_launch_policy_section(
            _policy_view(
                source=SOURCE_ENV_OVERRIDE,
                effective_mode="auto",
                reproducible_auto=True,
                env_present=True,
                env_value="auto",
            )
        )
        self.assertEqual("ok", verdict.status)
        self.assertEqual((), verdict.next_action)

    def test_invalid_env_is_warning_with_unset_guidance(self) -> None:
        verdict = evaluate_launch_policy_section(
            _policy_view(
                source=SOURCE_ENV_INVALID,
                effective_mode=None,
                reproducible_auto=False,
                env_present=True,
                env_value="autopilot",
            )
        )
        self.assertEqual("warning", verdict.status)
        self.assertEqual(1, len(verdict.next_action))
        self.assertIn("is not a valid Claude", verdict.next_action[0])
        self.assertIn("'autopilot'", verdict.next_action[0])

    def test_env_override_off_is_warning_with_override_rail(self) -> None:
        verdict = evaluate_launch_policy_section(
            _policy_view(
                source=SOURCE_ENV_OVERRIDE,
                effective_mode="default",
                reproducible_auto=False,
                env_present=True,
                env_value="default",
            )
        )
        self.assertEqual("warning", verdict.status)
        self.assertIn("overrides the cockpit", verdict.next_action[0])
        self.assertIn("--permission-mode default", verdict.next_action[0])

    def test_no_auto_policy_is_warning_with_build_guidance(self) -> None:
        verdict = evaluate_launch_policy_section(
            _policy_view(
                source=SOURCE_NONE,
                effective_mode=None,
                reproducible_auto=False,
                policy_default=None,
            )
        )
        self.assertEqual("warning", verdict.status)
        self.assertIn("this build has no auto launch policy", verdict.next_action[0])


class LaunchPolicySectionUseCaseTest(unittest.TestCase):
    def test_use_case_assembles_legacy_section_dict(self) -> None:
        policy = _policy_view(
            source=SOURCE_POLICY_DEFAULT,
            effective_mode="auto",
            reproducible_auto=True,
        )
        reads = FakeLaunchPolicyReads(policy)

        section = LaunchPolicySectionUseCase(reads).execute()

        self.assertEqual(
            {
                "status": "ok",
                "scope": SECTION_SCOPE,
                "effective_mode": "auto",
                "source": SOURCE_POLICY_DEFAULT,
                "reproducible_auto": True,
                "env_var": CLAUDE_PERMISSION_MODE_ENV,
                "env_present": False,
                "env_value": None,
                "policy_default": "auto",
                "next_action": [],
            },
            section,
        )
        self.assertEqual(1, reads.calls)

    def test_use_case_propagates_warning_next_action(self) -> None:
        policy = _policy_view(
            source=SOURCE_ENV_OVERRIDE,
            effective_mode="default",
            reproducible_auto=False,
            env_present=True,
            env_value="default",
        )
        section = LaunchPolicySectionUseCase(FakeLaunchPolicyReads(policy)).execute()

        self.assertEqual("warning", section["status"])
        self.assertEqual(1, len(section["next_action"]))
        self.assertIsInstance(section["next_action"], list)

    def test_live_adapter_satisfies_the_port_protocol(self) -> None:
        self.assertIsInstance(LiveLaunchPolicyReads(), LaunchPolicyReads)


class DoctorCollectorDelegationTest(unittest.TestCase):
    """``doctor_claude_launch_policy_section`` is now a thin handler over the
    use case; it still routes through the live launch-policy read."""

    def test_collector_matches_use_case_over_live_read(self) -> None:
        section = doctor.doctor_claude_launch_policy_section()
        expected = LaunchPolicySectionUseCase(LiveLaunchPolicyReads()).execute()
        self.assertEqual(expected, section)
        self.assertEqual(SECTION_SCOPE, section["scope"])


if __name__ == "__main__":
    unittest.main()
