"""Unit tests for the onboarding pre-seed use case against a fake filesystem port.

Redmine #15744, restructured per review j#108680 finding_filesystemportboundary
(verdict j#108694). Subject under test: the DECISION FLOW of
``...application.agent_provider_onboarding_preseed.preseed_provider_onboarding`` —
document selection, completion evaluation, typed reason mapping, the create-race
retry — isolated behind :class:`OnboardingDocumentFilesystem` with a fake port, so
every branch is expressed as port state rather than as a monkeypatched ``os``.

What is deliberately NOT here: the live adapter's own semantics (the race-free
``os.link`` create, the atomic ``os.replace``, real modes and mtimes). Those stay in
``tests/integration/.../test_onboarding_preseed_document.py`` against the real adapter
through temp directories — the port boundary does not move the byte-level guarantees
out of test coverage, it splits who asserts them.
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_onboarding_preseed import (  # noqa: E501
    OnboardingDocumentStat,
    SEED_REASON_BASE_UNUSABLE,
    SEED_REASON_DOCUMENT_NOT_MAPPING,
    SEED_REASON_DOCUMENT_UNREADABLE,
    SEED_REASON_FOREIGN_OWNER,
    SEED_REASON_HOME_UNRESOLVED,
    SEED_REASON_WRITE_FAILED,
    SEED_STATUS_ALREADY_COMPLETE,
    SEED_STATUS_FAILED,
    SEED_STATUS_NOT_DECLARED,
    SEED_STATUS_SEEDED,
    preseed_provider_onboarding,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_onboarding_seed import (  # noqa: E501
    OnboardingSeedDeclaration,
)

HOME = "/home/operator"
PRIMARY = f"{HOME}/.provider.json"
SECONDARY = f"{HOME}/.provider-dir/.config.json"


def _declaration() -> OnboardingSeedDeclaration:
    """The packaged claude shape, restated locally so these tests pin the use case
    against a declaration they own rather than against the shipped profile data."""
    return OnboardingSeedDeclaration.from_record(
        {
            "documents": [
                {
                    "id": "secondary_document",
                    "base_home_relative": ".provider-dir",
                    "filename": ".config.json",
                    "create_when_absent": False,
                },
                {
                    "id": "primary_document",
                    "base_home_relative": "",
                    "filename": ".provider.json",
                    "create_when_absent": True,
                },
            ],
            "completion_keys": {"hasCompletedOnboarding": True, "theme": "dark"},
        },
        provider_id="fake",
    )


def _profile_lookup(provider_id: str):
    return SimpleNamespace(onboarding_seed=_declaration())


class FakeOnboardingDocumentFilesystem:
    """In-memory :class:`OnboardingDocumentFilesystem`: documents are ``{path: text}``.

    Failure injection uses the same signal the real adapter uses — the ``OSError``
    family — so the use case's typed-reason mapping is exercised, not bypassed.
    ``create_races`` maps a path to the text "another writer" lands there the first
    time a create is attempted (which then raises ``FileExistsError``, as ``os.link``
    would).
    """

    def __init__(self) -> None:
        self.documents: dict[str, str] = {}
        self.modes: dict[str, int] = {}
        self.stats: dict[str, OnboardingDocumentStat] = {}
        self.unreadable: set[str] = set()
        self.unusable_bases: set[str] = set()
        self.failing_writes: set[str] = set()
        self.create_races: dict[str, str] = {}
        self.write_calls: list[tuple[str, str]] = []

    # --- port surface -----------------------------------------------------------

    def document_exists(self, path: str) -> bool:
        return path in self.documents

    def read_document_text(self, path: str) -> str:
        if path in self.unreadable:
            raise PermissionError(path)
        if path not in self.documents:
            raise FileNotFoundError(path)
        return self.documents[path]

    def stat_document(self, path: str) -> OnboardingDocumentStat:
        if path not in self.documents:
            raise FileNotFoundError(path)
        return self.stats.get(
            path, OnboardingDocumentStat(owner_is_caller=True, mode=0o600)
        )

    def ensure_base_directory(self, base: str, mode: int) -> None:
        if base in self.unusable_bases:
            raise PermissionError(base)

    def create_new_document(self, path: str, text: str, mode: int) -> None:
        self.write_calls.append(("create", path))
        if path in self.create_races:
            self.documents[path] = self.create_races.pop(path)
            raise FileExistsError(path)
        if path in self.failing_writes:
            raise PermissionError(path)
        if path in self.documents:
            raise FileExistsError(path)
        self.documents[path] = text
        self.modes[path] = mode

    def replace_document(self, path: str, text: str, mode: int) -> None:
        self.write_calls.append(("replace", path))
        if path in self.failing_writes:
            raise PermissionError(path)
        self.documents[path] = text
        self.modes[path] = mode


def _preseed(filesystem, env=None):
    return preseed_provider_onboarding(
        "fake",
        env if env is not None else {"HOME": HOME},
        profile_lookup=_profile_lookup,
        filesystem=filesystem,
    )


class PreseedDecisionFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.filesystem = FakeOnboardingDocumentFilesystem()

    def _document(self, path: str = PRIMARY) -> dict:
        return json.loads(self.filesystem.documents[path])

    # --- byte-invariant and fail-closed shapes ----------------------------------

    def test_a_provider_declaring_no_seed_touches_no_port_at_all(self) -> None:
        outcome = preseed_provider_onboarding(
            "codexish",
            {"HOME": HOME},
            profile_lookup=lambda provider_id: SimpleNamespace(onboarding_seed=None),
            filesystem=self.filesystem,
        )
        self.assertEqual(outcome.status, SEED_STATUS_NOT_DECLARED)
        self.assertEqual(self.filesystem.write_calls, [])

    def test_an_unresolvable_home_fails_before_any_port_call(self) -> None:
        for env in ({}, {"HOME": ""}, {"HOME": "relative/path"}):
            with self.subTest(env=env):
                outcome = _preseed(self.filesystem, env=env)
                self.assertEqual(outcome.status, SEED_STATUS_FAILED)
                self.assertEqual(outcome.reason, SEED_REASON_HOME_UNRESOLVED)
        self.assertEqual(self.filesystem.write_calls, [])

    def test_an_unreadable_document_is_refused_and_never_written(self) -> None:
        self.filesystem.documents[PRIMARY] = '{"hasCompletedOnboarding": false}'
        self.filesystem.unreadable.add(PRIMARY)
        outcome = _preseed(self.filesystem)
        self.assertEqual(outcome.status, SEED_STATUS_FAILED)
        self.assertEqual(outcome.reason, SEED_REASON_DOCUMENT_UNREADABLE)
        self.assertEqual(self.filesystem.write_calls, [])

    def test_unparseable_json_maps_to_document_unreadable(self) -> None:
        self.filesystem.documents[PRIMARY] = "{ not json"
        outcome = _preseed(self.filesystem)
        self.assertEqual(outcome.status, SEED_STATUS_FAILED)
        self.assertEqual(outcome.reason, SEED_REASON_DOCUMENT_UNREADABLE)
        self.assertEqual(self.filesystem.documents[PRIMARY], "{ not json")

    def test_a_non_object_document_maps_to_not_mapping(self) -> None:
        self.filesystem.documents[PRIMARY] = "[1, 2]"
        outcome = _preseed(self.filesystem)
        self.assertEqual(outcome.status, SEED_STATUS_FAILED)
        self.assertEqual(outcome.reason, SEED_REASON_DOCUMENT_NOT_MAPPING)

    def test_a_foreign_owned_document_is_refused_before_the_write(self) -> None:
        self.filesystem.documents[PRIMARY] = "{}"
        self.filesystem.stats[PRIMARY] = OnboardingDocumentStat(
            owner_is_caller=False, mode=0o600
        )
        outcome = _preseed(self.filesystem)
        self.assertEqual(outcome.status, SEED_STATUS_FAILED)
        self.assertEqual(outcome.reason, SEED_REASON_FOREIGN_OWNER)
        self.assertEqual(self.filesystem.write_calls, [])

    def test_an_unusable_base_maps_to_base_unusable(self) -> None:
        self.filesystem.unusable_bases.add(HOME)
        outcome = _preseed(self.filesystem)
        self.assertEqual(outcome.status, SEED_STATUS_FAILED)
        self.assertEqual(outcome.reason, SEED_REASON_BASE_UNUSABLE)
        self.assertEqual(self.filesystem.write_calls, [])

    def test_a_failed_write_maps_to_write_failed(self) -> None:
        self.filesystem.documents[PRIMARY] = "{}"
        self.filesystem.failing_writes.add(PRIMARY)
        outcome = _preseed(self.filesystem)
        self.assertEqual(outcome.status, SEED_STATUS_FAILED)
        self.assertEqual(outcome.reason, SEED_REASON_WRITE_FAILED)

    # --- the create path ----------------------------------------------------------

    def test_a_fresh_home_creates_the_creatable_document(self) -> None:
        outcome = _preseed(self.filesystem)
        self.assertEqual(outcome.status, SEED_STATUS_SEEDED)
        self.assertEqual(outcome.document_id, "primary_document")
        self.assertEqual(
            self._document(),
            {"hasCompletedOnboarding": True, "theme": "dark"},
        )
        self.assertEqual(self.filesystem.modes[PRIMARY], 0o600)

    def test_an_existing_candidate_wins_over_the_creatable_one(self) -> None:
        self.filesystem.documents[SECONDARY] = "{}"
        outcome = _preseed(self.filesystem)
        self.assertEqual(outcome.status, SEED_STATUS_SEEDED)
        self.assertEqual(outcome.document_id, "secondary_document")
        self.assertNotIn(PRIMARY, self.filesystem.documents)

    def test_a_lost_create_race_re_reads_and_merges_the_winners_document(self) -> None:
        # The one recoverable failure: another writer landed the document between the
        # existence probe and the create. The honest response is to merge into what
        # actually landed, not to report a failure the filesystem already resolved.
        self.filesystem.create_races[PRIMARY] = '{"numStartups": 1}'
        outcome = _preseed(self.filesystem)
        self.assertEqual(outcome.status, SEED_STATUS_SEEDED)
        merged = self._document()
        self.assertEqual(merged["numStartups"], 1)
        self.assertIs(merged["hasCompletedOnboarding"], True)

    def test_losing_the_create_race_twice_reports_the_gap(self) -> None:
        class AlwaysRacing(FakeOnboardingDocumentFilesystem):
            def read_document_text(self, path: str) -> str:
                raise FileNotFoundError(path)

            def create_new_document(self, path, text, mode) -> None:
                raise FileExistsError(path)

        outcome = _preseed(AlwaysRacing())
        self.assertEqual(outcome.status, SEED_STATUS_FAILED)
        self.assertEqual(outcome.reason, SEED_REASON_WRITE_FAILED)

    def test_an_unprobeable_candidate_fails_typed_instead_of_raising(self) -> None:
        # Review j#108770 finding_filesystemportexistenceerrorescapes: an existence
        # probe that raises (PermissionError on the parent) must become the typed
        # failed outcome — never an escaping exception, and never a fall-through to
        # creating a different document than the one the provider may read.
        class UnprobeableCandidate(FakeOnboardingDocumentFilesystem):
            def document_exists(self, path: str) -> bool:
                raise PermissionError(path)

        outcome = _preseed(UnprobeableCandidate())
        self.assertEqual(outcome.status, SEED_STATUS_FAILED)
        self.assertEqual(outcome.reason, SEED_REASON_DOCUMENT_UNREADABLE)

    # --- completion semantics at the write boundary (verdict j#108694) ------------

    def test_honored_flags_never_open_the_document_for_writing(self) -> None:
        # Complete means COMPLETE at the port: no create, no replace — even with the
        # non-flag theme default absent.
        self.filesystem.documents[PRIMARY] = '{"hasCompletedOnboarding": true}'
        outcome = _preseed(self.filesystem)
        self.assertEqual(outcome.status, SEED_STATUS_ALREADY_COMPLETE)
        self.assertEqual(self.filesystem.write_calls, [])
        self.assertEqual(
            self.filesystem.documents[PRIMARY], '{"hasCompletedOnboarding": true}'
        )

    def test_a_false_flag_is_reseeded_not_reported_complete(self) -> None:
        # The r1 presence bug: this exact document used to return already_complete
        # while the provider rendered its onboarding screen over it.
        self.filesystem.documents[PRIMARY] = json.dumps(
            {"hasCompletedOnboarding": False, "theme": "light", "numStartups": 4}
        )
        outcome = _preseed(self.filesystem)
        self.assertEqual(outcome.status, SEED_STATUS_SEEDED)
        self.assertEqual(outcome.seeded_keys, ("hasCompletedOnboarding",))
        merged = self._document()
        self.assertIs(merged["hasCompletedOnboarding"], True)
        # The operator's own choices survive the reseed untouched.
        self.assertEqual(merged["theme"], "light")
        self.assertEqual(merged["numStartups"], 4)

    def test_a_replace_preserves_the_operators_mode(self) -> None:
        self.filesystem.documents[PRIMARY] = '{"hasCompletedOnboarding": false}'
        self.filesystem.stats[PRIMARY] = OnboardingDocumentStat(
            owner_is_caller=True, mode=0o644
        )
        outcome = _preseed(self.filesystem)
        self.assertEqual(outcome.status, SEED_STATUS_SEEDED)
        self.assertEqual(self.filesystem.modes[PRIMARY], 0o644)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
