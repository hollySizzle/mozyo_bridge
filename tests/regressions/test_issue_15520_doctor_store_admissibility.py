"""Redmine #15520 — doctor reports a home whose stores refuse every managed launch.

Measured on the owner's Mac (mozyo-bridge 1.0.0, herdr backend) on 2026-08-15: `mozyo`
aborted with `managed-launch preflight refused the selected attestation store: ... is v3,
but a managed launch now requires the v4 terminal-identity shape`, while
`mozyo-bridge doctor --target .` on the same host reported `herdr: ok`. Doctor judged
server reachability, workspace-segment resolution, and the startup self-attestation of
live workspace agents — none of which is the store shape that was actually stopping the
launch. On that Mac the repo's workspace had no live pane either, so the section returned
green from the `no live managed agent` branch before any attestation logic ran.

Two properties are pinned here, and they fail differently:

- **Wiring**: the section's verdict must come from the SAME store gates the launch
  preflight uses (`decide_store_admission` + `probe_launch_generation_store`), driven off
  a real home on disk. A doctor that restated the rule would pass a policy test and still
  drift away from the rail that refuses the launch.
- **Byte-invariance**: an admissible home must produce exactly the section it produced
  before this change — asserted by comparing against the same evaluation with the new
  argument omitted, so the guarantee is not re-typed by hand and cannot rot.

The extraction in `herdr_launcher_capability` is code motion, so
:class:`StoreCompatibilityUnchangedTest` characterizes `decide_store_compatibility`
across every store state rather than trusting that claim.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.doctor_health import evaluate_doctor_health  # noqa: E402
from mozyo_bridge.application.doctor_herdr import (  # noqa: E402
    HerdrSectionUseCase,
    HerdrStoreAdmission,
    LiveHerdrStoreAdmissionReads,
    evaluate_herdr_section,
)
from mozyo_bridge.core.state.herdr_identity_attestation import (  # noqa: E402
    herdr_identity_attestation_path,
)
from mozyo_bridge.core.state.herdr_identity_attestation_schema import (  # noqa: E402
    HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION,
    StoreSchemaObservation,
    probe_store_schema,
)
from mozyo_bridge.core.state.herdr_launch_generation import (  # noqa: E402
    herdr_launch_generation_path,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launcher_capability import (  # noqa: E402,E501
    LauncherCapabilityObservation,
    decide_store_compatibility,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_observability import (  # noqa: E402,E501
    HerdrInventoryView,
)

_V1_DDL = (
    "CREATE TABLE herdr_identity_attestations ("
    "assigned_name TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, role TEXT NOT NULL, "
    "lane_id TEXT NOT NULL, locator TEXT NOT NULL, verdict TEXT NOT NULL, "
    "detail TEXT NOT NULL DEFAULT '', observed_at TEXT NOT NULL)"
)


def _seed_recognized_older_store(home: Path) -> Path:
    """A genuine recognized-but-older attestation store (the Mac's v3 class)."""
    path = herdr_identity_attestation_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA user_version = 1")
        conn.execute(_V1_DDL)
        conn.commit()
    finally:
        conn.close()
    return path


def _seed_unreadable_generation_store(home: Path) -> Path:
    """A launch-generation store this runtime cannot read (the Mac's corrupt state)."""
    path = herdr_launch_generation_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"this is not a sqlite database")
    return path


def _view(*, agents=(), ok: bool = True, backend_selected: bool = True):
    return HerdrInventoryView(
        backend_selected=backend_selected,
        ok=ok,
        workspace_segment="ws",
        agents=tuple(agents),
        raw_row_count=len(agents),
        invalid_row_count=0,
    )


class StoreAdmissionWiringTest(unittest.TestCase):
    """Drive the real adapter against real homes on disk — not a hand-made verdict.

    This is the case that would have caught the defect: it never states what v1-vs-v4
    means, it asks the shipped gate about a store that exists.
    """

    def _home(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def test_a_fresh_home_admits_a_managed_launch(self) -> None:
        # Absent stores are a legitimate fresh home, not a fault.
        admission = LiveHerdrStoreAdmissionReads(self._home()).describe()

        self.assertTrue(admission.ok, admission.detail)

    def test_a_recognized_older_attestation_store_refuses(self) -> None:
        home = self._home()
        _seed_recognized_older_store(home)

        admission = LiveHerdrStoreAdmissionReads(home).describe()

        self.assertFalse(admission.ok)
        self.assertEqual("attestation_store_launcher_cannot_write", admission.reason)
        # The operator is told the shape mismatch, from the preflight's own text.
        self.assertIn(
            f"v{HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION} terminal-identity shape",
            admission.detail,
        )

    def test_an_unreadable_launch_generation_store_refuses(self) -> None:
        home = self._home()
        _seed_unreadable_generation_store(home)

        admission = LiveHerdrStoreAdmissionReads(home).describe()

        self.assertFalse(admission.ok)
        self.assertEqual("launch_generation_store_unsupported", admission.reason)

    def test_the_refusal_never_carries_store_content(self) -> None:
        # The probe's own detail can quote the file; this section is printed to the
        # terminal and the command log, so the rendered detail must be fixed text.
        home = self._home()
        herdr_launch_generation_path(home).parent.mkdir(parents=True, exist_ok=True)
        herdr_launch_generation_path(home).write_bytes(b"SENTINEL_MUST_NOT_BE_ECHOED")

        admission = LiveHerdrStoreAdmissionReads(home).describe()

        self.assertFalse(admission.ok)
        self.assertNotIn("SENTINEL_MUST_NOT_BE_ECHOED", admission.detail)

    def test_the_section_and_overall_health_go_red_through_the_use_case(self) -> None:
        # End of the wire: adapter -> section -> `evaluate_doctor_health`. The Mac had
        # NO live agent in this repo's workspace, which is why the old code returned
        # green here, so that is the inventory used.
        home = self._home()
        _seed_recognized_older_store(home)

        class _Reads:
            def describe(self):
                return _view(agents=())

        section = HerdrSectionUseCase(
            _Reads(),
            attestation_reader=lambda name: None,
            store_admission_reads=LiveHerdrStoreAdmissionReads(home),
        ).execute()

        self.assertEqual("error", section["status"])
        self.assertTrue(section["next_action"], "an error must tell the operator what to do")
        self.assertIn("attestation-store migrate --write", section["next_action"][0])
        self.assertFalse(evaluate_doctor_health({"herdr": section}).ok)

    def test_an_admissible_home_still_reaches_the_ordinary_verdict(self) -> None:
        home = self._home()

        class _Reads:
            def describe(self):
                return _view(agents=())

        section = HerdrSectionUseCase(
            _Reads(),
            attestation_reader=lambda name: None,
            store_admission_reads=LiveHerdrStoreAdmissionReads(home),
        ).execute()

        self.assertEqual("ok", section["status"])
        self.assertTrue(evaluate_doctor_health({"herdr": section}).ok)


class AdmissibleHomeIsByteInvariantTest(unittest.TestCase):
    """An admitting home must produce the pre-#15520 section, character for character."""

    def test_admitting_admission_changes_nothing(self) -> None:
        for label, view in (
            ("no agents", _view(agents=())),
            ("server down", _view(ok=False)),
        ):
            with self.subTest(case=label):
                self.assertEqual(
                    evaluate_herdr_section(view),
                    evaluate_herdr_section(
                        view, store_admission=HerdrStoreAdmission(True)
                    ),
                )

    def test_a_non_herdr_target_has_no_section_even_when_stores_refuse(self) -> None:
        # tmux byte-invariance outranks this check: a target that does not select herdr
        # is not judged on herdr's stores at all.
        self.assertIsNone(
            evaluate_herdr_section(
                _view(backend_selected=False),
                store_admission=HerdrStoreAdmission(False, "r", "d"),
            )
        )

    def test_a_down_server_keeps_its_own_transport_error(self) -> None:
        # An unreachable server is the more actionable fact and already fails closed;
        # the store verdict must not overwrite its reason.
        section = evaluate_herdr_section(
            _view(ok=False), store_admission=HerdrStoreAdmission(False, "r", "d")
        )

        self.assertEqual("error", section["status"])
        self.assertNotIn("store_admission", section)


class StoreCompatibilityUnchangedTest(unittest.TestCase):
    """The extraction is code motion — characterize the joined decision, don't assume it.

    Every store state is passed through `decide_store_compatibility`, whose branches now
    live in `decide_store_admission`. A regression in the moved code shows up as a changed
    `(ok, reason)` here rather than as a silent behaviour change in the launch preflight.
    """

    _LAUNCHER = LauncherCapabilityObservation(
        True, HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION, frozenset({4})
    )

    def _decide(self, store, **kw):
        return decide_store_compatibility(
            self._LAUNCHER,
            store,
            required_schema_version=HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION,
            replacement_launch=kw.get("replacement_launch", False),
            epoch_launch=kw.get("epoch_launch", False),
        )

    def test_each_store_state_keeps_its_verdict(self) -> None:
        cases = (
            ("unreadable", StoreSchemaObservation("store_unreadable", None, False),
             False, "attestation_store_unreadable"),
            ("unsupported", StoreSchemaObservation("store_unsupported", 99, True),
             False, "attestation_store_unsupported"),
            ("absent", StoreSchemaObservation("store_absent", None, False),
             True, "attestation_store_ok"),
            ("older recognized", StoreSchemaObservation("store_recognized", 1, False),
             False, "attestation_store_launcher_cannot_write"),
            ("required version", StoreSchemaObservation("store_recognized", 4, False),
             True, "attestation_store_ok"),
        )
        for label, store, ok, reason in cases:
            with self.subTest(store=label):
                verdict = self._decide(store)
                self.assertEqual(ok, verdict.ok)
                self.assertEqual(reason, verdict.reason)

    def test_a_replacement_launch_onto_v1_is_still_refused_for_its_own_reason(self) -> None:
        verdict = self._decide(
            StoreSchemaObservation("store_recognized", 1, False), replacement_launch=True
        )

        self.assertFalse(verdict.ok)
        self.assertEqual("attestation_store_replacement_unsupported", verdict.reason)

    def test_the_probed_store_drives_the_same_answer_as_the_doctor_adapter(self) -> None:
        # The two callers must agree on a real store, which is the anti-drift claim the
        # whole extraction exists to make.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        path = _seed_recognized_older_store(home)

        preflight = self._decide(probe_store_schema(path))
        doctor = LiveHerdrStoreAdmissionReads(home).describe()

        self.assertFalse(preflight.ok)
        self.assertFalse(doctor.ok)
        self.assertEqual(preflight.reason, doctor.reason)
        self.assertEqual(preflight.detail, doctor.detail)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
