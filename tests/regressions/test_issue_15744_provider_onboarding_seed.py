"""Recurrence pin: a managed launch must not leave a worker on `first_run_theme`.

Redmine #15744. Symptom (#15722 j#108276, reproduced on a new envelope in j#108397): the
first managed Claude worker on a fresh provider install stopped at the first-run theme
picker. That screen renders INSTEAD OF a composer, so the #13760 admission classifier
correctly reported `receiver_startup_interaction_required` / `first_run_theme` and
refused to send — and the lane then waited for an operator to answer the picker by hand,
halting the whole wave behind it. Recognition was working; what was missing was that
nothing put the provider's own first-run defaults in place before the provider started.

Fixed by seeding those defaults in the managed-launch wrapper before the provider exec.
Every test below asserts the SYMPTOM cannot come back — that the precondition for the
screen is gone at the moment the provider boots. Recurrence pins only, per the
tests-placement policy (review j#108680 finding_regressionmixescontracts, verdict
j#108694): the general contracts this file once mixed in live with their subjects —

- the schema / completion semantics and the seed use case's behavior:
  `tests/unit/.../f_160_provider_registry/test_agent_provider_onboarding_seed.py`,
  `.../test_agent_provider_onboarding_preseed.py`, and
  `tests/integration/.../test_onboarding_preseed_document.py`;
- the wrapper's failed-seed launch gate (whose r1 boot-on-failure shape this file used
  to pin before verdict j#108694 overruled it):
  `tests/unit/.../f_130_terminal_runtime_provider/test_herdr_agent_attest.py`
  (`OnboardingSeedGateTest`);
- the #13760 startup-blocker declarations staying intact and unseeded:
  `tests/unit/.../f_160_provider_registry/test_agent_provider_profile.py`
  (`OnboardingSeedBlockerBoundaryTest`).
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest

from mozyo_bridge.core.state.startup_execution_events import (
    STAGE_ONBOARDING_SEED_APPLIED,
    STAGE_PROVIDER_EXEC_CALL_REACHED,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E501
    herdr_agent_attest,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_onboarding_preseed import (  # noqa: E501
    SEED_STATUS_SEEDED,
    preseed_provider_onboarding,
    resolve_document_path,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile import (  # noqa: E501
    require_profile,
)

class FirstRunThemeRecurrenceTests(unittest.TestCase):
    """The fresh-worker state that produced #15722 j#108276 cannot re-occur."""

    def setUp(self) -> None:
        self._root = tempfile.TemporaryDirectory(prefix="mozyo-15744-")
        self.addCleanup(self._root.cleanup)
        self.home = os.path.join(self._root.name, "home")
        os.makedirs(self.home)
        self.profile = require_profile("claude")

    def _config_path(self) -> str:
        declaration = self.profile.onboarding_seed
        return resolve_document_path(
            declaration.creatable_document, {"HOME": self.home}, self.home
        )

    def test_a_fresh_worker_home_no_longer_lacks_the_onboarding_flag(self) -> None:
        # The exact precondition of the blocker: a brand-new home, which is what every
        # freshly created lane worktree worker booted into.
        self.assertFalse(os.path.exists(self._config_path()))

        outcome = preseed_provider_onboarding("claude", {"HOME": self.home})

        self.assertEqual(outcome.status, SEED_STATUS_SEEDED)
        with open(self._config_path(), encoding="utf-8") as handle:
            document = json.load(handle)
        # The gate the provider evaluates before rendering its onboarding flow. Asserted
        # as an identity check because the provider tests it strictly: a `1` here would
        # leave the screen showing and reproduce the symptom while looking seeded.
        self.assertIs(document.get("hasCompletedOnboarding"), True)

    def test_the_wrapper_seeds_before_it_execs_the_provider(self) -> None:
        # Ordering IS the fix. A seed applied after the exec would be a seed applied
        # after the screen had already rendered, which is the state #15722 was stuck in.
        events: list[tuple[str, str]] = []
        recorded: list[tuple[str, str]] = []

        def fake_execvp(target: str, argv: list) -> None:
            recorded.append(("exec", target))
            raise SystemExit(0)

        original_execvp = os.execvp
        os.execvp = fake_execvp  # type: ignore[assignment]
        self.addCleanup(setattr, os, "execvp", original_execvp)

        original_appender = herdr_agent_attest._build_event_appender
        herdr_agent_attest._build_event_appender = (  # type: ignore[assignment]
            lambda action_id, participant="": (
                lambda stage, bounded_reason="": events.append((stage, bounded_reason))
            )
        )
        self.addCleanup(
            setattr, herdr_agent_attest, "_build_event_appender", original_appender
        )

        original_environ = os.environ
        os.environ = {"HOME": self.home, "PATH": "/nonexistent"}  # type: ignore[assignment]
        self.addCleanup(setattr, os, "environ", original_environ)

        args = argparse.Namespace(
            assigned_name="worker",
            workspace_id="ws",
            role="claude",
            lane="lane",
            provider_argv=["--", "/nonexistent/claude"],
        )
        with self.assertRaises(SystemExit):
            herdr_agent_attest.cmd_herdr_agent_attest(args)

        stages = [stage for stage, _reason in events]
        self.assertIn(STAGE_ONBOARDING_SEED_APPLIED, stages)
        self.assertIn(STAGE_PROVIDER_EXEC_CALL_REACHED, stages)
        self.assertLess(
            stages.index(STAGE_ONBOARDING_SEED_APPLIED),
            stages.index(STAGE_PROVIDER_EXEC_CALL_REACHED),
        )
        self.assertTrue(recorded, "the wrapper must still reach the provider exec")
        # And the document is on disk by the time the exec happens, not merely announced.
        with open(self._config_path(), encoding="utf-8") as handle:
            self.assertIs(json.load(handle).get("hasCompletedOnboarding"), True)

    def test_a_present_but_false_flag_still_gets_the_seed(self) -> None:
        # The second face of the same symptom (review j#108680
        # finding_completionstateaspresence): a worker whose config CARRIES the flag as
        # `false` renders the theme picker exactly like a fresh home does, and the r1
        # presence check returned `already_complete` over it — the stall with a
        # green-looking seed outcome. The flag must end up exactly `true`.
        with open(self._config_path(), "w", encoding="utf-8") as handle:
            json.dump({"hasCompletedOnboarding": False}, handle)

        outcome = preseed_provider_onboarding("claude", {"HOME": self.home})

        self.assertEqual(outcome.status, SEED_STATUS_SEEDED)
        with open(self._config_path(), encoding="utf-8") as handle:
            self.assertIs(json.load(handle).get("hasCompletedOnboarding"), True)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
