"""`sublane create --lane-kind` operator vocabulary (Redmine #13647 Tranche 1b).

Unit (`tests-placement-discovery-policy.md` 配置決定木 4): the single real collaborator is
the composed argument parser — no store, no launch, no I/O. It pins that the operator-facing
surface accepts exactly the canonical three tokens and rejects the 親 / 子 / 孫 aliases, so
the machine vocabulary cannot grow through the CLI (disposition j#85650 P3).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.lane_kind import (  # noqa: E402
    LANE_KIND_COORDINATOR,
    LANE_KIND_DELEGATED_COORDINATOR,
    LANE_KIND_IMPLEMENTATION,
)

ISSUE = "13647"
LANE = "issue_13647_lane"


def _parse(*extra: str):
    from mozyo_bridge.application.cli import build_parser

    return build_parser().parse_args(
        ["sublane", "create", "--issue", ISSUE, "--lane-label", LANE, *extra]
    )


class SublaneCreateLaneKindCliTest(unittest.TestCase):
    def test_each_canonical_token_parses(self) -> None:
        for token in (
            LANE_KIND_COORDINATOR,
            LANE_KIND_DELEGATED_COORDINATOR,
            LANE_KIND_IMPLEMENTATION,
        ):
            self.assertEqual(_parse("--lane-kind", token).lane_kind, token)

    def test_alias_tokens_are_rejected(self) -> None:
        # Owner-facing docs may render 親 / 子 / 孫; the machine vocabulary does not grow.
        for alias in ("parent", "child", "grandchild", "coordinator_assistant"):
            with self.assertRaises(SystemExit):
                _parse("--lane-kind", alias)

    def test_omitting_the_flag_records_no_kind(self) -> None:
        # The pre-#13647 invocation: no durable kind fact -> lane_class geometry.
        self.assertEqual(_parse().lane_kind, "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
