"""Regression pin for the #14685 synthetic Git repo teardown flake.

Redmine #14685 (parent #13490). The #14066 patch-equivalent regression fixture builds real git
repositories under a :class:`tempfile.TemporaryDirectory`. On the GitHub Actions Linux runner its
teardown intermittently died with

    OSError: [Errno 39] Directory not empty: '/tmp/tmpXXXXXXXX/primary/.git'

turning a required CI lane red on a diff that had nothing to do with it (``Test`` run
30422231848 attempt 1 at ``origin/main-next@5d1554f9``; the failed-job rerun of the same head was
green, and 20/20 local macOS repeats passed).

Measured cause, not inferred:

* the runner ships **git 2.54.0**. Since Git 2.50 a foreground command that ends in a commit
  (``commit`` / ``cherry-pick`` / ``fetch`` / ...) finishes by spawning
  ``git maintenance run --auto --no-quiet --detach``. git 2.43 spawns the same maintenance
  WITHOUT ``--detach``, and therefore waits for it — which is why this only bites on the runner.
* that process daemonizes: observed reparented to pid 1, and observed as ``[git] <defunct>``
  zombies in a failing teardown snapshot. So the fixture's ``subprocess.run(..., check=True)``
  returns while a git process is still writing inside ``<tmp>/primary/.git``.
* it writes through helpers that call ``safe_create_leading_directories()``
  (``update_info_file`` for ``.git/info/refs``, ``odb_mkstemp`` for ``.git/objects/...``), so it
  RE-CREATES a directory the teardown already removed and will never revisit.
* ``shutil.rmtree`` lists a directory once, removes the children it listed, then ``os.rmdir``s the
  directory. An entry appearing in between makes that ``os.rmdir`` fail with ENOTEMPTY — the exact
  CI traceback (``_rmtree_safe_fd`` → ``os.rmdir(name, dir_fd=dirfd)``).

A/B on Linux (Ubuntu 24.04, git 2.54.0, Python 3.12), 8 workers × 400 iterations of the #14066
``PatchEquivalentResolverTests``: replaying the pre-fix command sequence reproduced
``OSError: [Errno 39] Directory not empty: '.git'`` naturally, and left leftover trees containing
re-created ``.git/info`` / ``.git/objects/info/packs`` / ``.git/objects/pack``; with the guard in
place the same 3200 iterations were clean. ``maintenance.auto=false`` is what prevents the spawn —
measured, ``gc.auto=0`` does NOT (the detached process still starts and still writes).

The fix is fixture hermeticity, not teardown-error suppression: swallowing the teardown error
would hide a real leak instead of removing the racer. Nothing the #14066 fail-closed contract
asserts depends on git's background maintenance, so its synthetic repos opt out of it. This module
pins that guard on every repository the fixture materialises, DERIVED from the built tree, so a
repo-creating call site added later cannot silently escape it.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))

from regressions.test_issue_14066_patch_equivalent_terminal_retire import (  # noqa: E402
    _NO_AUTO_MAINTENANCE,
    _Scenario,
)


def _git_repositories_under(tmp: Path) -> list[Path]:
    """Every git repository materialised under ``tmp``, DERIVED from the tree.

    Derived rather than hand-listed: a repo-creating call site added later is picked up
    automatically instead of quietly escaping the maintenance guard. Two shapes are recognised —
    a work tree (a ``.git`` directory, or the ``.git`` FILE a linked worktree carries) and a bare
    repo (a directory that itself holds ``HEAD`` and ``objects``). A linked worktree's admin dir
    (``.git/worktrees/<name>``) has ``HEAD`` but no ``objects``, so it is correctly not counted as
    a separate repository.
    """
    found: list[Path] = []
    for path in sorted(tmp.rglob("*")):
        if path.name == ".git":
            found.append(path.parent)
        elif path.is_dir() and (path / "HEAD").is_file() and (path / "objects").is_dir():
            found.append(path)
    return found


class SyntheticRepoTeardownHermeticityTests(unittest.TestCase):
    """No synthetic repo may keep a detached git writer alive past the fixture's git calls."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_every_synthetic_repo_opts_out_of_detached_auto_maintenance(self) -> None:
        scenario = _Scenario(self.tmp, "issue_13879_hibernated_pin_repair", "13879")
        repos = _git_repositories_under(self.tmp)

        # Non-vacuity: the sweep below asserts nothing if the derivation found nothing. The
        # scenario materialises the primary work tree, its linked worktree, and the bare origin.
        self.assertIn(scenario.primary, repos)
        self.assertIn(scenario.lane_worktree, repos)
        self.assertIn(scenario.origin, repos)

        key, _, expected = _NO_AUTO_MAINTENANCE.partition("=")
        for repo in repos:
            with self.subTest(repo=repo.name):
                # Deliberately not the fixture's checked helper: an UNSET key exits non-zero, and
                # that is the regression this test reports, not an error aborting the sweep.
                probe = subprocess.run(
                    ["git", "-C", str(repo), "config", "--get", key],
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(
                    probe.stdout.strip(),
                    expected,
                    msg=(
                        f"{repo} would let a foreground git command leave a detached "
                        "`git maintenance run --auto --detach` writing inside it after the "
                        "fixture's subprocess.run returned, racing TemporaryDirectory.cleanup"
                    ),
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
