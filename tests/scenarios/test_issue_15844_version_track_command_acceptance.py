"""Operator-view acceptance for ``workflow version-track`` (Redmine #15844).

Drives the real shipped argv parser and the real command, and asserts the operator
contract: the exit codes a coordinator loop reads, the JSON envelope shape, and — the
acceptance condition of the US's first slice — that the command exposes no way to act.
"""

import contextlib
import io
import json
import unittest
from dataclasses import dataclass, field
from typing import Optional
from unittest import mock

_LIVE_BUCKET = (
    "mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure"
    ".live_fixed_version_bucket.read_live_fixed_version_bucket"
)
_LIFECYCLE = (
    "mozyo_bridge.core.state.lane_lifecycle_readonly.load_lane_lifecycle_readonly"
)
_WORKSPACE_SEGMENT = (
    "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider"
    ".application.herdr_session_start.herdr_workspace_segment"
)
WORKSPACE = "wAcceptance"


def _parser():
    """The real root parser, so the acceptance is against the shipped argv surface."""
    from mozyo_bridge.application.cli import build_parser

    return build_parser()


@dataclass(frozen=True)
class _Issue:
    issue_id: str
    is_closed: bool = False
    is_leaf: bool = True
    tracker: Optional[str] = "開発"
    status_name: Optional[str] = "未着手"
    parent_id: Optional[str] = None


@dataclass(frozen=True)
class _Row:
    lane_id: str
    issue_id: str
    lane_disposition: str = "active"
    repo_workspace_id: str = WORKSPACE


@dataclass(frozen=True)
class _Bucket:
    issues: tuple


@dataclass(frozen=True)
class _Resolution:
    bucket: Optional[_Bucket]
    skip: object = None

    @property
    def resolved(self) -> bool:
        return self.bucket is not None


@dataclass(frozen=True)
class _Provider:
    resolution: _Resolution

    def resolve_bucket(self, _version_id):
        return self.resolution


@dataclass(frozen=True)
class _LiveRead:
    provider: _Provider
    version_id: str = "329"
    version_name: str = "v2.2.0 ハーネス/運用整備"
    project_identifier: str = "giken-3800-mozyo-bridge"
    project_id: int = 92
    issue_count: int = 0
    extras: tuple = field(default=())


class _Base(unittest.TestCase):
    def _run(self, argv, *, issues=(), rows=(), live_error=None):
        args = _parser().parse_args(argv)
        live = _LiveRead(provider=_Provider(_Resolution(_Bucket(tuple(issues)))))
        patches = [
            mock.patch(
                _LIVE_BUCKET,
                side_effect=live_error,
                **({} if live_error else {"return_value": live}),
            ),
            # ``rows=None`` models the loader's fail-closed return (unreadable / newer /
            # malformed schema), which is NOT the same as its ``()`` for an absent store.
            mock.patch(
                _LIFECYCLE, return_value=None if rows is None else tuple(rows)
            ),
            mock.patch(_WORKSPACE_SEGMENT, return_value=WORKSPACE),
        ]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = args.func(args)
        return code, out.getvalue(), err.getvalue()


class ArgvSurfaceTest(unittest.TestCase):
    def test_the_command_is_registered_on_the_shipped_parser(self):
        args = _parser().parse_args(["workflow", "version-track", "--version-id", "329"])
        self.assertEqual(args.version_id, "329")
        self.assertTrue(callable(getattr(args, "func", None)))

    def test_the_command_exposes_no_way_to_act(self):
        """The first slice is detection + presentation. The absence of an actuation flag
        is the guarantee, not a default the caller can flip: giving this layer execution
        authority has to be a reviewable change to the surface, not a flag."""
        parser = _parser().parse_args(
            ["workflow", "version-track", "--version-id", "329"]
        )
        forbidden = ("execute", "retire", "drain", "apply", "close", "mode")
        for name in forbidden:
            with self.subTest(option=name):
                self.assertFalse(
                    hasattr(parser, name),
                    f"version-track must not expose an actuation option: {name}",
                )


class ExitCodeTest(_Base):
    def test_missing_version_selector_exits_two(self):
        code, _, err = self._run(["workflow", "version-track"])
        self.assertEqual(code, 2)
        self.assertIn("--version-id", err)

    def test_a_snapshot_with_owed_lanes_still_exits_zero(self):
        """A finding is not a command failure.

        A non-zero exit here would collide with the unavailable signal, and the consuming
        loop could no longer tell "this Version owes a drain" from "I could not look".
        """
        code, out, _ = self._run(
            ["workflow", "version-track", "--version-id", "329", "--json"],
            issues=[_Issue("15842", is_closed=True, status_name="クローズ")],
            rows=[_Row("issue_15842_x", "15842")],
        )
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["counts"]["drain_owed"], 1)
        self.assertEqual(payload["attention"][0]["issue_id"], "15842")

    def test_a_clean_version_exits_zero(self):
        code, out, _ = self._run(
            ["workflow", "version-track", "--version-id", "329", "--json"],
            issues=[_Issue("15844")],
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["counts"]["drain_owed"], 0)

    def test_an_unreadable_redmine_authority_exits_non_zero(self):
        from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_version_issue_source import (  # noqa: E501
            RedmineVersionReadUnavailable,
        )

        code, out, _ = self._run(
            ["workflow", "version-track", "--version-id", "999", "--json"],
            live_error=RedmineVersionReadUnavailable(
                "the requested Version is not among the ones the project can see",
                reason="version_not_found",
            ),
        )
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual(payload["state"], "unavailable")
        # The typed reason travels, so the operator is not left to guess which axis failed.
        self.assertIn("version_not_found", payload["detail"])

    def test_an_unreadable_lifecycle_authority_exits_non_zero(self):
        code, out, _ = self._run(
            ["workflow", "version-track", "--version-id", "329", "--json"],
            issues=[_Issue("15844")],
            rows=None,
        )
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["state"], "unavailable")

    def test_unavailable_text_output_goes_to_stderr(self):
        from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_version_issue_source import (  # noqa: E501
            RedmineVersionReadUnavailable,
        )

        code, out, err = self._run(
            ["workflow", "version-track", "--version-id", "999"],
            live_error=RedmineVersionReadUnavailable("nope", reason="project_unresolved"),
        )
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("unavailable", err)


class EnvelopeTest(_Base):
    def test_the_json_envelope_is_pasteable_into_a_journal(self):
        code, out, _ = self._run(
            ["workflow", "version-track", "--version-id", "329", "--json"],
            issues=[
                _Issue("15842", is_closed=True, status_name="クローズ"),
                _Issue("15844"),
            ],
            rows=[_Row("issue_15842_x", "15842"), _Row("issue_15110_x", "15110")],
        )
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["version_id"], "329")
        self.assertEqual(payload["issue_count"], 2)
        # The Version-scoped pass always states what it did NOT scope to.
        self.assertEqual(payload["unscoped_lane_count"], 1)
        self.assertEqual(payload["unscoped_lanes"][0]["lane_id"], "issue_15110_x")

    def test_text_output_names_the_existing_rail_entry_point(self):
        code, out, _ = self._run(
            ["workflow", "version-track", "--version-id", "329"],
            issues=[_Issue("15842", is_closed=True, status_name="クローズ")],
            rows=[_Row("issue_15842_x", "15842")],
        )
        self.assertEqual(code, 0)
        self.assertIn(
            "sublane reboot-audit --lane-label issue_15842_x",
            out,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
