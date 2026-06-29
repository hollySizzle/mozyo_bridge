"""Fake-port / pure-policy specifications for the doctor scaffold boundary (#12853).

These exercise the ``doctor_scaffold`` verdict authority and scaffold read port
directly, with a synthetic ``scaffold_status`` detail — without a real scaffolded
checkout / drifted-manifest topology and without parsing a real
``.mozyo-bridge`` manifest. They are the checkout-topology -> fake-port /
fake-policy migration for the ``scaffold`` section slice.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from mozyo_bridge.application import doctor
from mozyo_bridge.application.doctor_scaffold import (
    LiveScaffoldReads,
    ScaffoldReads,
    ScaffoldSectionUseCase,
    ScaffoldSectionVerdict,
    evaluate_scaffold_section,
)
from mozyo_bridge.scaffold.rules import PRESETS


def _view(
    *,
    detail: dict[str, Any],
    target: str = "/checkout",
    home: Path | None = None,
) -> dict[str, Any]:
    return {"detail": detail, "target": target, "home": home}


class FakeScaffoldReads:
    """In-memory fake of the ``ScaffoldReads`` port."""

    def __init__(self, view: dict[str, Any]) -> None:
        self._view = view
        self.calls = 0

    def describe(self) -> dict[str, Any]:
        self.calls += 1
        return self._view


class EvaluateScaffoldSectionPolicyTest(unittest.TestCase):
    def test_missing_manifest_emits_bootstrap_apply_command(self) -> None:
        verdict = evaluate_scaffold_section(_view(detail={"manifest": "missing"}))
        self.assertEqual("missing", verdict.status)
        self.assertEqual(
            (
                f"mozyo-bridge scaffold apply <{'|'.join(PRESETS)}> "
                "--target /checkout",
            ),
            verdict.next_action,
        )

    def test_missing_manifest_appends_home_suffix(self) -> None:
        verdict = evaluate_scaffold_section(
            _view(detail={"manifest": "missing"}, home=Path("/custom/home"))
        )
        self.assertEqual("missing", verdict.status)
        self.assertEqual(1, len(verdict.next_action))
        self.assertTrue(verdict.next_action[0].endswith("--target /checkout --home /custom/home"))

    def test_invalid_manifest_emits_regenerate_command(self) -> None:
        verdict = evaluate_scaffold_section(_view(detail={"manifest": "invalid"}))
        self.assertEqual("invalid", verdict.status)
        self.assertEqual(
            (
                "regenerate manifest with `mozyo-bridge scaffold apply <preset> "
                "--target /checkout --backup`",
            ),
            verdict.next_action,
        )

    def test_clean_manifest_is_ok_with_no_next_action(self) -> None:
        verdict = evaluate_scaffold_section(
            _view(detail={"manifest": "v2", "clean": True})
        )
        self.assertEqual(ScaffoldSectionVerdict(status="ok"), verdict)

    def test_drifted_central_missing_without_home_uses_bare_rules_install(self) -> None:
        verdict = evaluate_scaffold_section(
            _view(
                detail={
                    "manifest": "v2",
                    "clean": False,
                    "central_status": "missing",
                    "preset": "redmine-governed",
                    "files": [],
                }
            )
        )
        self.assertEqual("drifted", verdict.status)
        self.assertEqual(("mozyo-bridge rules install",), verdict.next_action)

    def test_drifted_central_missing_with_home_qualifies_rules_install(self) -> None:
        verdict = evaluate_scaffold_section(
            _view(
                detail={
                    "manifest": "v2",
                    "clean": False,
                    "central_status": "missing",
                    "preset": "redmine-governed",
                    "files": [],
                },
                home=Path("/custom/home"),
            )
        )
        self.assertEqual("drifted", verdict.status)
        self.assertEqual(
            ("mozyo-bridge rules install --home /custom/home",), verdict.next_action
        )

    def test_drifted_central_content_emits_scaffold_apply_backup(self) -> None:
        for central_status in ("drifted-content", "drifted-version", "ok-version-only"):
            with self.subTest(central_status=central_status):
                verdict = evaluate_scaffold_section(
                    _view(
                        detail={
                            "manifest": "v2",
                            "clean": False,
                            "central_status": central_status,
                            "preset": "redmine-governed",
                            "files": [],
                        }
                    )
                )
                self.assertEqual("drifted", verdict.status)
                self.assertEqual(
                    (
                        "mozyo-bridge scaffold apply redmine-governed "
                        "--target /checkout --backup",
                    ),
                    verdict.next_action,
                )

    def test_drifted_missing_preset_label_falls_back_to_placeholder(self) -> None:
        verdict = evaluate_scaffold_section(
            _view(
                detail={
                    "manifest": "v2",
                    "clean": False,
                    "central_status": "drifted-content",
                    "preset": None,
                    "files": [],
                }
            )
        )
        self.assertEqual(
            ("mozyo-bridge scaffold apply <preset> --target /checkout --backup",),
            verdict.next_action,
        )

    def test_drifted_router_files_append_review_and_restore(self) -> None:
        verdict = evaluate_scaffold_section(
            _view(
                detail={
                    "manifest": "v2",
                    "clean": False,
                    "central_status": "ok",
                    "preset": "redmine-governed",
                    "files": [
                        {"status": "ok"},
                        {"status": "drifted"},
                    ],
                }
            )
        )
        self.assertEqual("drifted", verdict.status)
        # central_status "ok" emits no rules-install/scaffold-apply line; only the
        # per-file review-and-restore guidance fires.
        self.assertEqual(
            (
                "review router files; rerun `mozyo-bridge scaffold apply "
                "redmine-governed --target /checkout --backup` to restore",
            ),
            verdict.next_action,
        )

    def test_drifted_central_and_files_emit_both_actions_in_order(self) -> None:
        verdict = evaluate_scaffold_section(
            _view(
                detail={
                    "manifest": "v2",
                    "clean": False,
                    "central_status": "drifted-version",
                    "preset": "redmine-governed",
                    "files": [{"status": "drifted"}],
                },
                home=Path("/custom/home"),
            )
        )
        self.assertEqual("drifted", verdict.status)
        self.assertEqual(
            (
                "mozyo-bridge scaffold apply redmine-governed --target /checkout "
                "--home /custom/home --backup",
                "review router files; rerun `mozyo-bridge scaffold apply "
                "redmine-governed --target /checkout --home /custom/home --backup` "
                "to restore",
            ),
            verdict.next_action,
        )


class ScaffoldSectionUseCaseTest(unittest.TestCase):
    def test_use_case_assembles_legacy_section_dict(self) -> None:
        detail = {"manifest": "v2", "clean": True, "target": "/checkout"}
        view = _view(detail=detail, target="/checkout")
        reads = FakeScaffoldReads(view)

        section = ScaffoldSectionUseCase(reads).execute()

        self.assertEqual(
            {
                "status": "ok",
                "target": "/checkout",
                "detail": detail,
                "next_action": [],
            },
            section,
        )
        # The legacy collector key insertion order is status/target/detail/next_action.
        self.assertEqual(
            ["status", "target", "detail", "next_action"], list(section.keys())
        )
        self.assertIs(detail, section["detail"])
        self.assertIsInstance(section["next_action"], list)
        self.assertEqual(1, reads.calls)

    def test_use_case_carries_verdict_next_action_for_missing(self) -> None:
        section = ScaffoldSectionUseCase(
            FakeScaffoldReads(_view(detail={"manifest": "missing"}))
        ).execute()
        self.assertEqual("missing", section["status"])
        self.assertEqual(1, len(section["next_action"]))

    def test_live_adapter_satisfies_the_port_protocol(self) -> None:
        self.assertIsInstance(LiveScaffoldReads(object()), ScaffoldReads)


class LiveScaffoldReadsTest(unittest.TestCase):
    """The live adapter resolves ``doctor_target`` / ``doctor_home`` /
    ``scaffold_status`` through the ``doctor`` module at call time."""

    def test_describe_routes_through_doctor_helpers_at_call_time(self) -> None:
        sentinel_detail = {"manifest": "v2", "clean": False, "central_status": "ok"}
        captured: dict[str, Any] = {}

        target = Path("/checkout/repo")
        home = Path("/custom/home")
        args = object()

        def fake_target(passed_args: Any) -> Path:
            captured["target_args"] = passed_args
            return target

        def fake_home(passed_args: Any) -> Path | None:
            captured["home_args"] = passed_args
            return home

        def fake_scaffold_status(passed_target: Path, home: Path | None = None) -> dict[str, Any]:
            captured["status_target"] = passed_target
            captured["status_home"] = home
            return sentinel_detail

        originals = (
            doctor.doctor_target,
            doctor.doctor_home,
            doctor.scaffold_status,
        )
        doctor.doctor_target = fake_target
        doctor.doctor_home = fake_home
        doctor.scaffold_status = fake_scaffold_status
        try:
            view = LiveScaffoldReads(args).describe()
        finally:
            (
                doctor.doctor_target,
                doctor.doctor_home,
                doctor.scaffold_status,
            ) = originals

        self.assertIs(sentinel_detail, view["detail"])
        self.assertEqual(str(target), view["target"])
        self.assertIs(home, view["home"])
        self.assertIs(args, captured["target_args"])
        self.assertIs(args, captured["home_args"])
        self.assertEqual(target, captured["status_target"])
        self.assertIs(home, captured["status_home"])


class DoctorCollectorDelegationTest(unittest.TestCase):
    """``doctor_scaffold_section`` is now a thin handler over the use case; it
    still routes through the live scaffold read."""

    def test_collector_matches_use_case_over_live_read(self) -> None:
        import argparse

        args = argparse.Namespace(repo="/tmp/doctor-scaffold-delegation-no-checkout", home=None)
        section = doctor.doctor_scaffold_section(args)
        expected = ScaffoldSectionUseCase(LiveScaffoldReads(args)).execute()
        self.assertEqual(expected, section)


if __name__ == "__main__":
    unittest.main()
