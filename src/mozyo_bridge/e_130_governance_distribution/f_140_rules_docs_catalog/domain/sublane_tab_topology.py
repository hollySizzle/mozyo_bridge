"""Repo-local sublane tab topology (Redmine #14567, Design Answer j#91144 Decision 1).

The closed vocabulary + fail-closed field contract for **which herdr tab a non-default
lane's pair is placed in**, inside the single sublane host workspace (#13380). It is the
self-contained sibling of :mod:`...domain.lane_placement`, exactly like
:mod:`...domain.agent_launch_argv`: the composing
:class:`~...domain.repo_local_config.RepoLocalConfig` stays a thin field contract, and the
governance-config module stays within its module-health budget.

Why a separate block from ``lane_placement``
--------------------------------------------
The two answer different questions and are deliberately never merged (Design Answer
j#91144 Decision 1, restating the boundary
``vibes/docs/specs/herdr-native-identity.md`` §5.1 "Boundary" already declared):

- ``lane_placement`` — *how the pair is split INSIDE its container* (direction + provider
  order). Unchanged by this block.
- ``sublane_tab_topology`` — *which container the lane goes in* (one tab per lane, or one
  tab shared by every lane of the project).

The block name is deliberately narrow. A wider ``lane_topology`` was rejected because a
name that broad reads as owning lane lifecycle / routing / workspace placement too; the
same reasoning that named ``lane_placement`` rather than ``pane_placement`` — pin the
responsibility in the machine key itself.

Closed vocabulary (unknown value fails closed)
----------------------------------------------
- :data:`PER_LANE_TAB` — the #13411 placement: every non-default lane occupies its OWN
  dedicated tab. This is the **product default**, so a workspace that declares nothing
  launches byte-for-byte as before (Design Answer j#91144 Decision 2). Unlike
  ``lane_placement`` — whose undeclared fields resolve to a product default that
  deliberately changed the geometry (#14568) — this block IS behavior-preserving when
  undeclared: owner intent asks the dogfood repo to opt in explicitly, not every adopter
  to be moved.
- :data:`SHARED_TAB` — every non-default lane of the project joins ONE shared tab in the
  sublane host workspace, each lane a column, so an operator sees every lane without
  switching tabs (owner intent 2026-07-27).

Any other ``mode`` string — or an unsupported ``version`` / an unknown key / a non-mapping
record — raises :class:`SublaneTabTopologyError` (fail-closed): a future, not-yet-understood
shape never reads as ``per_lane_tab`` by accident. That fail-closed posture is also what a
launcher predating this block relies on: its parser rejects the unknown top-level key, and
the #14258 config-parse preflight turns that into a zero-side-effect refusal *before* any
workspace / tab / agent is created (Design Answer j#91144 Decision 5).

Launch-time only
----------------
The mode decides where a *fresh* launch or a heal lands. It never moves an already-live
pair (herdr rejects same-tab re-split; live re-placement is #14605 / the #13648 relayout
runbook). This module is pure — it parses and validates a record; the tab resolution lives
in :mod:`...application.herdr_shared_tab` and the launch in ``prepare_session``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

#: Every non-default lane occupies its own dedicated herdr tab (the #13411 placement).
PER_LANE_TAB = "per_lane_tab"

#: Every non-default lane of the project joins ONE shared tab in the sublane host
#: workspace (Redmine #14567).
SHARED_TAB = "shared_tab"

#: The closed tab-topology vocabulary. Any other value fails closed.
SUBLANE_TAB_TOPOLOGY_MODES: frozenset[str] = frozenset({PER_LANE_TAB, SHARED_TAB})

#: The behavior-preserving default (block absent / mode unset): the historical
#: lane-per-tab placement, byte-for-byte the pre-#14567 launch (Design Answer j#91144
#: Decision 2 — the compatibility evaluation the issue's Close conditions asked for).
DEFAULT_SUBLANE_TAB_TOPOLOGY_MODE = PER_LANE_TAB

#: The supported record version. Kept small and self-contained (like the sibling
#: ``lane_placement`` / ``coordinator_placement_mode`` versions) so neither layer depends
#: on the other; any other value is rejected so a future schema never reads as version 1.
#: It is independent of the repo config's own top-level ``version``.
SUBLANE_TAB_TOPOLOGY_CONFIG_VERSION: int = 1

#: The top-level repo-local config key carrying this block.
SUBLANE_TAB_TOPOLOGY_KEY = "sublane_tab_topology"

#: The closed set of recognized keys inside the block: an optional ``version`` plus the
#: single ``mode`` knob. Deliberately minimal — this block carries *tab placement intent
#: only* and never any routing / target / approval / close / send authority.
SUBLANE_TAB_TOPOLOGY_KEYS: frozenset[str] = frozenset({"version", "mode"})


class SublaneTabTopologyError(ValueError):
    """A ``sublane_tab_topology`` block violates the closed schema (fail-closed).

    Inherits :class:`ValueError` for fail-closed semantics, matching the sibling
    repo-local domain errors. The composing :class:`RepoLocalConfig` re-raises this as its
    own ``RepoLocalConfigError`` so the repo-local config loader keeps a single
    fail-closed boundary.
    """


@dataclass(frozen=True)
class SublaneTabTopologyConfig:
    """The repo-local sublane tab topology (Redmine #14567) — field contract.

    Value field:

    - :attr:`mode` — a :data:`SUBLANE_TAB_TOPOLOGY_MODES` value. Defaults to
      :data:`DEFAULT_SUBLANE_TAB_TOPOLOGY_MODE` (``per_lane_tab``), the historical
      placement, so a repo that never opts in launches unchanged.

    Boundary, kept enforced in code (this is *placement intent*, not authority):

    - **Launch-time only.** The mode decides where a fresh launch / heal lands; it never
      moves an already-live pair (Non-goal: no live relayout — that is #14605).
    - **Placement only.** ``mode`` names a tab-placement strategy; the block can never
      address a live pane / target / route, name an executable, or grant approval / close /
      send authority. The repo-local schema boundary
      (:data:`...repo_local_config_records._FORBIDDEN_KEY_PARTS`) screens the key for those
      shapes before the allowed-key check.
    - **Default-preserving.** No block ⇒ default ⇒ ``per_lane_tab``, so a repo that never
      opts in launches exactly as before. This is the deliberate DIFFERENCE from
      ``lane_placement``, whose undeclared fields resolve to a product default that changed
      the geometry (#14568).
    """

    version: int = SUBLANE_TAB_TOPOLOGY_CONFIG_VERSION
    mode: str = DEFAULT_SUBLANE_TAB_TOPOLOGY_MODE

    def __post_init__(self) -> None:
        # Validate on construction too, so a directly-built config is checked as thoroughly
        # as one parsed from a record (no dataclass back door — the same requirement
        # Redmine #14139 review j#83383 F3 pinned on the sibling operator placement
        # config: a direct ``SublaneTabTopologyConfig(version=2)`` must fail closed exactly
        # like a record carrying that version).
        _check_version(self.version, source="sublane tab topology config")
        _check_mode(self.mode, source="sublane tab topology config")

    @property
    def shared_tab(self) -> bool:
        """True iff this repo places every lane in ONE shared tab (Redmine #14567).

        The single predicate every launch-side caller asks, so no call site re-compares
        the mode literal and a future third mode cannot silently read as ``shared_tab``
        at one site and not another.
        """
        return self.mode == SHARED_TAB

    @classmethod
    def default(cls) -> "SublaneTabTopologyConfig":
        """The behavior-preserving default: the historical lane-per-tab placement."""
        return cls()

    @classmethod
    def from_record(
        cls, record: "Mapping[str, object] | None" = None
    ) -> "SublaneTabTopologyConfig":
        """Normalize a ``sublane_tab_topology`` record into a typed policy (fail-closed).

        ``None`` or an empty mapping yields the behavior-preserving default. A non-mapping
        record, an unknown key, an unsupported / non-integer ``version``, or an unknown
        ``mode`` value raises :class:`SublaneTabTopologyError`.
        """
        if record is None:
            return cls.default()
        if not isinstance(record, Mapping):
            raise SublaneTabTopologyError(
                "sublane tab topology config record must be a mapping (a YAML table), got "
                f"{type(record).__name__}"
            )
        for key in record:
            if not isinstance(key, str) or key not in SUBLANE_TAB_TOPOLOGY_KEYS:
                raise SublaneTabTopologyError(
                    f"sublane tab topology config record has unknown key {key!r}; allowed "
                    f"keys: {sorted(SUBLANE_TAB_TOPOLOGY_KEYS)}"
                )
        version = record.get("version", SUBLANE_TAB_TOPOLOGY_CONFIG_VERSION)
        _check_version(version, source="sublane tab topology config record")
        mode = record.get("mode", DEFAULT_SUBLANE_TAB_TOPOLOGY_MODE)
        _check_mode(mode, source="sublane tab topology config")
        return cls(version=version, mode=mode)


def _check_version(version: object, *, source: str) -> None:
    """Fail closed unless ``version`` is exactly the supported integer version.

    ``bool`` is rejected explicitly: it is an ``int`` subclass, so ``True`` would otherwise
    read as version 1.
    """
    if isinstance(version, bool) or not isinstance(version, int):
        raise SublaneTabTopologyError(
            f"{source} 'version' must be an integer, got {version!r}"
        )
    if version != SUBLANE_TAB_TOPOLOGY_CONFIG_VERSION:
        raise SublaneTabTopologyError(
            f"unsupported {source} version {version!r}; this build understands version "
            f"{SUBLANE_TAB_TOPOLOGY_CONFIG_VERSION}"
        )


def _check_mode(mode: object, *, source: str) -> None:
    """Fail closed unless ``mode`` is a :data:`SUBLANE_TAB_TOPOLOGY_MODES` literal."""
    if not isinstance(mode, str) or mode not in SUBLANE_TAB_TOPOLOGY_MODES:
        raise SublaneTabTopologyError(
            f"{source} 'mode' must be one of {sorted(SUBLANE_TAB_TOPOLOGY_MODES)}, got "
            f"{mode!r}"
        )


__all__ = (
    "DEFAULT_SUBLANE_TAB_TOPOLOGY_MODE",
    "PER_LANE_TAB",
    "SHARED_TAB",
    "SUBLANE_TAB_TOPOLOGY_CONFIG_VERSION",
    "SUBLANE_TAB_TOPOLOGY_KEY",
    "SUBLANE_TAB_TOPOLOGY_KEYS",
    "SUBLANE_TAB_TOPOLOGY_MODES",
    "SublaneTabTopologyConfig",
    "SublaneTabTopologyError",
)
