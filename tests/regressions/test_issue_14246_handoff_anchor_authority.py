"""Redmine journal ownership is proven before handoff transport (Redmine #14246)."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_anchor_authority import (
    AnchorAuthorityError,
    RedmineAnchorRead,
    verify_live_handoff_anchor,
    verify_redmine_anchor,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    AsanaAnchor,
    RedmineAnchor,
    make_outcome,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_anchor_source import (
    LiveRedmineAnchorSource,
    RedmineAnchorSourceError,
)


class _Source:
    def __init__(self, observed: RedmineAnchorRead | Exception):
        self.observed = observed
        self.calls: list[tuple[str, str]] = []

    def read_anchor(self, issue: str, journal: str) -> RedmineAnchorRead:
        self.calls.append((issue, journal))
        if isinstance(self.observed, Exception):
            raise self.observed
        return self.observed


class AnchorAuthorityDecisionTest(unittest.TestCase):
    anchor = RedmineAnchor(issue="13627", journal="85353")

    def _reason(self, observed: RedmineAnchorRead | Exception) -> str:
        with self.assertRaises(AnchorAuthorityError) as caught:
            verify_redmine_anchor(self.anchor, _Source(observed))
        self.assertIs(caught.exception.anchor, self.anchor)
        return caught.exception.reason

    def test_valid_pair_is_admitted(self) -> None:
        source = _Source(
            RedmineAnchorRead("13627", "85353", True, "13627", ("1", "85353"))
        )
        self.assertIs(verify_redmine_anchor(self.anchor, source), self.anchor)
        self.assertEqual(source.calls, [("13627", "85353")])

    def test_missing_issue_has_a_distinct_reason(self) -> None:
        self.assertEqual(
            self._reason(RedmineAnchorRead("13627", "85353", False, None, ())),
            "anchor_issue_not_found",
        )

    def test_missing_journal_has_a_distinct_reason(self) -> None:
        self.assertEqual(
            self._reason(RedmineAnchorRead("13627", "85353", True, "13627", ("1",))),
            "anchor_journal_not_found",
        )

    def test_live_contract_fixture_reproduces_wrong_owner(self) -> None:
        """#13627/j#85353 was observed as owned by #14244, not #13627."""
        self.assertEqual(
            self._reason(
                RedmineAnchorRead(
                    "13627", "85353", True, "14244", ("85353",)
                )
            ),
            "anchor_issue_journal_mismatch",
        )

    def test_provider_error_and_ambiguous_rows_fail_closed(self) -> None:
        self.assertEqual(
            self._reason(RuntimeError("provider detail must not escape")),
            "anchor_provider_unreadable",
        )
        self.assertEqual(
            self._reason(
                RedmineAnchorRead("13627", "85353", True, "13627", ("８５３５３",))
            ),
            "anchor_provider_unreadable",
        )
        self.assertEqual(
            self._reason(
                RedmineAnchorRead(
                    "13627", "85353", True, "13627", ("85353", "85353")
                )
            ),
            "anchor_provider_unreadable",
        )

    def test_asana_does_not_construct_the_redmine_source(self) -> None:
        anchor = AsanaAnchor(task_id="T1", comment_id="C1")
        with patch.object(
            LiveRedmineAnchorSource,
            "from_environment",
            side_effect=AssertionError("Redmine source must not be built"),
        ) as construct:
            self.assertIs(verify_live_handoff_anchor(anchor), anchor)
        construct.assert_not_called()


class _Response(io.BytesIO):
    pass


class LiveRedmineAnchorSourceTest(unittest.TestCase):
    def _source(self, opener):
        return LiveRedmineAnchorSource(
            base_url="https://redmine.example.invalid",
            api_key="fixture-key",
            opener=opener,
        )

    def test_reads_only_identity_and_journal_ids_from_trusted_issue_endpoint(self) -> None:
        observed: dict[str, object] = {}

        def opener(request, timeout):
            observed.update(url=request.full_url, headers=dict(request.header_items()), timeout=timeout)
            return _Response(
                json.dumps(
                    {
                        "issue": {
                            "id": 13627,
                            "subject": "must not enter the fact model",
                            "journals": [{"id": 1, "notes": "private"}, {"id": 85353}],
                        }
                    }
                ).encode()
            )

        result = self._source(opener).read_anchor("13627", "85353")
        self.assertEqual(result.observed_issue, "13627")
        self.assertEqual(result.journal_ids, ("1", "85353"))
        self.assertEqual(
            observed["url"],
            "https://redmine.example.invalid/issues/13627.json?include=journals",
        )
        self.assertEqual(observed["headers"]["X-redmine-api-key"], "fixture-key")
        self.assertEqual(observed["timeout"], 5.0)

    def test_http_404_is_issue_not_found_fact(self) -> None:
        def opener(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)

        result = self._source(opener).read_anchor("13627", "85353")
        self.assertFalse(result.issue_exists)
        self.assertIsNone(result.observed_issue)
        self.assertEqual(result.journal_ids, ())

    def test_real_adapter_fixture_projects_the_observed_wrong_owner(self) -> None:
        source = self._source(
            lambda _request, _timeout: _Response(
                b'{"issue":{"id":14244,"journals":[{"id":85353}]}}'
            )
        )
        with self.assertRaises(AnchorAuthorityError) as caught:
            verify_redmine_anchor(RedmineAnchor("13627", "85353"), source)
        self.assertEqual(
            caught.exception.reason, "anchor_issue_journal_mismatch"
        )

    def test_malformed_or_unauthorized_response_is_unreadable(self) -> None:
        with self.assertRaises(RedmineAnchorSourceError):
            self._source(lambda _request, _timeout: _Response(b"[]")).read_anchor(
                "13627", "85353"
            )
        with self.assertRaises(RedmineAnchorSourceError) as caught:
            self._source(
                lambda request, _timeout: (_ for _ in ()).throw(
                    urllib.error.HTTPError(request.full_url, 403, "forbidden", {}, None)
                )
            ).read_anchor("13627", "85353")
        self.assertNotIn("fixture-key", str(caught.exception))

    def test_unicode_digit_identifiers_are_not_sent_to_redmine(self) -> None:
        opener = Mock()
        with self.assertRaises(RedmineAnchorSourceError):
            self._source(opener).read_anchor("１３６２７", "85353")
        opener.assert_not_called()


class CommandBoundaryZeroSendTest(unittest.TestCase):
    def test_mismatch_blocks_before_target_resolution_with_structured_outcome(self) -> None:
        args = build_parser().parse_args(
            [
                "handoff", "send", "--to", "codex", "--source", "redmine",
                "--issue", "13627", "--journal", "85353", "--kind", "reply",
                "--record-format", "json",
            ]
        )
        source = _Source(
            RedmineAnchorRead("13627", "85353", True, "14244", ("85353",))
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch(
            "mozyo_bridge.application.handoff_transport_wiring.resolve_handoff_transport_runtime",
            return_value=(None, None),
        ), patch(
            "mozyo_bridge.application.commands.herdr_effective_backend_selected",
            return_value=False,
        ), patch("mozyo_bridge.application.commands.require_tmux"), patch.object(
            LiveRedmineAnchorSource, "from_environment", return_value=source
        ), patch(
            "mozyo_bridge.application.commands.run_target_resolution",
            side_effect=AssertionError("target resolution must not run"),
        ) as target_resolution, patch(
            "mozyo_bridge.application.commands.run_tmux"
        ) as transport, patch(
            "mozyo_bridge.application.commands.capture_pane"
        ) as capture, patch(
            "mozyo_bridge.application.commands.wait_for_text"
        ) as wait, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                args.func(args)

        target_resolution.assert_not_called()
        transport.assert_not_called()
        capture.assert_not_called()
        wait.assert_not_called()
        outcome = json.loads(stdout.getvalue().splitlines()[-1])
        self.assertEqual(outcome["status"], "blocked")
        self.assertEqual(outcome["reason"], "anchor_issue_journal_mismatch")
        self.assertEqual(outcome["anchor"]["issue"], "13627")
        self.assertEqual(outcome["anchor"]["journal"], "85353")
        self.assertEqual(outcome["injection_stage"]["stage"], "not_sent")
        self.assertEqual(outcome["next_action_owner"], "sender")
        self.assertIn("does not belong", stderr.getvalue())

    def test_all_anchor_refusals_are_pre_injection(self) -> None:
        for reason in (
            "anchor_issue_not_found",
            "anchor_journal_not_found",
            "anchor_issue_journal_mismatch",
            "anchor_provider_unreadable",
        ):
            with self.subTest(reason=reason):
                outcome = make_outcome(
                    status="blocked",
                    reason=reason,
                    receiver="codex",
                    target=None,
                    anchor=RedmineAnchor("13627", "85353"),
                    mode="queue-enter",
                    kind="reply",
                    notification_marker=None,
                )
                self.assertEqual(outcome.injection_stage["stage"], "not_sent")
                self.assertEqual(outcome.next_action_owner, "sender")


if __name__ == "__main__":
    unittest.main()
