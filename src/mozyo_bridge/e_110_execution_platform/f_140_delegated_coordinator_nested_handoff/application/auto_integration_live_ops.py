"""Live subprocess adapter for the #13686 auto-integration actuator (Redmine #13686).

The concrete :class:`~...application.auto_integration_actuator.AutoIntegrationGitOperations`
implementation: real ``git`` invocations for the read probes the action-time preflight needs
and for the two weak mutations the port defines. It replaces the deliberately gated
``LiveSublaneGitOperations.merge_to_integration_branch`` ``NotImplementedError``, which was
held closed until this issue's design consultation (j#77124) and owner decision (j#96335)
settled what an auto-integration is allowed to be.

What the adapter can and cannot express is the safety story, so it is enumerated here:

- **Push is always non-force.** ``git push <remote> <sha>:refs/heads/<branch>``. There is no
  ``--force``, no ``--force-with-lease`` (still a force), and no ``+`` refspec — the last is
  additionally refused by construction, because ``+`` is how a force is spelled *inside* a
  refspec and a branch name is not trusted to be free of it.
- **Merge is built from objects**, never inside a checkout: ``merge-tree --write-tree`` writes
  the merged tree and ``commit-tree`` wraps it with the measured target as first parent. No
  worktree is switched, no index is written, no ref moves, and a conflict is reported rather
  than resolved. Review j#96406 finding 1 is why: the previous form merged inside a *path*
  whose identity an earlier probe had established, and a foreign lane's checkout swapped onto
  that path was switched off its own branch and had the merge built on it.
- **Nothing here is destructive.** A ref delete, and now a worktree removal, were shipped and
  removed, because none of them could enforce its own condition in one invocation:

  - the remote delete (R1, retired by review j#96344 finding 1) had no compare-and-swap
    against the remote tip and ran even when every local condition failed; a real CAS on a
    remote ref needs ``--force-with-lease``, a force, prohibited by j#96335;
  - the local delete (retired by review j#96396 finding 1) needed the tip to still be the
    recorded one *and* no worktree to hold the branch. Measured on git 2.50.1:
    ``update-ref -d <ref> <tip>`` compare-and-swaps the tip but deletes a branch a linked
    worktree is standing on and leaves that worktree's ``HEAD`` unresolvable; ``branch -D``
    refuses the held branch atomically but takes no tip constraint (a second argument is
    read as another *branch name*); and ``update-ref --stdin`` refuses ``verify`` +
    ``delete`` on one ref (``multiple updates for ref ... not allowed``). R7's two-invocation
    form was reproduced destroying a commit that landed in the window between them;
  - the worktree removal (retired by review j#96401 finding 1) named its target by *path*.
    ``git worktree remove`` checks that the path is registered and clean — both inside the
    invocation, both worth having — but nothing about *whose* checkout it is; that was a
    separate ``worktree list`` probe. Reproduced: replacing our lane's checkout with a
    foreign lane's clean one at the same path between the probe and the removal removed the
    foreign checkout, and the step recorded ``done``. Measured alternatives, all on git
    2.50.1: ``worktree remove`` has no expected-identity argument; the admin entry name under
    ``$GIT_DIR/worktrees`` is reused after such a swap, so it is not instance identity; and
    while ``worktree lock`` genuinely pins the path→entry binding (a competitor's ``remove``
    is refused, ``prune`` skips it, and even after an ``rm -rf`` a re-``add`` fails with *"is
    a missing but locked worktree"*), **no mutation runs while the lock is held** —
    ``remove`` and ``move`` both demand ``-f -f`` — so the unlock reopens the window, and the
    lock is not even attributable, since anyone may ``worktree unlock`` without the reason.

  Removing a lane's worktree and branch is an operator step in the ``preflight_sublane_retire``
  runbook, where a human decides. Nothing here removes a checkout or touches any ref.

Every probe fails closed: a ``git`` that could not run has proven nothing, so a failed
invocation reads as the unsafe answer (not a workspace, not an ancestor, not reachable,
dirty) rather than as a permissive one. A ref name that could not be handed to ``git`` in the
first place is the same kind of answer and is returned the same way — the reads refuse by
value, and only the mutations refuse by raising, because only they have no return value a
caller could ignore (j#96461 finding 2). The spawn-failure mapping mirrors
:meth:`...application.sublane_integration.LiveSublaneGitOperations._run` — a missing ``cwd``
after a host reboot (#14499) raises ``FileNotFoundError`` rather than exiting non-zero, and
letting that escape a read-only preflight is how six production runs ended in tracebacks.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_actuator import (
    MERGE_COMMIT_ERROR,
    MERGE_CONTENT_CONFLICT,
    MERGE_ERROR,
    MERGE_INVALID_INPUT,
    MERGE_MERGED,
    MERGE_PRIMITIVE_UNSUPPORTED,
    MERGE_PROBE_ERROR,
    MERGE_SANDBOX_ERROR,
    MergeResult,
    PushResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_records import (
    EMPTY_TARGET_HEAD,
    LaneWorktree,
)

#: The default shared remote. Named rather than inlined so the one place a remote is chosen
#: is visible; it is not operator-configurable here because the config schema deliberately
#: has no key that could redirect a push.
DEFAULT_REMOTE = "origin"

#: The identity every actuator-built merge commit carries. A LITERAL, not the host's git
#: configuration: review j#96412 finding 1 requires that the same action produce the same
#: commit id, and an identity that varies by host or by `user.name` cannot do that. It also
#: says plainly in `git log` that no human authored this commit.
ACTUATOR_IDENTITY_NAME = "mozyo-bridge auto-integration"
ACTUATOR_IDENTITY_EMAIL = "auto-integration@mozyo-bridge.invalid"

_HEX_DIGITS = frozenset("0123456789abcdef")


def _is_full_sha(value: str) -> bool:
    return len(value) == 40 and all(character in _HEX_DIGITS for character in value)


class UnsafeRefspecError(ValueError):
    """A ref name could not be turned into a provably non-force refspec.

    Raised by the MUTATIONS, which have nowhere to put a refusal: a branch name carrying
    ``+``, whitespace, or a leading ``-`` would change what the constructed ``git push`` argv
    *means* — ``+`` spells a force inside a refspec, and a leading ``-`` turns the value into
    an option — so for a push the argv is never built at all, and the caller is not offered a
    return value it could ignore.

    The READ probes catch it instead and answer their own fail-closed value (``""``, ``False``):
    a read that cannot answer has refused, and the actuator's preflight performs those reads
    before it reaches the apply that would have classified the same input as ``invalid_input``.
    R18 let this escape out of a read and take the whole run down (j#96461 finding 2).
    """


def _checked_branch(ref: str) -> str:
    """Return ``ref`` as a bare branch name, or raise :class:`UnsafeRefspecError`.

    Accepts either ``<branch>`` or ``refs/heads/<branch>`` and normalizes to the bare name, so
    that one spelling choice — how the caller qualifies the ref — does not decide whether the
    refspec is safe. That is the ONLY normalization performed here; everything else about the
    name is judged as written.
    """
    # No `.strip()`. R18 trimmed first and checked afterwards, so `'ma in'` was refused while
    # `' main '` and `'main\n'` were silently rewritten to `main` — the same character
    # accepted or rejected depending on where in the name it sat (j#96461 finding 2). This
    # function's job is to answer whether the ref AS SPELLED can be handed to git, so a
    # spelling it would have to be repaired to be usable is not one it can vouch for. Trimming
    # a configured value is a separate, deliberate step that happens once, upstream, in
    # `normalized_branch` when the action record is formed.
    candidate = ref or ""
    if candidate.startswith("refs/heads/"):
        candidate = candidate[len("refs/heads/") :]
    if not candidate:
        raise UnsafeRefspecError("target ref is empty")
    if candidate.startswith("-"):
        raise UnsafeRefspecError(
            f"target ref {ref!r} starts with '-' and would be read as an option"
        )
    # NUL and friends never reach git: `subprocess.run` raises `ValueError` before spawning,
    # which is not an `OSError` and so escaped `_run` entirely (j#96453 finding 2). A ref the
    # process boundary cannot carry is invalid input, not an exception.
    if any(character < " " or character == "\x7f" for character in candidate):
        raise UnsafeRefspecError(
            f"target ref {ref!r} contains a control character that cannot be passed to a "
            "process; refusing to construct the command"
        )
    forbidden = set("+ \t\n:^~?*[\\")
    if any(character in forbidden for character in candidate):
        raise UnsafeRefspecError(
            f"target ref {ref!r} contains a character that would change the refspec's "
            "meaning ('+' spells a force); refusing to construct the push"
        )
    return candidate


@dataclass
class LiveAutoIntegrationGitOperations:
    """Subprocess-backed auto-integration Git operations for a concrete repo root.

    ``repo_root`` is the checkout every command runs in. ``remote`` is the shared remote a
    push targets. Both are constructor state rather than per-call arguments so a single
    instance cannot be redirected mid-run.
    """

    repo_root: Path
    remote: str = DEFAULT_REMOTE
    #: The sanitized git directory an object-building call is currently running in, and the
    #: repository object store it writes through. Set only for the duration of
    #: :meth:`_open_sandbox`; ``None`` everywhere else, so a sealed call outside one
    #: raises rather than quietly running against the repository.
    _sandbox: Optional[Path] = field(default=None, init=False, repr=False, compare=False)
    _sandbox_objects: Optional[Path] = field(
        default=None, init=False, repr=False, compare=False
    )

    # -- infrastructure ---------------------------------------------------

    def _run(
        self,
        *args: str,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        seal_env: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """Run ``git`` in :attr:`repo_root` (or ``cwd``), mapping a spawn failure to a failure.

        ``subprocess.run(cwd=...)`` raises ``FileNotFoundError`` — not a non-zero exit — when
        ``cwd`` does not exist, and ``git`` missing from ``PATH`` raises the same ``OSError``
        family. Both are mapped onto a failed result so a read-only probe fails closed
        instead of raising out of a preflight (#14499).

        ``env`` OVERLAYS the inherited environment; ``seal_env`` REPLACES it. The distinction
        is the whole of j#96428 finding 1: R13 built an allowlist environment and then passed
        it here, where it was merged straight back into ``os.environ`` — so "the environment
        is built rather than inherited" was true of the dict and false of the child process.
        Measured: an inherited ``GIT_OBJECT_DIRECTORY`` reached git and changed the outcome.

        The unit test did not catch it because it stubs *this method* and inspects the dict it
        was handed, which is the wrong side of the boundary where the merge happens. A sealed
        environment is only observable from the child, so the regression for it runs real git.
        """
        try:
            return subprocess.run(
                ["git", *args],
                cwd=cwd or self.repo_root,
                text=True,
                capture_output=True,
                env=(env if seal_env else {**os.environ, **env}) if env else None,
            )
        except (OSError, ValueError) as exc:
            # `ValueError` is not hypothetical: an argument containing NUL makes
            # `subprocess.run` raise before git is spawned (j#96453 finding 2). Both mean the
            # same thing here — nothing ran — so both fail closed rather than propagate.
            return subprocess.CompletedProcess(
                args=["git", *args],
                returncode=127,
                stdout="",
                stderr=f"git could not be run in {cwd or self.repo_root}: {type(exc).__name__}",
            )

    # -- read probes ------------------------------------------------------

    def is_git_workspace(self) -> bool:
        result = self._run("rev-parse", "--is-inside-work-tree")
        return result.returncode == 0 and result.stdout.strip() == "true"

    def resolve_head(self, ref: str) -> str:
        """The full commit SHA ``ref`` resolves to, :data:`EMPTY_TARGET_HEAD` if it does not exist.

        Returning the explicit sentinel rather than ``""`` keeps "the target does not exist
        yet" a stated fact that the action record can carry, instead of an omission that
        would compare equal to an unmeasured field.
        """
        result = self._run("rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}")
        if result.returncode != 0:
            return EMPTY_TARGET_HEAD
        candidate = result.stdout.strip()
        return candidate if _is_full_sha(candidate) else EMPTY_TARGET_HEAD

    def is_ancestor(self, *, ancestor: str, descendant: str) -> bool:
        """True iff ``ancestor`` is an ancestor of ``descendant`` (fail-closed).

        This answers both "is the source already integrated" (target contains source) and
        "is a fast-forward possible" (source contains the expected target head), so the two
        questions share one probe and cannot drift apart.
        """
        if ancestor == EMPTY_TARGET_HEAD:
            # An empty target is contained in everything: creating the branch at the source
            # head is a fast-forward from nothing.
            return True
        if not (_is_full_sha(ancestor) and _is_full_sha(descendant)):
            return False
        result = self._run("merge-base", "--is-ancestor", ancestor, descendant)
        return result.returncode == 0

    def worktree_dirty(self, *, worktree_path: str = "") -> bool:
        """True iff the worktree has uncommitted / untracked changes (fail-closed).

        An unreadable status reads dirty: a checkout that cannot be inspected is never
        reported clean, because "clean" is what authorizes removing it.
        """
        cwd = Path(worktree_path) if worktree_path else self.repo_root
        result = self._run("status", "--porcelain", cwd=cwd)
        if result.returncode != 0:
            return True
        return bool(result.stdout.strip())

    def remote_branch_tip(self, branch: str) -> str:
        """The shared remote's CURRENT tip for ``branch``, or ``""`` (fresh, read-only).

        Observed with ``git ls-remote``, which asks the remote, rather than from the cached
        ``refs/remotes/*`` tracking refs — a stale tracking ref can assert a tip the remote
        no longer has, and #14066 fixed exactly that class of false reachability proof for
        the sibling terminal-retire path. No fetch, no ref update, no mutation.

        An unusable ref name is a probe that cannot answer, not a crash. R18 let
        :class:`UnsafeRefspecError` out of this read, and the actuator calls it from
        :meth:`AutoIntegrationUseCase._measure` **before** the apply that turns the same input
        into ``invalid_input`` — so a target ref carrying a NUL, a tab or a ``+`` took down
        the whole run instead of being refused by it (reproduced end to end, j#96461
        finding 2). The distinction the raise was protecting is real but belongs to the
        MUTATIONS: :meth:`push_non_force` still refuses to build an argv it cannot prove
        non-force. A read that returns ``""`` is already fail-closed — an empty tip matches no
        expected head, satisfies no reachability, and authorizes nothing.
        """
        try:
            name = _checked_branch(branch)
        except UnsafeRefspecError:
            return ""
        result = self._run(
            "ls-remote", "--heads", "--end-of-options", self.remote, f"refs/heads/{name}"
        )
        if result.returncode != 0:
            return ""
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == f"refs/heads/{name}" and _is_full_sha(parts[0]):
                return parts[0]
        return ""

    def commit_on_remote(self, commit: str, *, branch: str) -> bool:
        """True iff ``commit`` is reachable from the remote's CURRENT ``branch`` tip (fail-closed).

        Two steps, and both matter: the tip is read fresh from the remote
        (:meth:`remote_branch_tip`), and the ancestry is then computed locally against that
        exact tip. A tip the local clone does not have as an object is not proof of anything,
        so it fails closed rather than assuming the local view is complete.
        """
        if not _is_full_sha(commit):
            return False
        tip = self.remote_branch_tip(branch)
        if not tip:
            return False
        if self._run("cat-file", "-e", f"{tip}^{{commit}}").returncode != 0:
            return False
        return self.is_ancestor(ancestor=commit, descendant=tip)

    def describe_lane_worktree(self, *, path: str) -> LaneWorktree:
        """Measure the lane's own checkout (read-only), failing closed on every unknown.

        Every failure to measure reads as the UNSAFE answer: an unreadable worktree list
        means "not registered", an unreadable status means "not clean", an unreadable HEAD
        means no branch. Registration is compared on RESOLVED paths rather than on the
        strings, so a symlinked or differently-spelled path is still recognised.

        This is a read probe about the source of an integration. It no longer describes a
        checkout anything is performed in — see :meth:`apply_merge`.
        """
        candidate = Path(path)
        listed = self._run("worktree", "list", "--porcelain")
        registered = False
        if listed.returncode == 0:
            try:
                resolved = candidate.resolve()
            except OSError:
                resolved = candidate
            for line in listed.stdout.splitlines():
                if not line.startswith("worktree "):
                    continue
                entry = Path(line[len("worktree ") :].strip())
                try:
                    entry_resolved = entry.resolve()
                except OSError:
                    entry_resolved = entry
                if entry_resolved == resolved:
                    registered = True
                    break
        branch = self._run("rev-parse", "--abbrev-ref", "HEAD", cwd=candidate)
        return LaneWorktree(
            path=str(path),
            registered=registered,
            clean=not self.worktree_dirty(worktree_path=str(path)),
            checked_out_branch=branch.stdout.strip() if branch.returncode == 0 else "",
        )

    # -- mutations --------------------------------------------------------

    #: Config pinned on every object-building invocation, with why each one is here. `-c`
    #: overrides every config file including repo-local (measured), so this is the one lever
    #: that reaches all of them.
    #:
    #: - ``i18n.commitEncoding`` adds an encoding header and changes the commit id (measured,
    #:   j#96417 finding 1);
    #: - ``commit.gpgsign`` does not reach ``commit-tree`` at all (measured) but is pinned so
    #:   a future git cannot make it reach;
    #: - ``merge.directoryRenames`` / ``merge.renames`` / ``diff.renames`` each change the
    #:   MERGED TREE (measured, j#96422 finding 1 — flipping any one of them produced a
    #:   different tree for the same action). The values are git's documented defaults, so
    #:   pinning fixes them without changing what a merge means;
    #: - the two rename limits are pinned by reasoning rather than by a demonstrated
    #:   difference: our scene did not exceed them, but a *bound* that varies per host is a
    #:   varying input, and git falls back to no rename detection once it is exceeded.
    #:
    #: This list is the enforced set, stated exactly. It is not a claim that no other key can
    #: matter — three rounds running I wrote "the inputs are only X" and was wrong (j#96412,
    #: j#96417, j#96422). What IS claimed: these are pinned, the environment is built rather
    #: than inherited, replace refs are off, and the merge runs in a git directory where
    #: the repository's own config and attributes do not exist (:meth:`_open_sandbox`).
    _DETERMINISTIC_CONFIG: Tuple[str, ...] = (
        "-c",
        "i18n.commitEncoding=UTF-8",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "merge.directoryRenames=conflict",
        "-c",
        "merge.renames=true",
        "-c",
        "diff.renames=true",
        "-c",
        "merge.renameLimit=32767",
        "-c",
        "diff.renameLimit=32767",
        # `merge.renormalize` canonicalizes content (text/eol/filters) before merging and
        # `merge.default` picks the driver for paths no attribute names — both change what a
        # merge produces (j#96428 finding 3; `merge.default=union` turned a conflict into a
        # clean merge in our own scene). Pinned to git's built-in behaviour.
        "-c",
        "merge.renormalize=false",
        "-c",
        "merge.default=text",
        # The USER attributes file is config-selected, so `-c` reaches it; the system one is
        # a compiled-in path, disabled by `GIT_ATTR_NOSYSTEM` in the sealed environment. The
        # repository's own `.git/info/attributes` is neither reachable nor needed to be: the
        # merge does not run in a directory that has one (:meth:`_open_sandbox`).
        "-c",
        f"core.attributesFile={os.devnull}",
    )
    #: Passed to the merge so ``refs/replace/*`` cannot silently substitute an object for
    #: another. Replace refs live in the repository, so no environment isolation reaches them
    #: (j#96422 finding 1).
    _NO_REPLACE: Tuple[str, ...] = ("--no-replace-objects",)
    #: The environment variables a sealed invocation is allowed to inherit. Every ``GIT_*``
    #: variable is dropped rather than passed through: ``GIT_DIR``, ``GIT_OBJECT_DIRECTORY``,
    #: ``GIT_ALTERNATE_OBJECT_DIRECTORIES``, ``GIT_ATTR_NOSYSTEM`` and friends change what git
    #: reads and are not reachable by ``-c``. R12 overlaid a few variables onto the inherited
    #: environment and called that isolation (j#96422 finding 1); R13 built the allowlist and
    #: then handed it to a ``_run`` that merged it back (j#96428 finding 1). The dict was
    #: right both times; what reached the child was not.
    #:
    #: Windows needs ``SYSTEMROOT`` / ``TEMP`` / ``TMP`` for a process to start at all; they
    #: are inherited when present. Whether that list is sufficient on Windows is untested here
    #: (no Windows host), and is recorded as such rather than claimed.
    _INHERITABLE_ENV = (
        "PATH",
        "HOME",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "SYSTEMROOT",
        "USERPROFILE",
    )

    def _sealed_env(self, **overrides: str) -> dict[str, str]:
        """The COMPLETE environment for an invocation whose result must not vary by host.

        Used with ``seal_env=True``, so this dict is what the child gets — not a set of
        additions to whatever the parent happened to be running with.
        """
        env = {
            name: os.environ[name]
            for name in self._INHERITABLE_ENV
            if name in os.environ
        }
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_NO_REPLACE_OBJECTS"] = "1"
        # System-wide gitattributes are read from a compiled-in path that no config can
        # redirect; this is the documented way to ignore them (j#96428 finding 3).
        env["GIT_ATTR_NOSYSTEM"] = "1"
        env.update(overrides)
        return env

    def _open_sandbox(self) -> "tuple[Optional[Path], Optional[object]]":
        """Build a throwaway git directory that can SEE the repository's objects and nothing else.

        This is the answer to review j#96435 finding 1, and it is a different kind of answer
        from the rounds before it. Those all took the form "check the repository's state, then
        act": pin what can be pinned, probe for what cannot, refuse when the probe finds it.
        The reviewer's reproduction showed why that shape cannot work — a merge driver added
        to ``.git/config`` *between* the probe and the merge ran its shell command and rewrote
        the merged content. A check and a mutation in two invocations are never bound to the
        same instant. That is the identical defect that retired the local branch delete and
        the worktree removal; here, unlike there, an alternative exists.

        So the merge does not run in the repository. It runs in a bare git directory created
        for this call, whose object store IS the repository's (``GIT_OBJECT_DIRECTORY``), so
        every object is readable and anything written lands where the push will find it — but
        whose config, ``info/attributes`` and ``shallow`` are those of an empty repository
        that has existed for microseconds. The hostile state is not refused; it is **not
        visible**, and there is no window in which it can become visible.

        Setup, use and teardown are three separate steps on purpose. R17 wrapped all three in
        one ``@contextmanager`` with a single ``except OSError``, which (a) swallowed a
        cleanup failure and reported the merge as ``merged`` with the sandbox still on disk,
        and (b) caught an ``OSError`` raised *into* the generator from the body and tried to
        yield a second time, producing ``generator didn't stop after throw()`` (j#96453
        finding 1). Three different failures cannot share one handler.

        Returns ``(sandbox, scratch)``, or ``(None, scratch_or_None)`` when the sandbox could
        not be built. The caller must pass whatever it gets back to :meth:`_close_sandbox`.
        """
        environment = self._sealed_env()
        scratch = None
        try:
            object_format = self._run(
                "rev-parse", "--show-object-format", env=environment, seal_env=True
            )
            # `--absolute-git-dir` is the WRONG question in a linked worktree: it answers
            # `$GIT_COMMON_DIR/worktrees/<name>`, which holds no object database. This lane is
            # itself a linked worktree, so R15's merge could not have worked here at all
            # (j#96441 finding 2) — and every scene I had tested was a plain checkout.
            common_dir = self._run(
                "rev-parse", "--path-format=absolute", "--git-common-dir",
                env=environment, seal_env=True,
            )
            if object_format.returncode != 0 or common_dir.returncode != 0:
                return None, None
            objects = Path(common_dir.stdout.strip()) / "objects"
            if not objects.is_dir():
                return None, None
            scratch = tempfile.TemporaryDirectory(prefix="mozyo-merge-")
            root = Path(scratch.name)
            sandbox = root / "sanitized.git"
            # An EMPTY template we own, so `init` cannot be pointed at one that seeds the
            # sandbox with attributes, hooks or config (j#96441 finding 1).
            template = root / "empty-template"
            template.mkdir()
            created = self._run(
                "init",
                "--bare",
                "--quiet",
                f"--template={template}",
                "--object-format",
                object_format.stdout.strip() or "sha1",
                str(sandbox),
                env=environment,
                seal_env=True,
            )
            if created.returncode != 0 or not sandbox.is_dir():
                return None, scratch
        except OSError:
            return None, scratch
        self._sandbox = sandbox
        self._sandbox_objects = objects
        return sandbox, scratch

    def _close_sandbox(self, scratch: "Optional[object]") -> bool:
        """Tear the sandbox down. ``True`` iff it could not be removed.

        A sandbox that will not delete is not a merge that succeeded: the accepted contract
        (j#96449 finding 2) says every failure in this lifecycle lands as
        :data:`~...domain.auto_integration_records.MERGE_SANDBOX_ERROR`, and R17 decided on
        its own to report ``merged`` and leave the directory behind instead. That was a change
        to an accepted contract made without a ruling, so it is reverted here.
        """
        self._sandbox = None
        self._sandbox_objects = None
        if scratch is None:
            return False
        try:
            scratch.cleanup()
        except OSError:
            return True
        return False

    def _sealed(self, *args: str, **overrides: str) -> subprocess.CompletedProcess[str]:
        """Run git with the pinned config, a REPLACED environment, and no repository state.

        Every invocation whose answer feeds the integration commit goes through here — the
        merge, the commit, and the timestamp reads. j#96428 finding 2 is why the last of those
        is on the list: R13 sealed the two object-building calls and left the ``git show`` that
        decides the commit's timestamps running in the ambient environment, so a replace ref
        still changed the resulting commit (measured).

        Requires a sandbox opened by :meth:`_open_sandbox`; without one there is no context to
        run in and the caller has nothing to fall back to.
        """
        if self._sandbox is None or self._sandbox_objects is None:
            raise RuntimeError("a sealed git invocation requires a sanitized git directory")
        return self._run(
            *self._DETERMINISTIC_CONFIG,
            *self._NO_REPLACE,
            *args,
            env=self._sealed_env(
                GIT_DIR=str(self._sandbox),
                GIT_OBJECT_DIRECTORY=str(self._sandbox_objects),
                **overrides,
            ),
            seal_env=True,
        )

    def _merge_tree_capability(self) -> "tuple[str, str]":
        """``supported`` / ``unsupported`` / ``probe_error`` — three answers, not two.

        R10 review j#96412 required that an unknown operational error never be reported as
        "the primitive is unavailable", and R11 collapsed a failed or unparseable
        ``git --version`` into exactly that claim (j#96417 finding 3). Not being able to ask
        the question is a different fact from having asked it and been told no.
        """
        result = self._run("--version")
        if result.returncode != 0:
            return MERGE_PROBE_ERROR, ""
        version = result.stdout.strip()
        for token in version.split():
            parts = token.split(".")
            if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
                continue
            supported = (int(parts[0]), int(parts[1])) >= (2, 38)
            return (MERGE_MERGED if supported else MERGE_PRIMITIVE_UNSUPPORTED), version
        return MERGE_PROBE_ERROR, ""

    def _checked_target_branch(self, target_ref: str) -> str:
        """The bare branch name, validated against BOTH grammars it has to satisfy.

        ``_checked_branch`` answers "can this be spelled in a refspec without changing what
        the argv means" — it rejects ``+`` (which spells a force) and a leading ``-`` (which
        turns the value into an option). That is not the same question as "is this a legal
        branch name", and R12 shipped only the first: ``main..bad``, ``main.lock``,
        ``main@{bad`` and ``main//bad`` all merged and produced commits (measured, j#96422
        finding 3). The LITERAL ``git check-ref-format refs/heads/<name>`` answers the second
        and rejects all four. Neither subsumes the other — ``ma+in`` is a legal ref name that
        must still be refused as a refspec — so both run.

        Raises :class:`UnsafeRefspecError`, which the caller turns into ``invalid_input``.
        """
        branch = _checked_branch(target_ref)
        # The LITERAL form, not `--branch`. `--branch` is not a validator: it also expands
        # `@{-n}` into whatever branch was checked out n switches ago, so it reads repository
        # state and answers differently under a different `GIT_DIR` — measured, `@{-1}` came
        # back rc=0 with `other`, and rc=128 elsewhere (j#96447 finding 1). R16 then discarded
        # the expansion and kept `@{-1}` as the target, which would have built a merge for a
        # ref name no push could ever use. `check-ref-format refs/heads/<name>` is a pure
        # string check: measured, it rejects `@{-1}` along with `main..bad`, `main.lock`,
        # `main@{bad` and `main//bad`, and it works outside any repository with an empty
        # environment — which is how it is run here.
        #
        # `--end-of-options` is not accepted by this command (measured: exit 129); it is safe
        # without one only because `_checked_branch` has already refused a leading `-`, so the
        # order of these two checks is load bearing, not incidental.
        checked = self._run(
            "check-ref-format",
            f"refs/heads/{branch}",
            env=self._sealed_env(),
            seal_env=True,
        )
        if checked.returncode != 0:
            raise UnsafeRefspecError(
                f"target ref {target_ref!r} is not a valid branch name: "
                f"{checked.stderr.strip()[:120]}"
            )
        return branch

    # There is no `_nondeterministic_merge_config` and no `_external_attributes` any more.
    # Both were probes that read the repository, decided it was hazardous, and refused — and
    # review j#96435 finding 1 reproduced a driver added between the probe and the merge doing
    # exactly what the probe existed to prevent. A check in one invocation cannot bind a
    # mutation in another. The merge now runs where those inputs do not exist
    # (:meth:`_open_sandbox`), which also retires the false positives the refusals caused
    # (an unused driver, an attributes file about other paths) and the feature-stop they
    # implied for repositories that legitimately use either.

    def _commit_date(self, commit: str) -> str:
        """The committer date of ``commit``, in a form ``commit-tree`` accepts verbatim.

        Sealed like the merge itself: a replace ref substitutes a different object for this
        one, and R13 read the date through the ambient environment, so a replacement carrying
        another timestamp changed the integration commit (measured, j#96428 finding 2).
        """
        result = self._sealed("show", "-s", "--format=%cI", "--end-of-options", commit)
        return result.stdout.strip() if result.returncode == 0 else ""

    def _merge_timestamp(self, *, source_head: str, target_head: str) -> str:
        """The later of the two parents' committer dates, or ``""`` if either is unreadable.

        Both are properties of objects the action key covers, so the choice stays a function
        of the action. R11 used the source's alone, and review j#96417 finding 5 pointed out
        what that costs: in the ordinary non-fast-forward case the target has moved on since
        the lane branched, so the merge commit would carry a timestamp OLDER than its own
        first parent — and ``git log --since`` filters on commit date, so a freshly integrated
        merge could fall out of a time-bounded search. Taking the later date keeps the
        chronology without reintroducing the clock.
        """
        dates = [self._commit_date(source_head), self._commit_date(target_head)]
        if not all(dates):
            return ""
        # ISO-8601 with an offset does not sort lexically across offsets, so compare the
        # instants git itself reports rather than the strings.
        stamps = [
            self._sealed("show", "-s", "--format=%ct", "--end-of-options", head)
            for head in (source_head, target_head)
        ]
        if any(stamp.returncode != 0 or not stamp.stdout.strip().isdigit() for stamp in stamps):
            return ""
        return dates[0] if int(stamps[0].stdout) >= int(stamps[1].stdout) else dates[1]

    def apply_merge(
        self, *, source_head: str, target_ref: str, expected_target_head: str
    ) -> MergeResult:
        """Create the merge commit **as objects**, touching no worktree, index, ref or HEAD.

        ``git merge-tree --write-tree <target> <source>`` writes the merged tree into the
        object database and prints its id; ``git commit-tree`` then wraps it with the two
        parents. The target and source are object ids, so there is no name for anything to
        re-point between the decision and the mutation — which is the point of building the
        merge this way (review j#96406 finding 1 reproduced a foreign lane's checkout being
        switched off its own branch and having the merge built on it, back when the merge ran
        inside a path an earlier probe had vouched for).

        **What determinism means here, exactly.** The same action rebuilds the same commit
        **under the same git version, given the same repository content**. Getting there took
        several corrections, each measured producing a different SHA for identical inputs: the
        host's git identity and the clock (j#96412 finding 1), ``i18n.commitEncoding``
        (j#96417 finding 1), the rename settings and replace refs (j#96422 finding 1), and the
        inherited environment itself (j#96428 finding 1). Identity and timestamps are supplied
        explicitly, the measured config is pinned per-invocation, the environment is replaced
        rather than overlaid — and repository-local state that no option can pin is not
        refused but made invisible, because the merge runs in a git directory that has none
        (:meth:`_open_sandbox`, j#96435 finding 1). The git binary cannot be pinned at all, so
        the version is recorded rather than claimed away.

        Failure is a typed status, never a boolean and never prose. ``merge-tree`` exits 1 for
        a missing object exactly as it does for a real conflict (measured), so the exit code
        alone cannot classify: a conflict prints the merged tree's id first, an operational
        failure prints none.
        """
        if not _is_full_sha(expected_target_head) or not _is_full_sha(source_head):
            return MergeResult(
                status=MERGE_INVALID_INPUT,
                detail=(
                    "refusing to merge: both the source and the expected target must be full "
                    f"commit SHAs (source={source_head!r} target={expected_target_head!r})"
                ),
            )
        try:
            branch = self._checked_target_branch(target_ref)
        except UnsafeRefspecError as unsafe:
            # R11 declared this vocabulary member and then let the exception escape into the
            # caller (j#96417 finding 3): a status that says it covers unusable ref names, and
            # an operation that instead crashes the actuator on one.
            return MergeResult(status=MERGE_INVALID_INPUT, detail=str(unsafe))
        capability, git_version = self._merge_tree_capability()
        if capability == MERGE_PRIMITIVE_UNSUPPORTED:
            return MergeResult(
                status=MERGE_PRIMITIVE_UNSUPPORTED,
                detail=(
                    "`git merge-tree --write-tree` requires git >= 2.38; this workspace's git "
                    "is older, and merging inside a checkout is not an available fallback "
                    "(#13686 j#96406)"
                ),
            )
        if capability != MERGE_MERGED or not git_version:
            return MergeResult(
                status=MERGE_PROBE_ERROR,
                detail=(
                    "could not establish whether this git can build an object-level merge; "
                    "not knowing is not the same as knowing it cannot"
                ),
            )
        sandbox, scratch = self._open_sandbox()
        if sandbox is None:
            self._close_sandbox(scratch)
            return MergeResult(
                status=MERGE_SANDBOX_ERROR,
                detail=(
                    "could not build the isolated git directory the merge runs in (or locate "
                    "this repository's object store); refusing rather than merging in the "
                    "repository, where state added mid-run would change the result"
                ),
            )
        try:
            result = self._merge_in(
                sandbox,
                source_head=source_head,
                branch=branch,
                expected_target_head=expected_target_head,
                git_version=git_version,
            )
        except (OSError, ValueError) as broken:
            # The BODY failing is an operational failure of this merge, and it lands as one.
            # R18 split the lifecycle into three steps but converted only two of them: a
            # filesystem failure during the merge left `apply_merge` raising a raw `OSError`
            # at its caller, which is the same escape R17's `generator didn't stop after
            # throw()` was (j#96461 finding 1), wearing a different exception type. The two
            # caught classes are the ones the process boundary itself produces — `_run` maps
            # the same pair for the same reason (a NUL in argv is a `ValueError` raised
            # before any spawn), so nothing that git can do to us leaves this method by
            # exception.
            result = MergeResult(
                status=MERGE_ERROR,
                detail=(
                    "the merge failed inside the isolated git directory: "
                    f"{type(broken).__name__}: {str(broken)[:120]}"
                ),
                git_version=git_version,
            )
        finally:
            leaked = self._close_sandbox(scratch)
        if leaked:
            # PRECEDENCE, stated rather than implied: a leaked sandbox outranks whatever the
            # body reported, success or failure. The merge may well have produced a commit,
            # and the body may instead have failed — either way, what cannot be reported is
            # that this call is done with, because the isolation the whole design rests on is
            # not known to have been torn down. That is the fact an operator has to act on.
            return MergeResult(
                status=MERGE_SANDBOX_ERROR,
                detail=(
                    "the isolated git directory could not be removed after the merge; "
                    "reporting the sandbox failure rather than a success, and leaving the "
                    "directory for an operator to inspect"
                ),
            )
        return result

    def _merge_in(
        self,
        sandbox: Path,
        *,
        source_head: str,
        branch: str,
        expected_target_head: str,
        git_version: str,
    ) -> MergeResult:
        """Build the merge inside an open sanitized git directory."""
        merged = self._sealed(
            "merge-tree",
            "--write-tree",
            "--end-of-options",
            expected_target_head,
            source_head,
        )
        first_line = merged.stdout.strip().splitlines()[0].strip() if merged.stdout.strip() else ""
        tree = first_line if _is_full_sha(first_line) else ""
        if merged.returncode != 0:
            # A conflict names the tree it produced; an operational failure names nothing.
            if merged.returncode == 1 and tree:
                return MergeResult(
                    status=MERGE_CONTENT_CONFLICT,
                    detail=(
                        "the branches conflict in content; auto-resolution and auto-rebase "
                        f"are prohibited: {merged.stdout.strip()[:400]}"
                    ),
                )
            return MergeResult(
                status=MERGE_ERROR,
                detail=(
                    f"the object-level merge failed (exit {merged.returncode}) without "
                    "producing a tree; this is NOT a content conflict and NOT proof that the "
                    f"primitive is unavailable: {merged.stderr.strip()[:300]}"
                ),
            )
        if not tree:
            return MergeResult(
                status=MERGE_ERROR,
                detail="the merge reported success but named no tree; refusing to commit it",
            )
        timestamp = self._merge_timestamp(
            source_head=source_head, target_head=expected_target_head
        )
        if not timestamp:
            return MergeResult(
                status=MERGE_ERROR,
                detail=(
                    "could not read both parents' committer dates, which the merge commit's "
                    "timestamps are derived from; refusing to fall back to the clock"
                ),
            )
        committed = self._sealed(
            "commit-tree",
            tree,
            "-p",
            expected_target_head,
            "-p",
            source_head,
            "-m",
            f"Merge {source_head} into {branch}",
            GIT_AUTHOR_NAME=ACTUATOR_IDENTITY_NAME,
            GIT_AUTHOR_EMAIL=ACTUATOR_IDENTITY_EMAIL,
            GIT_AUTHOR_DATE=timestamp,
            GIT_COMMITTER_NAME=ACTUATOR_IDENTITY_NAME,
            GIT_COMMITTER_EMAIL=ACTUATOR_IDENTITY_EMAIL,
            GIT_COMMITTER_DATE=timestamp,
        )
        integration_head = committed.stdout.strip() if committed.returncode == 0 else ""
        if not _is_full_sha(integration_head):
            return MergeResult(
                status=MERGE_COMMIT_ERROR,
                detail=(
                    "the merged tree could not be committed: "
                    f"{committed.stderr.strip()[:200]}"
                ),
            )
        return MergeResult(
            status=MERGE_MERGED,
            integration_head=integration_head,
            # The version the capability probe READ, carried here rather than asked again:
            # R15 re-probed after the commit and let an empty answer through with a `merged`
            # status (j#96441 finding 4). A success cannot be reported without it.
            git_version=git_version,
            detail=(
                f"merged {source_head} onto {expected_target_head} as objects "
                "(first parent is the measured target; no worktree, index or ref touched; "
                "identity, timestamps and encoding pinned, so the same action on the same "
                f"repository under {git_version} rebuilds this exact commit)"
            ),
        )

    def push_non_force(self, *, source_head: str, target_ref: str) -> PushResult:
        """Push ``source_head`` to ``target_ref`` with a normal, non-force push.

        The refspec is built from a validated bare branch name, so it can never carry the
        leading ``+`` that spells a force. A rejection means the remote moved; the answer is
        to re-form the action against the new head, and this adapter offers no other one.
        """
        branch = _checked_branch(target_ref)
        if not _is_full_sha(source_head):
            return PushResult(
                accepted=False,
                rejected=False,
                detail=f"refusing to push {source_head!r}: not a full commit SHA",
            )
        result = self._run(
            "push",
            "--atomic",
            "--end-of-options",
            self.remote,
            f"{source_head}:refs/heads/{branch}",
        )
        if result.returncode == 0:
            return PushResult(
                accepted=True,
                detail=f"pushed {source_head} to {self.remote}/{branch} (non-force)",
            )
        return PushResult(
            accepted=False,
            rejected=True,
            detail=(
                f"non-force push to {self.remote}/{branch} was rejected; re-form the action "
                f"against the new target head (never force, never rebase): "
                f"{result.stderr.strip()}"
            ),
        )


__all__ = (
    "DEFAULT_REMOTE",
    "UnsafeRefspecError",
    "LiveAutoIntegrationGitOperations",
)
