"""Regression tests for the TestPyPI `source_ref` client preflight (Redmine #13883).

Run `29481593519` passed a LOCAL remote-tracking name (`origin/int_release_...`)
straight through `release publish --testpypi`. The helper dispatched it, and the
workflow's `git ls-remote origin <source_ref>` gate then resolved zero refs and
failed the run before build. The fix moves that proof to action-time on the
client so an unresolvable ref costs ZERO dispatches.

These tests pin both halves of the contract:

  1. Spelling policy (`release-helper-contract.md` -> `source_ref Spelling
     Policy`): local remote-tracking spellings are REJECTED with an exact
     correction, never silently normalized. `origin/<branch>` -> `<branch>`
     normalization is refused *because it is unsafe*: a remote can legitimately
     carry a branch literally named `origin/<branch>`, which
     `test_origin_prefixed_branch_can_really_exist_on_origin` proves against
     real git — normalizing would retarget the artifact authority silently.
  2. Preflight: exactly one non-peel origin ref whose tip == source_sha, else
     no dispatch. Every refusal path asserts the dispatch count is 0, which is
     the acceptance criterion (`dispatch を 0 回`).

The origin here is an isolated bare repo in a temp dir, so real `git ls-remote`
semantics (tail-glob matching, `^{}` peel lines) are exercised without touching
any real remote.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import (  # noqa: E402
    release as release_mod,
    source_ref as source_ref_mod,
)

_EXPECTED_VERSION = "0.12.0a2"


def _git(cwd: Path, *argv: str) -> str:
    result = subprocess.run(
        ["git", *argv],
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout


class SourceRefPreflightTest(unittest.TestCase):
    """Drive `_publish_testpypi` against an isolated origin.

    Only `_gh_dispatch_testpypi` is stubbed — everything up to it (validation +
    `git ls-remote` preflight) runs for real, so these tests fail if the
    preflight stops actually resolving refs.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.srv = root / "srv"
        self.clone = root / "cli"
        _git(root, "init", "-q", "--bare", str(self.srv))
        _git(root, "clone", "-q", str(self.srv), str(self.clone))
        _git(self.clone, "config", "user.email", "probe@example.invalid")
        _git(self.clone, "config", "user.name", "probe")
        (self.clone / "a.txt").write_text("a\n", encoding="utf-8")
        _git(self.clone, "add", "-A")
        _git(self.clone, "commit", "-qm", "init")
        self.head = _git(self.clone, "rev-parse", "HEAD").strip()
        _git(self.clone, "push", "-q", "origin", "HEAD:refs/heads/main")

    # -- helpers ---------------------------------------------------------

    def _args(self, source_ref: str, source_sha: str | None = None) -> argparse.Namespace:
        return argparse.Namespace(
            repo=str(self.clone),
            testpypi=True,
            source_sha=source_sha or self.head,
            expected_version=_EXPECTED_VERSION,
            source_ref=source_ref,
            version=None,
        )

    def _dispatch_stub(self):
        stub = patch.object(release_mod, "_gh_dispatch_testpypi")
        mock = stub.start()
        self.addCleanup(stub.stop)
        mock.return_value = {
            "match": "one",
            "run_id": "1",
            "name": "n",
            "url": "u",
            "created_at": "c",
            "head_sha": self.head,
            "status": "queued",
        }
        return mock

    def _publish(self, source_ref: str, source_sha: str | None = None) -> tuple[int, str]:
        dispatch = self._dispatch_stub()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = release_mod._publish_testpypi(self._args(source_ref, source_sha))
        self.assertEqual(1, dispatch.call_count)
        return rc, out.getvalue()

    def _assert_refused(self, source_ref: str, source_sha: str | None = None) -> str:
        """Assert the ref is refused AND that zero dispatches were issued."""
        dispatch = self._dispatch_stub()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                release_mod._publish_testpypi(self._args(source_ref, source_sha))
        self.assertNotEqual(0, caught.exception.code)
        # The acceptance criterion: zero / multi / mismatch dispatch 0 times.
        self.assertEqual(
            0,
            dispatch.call_count,
            msg=f"source_ref {source_ref!r} must not reach `gh workflow run`",
        )
        return stderr.getvalue()

    # -- accepted spellings ----------------------------------------------

    def test_plain_branch_resolves_and_dispatches(self) -> None:
        rc, out = self._publish("main")
        self.assertEqual(release_mod.EXIT_CLEAN, rc)
        self.assertIn(f"source_ref_resolved: refs/heads/main -> {self.head}", out)

    def test_refs_heads_form_resolves_and_dispatches(self) -> None:
        # The canonical spelling is supported. It is the least ambiguous form,
        # NOT a uniqueness guarantee — see
        # test_full_ref_path_is_not_exactly_one_by_construction.
        rc, out = self._publish("refs/heads/main")
        self.assertEqual(release_mod.EXIT_CLEAN, rc)
        self.assertIn(f"source_ref_resolved: refs/heads/main -> {self.head}", out)

    def test_slash_bearing_branch_is_not_mistaken_for_tracking_name(self) -> None:
        # `feature` is not a configured remote, so `feature/release` is a plain
        # branch name and must pass — the reject rule keys on real remotes, not
        # on the presence of a slash.
        _git(self.clone, "push", "-q", "origin", "HEAD:refs/heads/feature/release")
        rc, out = self._publish("refs/heads/feature/release")
        self.assertEqual(release_mod.EXIT_CLEAN, rc)
        self.assertIn("source_ref_resolved: refs/heads/feature/release", out)

    # -- policy: reject local remote-tracking spellings -------------------

    def test_origin_prefixed_name_is_rejected_with_exact_correction(self) -> None:
        # The #13883 failure mode: `origin/main` is git's LOCAL spelling, which
        # the old path dispatched as-is and which died in-run.
        err = self._assert_refused("origin/main")
        # The stated reason must be AMBIGUITY, not non-existence: this repo's own
        # test_origin_prefixed_branch_can_really_exist_on_origin shows the name
        # can resolve (to a different branch). Claiming "resolves zero refs"
        # would contradict it (j#79995 F3).
        self.assertIn("is ambiguous", err)
        self.assertNotIn("resolves zero refs", err)
        # The correction must be exact and pasteable, per Acceptance 1, and must
        # offer BOTH readings so the operator picks rather than the helper.
        self.assertIn("--source-ref refs/heads/main", err)
        self.assertIn("--source-ref main", err)
        self.assertIn("--source-ref refs/heads/origin/main", err)

    def test_refs_remotes_form_is_rejected_with_exact_correction(self) -> None:
        err = self._assert_refused("refs/remotes/origin/main")
        self.assertIn("LOCAL remote-tracking namespace", err)
        self.assertIn("--source-ref refs/heads/main", err)

    def test_origin_prefixed_branch_can_really_exist_on_origin(self) -> None:
        """The evidence that normalization would be UNSAFE (Redmine #13883).

        A branch literally named `origin/main` can exist on the remote. Had the
        helper normalized `origin/main` -> `main`, it would have silently
        retargeted the artifact authority from `refs/heads/origin/main` to a
        different commit. Reject + the explicit `refs/heads/origin/main` escape
        keeps both meanings reachable and unambiguous.
        """
        _git(self.clone, "checkout", "-q", "-b", "origin/main")
        (self.clone / "b.txt").write_text("b\n", encoding="utf-8")
        _git(self.clone, "add", "-A")
        _git(self.clone, "commit", "-qm", "second")
        other = _git(self.clone, "rev-parse", "HEAD").strip()
        _git(self.clone, "push", "-q", "origin", "origin/main:refs/heads/origin/main")
        self.assertNotEqual(self.head, other)

        # The two refs carry DIFFERENT commits, so normalization is a silent
        # authority substitution, not a convenience.
        listing = _git(self.clone, "ls-remote", "origin")
        self.assertIn(f"{other}\trefs/heads/origin/main", listing)
        self.assertIn(f"{self.head}\trefs/heads/main", listing)

        # Ambiguous spelling is still refused even though it resolves on origin.
        self._assert_refused("origin/main", source_sha=other)
        # The unambiguous escape names the real branch and dispatches.
        rc, out = self._publish("refs/heads/origin/main", source_sha=other)
        self.assertEqual(release_mod.EXIT_CLEAN, rc)
        self.assertIn(f"source_ref_resolved: refs/heads/origin/main -> {other}", out)

    # -- preflight: zero / multi / mismatch -> zero dispatches ------------

    def test_zero_resolution_refuses_before_dispatch(self) -> None:
        err = self._assert_refused("no-such-branch")
        self.assertIn("resolved to 0 refs on origin", err)
        self.assertIn("Nothing was dispatched", err)

    def test_multi_resolution_refuses_before_dispatch(self) -> None:
        # `git ls-remote` matches a ref-name TAIL, so a plain `main` also matches
        # `refs/tags/main`: a plain branch name is NOT exactly-one by construction.
        _git(self.clone, "tag", "main")
        _git(self.clone, "push", "-q", "origin", "refs/tags/main")
        err = self._assert_refused("main")
        self.assertIn("origin refs", err)
        self.assertIn("require exactly one", err)
        # The canonical full path disambiguates the very same repo state.
        rc, _ = self._publish("refs/heads/main")
        self.assertEqual(release_mod.EXIT_CLEAN, rc)

    def test_full_ref_path_is_not_exactly_one_by_construction(self) -> None:
        """A canonical full ref path can still collide (Redmine #13883 j#79995 F1).

        `git ls-remote` matches a ref-name TAIL at `/` boundaries, and that
        applies to full paths too: a branch `foo/refs/heads/main` is matched by
        the pattern `refs/heads/main`. This pins the refutation of the earlier
        "canonical, always exactly one" claim, and pins that the refusal tells
        the operator the truth — re-spelling cannot fix it, because the pattern
        IS already the full path.
        """
        _git(self.clone, "checkout", "-q", "-b", "nested")
        (self.clone / "n.txt").write_text("n\n", encoding="utf-8")
        _git(self.clone, "add", "-A")
        _git(self.clone, "commit", "-qm", "nested")
        nested = _git(self.clone, "rev-parse", "HEAD").strip()
        _git(self.clone, "push", "-q", "origin", "nested:refs/heads/foo/refs/heads/main")

        # Real git really does return both for the canonical spelling.
        listing = _git(self.clone, "ls-remote", "origin", "refs/heads/main")
        self.assertIn("refs/heads/foo/refs/heads/main", listing)
        self.assertIn(f"{self.head}\trefs/heads/main", listing)

        err = self._assert_refused("refs/heads/main")
        self.assertIn("resolved to 2 origin refs", err)
        # The colliding refs are named, so the operator can act on them.
        self.assertIn("refs/heads/foo/refs/heads/main", err)
        self.assertIn(nested, err)
        # The input names a matched ref exactly, so no re-spelling can narrow
        # it; the only real recovery is on origin.
        self.assertIn("is itself one of the refs above", err)
        self.assertIn("rename/delete the colliding ref on origin", err)

    def test_short_name_collision_is_told_to_use_the_full_path(self) -> None:
        # The other branch of the recovery: a short name CAN often be
        # disambiguated by spelling the full path, so that advice is given
        # only here — not when the input already names a ref exactly.
        _git(self.clone, "tag", "main")
        _git(self.clone, "push", "-q", "origin", "refs/tags/main")
        err = self._assert_refused("main")
        self.assertIn("resolved to 2 origin refs", err)
        self.assertIn("is a tail pattern here", err)
        self.assertIn("--source-ref refs/heads/main", err)
        self.assertNotIn("is itself one of the refs above", err)

    def test_refs_prefixed_short_name_gets_short_name_recovery(self) -> None:
        """A `refs/`-prefixed BRANCH short name is not a full path (j#80048 R2-F1).

        `refs/foo` is a legal branch short name — origin publishes it as
        `refs/heads/refs/foo`. Classifying by the `refs/` prefix would call it a
        full path and tell the operator to delete refs on origin, hiding the
        full-path correction that actually works. The classification must come
        from the match facts: `refs/foo` names none of the matched refs exactly,
        so a more specific spelling exists.
        """
        self.assertEqual(
            0,
            subprocess.run(
                ["git", "-C", str(self.clone), "check-ref-format", "--branch", "refs/foo"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ).returncode,
            msg="premise: `refs/foo` must be a legal branch short name",
        )
        _git(self.clone, "push", "-q", "origin", "HEAD:refs/heads/refs/foo")
        _git(self.clone, "push", "-q", "origin", "HEAD:refs/tags/refs/foo")

        err = self._assert_refused("refs/foo")
        self.assertIn("resolved to 2 origin refs", err)
        # Correct branch: a more specific spelling exists, so say so.
        self.assertIn("is a tail pattern here", err)
        self.assertIn("--source-ref refs/heads/refs/foo", err)
        # Must NOT claim the input is already the ref's full name, and must not
        # send the operator to mutate origin when re-spelling would work.
        self.assertNotIn("is itself one of the refs above", err)
        self.assertNotIn("rename/delete the colliding ref on origin", err)

        # And the advertised correction must actually resolve.
        rc, out = self._publish("refs/heads/refs/foo")
        self.assertEqual(release_mod.EXIT_CLEAN, rc)
        self.assertIn("source_ref_resolved: refs/heads/refs/foo", out)

    def test_cited_recovery_is_never_a_dead_end(self) -> None:
        """The cited correction must actually resolve (Redmine #13883 j#80090 R3-F1).

        With `refs/heads/a` and `refs/heads/z/refs/heads/a` on origin, the short
        name `a` matches both — and so does `refs/heads/a`, because it is itself
        a tail of the nested ref. Citing the first match would hand the operator
        a correction that reproduces the same refusal. The citation must be the
        longest match, which provably re-resolves to exactly one.
        """
        _git(self.clone, "push", "-q", "origin", "HEAD:refs/heads/a")
        _git(self.clone, "push", "-q", "origin", "HEAD:refs/heads/z/refs/heads/a")

        err = self._assert_refused("a")
        self.assertIn("resolved to 2 origin refs", err)
        self.assertIn("is a tail pattern here", err)
        # The dead-end candidate must NOT be the cited correction.
        self.assertNotIn("--source-ref refs/heads/a resolves uniquely", err)
        self.assertIn("--source-ref refs/heads/z/refs/heads/a resolves uniquely", err)

        # Prove the citation is not a promise on paper: feeding it back must
        # resolve to exactly one and reach dispatch.
        rc, out = self._publish("refs/heads/z/refs/heads/a")
        self.assertEqual(release_mod.EXIT_CLEAN, rc)
        self.assertIn(
            f"source_ref_resolved: refs/heads/z/refs/heads/a -> {self.head}", out
        )

        # And the dead-end candidate, if the operator picks it anyway, gets the
        # correct advice for its situation rather than another citation loop.
        dead_end = self._assert_refused("refs/heads/a")
        self.assertIn("is itself one of the refs above", dead_end)
        self.assertIn("rename/delete the colliding ref on origin", dead_end)

    def test_mismatch_refuses_before_dispatch(self) -> None:
        _git(self.clone, "checkout", "-q", "-b", "other")
        (self.clone / "c.txt").write_text("c\n", encoding="utf-8")
        _git(self.clone, "add", "-A")
        _git(self.clone, "commit", "-qm", "third")
        other = _git(self.clone, "rev-parse", "HEAD").strip()
        _git(self.clone, "push", "-q", "origin", "other:refs/heads/other")
        # `refs/heads/other` resolves fine, but its tip is not source_sha.
        err = self._assert_refused("refs/heads/other", source_sha=self.head)
        self.assertIn(f"not source_sha {self.head}", err)
        self.assertIn(other, err)

    def test_annotated_tag_peel_ambiguity_refuses_before_dispatch(self) -> None:
        # Peel lines (`^{}`) are dropped so the tag counts once — which leaves
        # the TAG OBJECT sha as the tip, never the commit. A tag source_ref is
        # therefore a mismatch here, exactly as it is server-side.
        _git(self.clone, "tag", "-a", "v-annot", "-m", "annotated")
        _git(self.clone, "push", "-q", "origin", "refs/tags/v-annot")
        tag_obj = _git(self.clone, "rev-parse", "v-annot").strip()
        self.assertNotEqual(self.head, tag_obj)
        err = self._assert_refused("v-annot")
        self.assertIn("annotated tag", err)
        # It refused on the tag object vs commit, not by miscounting the peel.
        self.assertNotIn("require exactly one", err)

    def test_lightweight_tag_at_candidate_resolves_and_dispatches(self) -> None:
        # A lightweight tag DOES point at the commit (no peel line), so unlike
        # an annotated tag it satisfies the SHA check and is accepted. Pinned so
        # the annotated-tag refusal above is understood as a peel/tag-object
        # consequence, not a blanket "tags are refused" rule.
        _git(self.clone, "tag", "release-candidate")
        _git(self.clone, "push", "-q", "origin", "refs/tags/release-candidate")
        rc, out = self._publish("release-candidate")
        self.assertEqual(release_mod.EXIT_CLEAN, rc)
        self.assertIn("source_ref_resolved: refs/tags/release-candidate", out)

    # -- shell safety -----------------------------------------------------

    def test_shell_unsafe_and_glob_values_refuse_before_dispatch(self) -> None:
        hostile = (
            "main; rm -rf /",
            "main && echo pwned",
            "main | cat",
            "$(id)",
            "`id`",
            "main$(id)",
            "refs/heads/*",
            "refs/heads/?ain",
            "refs/heads/m[a]in",
            "main with space",
            "main\nsecond",
            "main\ttab",
            "--upload-pack=touch /tmp/x",
        )
        for value in hostile:
            with self.subTest(source_ref=value):
                err = self._assert_refused(value)
                self.assertIn("Nothing was dispatched", err)

    def test_empty_source_ref_refuses_before_dispatch(self) -> None:
        # Empty is caught by the required-argument guard ahead of the charset
        # check, so it carries that message rather than the shell-safety one;
        # the invariant that matters (zero dispatches) is asserted either way.
        err = self._assert_refused("")
        self.assertIn("requires --source-ref", err)

    def test_charset_guard_matches_trusted_workflow_gate(self) -> None:
        # The client guard must not be looser than the server gate it mirrors,
        # or the helper would green-light a value the workflow then refuses.
        workflow = (ROOT / ".github" / "workflows" / "testpypi.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("*[!A-Za-z0-9._/-]* | \"\"", workflow)
        self.assertEqual("^[A-Za-z0-9._/-]+$", source_ref_mod.CHARSET_RE.pattern)


if __name__ == "__main__":
    unittest.main()
