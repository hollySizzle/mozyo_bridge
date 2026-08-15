"""Redmine #15517 — the bootstrap backend steps stay runnable and fail-closed.

Five review rounds on this one document all failed the same way: a property was
asserted in prose, checked by eye, and quietly broken by the next edit. The
properties below are the ones that actually cost something when they regress,
so they are pinned mechanically rather than re-read:

- Stage 0 runs before Stage 1 installs the CLI, so it must invoke neither
  `mozyo-bridge` nor a backend probe — the config that says which backend
  applies cannot be read correctly yet, and probing the wrong one reports a
  failure the host does not have (review j#105829, j#105833 finding_1).
- Stage 5 must run `config check-parse` BEFORE reading the resolved backend. A
  malformed config folds to the tmux default inside the runtime, so the
  selector prints `tmux` and is indistinguishable from a healthy default
  project; check-parse is the only thing that separates them (review j#105833
  finding_2).
- The backend reading must stay derived from the runtime's own answer rather
  than from re-parsing the YAML, whose key order is not the backend contract
  (review j#105825).
"""

from __future__ import annotations

import re
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


class StageFiveChecksTheConfigBeforeTrustingItTest(unittest.TestCase):
    def test_check_parse_precedes_the_backend_reading(self) -> None:
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
