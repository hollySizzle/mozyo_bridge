"""`sublane quarantine-inspect` CLI surface (Redmine #14234).

Drives the real argv through the real parser. The load-bearing test is
:meth:`ApprovalRoundTripTest.test_rendered_approval_parses_as_a_valid_execute_invocation`: it
takes the argv the inspection rendered and feeds it back into the parser for
``sublane quarantine``, proving the inspection→approval→execute loop actually closes. Before
#14234 that loop could not be closed from any public surface at all.

No actuation: the inspection is read-only, and the round-trip test only PARSES the execute argv
(it never dispatches it), so no receiver is ever replaced here.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
    sublane_quarantine_inspect as inspect_module,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_quarantine import (  # noqa: E501
    QuarantineInspection,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_pending_composer import (  # noqa: E501
    PendingComposerSignal,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)

WS = "wProj"
ISSUE = "14234"
LANE = "issue_14234_quarantine_inspection"
ROLE = "claude"
NAME = encode_assigned_name(WS, ROLE, LANE)
LOCATOR = f"{WS}:p44"
REVISION = 7
ATTESTED_AT = "2026-07-21T00:00:00+00:00"
SECRET_BODY = "coordinatorへ… private unsent draft"

ARGV = [
    "sublane", "quarantine-inspect",
    "--issue", ISSUE,
    "--lane", LANE,
    "--role", ROLE,
]


def _run(argv) -> tuple[int, str]:
    args = build_parser().parse_args(argv)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = args.func(args)
    return int(rc or 0), buf.getvalue()


class _FakeOps:
    def __init__(self, inspection):
        self._inspection = inspection

    def inspect(self, request):
        return self._inspection


def _inspection(**kw) -> QuarantineInspection:
    signal = PendingComposerSignal(
        inventory_readable=True,
        has_pending=True,
        agent_state="idle",
        identity_attested=True,
        generation_matches=True,
    )
    base = dict(
        workspace_id=WS,
        signal=signal,
        row_revision=REVISION,
        attested_at=ATTESTED_AT,
        receiver_present=True,
        detail="classified_without_persisting_composer_body",
    )
    base.update(kw)
    return QuarantineInspection(**base)


@contextlib.contextmanager
def _live(rows=None, inspection=None):
    """Patch only the two live seams: the workspace scope and the use case's collaborators."""
    rows = [{"name": NAME, "pane_id": LOCATOR, "revision": REVISION}] if rows is None else rows
    real_init = inspect_module.SublaneQuarantineInspectUseCase.__init__

    def _init(self, **kw):
        kw.setdefault("rows_reader", lambda: rows)
        kw.setdefault("ops_factory", lambda _rows: _FakeOps(inspection or _inspection()))
        real_init(self, **kw)

    with mock.patch.object(inspect_module, "repo_scope_workspace_id", return_value=WS), \
         mock.patch.object(inspect_module.SublaneQuarantineInspectUseCase, "__init__", _init):
        yield


class RegistrationTest(unittest.TestCase):
    def test_registered_under_sublane(self):
        args = build_parser().parse_args(ARGV)
        self.assertEqual(args.func.__name__, "cmd_sublane_quarantine_inspect")

    def test_exact_lane_coordinates_are_required(self):
        for flag in ("--issue", "--lane", "--role"):
            with self.subTest(flag=flag):
                argv = list(ARGV)
                i = argv.index(flag)
                del argv[i:i + 2]
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        build_parser().parse_args(argv)

    def test_has_no_execute_flag(self):
        # The inspection surface is read-only by construction; --execute lives on `quarantine`.
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                build_parser().parse_args(ARGV + ["--execute"])


class CommandTest(unittest.TestCase):
    def test_json_reports_every_exact_token(self):
        with _live():
            rc, out = _run(ARGV + ["--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["action"], "quarantine-inspect")
        self.assertEqual(payload["assigned_name"], NAME)
        self.assertEqual(payload["locator"], LOCATOR)
        self.assertEqual(payload["agent_revision"], REVISION)
        self.assertEqual(payload["attested_at"], ATTESTED_AT)
        self.assertEqual(payload["action_generation"], f"quarantine:{LANE}:{ROLE}:{LOCATOR}")
        self.assertTrue(payload["approval_ready"])

    def test_text_mode_renders_the_pasteable_template(self):
        with _live():
            rc, out = _run(ARGV)
        self.assertEqual(rc, 0)
        self.assertIn("Owner Approval", out)
        self.assertIn(NAME, out)

    def test_composer_body_is_never_emitted(self):
        rows = [{"name": NAME, "pane_id": LOCATOR, "revision": REVISION, "composer": SECRET_BODY}]
        with _live(rows=rows):
            rc, out = _run(ARGV + ["--json"])
        self.assertEqual(rc, 0)
        self.assertNotIn(SECRET_BODY, out)
        self.assertNotIn("body", json.loads(out))

    def test_refusal_exits_non_zero(self):
        with _live(rows=[]):
            rc, out = _run(ARGV + ["--json"])
        self.assertEqual(rc, 1)
        payload = json.loads(out)
        self.assertFalse(payload["approval_ready"])
        self.assertIsNone(payload["approval_command"])


class ApprovalRoundTripTest(unittest.TestCase):
    """Acceptance 2/5: inspection → approval → execute closes as one loop."""

    def test_rendered_approval_parses_as_a_valid_execute_invocation(self):
        with _live():
            _, out = _run(ARGV + ["--json"])
        argv = json.loads(out)["approval_command"]
        self.assertEqual(argv[0], "mozyo-bridge")

        # Substitute the real approval journal id the owner would post, then parse the rendered
        # command with the REAL parser for `sublane quarantine`. Parsing only — no dispatch.
        rendered = argv[1:]
        journal_index = rendered.index("--journal") + 1
        rendered[journal_index] = "85200"
        parsed = build_parser().parse_args(rendered)

        self.assertEqual(parsed.func.__name__, "cmd_sublane_quarantine")
        self.assertTrue(parsed.execute)
        self.assertEqual(parsed.assigned_name, NAME)
        self.assertEqual(parsed.locator, LOCATOR)
        self.assertEqual(parsed.action_generation, f"quarantine:{LANE}:{ROLE}:{LOCATOR}")
        self.assertEqual(parsed.approved_revision, REVISION)
        self.assertEqual(parsed.approval_observed_at, ATTESTED_AT)
        self.assertEqual(parsed.issue, ISSUE)
        self.assertEqual(parsed.lane, LANE)
        self.assertEqual(parsed.role, ROLE)

    def test_every_required_execute_flag_is_supplied_by_the_template(self):
        with _live():
            _, out = _run(ARGV + ["--json"])
        argv = json.loads(out)["approval_command"]
        for flag in (
            "--issue", "--lane", "--journal", "--role", "--assigned-name", "--locator",
            "--action-generation", "--approval-observed-at", "--approved-revision", "--execute",
        ):
            self.assertIn(flag, argv, f"the rendered approval omits {flag}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
