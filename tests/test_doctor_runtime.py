"""Runtime fingerprint tests (`doctor runtime`, Redmine #12612).

Covers the pure classifier / verdict logic (`classify_surface`,
`evaluate_fingerprint`) including the headline silent-drift case (versions match
but a gate-critical feature probe differs), the CLI subcommand dispatch, and an
end-to-end `run_runtime_fingerprint` against this checkout (which, run under
`PYTHONPATH=src`, must classify itself as the source tree)."""

from __future__ import annotations

import argparse
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.application.doctor_runtime import (
    SOURCE_PROBE_MARKERS,
    SOURCE_PROBE_SCAN_EXCLUDE,
    STATUS_DRIFTED,
    STATUS_OK,
    STATUS_WARNING,
    _source_feature_probes,
    classify_surface,
    evaluate_fingerprint,
    run_runtime_fingerprint,
)


def _active(surface="pipx", version="0.9.0", probes=None, package_path="/x/pkg"):
    return {
        "version": version,
        "package_path": package_path,
        "surface": surface,
        "feature_probes": probes
        if probes is not None
        else {"standard_target_admission": True, "no_target_activation": True},
    }


def _source(present=True, version="0.9.0", probes=None, package_path="/repo/src/mozyo_bridge"):
    return {
        "present": present,
        "package_path": package_path,
        "version": version,
        "feature_probes": probes
        if probes is not None
        else {"standard_target_admission": True, "no_target_activation": True},
    }


class ClassifySurfaceTest(unittest.TestCase):
    def test_path_equal_to_source_is_source_tree(self) -> None:
        src = Path("/repo/src/mozyo_bridge")
        self.assertEqual("source_tree", classify_surface("/repo/src/mozyo_bridge", src))

    def test_pipx_path(self) -> None:
        src = Path("/repo/src/mozyo_bridge")
        path = f"{os.sep}home{os.sep}u{os.sep}.local{os.sep}pipx{os.sep}venvs{os.sep}mozyo-bridge{os.sep}lib{os.sep}py{os.sep}site-packages{os.sep}mozyo_bridge"
        self.assertEqual("pipx", classify_surface(path, src))

    def test_site_packages_path(self) -> None:
        src = Path("/repo/src/mozyo_bridge")
        path = f"{os.sep}usr{os.sep}lib{os.sep}python3{os.sep}site-packages{os.sep}mozyo_bridge"
        self.assertEqual("site_packages", classify_surface(path, src))

    def test_other_src_tree_path(self) -> None:
        src = Path("/repo/src/mozyo_bridge")
        path = f"{os.sep}other{os.sep}checkout{os.sep}src{os.sep}mozyo_bridge"
        self.assertEqual("source_tree", classify_surface(path, src))

    def test_unknown_path(self) -> None:
        src = Path("/repo/src/mozyo_bridge")
        self.assertEqual(
            "unknown", classify_surface(f"{os.sep}opt{os.sep}weird{os.sep}mozyo_bridge", src)
        )


class EvaluateFingerprintTest(unittest.TestCase):
    def test_no_source_is_ok(self) -> None:
        verdict = evaluate_fingerprint(_active(), _source(present=False))
        self.assertEqual(STATUS_OK, verdict["status"])
        self.assertTrue(verdict["ok"])
        self.assertEqual("no-source", verdict["relation"])

    def test_active_is_source_is_ok(self) -> None:
        verdict = evaluate_fingerprint(
            _active(surface="source_tree", package_path="/repo/src/mozyo_bridge"),
            _source(package_path="/repo/src/mozyo_bridge"),
        )
        self.assertEqual(STATUS_OK, verdict["status"])
        self.assertTrue(verdict["ok"])
        self.assertEqual("active-is-source", verdict["relation"])

    def test_same_version_probe_drift_is_drifted(self) -> None:
        # The originating #12612 case: identical version, but the active runtime
        # lacks a gate-critical behavior the source ships.
        active = _active(
            version="0.9.0",
            probes={"standard_target_admission": False, "no_target_activation": False},
        )
        verdict = evaluate_fingerprint(active, _source(version="0.9.0"))
        self.assertEqual(STATUS_DRIFTED, verdict["status"])
        self.assertFalse(verdict["ok"])
        self.assertEqual("same-version-probe-drift", verdict["relation"])
        mismatched = {m["probe"] for m in verdict["probe_mismatch"]}
        self.assertEqual(
            {"standard_target_admission", "no_target_activation"}, mismatched
        )
        self.assertIn("0.9.0", verdict["summary"])

    def test_version_differs_probe_drift_is_warning(self) -> None:
        active = _active(
            version="0.8.0",
            probes={"standard_target_admission": False, "no_target_activation": True},
        )
        verdict = evaluate_fingerprint(active, _source(version="0.9.0"))
        self.assertEqual(STATUS_WARNING, verdict["status"])
        self.assertFalse(verdict["ok"])
        self.assertEqual("version-differs-probe-drift", verdict["relation"])
        self.assertEqual(
            ["standard_target_admission"],
            [m["probe"] for m in verdict["probe_mismatch"]],
        )

    def test_equal_probes_path_drift_same_version_is_warning(self) -> None:
        # Paths differ, probes match, versions equal: the pre-#12612 path drift
        # (equal version != equal commits). Still non-ok so a gate flags it.
        verdict = evaluate_fingerprint(
            _active(version="0.9.0"), _source(version="0.9.0")
        )
        self.assertEqual(STATUS_WARNING, verdict["status"])
        self.assertFalse(verdict["ok"])
        self.assertEqual("same-version", verdict["relation"])
        self.assertEqual([], verdict["probe_mismatch"])

    def test_active_superset_probes_is_not_drift(self) -> None:
        # Active has a feature the source lacks (e.g. running newer than source):
        # no source-ships-active-lacks mismatch, so not drifted.
        active = _active(probes={"standard_target_admission": True, "no_target_activation": True})
        source = _source(
            probes={"standard_target_admission": True, "no_target_activation": False}
        )
        verdict = evaluate_fingerprint(active, source)
        self.assertEqual([], verdict["probe_mismatch"])
        self.assertNotEqual(STATUS_DRIFTED, verdict["status"])


class SourceFeatureProbesTest(unittest.TestCase):
    """Redmine #12612 j#65856: the source probe must verify the real feature
    definition, not the diagnostic module's own marker literals."""

    def _pkg(self, base: Path) -> Path:
        pkg = base / "mozyo_bridge"
        pkg.mkdir(parents=True, exist_ok=True)
        return pkg

    def test_returns_none_without_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(_source_feature_probes(Path(tmp) / "mozyo_bridge"))

    def test_diagnostic_module_markers_do_not_self_satisfy(self) -> None:
        # A source tree whose ONLY file is a copy of the diagnostic module
        # (carrying the marker literals) must NOT register the features as
        # present — the markers there are probe definitions, not the real impl.
        with tempfile.TemporaryDirectory() as tmp:
            pkg = self._pkg(Path(tmp))
            lines = ["SOURCE_PROBE_MARKERS = {"]
            for key, marker in SOURCE_PROBE_MARKERS.items():
                lines.append(f"    {key!r}: {marker!r},")
            lines.append("}")
            (pkg / SOURCE_PROBE_SCAN_EXCLUDE).write_text(
                "\n".join(lines), encoding="utf-8"
            )
            probes = _source_feature_probes(pkg)
            self.assertEqual(
                {"standard_target_admission": False, "no_target_activation": False},
                probes,
            )

    def test_real_definitions_are_detected(self) -> None:
        # Definition-anchored markers in real-looking impl files register True.
        with tempfile.TemporaryDirectory() as tmp:
            pkg = self._pkg(Path(tmp))
            (pkg / "handoff.py").write_text(
                "def resolve_standard_target_admission_policy(x):\n    return x\n",
                encoding="utf-8",
            )
            (pkg / "cli_handoff.py").write_text(
                'p.add_argument(\n    "--no-target-activation",\n'
                '    action="store_true",\n)\n',
                encoding="utf-8",
            )
            probes = _source_feature_probes(pkg)
            self.assertEqual(
                {"standard_target_admission": True, "no_target_activation": True},
                probes,
            )

    def test_real_definitions_detected_even_with_diagnostic_module_present(self) -> None:
        # Excluding the diagnostic module must not hide real definitions living
        # in other files.
        with tempfile.TemporaryDirectory() as tmp:
            pkg = self._pkg(Path(tmp))
            (pkg / SOURCE_PROBE_SCAN_EXCLUDE).write_text(
                "SOURCE_PROBE_MARKERS = {}\n", encoding="utf-8"
            )
            (pkg / "handoff.py").write_text(
                "def resolve_standard_target_admission_policy(x):\n    return x\n",
                encoding="utf-8",
            )
            (pkg / "cli_handoff.py").write_text(
                'p.add_argument("--no-target-activation")\n', encoding="utf-8"
            )
            probes = _source_feature_probes(pkg)
            self.assertTrue(probes["standard_target_admission"])
            self.assertTrue(probes["no_target_activation"])

    def test_bare_mention_does_not_satisfy_admission_probe(self) -> None:
        # An import / call / prose mention (no `def`) must not register True.
        with tempfile.TemporaryDirectory() as tmp:
            pkg = self._pkg(Path(tmp))
            (pkg / "user.py").write_text(
                "from x import resolve_standard_target_admission_policy\n"
                "resolve_standard_target_admission_policy(1)\n",
                encoding="utf-8",
            )
            probes = _source_feature_probes(pkg)
            self.assertFalse(probes["standard_target_admission"])


class DoctorRuntimeDispatchTest(unittest.TestCase):
    def test_doctor_runtime_is_a_doctor_subcommand(self) -> None:
        args = build_parser().parse_args(["doctor", "runtime"])
        self.assertEqual("cmd_doctor_runtime", args.func.__name__)

    def test_doctor_runtime_accepts_json_and_repo(self) -> None:
        args = build_parser().parse_args(
            ["doctor", "runtime", "--json", "--repo", "/tmp/x"]
        )
        self.assertTrue(args.json)
        self.assertEqual("/tmp/x", args.repo)

    def test_bare_doctor_still_runs_diagnostics(self) -> None:
        args = build_parser().parse_args(["doctor"])
        self.assertEqual("cmd_doctor", args.func.__name__)


class RunRuntimeFingerprintEndToEndTest(unittest.TestCase):
    def test_checkout_classifies_as_source_tree_and_ok(self) -> None:
        # The test process runs under PYTHONPATH=src, so the active package IS
        # this checkout's source tree: the fingerprint must say so and pass, and
        # the live feature probes must detect the #12597 symbols that exist here.
        args = argparse.Namespace(repo=str(ROOT), home=None, json=False)
        result = run_runtime_fingerprint(args)
        self.assertTrue(result["ok"])
        self.assertEqual("source_tree", result["active"]["surface"])
        self.assertTrue(result["active"]["feature_probes"]["standard_target_admission"])
        self.assertTrue(result["active"]["feature_probes"]["no_target_activation"])
        self.assertTrue(result["source"]["present"])
        self.assertTrue(result["repo"]["is_repo"])


if __name__ == "__main__":
    unittest.main()
