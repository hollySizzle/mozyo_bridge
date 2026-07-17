"""``workflow callback-admit``: the exit-code contract and its registration (#13910).

The exit code is not cosmetic — it is the whole enforcement surface for a shell caller. ``... &&
<effect>`` must actuate on ``admitted`` and on nothing else, so "already actuated" can never be
mistaken for "go ahead". A single zero-for-success convention would silently reinstate the
duplicate this command exists to prevent, which is why the mapping is asserted directly.
"""

import argparse
import os
import tempfile
import unittest

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_recovery_admission import (
    ADMIT_ADMITTED,
    ADMIT_CONFLICT,
    ADMIT_DUPLICATE,
    ADMIT_SUPERSEDED,
    ADMIT_UNREADABLE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_callbacks import (
    register_callbacks,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_recovery_admission import (
    EXIT_ADMITTED,
    EXIT_CONFLICT,
    EXIT_DUPLICATE,
    EXIT_SUPERSEDED,
    EXIT_UNREADABLE,
    _exit_code,
)


class ExitCodeContractTests(unittest.TestCase):
    def test_only_admitted_exits_zero(self):
        """The load-bearing property: `&&` chaining cannot actuate on a refusal."""
        self.assertEqual(_exit_code(ADMIT_ADMITTED), 0)
        for outcome in (ADMIT_DUPLICATE, ADMIT_SUPERSEDED, ADMIT_CONFLICT, ADMIT_UNREADABLE):
            with self.subTest(outcome=outcome):
                self.assertNotEqual(
                    _exit_code(outcome), 0,
                    f"{outcome} must not exit 0: a shell caller would read it as authorization",
                )

    def test_each_outcome_has_a_distinct_code(self):
        """Distinct codes so an operator can tell a duplicate from a torn store."""
        codes = [
            _exit_code(o)
            for o in (ADMIT_ADMITTED, ADMIT_DUPLICATE, ADMIT_SUPERSEDED, ADMIT_CONFLICT,
                      ADMIT_UNREADABLE)
        ]
        self.assertEqual(len(codes), len(set(codes)), f"exit codes collide: {codes}")

    def test_codes_match_the_documented_constants(self):
        self.assertEqual(_exit_code(ADMIT_ADMITTED), EXIT_ADMITTED)
        self.assertEqual(_exit_code(ADMIT_DUPLICATE), EXIT_DUPLICATE)
        self.assertEqual(_exit_code(ADMIT_SUPERSEDED), EXIT_SUPERSEDED)
        self.assertEqual(_exit_code(ADMIT_CONFLICT), EXIT_CONFLICT)
        self.assertEqual(_exit_code(ADMIT_UNREADABLE), EXIT_UNREADABLE)

    def test_an_unknown_outcome_fails_closed(self):
        """A future outcome must never default to authorizing an effect."""
        self.assertEqual(_exit_code("something_new"), EXIT_UNREADABLE)
        self.assertNotEqual(_exit_code("something_new"), 0)


class PublicCommandExitTests(unittest.TestCase):
    """The exit contract asserted through the PUBLIC command, not only its mapping helper.

    Review j#81021 F3. `ExitCodeContractTests` above drove `_exit_code()` — a pure function that
    only ever saw outcomes — and passed while the command itself returned 0 for `--bootstrap`,
    admitting nothing. A test that exercises only the branch its author was thinking about proves
    only that the author was consistent. These drive `cmd_workflow_callback_admit` itself.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._prev = os.environ.get("MOZYO_BRIDGE_HOME")
        os.environ["MOZYO_BRIDGE_HOME"] = self._tmp.name
        self.addCleanup(self._restore)

    def _restore(self):
        if self._prev is None:
            os.environ.pop("MOZYO_BRIDGE_HOME", None)
        else:
            os.environ["MOZYO_BRIDGE_HOME"] = self._prev

    def test_admit_never_exits_zero_without_an_admission(self):
        """The regression F3 names: no non-admitting path through this command may exit 0."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_recovery_admission import (  # noqa: E501
            cmd_workflow_callback_admit,
        )

        args = argparse.Namespace(
            issue="13910", journal="99999", route="w-1", receiver="codex", workspace_id=""
        )
        rc = cmd_workflow_callback_admit(args)
        self.assertNotEqual(
            rc, 0, "callback-admit exited 0 without admitting: `... && <effect>` would actuate"
        )

    def test_admit_has_no_bootstrap_flag(self):
        """Bootstrap cannot live on a command whose exit 0 means 'admitted'."""
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="cmd")
        register_callbacks(sub)
        with self.assertRaises(SystemExit):
            parser.parse_args(["callback-admit", "--bootstrap"])

    def test_receipt_bootstrap_is_its_own_command(self):
        """The store lifecycle has a surface — it was not simply deleted."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_recovery_admission import (  # noqa: E501
            cmd_workflow_callback_receipt,
        )

        self.assertEqual(cmd_workflow_callback_receipt(argparse.Namespace(bootstrap=True)), 0)
        # ...and status now reports a ready store.
        self.assertEqual(cmd_workflow_callback_receipt(argparse.Namespace(bootstrap=False)), 0)


class RegistrationTests(unittest.TestCase):
    def test_callback_admit_is_reachable_on_the_real_workflow_parser(self):
        """The rail is only a rail if the shipped CLI actually exposes it."""
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="cmd")
        register_callbacks(sub)
        args = parser.parse_args(
            ["callback-admit", "--issue", "13910", "--journal", "80500",
             "--route", "w-1", "--receiver", "codex"]
        )
        self.assertEqual(args.issue, "13910")
        self.assertEqual(args.journal, "80500")
        self.assertEqual(args.route, "w-1")
        self.assertEqual(args.receiver, "codex")
        self.assertTrue(callable(getattr(args, "func", None)))

    def test_under_specified_admission_is_refused(self):
        """An admission that cannot name its action must not run at all."""
        import argparse

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_callbacks import (  # noqa: E501
            register_callbacks,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_recovery_admission import (  # noqa: E501
            cmd_workflow_callback_admit,
        )

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="cmd")
        register_callbacks(sub)
        args = parser.parse_args(["callback-admit", "--issue", "13910"])
        with self.assertRaises(SystemExit):
            cmd_workflow_callback_admit(args)


if __name__ == "__main__":
    unittest.main()
