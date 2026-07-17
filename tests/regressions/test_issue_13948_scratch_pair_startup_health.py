"""Redmine #13948 — session-start must not report a dead launch as a success.

The live defect (#13882 j#80951 / j#80968, reproduced twice on a clean repo with the same
installed runtime): `herdr session-start` reported Claude and Codex both ``launched`` and
exited 0, while Claude had already exec'd and left. The immediately-following dry-run saw
``stale_named_slot`` / shell residue for Claude and a live Codex — a partial pair that
``session-retire`` then refused to converge without an owner approval.

These regressions pin the contract from Design Answer j#80989 (+ j#80991 reconciliation):

1. success means *observed*, per role — live at the locator we launched, startup screen
   clear, and locator-matched self-attestation — never "the launcher accepted the start";
2. the cause is *named* on its own axis (trust interaction / provider exit / shell residue
   / attestation timeout / mismatch), never collapsed into one token;
3. a failed launch owes a compensation that ONLY the explicit public rollback rail may
   act on — session-start closes nothing itself.

The classifier tests below drive :func:`classify_startup_health` directly because it is
the whole decision. The precedence between the axes is not incidental: it is what makes
the report name the *actionable* cause rather than the first one noticed.
"""

from __future__ import annotations

import unittest

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.startup_health import (  # noqa: E501
    ATTESTATION_ABSENT,
    ATTESTATION_INVALID,
    ATTESTATION_NOT_PROBED,
    ATTESTATION_OK,
    COMPENSATION_NOT_NEEDED,
    COMPENSATION_ROLLBACK_OWED,
    DISPOSITION_ADOPTED,
    DISPOSITION_FRESH_LAUNCHED,
    DISPOSITION_SURFACED,
    HEALTH_ATTESTATION_MISMATCH,
    HEALTH_ATTESTATION_TIMEOUT,
    HEALTH_ATTESTATION_UNAVAILABLE,
    HEALTH_DETAIL,
    HEALTH_HEALTHY,
    HEALTH_INVENTORY_UNREADABLE,
    HEALTH_LOCATOR_DRIFT,
    HEALTH_OUTCOMES,
    HEALTH_PROVIDER_EXITED,
    HEALTH_RECEIVER_UNREADABLE,
    HEALTH_SHELL_RESIDUE,
    HEALTH_STARTUP_INTERACTION,
    HEALTH_UNPROFILED_PROVIDER,
    SCREEN_BLOCKED,
    SCREEN_CLEAR,
    SCREEN_NOT_PROBED,
    SCREEN_UNPROFILED,
    SCREEN_UNREADABLE,
    SlotHealth,
    StartupHealthError,
    classify_startup_health,
)


def _facts(**over):
    """The all-positive (healthy) fact set; each test negates exactly one thing."""
    base = dict(
        inventory_readable=True,
        row_present=True,
        row_stale=False,
        live_locator="w2G:p3",
        launched_locator="w2G:p3",
        screen=SCREEN_CLEAR,
        attestation=ATTESTATION_OK,
    )
    base.update(over)
    return base


class ClassifyStartupHealthTest(unittest.TestCase):
    def test_all_positive_is_the_only_healthy_path(self):
        self.assertEqual(classify_startup_health(**_facts()), HEALTH_HEALTHY)

    def test_unreadable_inventory_never_decays_to_healthy(self):
        # #13845 discipline: absence of a liveness proof is not proof of liveness. An
        # unreadable inventory outranks every other fact because it can prove none of them.
        self.assertEqual(
            classify_startup_health(**_facts(inventory_readable=False)),
            HEALTH_INVENTORY_UNREADABLE,
        )

    def test_launched_locator_gone_is_provider_exited(self):
        # The live #13882 Claude shape: `agent start` returned a locator, and by the time
        # anyone looked, nothing was there.
        self.assertEqual(
            classify_startup_health(**_facts(row_present=False)),
            HEALTH_PROVIDER_EXITED,
        )

    def test_positive_residue_is_shell_residue_not_provider_exited(self):
        # The other live #13882 shape: the name survives, the agent does not. These are
        # different operator situations (nothing there vs. a dead pane to reclaim) and
        # must not be reported as one.
        self.assertEqual(
            classify_startup_health(**_facts(row_stale=True)),
            HEALTH_SHELL_RESIDUE,
        )

    def test_name_resolving_elsewhere_is_locator_drift(self):
        self.assertEqual(
            classify_startup_health(**_facts(live_locator="w2G:p9")),
            HEALTH_LOCATOR_DRIFT,
        )

    def test_blank_live_locator_is_drift_not_healthy(self):
        # A row with no locator (or an ambiguous duplicate, which the probe collapses to
        # a blank locator) is unusable — never silently accepted as ours.
        self.assertEqual(
            classify_startup_health(**_facts(live_locator="")),
            HEALTH_LOCATOR_DRIFT,
        )

    def test_trust_screen_is_named_startup_interaction(self):
        self.assertEqual(
            classify_startup_health(**_facts(screen=SCREEN_BLOCKED)),
            HEALTH_STARTUP_INTERACTION,
        )

    def test_unreadable_pane_is_not_startup_clear(self):
        self.assertEqual(
            classify_startup_health(**_facts(screen=SCREEN_UNREADABLE)),
            HEALTH_RECEIVER_UNREADABLE,
        )

    def test_unprofiled_provider_is_never_guessed_clear(self):
        self.assertEqual(
            classify_startup_health(**_facts(screen=SCREEN_UNPROFILED)),
            HEALTH_UNPROFILED_PROVIDER,
        )

    def test_unclassified_visible_state_fails_closed(self):
        # Answer j#80989 Q1.6: an unclassified visible state is never admitted. A screen
        # fact the caller never established must not fall through to "clear".
        for screen in (SCREEN_NOT_PROBED, "something-new"):
            with self.subTest(screen=screen):
                self.assertEqual(
                    classify_startup_health(**_facts(screen=screen)),
                    HEALTH_RECEIVER_UNREADABLE,
                )

    def test_absent_record_is_timeout_and_invalid_record_is_mismatch(self):
        # Distinct causes with distinct fixes: "nothing arrived" vs "something arrived and
        # does not bind to this generation".
        self.assertEqual(
            classify_startup_health(**_facts(attestation=ATTESTATION_ABSENT)),
            HEALTH_ATTESTATION_TIMEOUT,
        )
        self.assertEqual(
            classify_startup_health(**_facts(attestation=ATTESTATION_INVALID)),
            HEALTH_ATTESTATION_MISMATCH,
        )

    def test_unwrapped_launch_is_unavailable_not_timeout(self):
        # An unwrapped launch (#13637 fallback: no `mozyo-bridge` on the launch PATH) can
        # never produce a record. Calling that a *timeout* would tell the operator to wait
        # for something that is not coming.
        self.assertEqual(
            classify_startup_health(**_facts(attestation=ATTESTATION_NOT_PROBED)),
            HEALTH_ATTESTATION_UNAVAILABLE,
        )

    def test_unrecognised_attestation_fact_is_never_healthy(self):
        self.assertEqual(
            classify_startup_health(**_facts(attestation="brand-new")),
            HEALTH_ATTESTATION_UNAVAILABLE,
        )

    def test_process_facts_outrank_screen_and_attestation(self):
        # A dead slot is dead regardless of what a screen/attestation read would have
        # said: reporting `attestation_timeout` for a pane with no process names the
        # wrong cause and sends the operator to the wrong fix.
        self.assertEqual(
            classify_startup_health(
                **_facts(
                    row_present=False,
                    screen=SCREEN_BLOCKED,
                    attestation=ATTESTATION_ABSENT,
                )
            ),
            HEALTH_PROVIDER_EXITED,
        )

    def test_screen_outranks_attestation(self):
        # The wrapper writes its record BEFORE exec (#13637), so a trust-screened agent
        # still has a valid record; and a trust-screened agent that somehow lacks one is
        # still, actionably, a trust screen.
        self.assertEqual(
            classify_startup_health(
                **_facts(screen=SCREEN_BLOCKED, attestation=ATTESTATION_ABSENT)
            ),
            HEALTH_STARTUP_INTERACTION,
        )

    def test_every_health_token_has_an_operator_detail(self):
        # A named cause with no sentence is a token the operator cannot act on.
        for token in HEALTH_OUTCOMES:
            with self.subTest(token=token):
                self.assertTrue(HEALTH_DETAIL.get(token, "").strip(), token)


class SlotHealthContractTest(unittest.TestCase):
    def _health(self, **over):
        base = dict(
            provider="claude",
            assigned_name="mzb1_ws_claude_lane",
            disposition=DISPOSITION_FRESH_LAUNCHED,
            health=HEALTH_HEALTHY,
        )
        base.update(over)
        return SlotHealth(**base)

    def test_only_healthy_reads_healthy(self):
        self.assertTrue(self._health().healthy)
        self.assertFalse(
            self._health(health=HEALTH_PROVIDER_EXITED).healthy
        )

    def test_unknown_tokens_fail_closed(self):
        with self.assertRaises(StartupHealthError):
            self._health(health="fine-probably")
        with self.assertRaises(StartupHealthError):
            self._health(disposition="sort-of-launched")
        with self.assertRaises(StartupHealthError):
            self._health(compensation="undone-ish")

    def test_startup_interaction_must_name_its_blocker(self):
        # The blocker id is the ONLY thing about a startup screen that may leave the pane
        # (#13760 j#77947 invariant 3) — and a blocked verdict without one is unactionable.
        with self.assertRaises(StartupHealthError):
            self._health(health=HEALTH_STARTUP_INTERACTION)
        ok = self._health(
            health=HEALTH_STARTUP_INTERACTION, blocker_id="workspace_trust_confirmation"
        )
        self.assertEqual(ok.blocker_id, "workspace_trust_confirmation")

    def test_a_blocker_id_may_not_ride_any_other_verdict(self):
        with self.assertRaises(StartupHealthError):
            self._health(health=HEALTH_HEALTHY, blocker_id="workspace_trust_confirmation")

    def test_only_a_fresh_launch_can_owe_a_compensation(self):
        # Answer j#80989 Q1.2 / j#80991: what this action did not start is never this
        # action's to undo. An adopted or surfaced slot carrying `rollback_owed` would be
        # a standing invitation for the rollback rail to close somebody else's agent.
        for disposition in (DISPOSITION_ADOPTED, DISPOSITION_SURFACED):
            with self.subTest(disposition=disposition):
                with self.assertRaises(StartupHealthError):
                    self._health(
                        disposition=disposition,
                        health=HEALTH_PROVIDER_EXITED,
                        compensation=COMPENSATION_ROLLBACK_OWED,
                    )
        owed = self._health(
            disposition=DISPOSITION_FRESH_LAUNCHED,
            health=HEALTH_PROVIDER_EXITED,
            compensation=COMPENSATION_ROLLBACK_OWED,
        )
        self.assertEqual(owed.compensation, COMPENSATION_ROLLBACK_OWED)

    def test_payload_carries_every_axis_and_no_pane_content(self):
        payload = self._health(
            health=HEALTH_STARTUP_INTERACTION, blocker_id="login_required"
        ).as_payload()
        self.assertEqual(payload["disposition"], DISPOSITION_FRESH_LAUNCHED)
        self.assertEqual(payload["health"], HEALTH_STARTUP_INTERACTION)
        self.assertEqual(payload["blocker_id"], "login_required")
        self.assertEqual(payload["compensation"], COMPENSATION_NOT_NEEDED)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
