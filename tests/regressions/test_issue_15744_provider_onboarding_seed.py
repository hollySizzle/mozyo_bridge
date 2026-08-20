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
screen is gone at the moment the provider boots, and that closing it did not open a new
one. The schema's public contract and the seed use case's own behavior are asserted in
`tests/unit/.../test_agent_provider_onboarding_seed.py` and
`tests/integration/.../test_onboarding_preseed_document.py`; they are deliberately not
restated here.
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

#: The blocker id the #13760 classifier reports for the screen this issue is about. If
#: the provider profile ever stops declaring it, the pin below stops meaning anything,
#: so the tests name it rather than assume it.
FIRST_RUN_THEME_BLOCKER = "first_run_theme"

#: The screens that are NOT in scope: a credential boundary and the two trust
#: confirmations. #13760 ruled that mozyo refuses to answer these and hands the lane back
#: to the operator, and #15744 must not have quietly widened that.
OPERATOR_RESOLVED_BLOCKERS = (
    "login_required",
    "workspace_trust_confirmation",
    "directory_trust_confirmation",
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

    def test_a_failed_seed_still_lets_the_provider_boot(self) -> None:
        # The symptom was a stalled wave. Trading it for a wave that cannot start at all
        # would not be a fix, so a seed that cannot be applied must not block the exec.
        events: list[tuple[str, str]] = []

        def exploding_seed(provider_id: str, env: object) -> object:
            raise RuntimeError("seed path defect")

        status = herdr_agent_attest.seed_provider_onboarding(
            "claude",
            {"HOME": self.home},
            append_event=lambda stage, bounded_reason="": events.append(
                (stage, bounded_reason)
            ),
            preseed=exploding_seed,
        )

        self.assertEqual(status, herdr_agent_attest.SEED_REASON_SEED_RAISED)
        self.assertEqual(len(events), 1)


class ClassifierNotWeakenedTests(unittest.TestCase):
    """Closing the screen by configuration did not relax the screen's detection."""

    def setUp(self) -> None:
        self.profile = require_profile("claude")

    def test_first_run_theme_is_still_a_declared_startup_blocker(self) -> None:
        # If a future edit removed the blocker instead of seeding past it, a provider
        # that DID render the screen (an unwrapped launch, a seed that failed) would be
        # treated as a ready composer and the #13582 lost-request shape would return.
        declared = [blocker.blocker_id for blocker in self.profile.startup_blockers]
        self.assertIn(FIRST_RUN_THEME_BLOCKER, declared)

    def test_the_operator_resolved_screens_remain_declared_and_unseeded(self) -> None:
        # #15744 is allowed to close a cosmetic first-run question and nothing else. The
        # credential and trust screens must still be recognised, and no seeded key may
        # correspond to accepting one of them.
        declared = [blocker.blocker_id for blocker in self.profile.startup_blockers]
        for blocker_id in OPERATOR_RESOLVED_BLOCKERS:
            with self.subTest(blocker_id=blocker_id):
                self.assertIn(blocker_id, declared)

        seeded_keys = {
            key.casefold() for key in self.profile.onboarding_seed.completion_key_map
        }
        for marker in ("trust", "login", "auth", "token", "key", "permission"):
            with self.subTest(marker=marker):
                self.assertFalse(
                    [key for key in seeded_keys if marker in key],
                    f"a seeded key matching {marker!r} would mean the managed launch "
                    f"pre-accepted an operator-resolved boundary",
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
