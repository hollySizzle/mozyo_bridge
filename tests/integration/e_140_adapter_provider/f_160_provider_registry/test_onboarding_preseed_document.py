"""Onboarding pre-seed against a real config document (Redmine #15744).

Integration rather than unit: the wiring under test is the use case
(``agent_provider_onboarding_preseed``) joined to two other real collaborators — the
packaged provider profile registry and the filesystem — and the behavior worth asserting
lives in that join. Hermetic throughout: every document is inside a per-test temp
directory and the environment is passed in explicitly, so nothing reads or writes the
operator's real home.

The central claim is narrow and non-obvious: **a document that already carries the
declared defaults is never opened for writing**, so an operator who has already
onboarded gets a byte-identical file — mtime included — out of every managed launch.
That is asserted directly rather than inferred from a status token, because "we returned
already_complete" and "we did not touch the file" are different statements and only the
second one is the guarantee.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_onboarding_preseed import (  # noqa: E501
    SEED_REASON_DOCUMENT_NOT_MAPPING,
    SEED_REASON_DOCUMENT_UNREADABLE,
    SEED_REASON_HOME_UNRESOLVED,
    SEED_STATUS_ALREADY_COMPLETE,
    SEED_STATUS_FAILED,
    SEED_STATUS_NOT_DECLARED,
    SEED_STATUS_SEEDED,
    preseed_provider_onboarding,
    resolve_document_path,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile import (  # noqa: E501
    require_profile,
)


class OnboardingPreseedDocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._root = tempfile.TemporaryDirectory(prefix="mozyo-seed-test-")
        self.addCleanup(self._root.cleanup)
        self.home = os.path.join(self._root.name, "home")
        os.makedirs(self.home)
        self.declaration = require_profile("claude").onboarding_seed
        self.assertIsNotNone(
            self.declaration,
            "the packaged claude profile must declare an onboarding seed for this "
            "wiring to have anything to exercise",
        )
        self.primary = resolve_document_path(
            self.declaration.creatable_document, {"HOME": self.home}, self.home
        )

    def _read(self, path: str = "") -> dict:
        with open(path or self.primary, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def _text(self, path: str = "") -> str:
        with open(path or self.primary, "r", encoding="utf-8") as handle:
            return handle.read()

    def _bytes(self, path: str = "") -> bytes:
        with open(path or self.primary, "rb") as handle:
            return handle.read()

    # --- creating a fresh document -------------------------------------------------

    def test_seeds_a_fresh_home_with_every_declared_default(self) -> None:
        outcome = preseed_provider_onboarding("claude", {"HOME": self.home})
        self.assertEqual(outcome.status, SEED_STATUS_SEEDED)
        self.assertTrue(outcome.changed)
        self.assertEqual(self._read(), self.declaration.completion_key_map)

    def test_a_created_document_is_owner_only(self) -> None:
        # The provider's global config later accumulates account state, so it is not
        # left at whatever the launching process's umask happened to be.
        preseed_provider_onboarding("claude", {"HOME": self.home})
        self.assertEqual(os.stat(self.primary).st_mode & 0o777, 0o600)

    # --- idempotency and non-destructiveness ---------------------------------------

    def test_a_second_launch_writes_nothing_at_all(self) -> None:
        preseed_provider_onboarding("claude", {"HOME": self.home})
        before = self._bytes()
        before_mtime = os.stat(self.primary).st_mtime_ns

        outcome = preseed_provider_onboarding("claude", {"HOME": self.home})

        self.assertEqual(outcome.status, SEED_STATUS_ALREADY_COMPLETE)
        self.assertFalse(outcome.changed)
        self.assertEqual(self._bytes(), before)
        self.assertEqual(os.stat(self.primary).st_mtime_ns, before_mtime)

    def test_an_already_onboarded_config_is_byte_identical_afterwards(self) -> None:
        # Deliberately hand-formatted and carrying operator choices that differ from the
        # declared defaults: a seed must neither reformat it nor overwrite a value.
        body = (
            "{\n"
            '  "hasCompletedOnboarding": true,\n'
            '  "theme": "light",\n'
            '  "numStartups": 412\n'
            "}\n"
        )
        with open(self.primary, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.chmod(self.primary, 0o644)
        before_mtime = os.stat(self.primary).st_mtime_ns

        outcome = preseed_provider_onboarding("claude", {"HOME": self.home})

        self.assertEqual(outcome.status, SEED_STATUS_ALREADY_COMPLETE)
        self.assertEqual(self._text(), body)
        self.assertEqual(os.stat(self.primary).st_mtime_ns, before_mtime)
        self.assertEqual(os.stat(self.primary).st_mode & 0o777, 0o644)

    def test_a_partial_config_keeps_every_existing_value_and_its_mode(self) -> None:
        with open(self.primary, "w", encoding="utf-8") as handle:
            json.dump({"theme": "light", "numStartups": 3}, handle)
        os.chmod(self.primary, 0o644)

        outcome = preseed_provider_onboarding("claude", {"HOME": self.home})

        self.assertEqual(outcome.status, SEED_STATUS_SEEDED)
        merged = self._read()
        # The operator's own theme survives; only the genuinely missing key is added.
        self.assertEqual(merged["theme"], "light")
        self.assertEqual(merged["numStartups"], 3)
        self.assertIs(merged["hasCompletedOnboarding"], True)
        self.assertNotIn("theme", outcome.seeded_keys)
        # A seed neither widens nor narrows the mode the operator chose.
        self.assertEqual(os.stat(self.primary).st_mode & 0o777, 0o644)

    # --- document resolution --------------------------------------------------------

    def test_a_relocation_variable_moves_the_seeded_document(self) -> None:
        relocated = os.path.join(self._root.name, "elsewhere")
        env = {"HOME": self.home, "CLAUDE_CONFIG_DIR": relocated}

        outcome = preseed_provider_onboarding("claude", env)

        self.assertEqual(outcome.status, SEED_STATUS_SEEDED)
        self.assertTrue(os.path.exists(os.path.join(relocated, ".claude.json")))
        # Seeding the default location too would leave a document nothing reads.
        self.assertFalse(os.path.exists(self.primary))

    def test_an_existing_candidate_wins_over_the_creatable_one(self) -> None:
        # The provider resolves its config to the first candidate that exists; seeding a
        # different file would report success while changing nothing the provider reads.
        alternate = os.path.join(self.home, ".claude", ".config.json")
        os.makedirs(os.path.dirname(alternate))
        with open(alternate, "w", encoding="utf-8") as handle:
            handle.write("{}")

        outcome = preseed_provider_onboarding("claude", {"HOME": self.home})

        self.assertEqual(outcome.status, SEED_STATUS_SEEDED)
        self.assertEqual(outcome.document_id, "config_dir_document")
        self.assertEqual(
            self._read(alternate),
            self.declaration.completion_key_map,
        )
        self.assertFalse(os.path.exists(self.primary))

    # --- fail-closed and byte-invariant paths ---------------------------------------

    def test_a_provider_declaring_no_seed_touches_nothing(self) -> None:
        outcome = preseed_provider_onboarding("codex", {"HOME": self.home})
        self.assertEqual(outcome.status, SEED_STATUS_NOT_DECLARED)
        self.assertEqual(os.listdir(self.home), [])

    def test_an_unregistered_provider_is_a_no_op_not_a_failure(self) -> None:
        outcome = preseed_provider_onboarding("not-a-provider", {"HOME": self.home})
        self.assertEqual(outcome.status, SEED_STATUS_NOT_DECLARED)
        self.assertEqual(os.listdir(self.home), [])

    def test_an_unparseable_document_is_refused_and_left_alone(self) -> None:
        # Fail closed: a document this code cannot parse is one it must not rewrite,
        # because a merge would discard whatever the unparsed bytes meant.
        with open(self.primary, "w", encoding="utf-8") as handle:
            handle.write("{ not json")

        outcome = preseed_provider_onboarding("claude", {"HOME": self.home})

        self.assertEqual(outcome.status, SEED_STATUS_FAILED)
        self.assertEqual(outcome.reason, SEED_REASON_DOCUMENT_UNREADABLE)
        self.assertEqual(self._text(), "{ not json")

    def test_a_non_object_document_is_refused_and_left_alone(self) -> None:
        with open(self.primary, "w", encoding="utf-8") as handle:
            handle.write("[1, 2]")

        outcome = preseed_provider_onboarding("claude", {"HOME": self.home})

        self.assertEqual(outcome.status, SEED_STATUS_FAILED)
        self.assertEqual(outcome.reason, SEED_REASON_DOCUMENT_NOT_MAPPING)
        self.assertEqual(self._text(), "[1, 2]")

    def test_an_unresolvable_home_fails_without_guessing_one(self) -> None:
        # The wrapper runs as the process the provider will become, so a missing HOME
        # means the provider's config location is genuinely unknown. Falling back to the
        # ambient user database would seed a file the provider does not read.
        for env in ({}, {"HOME": ""}, {"HOME": "relative/path"}):
            with self.subTest(env=env):
                outcome = preseed_provider_onboarding("claude", env)
                self.assertEqual(outcome.status, SEED_STATUS_FAILED)
                self.assertEqual(outcome.reason, SEED_REASON_HOME_UNRESOLVED)

    def test_the_seed_never_raises_on_a_read_only_home(self) -> None:
        locked = os.path.join(self._root.name, "locked")
        os.makedirs(locked)
        os.chmod(locked, 0o500)
        self.addCleanup(os.chmod, locked, 0o700)

        outcome = preseed_provider_onboarding("claude", {"HOME": locked})

        # The claim is "never raises, always a declared token" — the caller is a startup
        # wrapper that must exec the provider whatever this returns. Which token appears
        # is left open on purpose: a suite running as root can legitimately write into a
        # mode-0o500 directory, and pinning `failed` would make this fail there for a
        # reason that has nothing to do with the contract.
        self.assertIn(
            outcome.status,
            (
                SEED_STATUS_FAILED,
                SEED_STATUS_SEEDED,
                SEED_STATUS_ALREADY_COMPLETE,
                SEED_STATUS_NOT_DECLARED,
            ),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
