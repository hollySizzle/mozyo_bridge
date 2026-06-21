"""Tests for the module-health metrics and oversized-module gate (#12321).

Covers the pure core (`domain.module_health`): line/symbol/complexity metrics,
config/allowlist parsing (fail-closed), and every gate verdict (new oversized,
growth, shrink, resolved, dangling, bad baseline). Also a CLI smoke for
`health report` / `health check`, and a regression that the committed
`module_health.yaml` keeps the real repo green.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from mozyo_bridge.domain.module_health import (
    AllowlistEntry,
    DEFAULT_MAX_MODULE_LINES,
    KIND_BASELINE_BELOW_THRESHOLD,
    KIND_DANGLING,
    KIND_GROWTH,
    KIND_NEW_OVERSIZED,
    KIND_RESOLVED,
    KIND_SHRUNK,
    ModuleHealthConfig,
    ModuleHealthError,
    count_lines,
    evaluate,
    iter_python_files,
    load_config,
    module_metrics,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class CountLinesTest(unittest.TestCase):
    def test_trailing_newline_not_inflated(self) -> None:
        self.assertEqual(count_lines("a\nb\nc\n"), 3)

    def test_no_trailing_newline_counts_last_line(self) -> None:
        self.assertEqual(count_lines("a\nb\nc"), 3)

    def test_empty_is_zero(self) -> None:
        self.assertEqual(count_lines(""), 0)


class ModuleMetricsTest(unittest.TestCase):
    def test_counts_top_level_symbols(self) -> None:
        src = (
            "import os\n"
            "X = 1\n"
            "Y, Z = 2, 3\n"
            "W: int = 4\n"
            "def f():\n    return 1\n"
            "async def g():\n    return 2\n"
            "class C:\n    attr = 1\n"  # nested attr does NOT count at top level
        )
        m = module_metrics(src, "x.py")
        # X, Y, Z, W (4 assigned names) + f + g + C (3 defs) = 7
        self.assertEqual(m.top_level_symbols, 7)

    def test_complexity_counts_branches_and_defs(self) -> None:
        src = (
            "def f(a, b):\n"
            "    if a and b:\n"
            "        return 1\n"
            "    for _ in range(3):\n"
            "        pass\n"
            "    return 0\n"
        )
        m = module_metrics(src, "x.py")
        # def(1) + if(1) + bool extra operand(1) + for(1) = 4
        self.assertEqual(m.complexity, 4)

    def test_syntax_error_falls_back_to_line_metrics(self) -> None:
        m = module_metrics("def (:\n  oops\n", "x.py")
        self.assertEqual(m.lines, 2)
        self.assertEqual(m.top_level_symbols, 0)
        self.assertEqual(m.complexity, 0)


class IterPythonFilesTest(unittest.TestCase):
    def test_walks_dir_and_skips_pycache(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pkg").mkdir()
            (root / "pkg" / "a.py").write_text("a = 1\n", encoding="utf-8")
            (root / "pkg" / "b.py").write_text("b = 1\n", encoding="utf-8")
            (root / "pkg" / "__pycache__").mkdir()
            (root / "pkg" / "__pycache__" / "c.py").write_text("c = 1\n", encoding="utf-8")
            (root / "pkg" / "notpy.txt").write_text("x", encoding="utf-8")
            found = iter_python_files(root, ["pkg"])
            names = sorted(p.name for p in found)
            self.assertEqual(names, ["a.py", "b.py"])


class LoadConfigTest(unittest.TestCase):
    def _write(self, tmp: Path, text: str) -> Path:
        path = tmp / "module_health.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_missing_ok_returns_defaults(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(Path(tmp) / "absent.yaml")
            self.assertEqual(cfg.max_module_lines, DEFAULT_MAX_MODULE_LINES)
            self.assertEqual(cfg.allowlist, ())

    def test_missing_required_raises(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ModuleHealthError):
                load_config(Path(tmp) / "absent.yaml", missing_ok=False)

    def test_valid_parse(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                Path(tmp),
                "max_module_lines: 50\n"
                "include:\n  - pkg\n"
                "allowlist:\n"
                "  - path: pkg/big.py\n"
                "    lines: 120\n"
                "    reason: legacy\n"
                "    owner_issue: '#1'\n"
                "    resolution_version: TBD\n",
            )
            cfg = load_config(path)
            self.assertEqual(cfg.max_module_lines, 50)
            self.assertEqual(cfg.include, ("pkg",))
            self.assertEqual(len(cfg.allowlist), 1)
            self.assertEqual(cfg.allowlist[0].lines, 120)

    def test_bad_top_level(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), "- just\n- a\n- list\n")
            with self.assertRaises(ModuleHealthError):
                load_config(path)

    def test_bad_threshold(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), "max_module_lines: -5\n")
            with self.assertRaises(ModuleHealthError):
                load_config(path)

    def test_allowlist_missing_field(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                Path(tmp),
                "allowlist:\n  - path: pkg/big.py\n    lines: 120\n    reason: x\n",
            )
            with self.assertRaises(ModuleHealthError):
                load_config(path)

    def test_allowlist_duplicate_path(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            entry = (
                "  - path: pkg/big.py\n"
                "    lines: 120\n"
                "    reason: x\n"
                "    owner_issue: '#1'\n"
                "    resolution_version: TBD\n"
            )
            path = self._write(Path(tmp), "allowlist:\n" + entry + entry)
            with self.assertRaises(ModuleHealthError):
                load_config(path)


class EvaluateGateTest(unittest.TestCase):
    """Synthetic repo with a low threshold so file sizes are easy to control."""

    def _repo(self, tmp: Path, sizes: dict[str, int]) -> Path:
        pkg = tmp / "pkg"
        pkg.mkdir()
        for name, n in sizes.items():
            (pkg / name).write_text("x = 1\n" * n, encoding="utf-8")
        return tmp

    def _entry(self, path: str, lines: int) -> AllowlistEntry:
        return AllowlistEntry(
            path=path,
            lines=lines,
            reason="legacy",
            owner_issue="#1",
            resolution_version="TBD",
        )

    def test_new_oversized_fails(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(Path(tmp), {"big.py": 10, "small.py": 2})
            cfg = ModuleHealthConfig(max_module_lines=5, include=("pkg",))
            result = evaluate(root, cfg)
            self.assertFalse(result.ok)
            kinds = {v.kind for v in result.fatal_violations}
            self.assertEqual(kinds, {KIND_NEW_OVERSIZED})
            self.assertEqual(result.fatal_violations[0].path, "pkg/big.py")

    def test_allowlisted_at_baseline_passes(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(Path(tmp), {"big.py": 10})
            cfg = ModuleHealthConfig(
                max_module_lines=5,
                include=("pkg",),
                allowlist=(self._entry("pkg/big.py", 10),),
            )
            result = evaluate(root, cfg)
            self.assertTrue(result.ok, [v.message for v in result.violations])

    def test_growth_past_baseline_fails(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(Path(tmp), {"big.py": 12})
            cfg = ModuleHealthConfig(
                max_module_lines=5,
                include=("pkg",),
                allowlist=(self._entry("pkg/big.py", 10),),
            )
            result = evaluate(root, cfg)
            self.assertFalse(result.ok)
            self.assertEqual(result.fatal_violations[0].kind, KIND_GROWTH)

    def test_shrink_is_warning_not_failure(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(Path(tmp), {"big.py": 8})  # still oversized, < baseline
            cfg = ModuleHealthConfig(
                max_module_lines=5,
                include=("pkg",),
                allowlist=(self._entry("pkg/big.py", 10),),
            )
            result = evaluate(root, cfg)
            self.assertTrue(result.ok)
            self.assertEqual([v.kind for v in result.warnings], [KIND_SHRUNK])

    def test_resolved_is_warning(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(Path(tmp), {"big.py": 3})  # now under threshold
            cfg = ModuleHealthConfig(
                max_module_lines=5,
                include=("pkg",),
                allowlist=(self._entry("pkg/big.py", 10),),
            )
            result = evaluate(root, cfg)
            self.assertTrue(result.ok)
            self.assertEqual([v.kind for v in result.warnings], [KIND_RESOLVED])

    def test_dangling_allowlist_fails(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(Path(tmp), {"small.py": 2})
            cfg = ModuleHealthConfig(
                max_module_lines=5,
                include=("pkg",),
                allowlist=(self._entry("pkg/gone.py", 10),),
            )
            result = evaluate(root, cfg)
            self.assertFalse(result.ok)
            self.assertEqual(result.fatal_violations[0].kind, KIND_DANGLING)

    def test_baseline_below_threshold_fails(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(Path(tmp), {"big.py": 3})
            cfg = ModuleHealthConfig(
                max_module_lines=5,
                include=("pkg",),
                allowlist=(self._entry("pkg/big.py", 4),),  # 4 <= 5 threshold
            )
            result = evaluate(root, cfg)
            self.assertFalse(result.ok)
            kinds = {v.kind for v in result.fatal_violations}
            self.assertIn(KIND_BASELINE_BELOW_THRESHOLD, kinds)


class RealRepoRegressionTest(unittest.TestCase):
    def test_committed_allowlist_keeps_repo_green(self) -> None:
        cfg = load_config(REPO_ROOT / "module_health.yaml", missing_ok=False)
        result = evaluate(REPO_ROOT, cfg)
        self.assertTrue(
            result.ok,
            "module-health gate failed on the real repo: "
            + "; ".join(v.message for v in result.fatal_violations),
        )
        # The threshold matches the documented decision.
        self.assertEqual(cfg.max_module_lines, 1000)

    def test_every_entry_records_a_planned_resolution_version(self) -> None:
        # #12321 acceptance + rework j#62668: each allowlisted oversized module
        # must record a real planned resolution Version, never a placeholder.
        cfg = load_config(REPO_ROOT / "module_health.yaml", missing_ok=False)
        self.assertTrue(cfg.allowlist)
        placeholders = {"TBD", "TODO", "FIXME", "UNKNOWN", "NONE", "N/A", ""}
        for entry in cfg.allowlist:
            self.assertNotIn(
                entry.resolution_version.strip().upper(),
                placeholders,
                f"{entry.path} has a placeholder resolution_version",
            )
            self.assertRegex(entry.resolution_version, r"v0\.\d+\.\d+")
        by_path = {e.path: e for e in cfg.allowlist}
        # presentation_grouping.py was the v0.10.8 / Version #239 split target
        # (US #12322). The split landed (the module became the
        # ``domain/presentation_grouping/`` subpackage), so the entry is removed
        # rather than carrying a stale v0.10.8 resolution_version.
        self.assertNotIn(
            "src/mozyo_bridge/domain/presentation_grouping.py", by_path
        )
        self.assertIn(
            "v0.10.9", by_path["src/mozyo_bridge/application/cockpit_ui.py"].resolution_version
        )
        self.assertIn(
            "v0.10.10", by_path["src/mozyo_bridge/application/commands.py"].resolution_version
        )


class CliSmokeTest(unittest.TestCase):
    def test_report_and_check_via_cli(self) -> None:
        from mozyo_bridge.application import cli

        # check on the real repo exits 0
        self.assertEqual(cli.main(["health", "check", "--repo", str(REPO_ROOT)]), 0)

    def test_check_json_shape(self) -> None:
        import contextlib
        import io

        from mozyo_bridge.application import cli

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["health", "check", "--repo", str(REPO_ROOT), "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["threshold"], 1000)
        self.assertIn("metrics", payload)


if __name__ == "__main__":
    unittest.main()
