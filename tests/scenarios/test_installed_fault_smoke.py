"""Hermetic unit tests for the installed-fault-smoke PURE decision surface (Redmine #14097).

The real build+venv+subprocess run of ``smoke/installed_fault_smoke.py`` is the CI installed
gate (network + install), not this offline suite. Here we pin its pure logic — the provenance
verdict, the summary verdict, and the artifact digest — with no subprocess, exactly as
``test_disposable_ubuntu_smoke.py`` pins that smoke's pure surface.
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support.private_path_fixtures import linux_home_path

ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = ROOT / "smoke" / "installed_fault_smoke.py"

_spec = importlib.util.spec_from_file_location("installed_fault_smoke", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(mod)

# A global pipx install lives under the user's home, so this fixture path is genuinely
# home-shaped and is composed at runtime: `release check tree` blocks a `/home/<name>/`
# literal in a tracked file, fixtures included.
_PIPX_GLOBAL_BIN = linux_home_path("u") + "/.local/pipx/venvs/mozyo-bridge/bin/mozyo-bridge"


class VerifyProvenanceTests(unittest.TestCase):
    def _facts(self, **over):
        base = dict(
            executable="/venv/bin/mozyo-bridge",
            module_file="/venv/lib/python3.12/site-packages/mozyo_bridge/__init__.py",
            version="mozyo-bridge 0.12.2", venv_dir="/venv", checkout_root="/checkout",
        )
        base.update(over)
        return base

    def test_installed_artifact_has_no_problems(self):
        self.assertEqual(mod.verify_provenance(**self._facts()), [])

    def test_module_from_checkout_is_flagged(self):
        problems = mod.verify_provenance(
            **self._facts(module_file="/checkout/src/mozyo_bridge/__init__.py")
        )
        self.assertTrue(any("checkout" in p for p in problems))

    def test_executable_outside_venv_is_flagged(self):
        problems = mod.verify_provenance(**self._facts(executable="/usr/local/bin/mozyo-bridge"))
        self.assertTrue(any("not inside the venv" in p for p in problems))

    def test_pipx_global_is_flagged(self):
        problems = mod.verify_provenance(
            **self._facts(executable=_PIPX_GLOBAL_BIN)
        )
        self.assertTrue(any("pipx" in p for p in problems))

    def test_non_site_packages_module_is_flagged(self):
        problems = mod.verify_provenance(
            **self._facts(module_file="/venv/lib/mozyo_bridge/__init__.py")
        )
        self.assertTrue(any("site-packages" in p for p in problems))

    def test_empty_version_is_flagged(self):
        self.assertTrue(any("version" in p for p in mod.verify_provenance(**self._facts(version=""))))


class BuildSummaryTests(unittest.TestCase):
    def _summary(self, **over):
        base = dict(
            provenance_problems=[], wheel_name="mozyo_bridge-0.12.2-py3-none-any.whl",
            wheel_sha256="deadbeef",
            entrypoints={s: 0 for s, _ in mod.SHAPE_ENTRYPOINTS},
            representative={k: True for k in mod.REQUIRED_REPRESENTATIVE},
        )
        base.update(over)
        return mod.build_summary(**base)

    def test_full_pass(self):
        summary = self._summary()
        self.assertTrue(summary["ok"])
        self.assertTrue(summary["provenance_ok"])
        self.assertEqual(summary["artifact"]["sha256"], "deadbeef")
        self.assertEqual(summary["representative_missing"], [])

    def test_a_missing_required_critical_path_fails_closed(self):
        # A shape whose installed critical path was never driven must not read ok (review j#84441).
        partial = {"callback_lease": True, "sublane_list": True}
        summary = self._summary(representative=partial)
        self.assertFalse(summary["representative_ok"])
        self.assertFalse(summary["ok"])
        self.assertIn("recover_stale", summary["representative_missing"])
        self.assertIn("session_rollback", summary["representative_missing"])
        self.assertIn("callback_exactly_once", summary["representative_missing"])

    def test_required_paths_cover_f2_f3_f4(self):
        # The F2 / F3 / F4 accepted-finding critical paths are all required installed. F2 carries a
        # negative control too: an injected uncertain redispatch must not read as completed
        # (Redmine #14097 review j#85090 F2).
        for key in ("recover_stale", "recover_stale_negative", "session_rollback",
                    "callback_exactly_once"):
            self.assertIn(key, mod.REQUIRED_REPRESENTATIVE)

    def test_provenance_problem_fails(self):
        summary = self._summary(provenance_problems=["module loaded from the checkout"])
        self.assertFalse(summary["provenance_ok"])
        self.assertFalse(summary["ok"])

    def test_a_nonzero_entrypoint_fails(self):
        entry = {s: 0 for s, _ in mod.SHAPE_ENTRYPOINTS}
        entry["recover_stale"] = 1
        summary = self._summary(entrypoints=entry)
        self.assertFalse(summary["entrypoints_ok"])
        self.assertFalse(summary["ok"])

    def test_a_failed_representative_path_fails(self):
        summary = self._summary(representative={"callback_lease": True, "sublane_list": False})
        self.assertFalse(summary["representative_ok"])
        self.assertFalse(summary["ok"])

    def test_every_shape_has_an_entrypoint(self):
        # The smoke must dispatch every fault shape's installed entrypoint.
        shapes = {s for s, _ in mod.SHAPE_ENTRYPOINTS}
        self.assertEqual(
            shapes,
            {"recover_stale", "session_rollback", "sublane_list", "callback_lease", "retire_migrate"},
        )

    def test_summary_is_secret_free_json(self):
        import json

        text = json.dumps(self._summary())
        for banned in ("token", "password", "secret", "credential"):
            self.assertNotIn(banned, text.lower())


class RecoverStaleAcceptsTests(unittest.TestCase):
    """The SINGLE F2 acceptance predicate — shared by the installed positive/negative drives and the
    hermetic scenario (Redmine #14097 review j#85253). Every conjunct must be load-bearing so the
    negative control (``not recover_stale_accepts(uncertain)``) catches a weakening regression."""

    def _make_outcome(self, *, pass1=None, pass2=None, **over):
        p1 = {"closed_old_worker": True, "status": "stopped", "recovery_status": "in_progress"}
        p1.update(pass1 or {})
        p2 = {"status": "completed", "recovery_status": "recovered",
              "redispatch_status": "confirmed", "fresh_slot_attested": True,
              "post_close_resume": True, "closed_old_worker": True}
        p2.update(pass2 or {})
        base = {"pass1": p1, "pass2": p2, "fresh_locator": "w1:p4", "old_locator": "w1:p2",
                "agents_unchanged": True, "redispatch_attempt_count": 1, "redispatch_ok_count": 1}
        base.update(over)
        return base

    def test_accepts_the_completed_terminal(self):
        self.assertTrue(mod.recover_stale_accepts(self._make_outcome()))

    def test_a_missing_or_malformed_outcome_is_not_accepted(self):
        self.assertFalse(mod.recover_stale_accepts(None))
        self.assertFalse(mod.recover_stale_accepts({}))
        self.assertFalse(mod.recover_stale_accepts("nope"))

    def test_the_injected_uncertain_outcome_is_rejected(self):
        # The exact shape the negative control injects: the redispatch fired but the confirm fence
        # left it uncertain and the terminal stopped short of completed.
        uncertain = self._make_outcome(pass2={"status": "stopped", "redispatch_status": "uncertain"})
        self.assertFalse(mod.recover_stale_accepts(uncertain))

    def test_every_conjunct_is_load_bearing(self):
        # Weakening ANY single acceptance dimension flips the predicate to False — so a positive
        # drive that regresses on it, or a negative control on a laxer copy, cannot read green.
        for label, over in (
            ("pass1 not closed", {"pass1": {"closed_old_worker": False}}),
            ("pass1 not stopped", {"pass1": {"status": "completed"}}),
            ("pass1 not in_progress", {"pass1": {"recovery_status": "recovered"}}),
            ("pass2 not completed", {"pass2": {"status": "stopped"}}),
            ("pass2 not recovered", {"pass2": {"recovery_status": "in_progress"}}),
            ("redispatch not confirmed", {"pass2": {"redispatch_status": "uncertain"}}),
            ("fresh not attested", {"pass2": {"fresh_slot_attested": False}}),
            ("no post_close_resume", {"pass2": {"post_close_resume": False}}),
            ("pass2 closed flag false", {"pass2": {"closed_old_worker": False}}),
            ("additional close (agents changed)", {"agents_unchanged": False}),
            ("fresh == old locator", {"fresh_locator": "w1:p2"}),
            ("no fresh locator", {"fresh_locator": ""}),
            ("two dispatch attempts", {"redispatch_attempt_count": 2}),
            ("dispatch not confirmed", {"redispatch_ok_count": 0}),
            ("confirmed + extra non-ok attempt", {"redispatch_attempt_count": 2,
                                                  "redispatch_ok_count": 1}),
        ):
            with self.subTest(label):
                self.assertFalse(mod.recover_stale_accepts(self._make_outcome(**over)), label)

    def test_injected_fault_drives_summary_ok_false_and_nonzero_exit(self):
        # The end-to-end proof review j#85253 requires: when the injected fault makes the SHARED
        # predicate reject the POSITIVE result, that False propagates through build_summary to
        # ok=false and through main to a non-zero exit — a separate expected-negative green key is
        # not a substitute for this.
        uncertain = self._make_outcome(pass2={"status": "stopped", "redispatch_status": "uncertain"})
        recover_stale_result = mod.recover_stale_accepts(uncertain)  # the positive drive's verdict
        self.assertFalse(recover_stale_result)

        representative = {k: True for k in mod.REQUIRED_REPRESENTATIVE}
        representative["recover_stale"] = recover_stale_result  # the fault reached the positive path
        summary = mod.build_summary(
            provenance_problems=[], wheel_name="w.whl", wheel_sha256="d",
            entrypoints={s: 0 for s, _ in mod.SHAPE_ENTRYPOINTS}, representative=representative,
        )
        self.assertFalse(summary["representative_ok"])
        self.assertFalse(summary["ok"])

        with mock.patch.object(mod, "run_smoke", return_value=summary):
            self.assertEqual(mod.main([]), 1)  # exit non-zero

    def test_a_clean_run_exits_zero(self):
        # Symmetry: an all-green summary maps to exit 0, so the non-zero above is meaningful.
        representative = {k: True for k in mod.REQUIRED_REPRESENTATIVE}
        summary = mod.build_summary(
            provenance_problems=[], wheel_name="w.whl", wheel_sha256="d",
            entrypoints={s: 0 for s, _ in mod.SHAPE_ENTRYPOINTS}, representative=representative,
        )
        self.assertTrue(summary["ok"])
        with mock.patch.object(mod, "run_smoke", return_value=summary):
            self.assertEqual(mod.main([]), 0)


class Sha256Tests(unittest.TestCase):
    def test_matches_hashlib(self):
        import hashlib

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "w.whl"
            p.write_bytes(b"wheelbytes")
            self.assertEqual(mod.sha256_file(p), hashlib.sha256(b"wheelbytes").hexdigest())


if __name__ == "__main__":
    unittest.main()


class CallbackDiagnosticsShapeTests(unittest.TestCase):
    """Redmine #14248 review j#85410 F1 — the failure diagnostics SHAPE is itself pinned.

    The previous diagnostics read a `reason` key the deliver payload never had, so the field was
    permanently `null` and no test noticed. These pin the contract the reviewer asked for: a green
    run emits NOTHING, and a failing run names the dimension AND carries a non-null send reason.
    """

    def _summary(self, representative, diagnostics):
        return mod.build_summary(
            provenance_problems=[], wheel_name="w.whl", wheel_sha256="sha",
            entrypoints={"callback_lease": 0}, representative=representative,
            representative_diagnostics=diagnostics,
        )

    def test_a_green_run_emits_no_diagnostics(self):
        rep = {k: True for k in mod.REQUIRED_REPRESENTATIVE}
        summary = self._summary(rep, {"callback_exactly_once": {"first_send_outcome": "delivered"}})
        self.assertEqual(summary["representative_diagnostics"], {})

    def test_a_failing_path_emits_its_diagnostic(self):
        rep = {k: True for k in mod.REQUIRED_REPRESENTATIVE}
        rep["callback_exactly_once"] = False
        detail = {
            "first_send_outcome": "not_sent",
            "first_send_reason": "target_unavailable",
            "failed_conjuncts": ["first_send_outcome_delivered"],
        }
        summary = self._summary(rep, {"callback_exactly_once": detail})
        emitted = summary["representative_diagnostics"]["callback_exactly_once"]
        self.assertEqual(emitted["first_send_outcome"], "not_sent")
        self.assertIsNotNone(
            emitted["first_send_reason"],
            "a known failure must name its send reason, not report null",
        )
        self.assertTrue(emitted["failed_conjuncts"])

    def test_the_driver_reads_the_key_the_payload_actually_publishes(self):
        """The specific defect: the diagnostic must read `send_reason`, not a nonexistent `reason`.

        Asserted against the REAL `DeliveryOutcome.as_payload` key set, so renaming the payload
        field without updating the driver fails here instead of silently returning null forever.
        """
        import inspect

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_outbox_processor import (  # noqa: E501
            DeliveryOutcome,
        )
        from mozyo_bridge.core.state.callback_outbox import CallbackOutboxKey

        # The driver is a sibling script, loaded the same way this module loads the smoke.
        spec = importlib.util.spec_from_file_location(
            "installed_fault_smoke_driver", ROOT / "smoke" / "installed_fault_smoke_driver.py"
        )
        driver = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(driver)

        key = CallbackOutboxKey(source="redmine", issue="1", journal="2",
                                normalized_gate="g", callback_route="coordinator")
        published = set(DeliveryOutcome(key=key, send_outcome="not_sent",
                                        resulting_state="pending").as_payload())
        source = inspect.getsource(driver._drive_callback_exactly_once)
        self.assertIn('"send_reason"', source)
        self.assertIn("send_reason", published)
        self.assertNotIn("reason", published - {"send_reason", "persist_reason"})
