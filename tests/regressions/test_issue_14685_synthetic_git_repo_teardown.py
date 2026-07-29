"""Regression pin for the #14685 synthetic Git repo teardown flake.

Redmine #14685 (parent #13490). The #14066 patch-equivalent regression fixture builds real git
repositories under a :class:`tempfile.TemporaryDirectory`. On the GitHub Actions Linux runner its
teardown intermittently died with

    OSError: [Errno 39] Directory not empty: '/tmp/tmpXXXXXXXX/primary/.git'

turning a required CI lane red on a diff that had nothing to do with it (``Test`` run
30422231848 attempt 1 at ``origin/main-next@5d1554f9``; the failed-job rerun of the same head was
green, and 20/20 local macOS repeats passed).

Measured cause, not inferred:

* **Git 2.47.0 made auto maintenance detach by default.** ``prepare_auto_maintenance()`` in
  upstream ``run-command.c`` pushes ``--detach`` when neither ``maintenance.autoDetach`` nor
  ``gc.autoDetach`` is set, so a foreground command that ends in a commit (``commit`` /
  ``cherry-pick`` / ``fetch`` / ...) finishes by spawning
  ``git maintenance run --auto --no-quiet --detach``. Upstream tag boundary read directly from
  ``run-command.c``: **v2.46.0 pushes no ``--detach``; v2.47.0 introduces the ``auto_detach``
  default and pushes it.** The runner that failed ships **git 2.54.0**.
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

On **git 2.43.0** the same 2000 loaded iterations observed no asynchronous write. That is a
measurement about THESE repositories, not a claim about 2.43 in general: a pre-2.47 ``git gc
--auto`` still daemonized itself when it had work, and these synthetic repos are far below the
``gc.auto`` loose-object threshold, so ``need_to_gc()`` is false for them.

The fix is fixture hermeticity, not teardown-error suppression: swallowing the teardown error
would hide a real leak instead of removing the racer. Nothing the #14066 fail-closed contract
asserts depends on git's background maintenance, so its synthetic repos opt out of it. This module
pins that guard on every repository the fixture materialises, DERIVED from the built tree, so a
repo-creating call site added later cannot silently escape it.
"""

from __future__ import annotations

import os
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

_KEY, _, _EXPECTED = _NO_AUTO_MAINTENANCE.partition("=")


def _repository_local_setting(repo: Path, *, ambient_config: Path | None = None) -> str | None:
    """The ``maintenance.auto`` value pinned in ``repo``'s OWN config, or ``None`` when unpinned.

    ``--local`` is load-bearing (review j#94444 F1). A bare ``git config --get`` reads the
    EFFECTIVE value across system / global / command scopes, so an ambient
    ``maintenance.auto=false`` — a developer's ``~/.gitconfig``, a CI image's system config — makes
    the assertion pass on a repository the fixture never pinned at all. That false green would cost
    exactly what this pin exists to buy: detection when a repo-creating call site escapes the
    guard. ``--local`` restricts the read to the repository's own config file. For a linked
    worktree that is the shared common-dir config (verified: these repos do not enable
    ``extensions.worktreeConfig``), which is where the fixture's pin lives.

    ``ambient_config`` points ``GIT_CONFIG_GLOBAL`` at a file, so a test can prove the probe still
    reports "unpinned" while an ambient value is present.
    """
    env = dict(os.environ)
    if ambient_config is not None:
        env["GIT_CONFIG_GLOBAL"] = str(ambient_config)
    probe = subprocess.run(
        ["git", "-C", str(repo), "config", "--local", "--get", _KEY],
        text=True,
        capture_output=True,
        env=env,
    )
    # An unset key exits non-zero; that is the regression this reports, not an error to raise.
    if probe.returncode != 0:
        return None
    return probe.stdout.strip()


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

        for repo in repos:
            with self.subTest(repo=repo.name):
                self.assertEqual(
                    _repository_local_setting(repo),
                    _EXPECTED,
                    msg=(
                        f"{repo} does not pin {_KEY}={_EXPECTED} in its OWN config, so a "
                        "foreground git command may leave a detached `git maintenance run "
                        "--auto --detach` writing inside it after the fixture's subprocess.run "
                        "returned, racing TemporaryDirectory.cleanup"
                    ),
                )

    def test_the_guard_probe_is_not_satisfied_by_an_ambient_config(self) -> None:
        """The sweep above must still fail on an unpinned repo when ambient config sets the key.

        Guard on the guard (review j#94444 F1): the first version of this pin used a bare
        ``git config --get``, which an ambient ``maintenance.auto=false`` satisfies. That version
        went RED under `guard removed` only because the measuring host happened to have no such
        ambient value — so the RED observation did NOT establish that the probe reads the right
        scope. This test constructs the exact ambient condition and asserts the probe reports the
        repo as unpinned anyway.
        """
        unpinned = self.tmp / "unpinned"
        unpinned.mkdir()
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(unpinned)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        ambient = self.tmp / "ambient.gitconfig"
        ambient.write_text(f"[{_KEY.split('.')[0]}]\n\t{_KEY.split('.')[1]} = {_EXPECTED}\n",
                           encoding="utf-8")

        # The ambient value really is visible to git in this repo, otherwise the case is vacuous.
        effective = subprocess.run(
            ["git", "-C", str(unpinned), "config", "--get", _KEY],
            text=True, capture_output=True,
            env={**os.environ, "GIT_CONFIG_GLOBAL": str(ambient)},
        )
        self.assertEqual(effective.returncode, 0)
        self.assertEqual(effective.stdout.strip(), _EXPECTED)

        # ...and the probe the sweep uses is NOT satisfied by it.
        self.assertIsNone(
            _repository_local_setting(unpinned, ambient_config=ambient),
            msg=(
                "the guard probe accepted an ambient config instead of a repository-local pin, "
                "so the sweep would false-green on a synthetic repo the fixture never pinned"
            ),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
