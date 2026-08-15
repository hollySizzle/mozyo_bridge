"""Redmine #15517 — the bootstrap backend steps stay runnable and fail-closed.

Five review rounds on this one document all failed the same way: a property was
asserted in prose, checked by eye, and quietly broken by the next edit. The
properties below are the ones that actually cost something when they regress,
so they are pinned mechanically rather than re-read:

- Stage 0 runs before Stage 1 installs the CLI, so it must invoke neither
  `mozyo-bridge` nor a backend probe — the config that says which backend
  applies cannot be read correctly yet, and probing the wrong one reports a
  failure the host does not have (review j#105829, j#105833 finding_1).
- Stage 5 must not read the backend when the config does not parse. A malformed
  config folds to the tmux default inside the runtime, so the reading prints
  `tmux` and is indistinguishable from a healthy default project (review
  j#105833 finding_2). Ordering alone does not achieve this: a bare `if` lets a
  failed parse fall through into the reading, which is how the fail-open path
  survived a round while a position-only assertion passed (review j#105839).
  So the block is EXECUTED here, against a stubbed CLI, and judged on whether
  the reading actually ran.
- The backend reading must stay derived from the runtime's own answer rather
  than from re-parsing the YAML, whose key order is not the backend contract
  (review j#105825).
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_BOOTSTRAP = ROOT / "vibes" / "docs" / "logics" / "bootstrap.md"


def _stage(name: str) -> str:
    """The body of one `## Stage N — ...` section."""
    text = _BOOTSTRAP.read_text(encoding="utf-8")
    starts = [m for m in re.finditer(r"^## Stage \S+", text, re.M)]
    for index, match in enumerate(starts):
        heading = text[match.start() : text.index("\n", match.start())]
        if heading.startswith(f"## Stage {name}"):
            end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
            return text[match.start() : end]
    raise AssertionError(f"Stage {name} not found in {_BOOTSTRAP}")


def _shell_lines(section: str) -> list[str]:
    """Executable lines inside ```bash fences, comments and blanks dropped."""
    lines: list[str] = []
    for block in re.findall(r"```bash\n(.*?)```", section, re.S):
        for raw in block.splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                lines.append(line)
    return lines


class StageZeroNeedsNothingItDoesNotHaveTest(unittest.TestCase):
    def test_stage_0_does_not_invoke_the_cli_it_has_not_installed(self) -> None:
        commands = _shell_lines(_stage("0"))
        offenders = [line for line in commands if "mozyo-bridge" in line]
        self.assertEqual(
            [],
            offenders,
            msg=(
                "Stage 0 runs before Stage 1 installs the CLI, so these lines "
                "cannot work on the fresh install this stage is written for:\n"
                + "\n".join(offenders)
            ),
        )

    def test_stage_0_does_not_probe_a_backend_it_cannot_identify(self) -> None:
        commands = _shell_lines(_stage("0"))
        offenders = [
            line
            for line in commands
            if re.search(r"\b(tmux|herdr)\b", line) or "MOZYO_HERDR_BINARY" in line
        ]
        self.assertEqual(
            [],
            offenders,
            msg=(
                "Stage 0 cannot yet tell which backend applies, so probing one "
                "reports a failure the host may not have; leave it to Stage 5:\n"
                + "\n".join(offenders)
            ),
        )


def _stage5_backend_block() -> str:
    """The Stage 5 bash block that reads the backend."""
    for block in re.findall(r"```bash\n(.*?)```", _stage("5"), re.S):
        if "check-parse" in block:
            return block
    raise AssertionError("Stage 5 has no backend-reading block")


class StageFiveStopsWhenTheConfigDoesNotParseTest(unittest.TestCase):
    """Run the documented block with a stubbed CLI and watch what it does.

    The previous version of this file asserted only that `check-parse` appeared
    before the reading. That is true of a fail-OPEN block too, so it passed the
    exact defect it was meant to catch. These cases execute the block instead.
    """

    def _run_block(self, *, check_parse_exit: int, with_config: bool):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        project = root / "project"
        (project / ".mozyo-bridge").mkdir(parents=True)
        if with_config:
            (project / ".mozyo-bridge" / "config.yaml").write_text("version: 2\n")

        # A mozyo-bridge that records whether the backend reading was reached.
        bindir = root / "bin"
        bindir.mkdir()
        doctor_marker = root / "doctor-ran"
        stub = bindir / "mozyo-bridge"
        stub.write_text(
            textwrap.dedent(
                f"""            #!/bin/sh
            case "$1 $2" in
              "config check-parse") exit {check_parse_exit} ;;
              "doctor --json")
                : > "{doctor_marker}"
                echo '{{"sections": {{}}}}'
                ;;
            esac
            """
            )
        )
        stub.chmod(0o755)

        script = _stage5_backend_block().replace("/path/to/project", str(project))
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"},
        )
        return result, doctor_marker.exists()

    def test_a_failed_parse_never_reaches_the_backend_reading(self) -> None:
        result, doctor_ran = self._run_block(check_parse_exit=2, with_config=True)

        self.assertFalse(
            doctor_ran, "the backend reading ran despite the config not parsing"
        )
        self.assertNotEqual(0, result.returncode, "a failed parse must fail the block")
        self.assertNotIn("tmux", result.stdout)
        self.assertNotIn("herdr", result.stdout)

    def test_a_clean_parse_reaches_the_backend_reading(self) -> None:
        result, doctor_ran = self._run_block(check_parse_exit=0, with_config=True)

        self.assertTrue(doctor_ran, "the backend reading did not run")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("tmux", result.stdout.strip())

    def test_no_config_file_is_the_ordinary_default(self) -> None:
        # Absence must short-circuit as healthy, not as a parse failure.
        result, doctor_ran = self._run_block(check_parse_exit=2, with_config=False)

        self.assertTrue(doctor_ran, "a project with no config must still be read")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("tmux", result.stdout.strip())


class StageFiveChecksTheConfigBeforeTrustingItTest(unittest.TestCase):
    def test_check_parse_precedes_the_backend_reading(self) -> None:
        # Secondary to the executed cases above: necessary, never sufficient.
        section = _stage("5")
        parse_at = section.find("config check-parse")
        selector_at = section.find('sections"].get("herdr")')
        self.assertNotEqual(-1, parse_at, "Stage 5 must run config check-parse")
        self.assertNotEqual(-1, selector_at, "Stage 5 must read the resolved backend")
        self.assertLess(
            parse_at,
            selector_at,
            msg=(
                "a malformed config resolves to the tmux default inside the "
                "runtime, so reading the backend first shows `tmux` and hides "
                "the breakage; check-parse has to come first"
            ),
        )

    def test_the_backend_reading_comes_from_the_runtime_not_the_yaml(self) -> None:
        section = _stage("5")
        self.assertIn("doctor --json", section)
        # Absent herdr section == tmux default (#13355), which is the contract
        # this reading depends on.
        self.assertIn('.get("backend", "tmux")', section)
        # The rejected approach: deciding the backend by reading config text.
        self.assertNotIn("grep -A1 '^terminal_transport:'", section)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
