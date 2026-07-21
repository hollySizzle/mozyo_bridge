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

ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = ROOT / "smoke" / "installed_fault_smoke.py"

_spec = importlib.util.spec_from_file_location("installed_fault_smoke", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(mod)


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
            **self._facts(executable="/home/u/.local/pipx/venvs/mozyo-bridge/bin/mozyo-bridge")
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
        # The F2 / F3 / F4 accepted-finding critical paths are all required installed.
        for key in ("recover_stale", "session_rollback", "callback_exactly_once"):
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


class Sha256Tests(unittest.TestCase):
    def test_matches_hashlib(self):
        import hashlib

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "w.whl"
            p.write_bytes(b"wheelbytes")
            self.assertEqual(mod.sha256_file(p), hashlib.sha256(b"wheelbytes").hexdigest())


if __name__ == "__main__":
    unittest.main()
