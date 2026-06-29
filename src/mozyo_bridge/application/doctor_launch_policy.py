"""Doctor claude-launch-policy section boundary (#12835).

The ``doctor_claude_launch_policy_section`` collector historically mixed two
responsibilities in one free-function body: the *external read* that introspects
the cockpit / sublane Claude launch policy (env + policy default) and the
*verdict authority* that maps that policy view to the section ``status`` and the
operator ``next_action`` guidance. This module carves the collector slice out of
the ``doctor`` body into an OOP-first boundary (#12638 / #12833 follow-up):

- :class:`LaunchPolicySectionVerdict` is the typed value object for the verdict
  (status + the ordered next-action guidance).
- :func:`evaluate_launch_policy_section` is the pure domain policy that decides
  the verdict from a policy view alone (no env access, no I/O).
- :class:`LaunchPolicyReads` is the port for reading the launch-policy view and
  :class:`LiveLaunchPolicyReads` is the live adapter over
  ``describe_launch_policy``.
- :class:`LaunchPolicySectionUseCase` composes the port and the policy and
  re-assembles the legacy section dict byte-for-byte.

The live adapter resolves ``describe_launch_policy`` *at call time* (a localized
lazy import), mirroring :class:`mozyo_bridge.application.doctor_health.LiveDoctorSections`.
The pure policy / value object can now be specified directly with a synthetic
policy view — no ``os.environ`` patching and no ``doctor.*`` / ``commands.*``
monkeypatch is needed to test the verdict authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.claude_permission_policy import (
    SOURCE_ENV_INVALID,
    SOURCE_ENV_OVERRIDE,
)


# The diagnostic scope blurb is a constant, not a verdict input: the launch
# policy is non-retroactive, so the section always describes future panes only.
SECTION_SCOPE = "future cockpit / sublane managed Claude panes (non-retroactive)"


@dataclass(frozen=True)
class LaunchPolicySectionVerdict:
    """Typed verdict for the ``claude_launch_policy`` doctor section.

    ``status`` mirrors the historical section status (``ok`` when future cockpit
    / sublane Claude panes reproducibly launch ``auto``; ``warning`` otherwise).
    ``next_action`` carries the ordered operator guidance (empty when ``ok``).
    """

    status: str
    next_action: tuple[str, ...] = ()


def evaluate_launch_policy_section(
    policy: Mapping[str, Any]
) -> LaunchPolicySectionVerdict:
    """Pure policy: derive the section verdict from a launch-policy view.

    The view is the mapping returned by ``describe_launch_policy`` (env
    observation + effective mode + source token). This preserves the legacy
    branching: an invalid env override warns, a reproducible-auto policy is ok,
    an explicit non-auto override warns with the override rail, and a build with
    no auto policy warns that future panes will not launch auto.
    """

    source = policy["source"]
    if source == SOURCE_ENV_INVALID:
        # Would `die()` at actual launch — surface it as a warning here.
        return LaunchPolicySectionVerdict(
            status="warning",
            next_action=(
                f"{policy['env_var']}={policy['env_value']!r} is not a valid Claude "
                "permission mode; future cockpit / sublane Claude panes will fail to "
                "launch until it is unset or set to a valid mode (auto recommended)",
            ),
        )
    if policy["reproducible_auto"]:
        return LaunchPolicySectionVerdict(status="ok")
    # Either an explicit non-auto env override, or no auto policy at all.
    if source == SOURCE_ENV_OVERRIDE:
        return LaunchPolicySectionVerdict(
            status="warning",
            next_action=(
                f"{policy['env_var']}={policy['env_value']!r} overrides the cockpit "
                "auto policy; future cockpit / sublane Claude panes will launch "
                f"`--permission-mode {policy['effective_mode']}` instead of auto. "
                "Unset it to restore reproducible auto mode",
            ),
        )
    return LaunchPolicySectionVerdict(
        status="warning",
        next_action=(
            "future cockpit / sublane Claude panes will not launch in auto "
            "mode; this build has no auto launch policy configured",
        ),
    )


@runtime_checkable
class LaunchPolicyReads(Protocol):
    """Port: read the cockpit / sublane Claude launch-policy view.

    Implementations own the external read (env + policy default). The use case
    and policy depend only on the returned policy-view mapping.
    """

    def describe(self) -> dict[str, Any]:
        ...


class LiveLaunchPolicyReads:
    """Live adapter: introspect the launch policy via ``describe_launch_policy``.

    The function is resolved through a localized lazy import *at call time* so
    the read stays cheap at module import and mirrors the call-time resolution
    discipline of ``doctor_health.LiveDoctorSections``.
    """

    def describe(self) -> dict[str, Any]:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.claude_permission_policy import (  # noqa: E501
            describe_launch_policy,
        )

        return describe_launch_policy()


class LaunchPolicySectionUseCase:
    """Use case: read the launch-policy view, apply the verdict policy.

    Returns the legacy ``doctor_claude_launch_policy_section`` dict shape
    byte-for-byte so the ``run_doctor`` aggregation, JSON output, and
    ``format_doctor_text`` rendering are unchanged.
    """

    def __init__(self, reads: LaunchPolicyReads) -> None:
        self._reads = reads

    def execute(self) -> dict[str, Any]:
        policy = self._reads.describe()
        verdict = evaluate_launch_policy_section(policy)
        return {
            "status": verdict.status,
            "scope": SECTION_SCOPE,
            "effective_mode": policy["effective_mode"],
            "source": policy["source"],
            "reproducible_auto": policy["reproducible_auto"],
            "env_var": policy["env_var"],
            "env_present": policy["env_present"],
            "env_value": policy["env_value"],
            "policy_default": policy["policy_default"],
            "next_action": list(verdict.next_action),
        }
