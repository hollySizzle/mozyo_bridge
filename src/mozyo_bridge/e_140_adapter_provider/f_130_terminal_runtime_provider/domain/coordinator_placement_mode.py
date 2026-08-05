"""Operator-scoped coordinator role placement mode (Redmine #14139 / #14996).

The closed vocabulary + fail-closed field contract for *where coordinator
pairs are placed* on the herdr terminal — an **operator-scoped
home-level** knob, deliberately distinct from the repo-committed
``lane_placement`` (Redmine #13646, pane geometry) and from the two placement
*axes* (#13380 dedicated sublane host workspace, #13411 lane=tab) that this
module leaves untouched.

Why operator-scoped (home-level), not repo-committed
----------------------------------------------------
Two operators legitimately disagree about the SAME repos: one wants every
project's coordinator pair in ONE herdr workspace to oversee them all at once
(the tmux-era overview), another wants a per-project workspace they switch
between on a small monitor. Committing the choice into ``.mozyo-bridge/config``
would make one operator's preference collide across N repos and let a committed
value override an operator's private choice (portable value vs operator-private
boundary). So the mode lives at the mozyo-bridge *home* root, per operator, and
is never committed. The repo keeps only the pair-internal geometry
(``lane_placement``, #13646/#13647).

Closed vocabulary (unknown value fails closed)
----------------------------------------------
- :data:`PER_PROJECT_SPACE` — the historical placement: the coordinator pair
  lives in its own project workspace (#13380). This is the default when the file
  is absent, so an operator who never opts in launches byte-for-byte as before.
- :data:`SHARED_SPACE` — every project's coordinator pair joins ONE stable
  *shared coordinators* herdr workspace, each project a column; the resolver and
  the launch site implement it (``herdr_lane_topology._shared_coordinator_target``).
- :data:`ROLE_GROUPED_SPACE` — the explicitly identified top coordinator keeps
  its dedicated workspace, every other project coordinator joins one shared
  workspace, and implementation lanes keep the existing per-project sublane
  host placement.

Any other ``mode`` string — or an unsupported ``version`` / an unknown key / a
non-mapping record — raises :class:`CoordinatorPlacementError` (fail-closed): a
future, not-yet-understood shape never reads as ``per_project_space`` by
accident.

Launch-time only
----------------
The mode is a launch/adopt-time policy: it decides where a *fresh* managed pair
launch or an adopt lands. It never moves an already-live pair (herdr
rejects same-tab re-split; live re-placement is the live-relayout runbook only,
#13648). This module is pure — it parses and validates a record; the home-file
IO lives in the application loader, and the placement decision in the topology
core.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

#: The coordinator pair lives in its own project workspace (the #13380 default).
PER_PROJECT_SPACE = "per_project_space"

#: Every project's coordinator pair joins one stable shared coordinators
#: workspace, each project a column (Redmine #14139).
SHARED_SPACE = "shared_space"

#: The top coordinator is dedicated, project coordinators share one workspace,
#: and implementation lanes retain their per-project host (Redmine #14996).
ROLE_GROUPED_SPACE = "role_grouped_space"

#: The closed placement-mode vocabulary. Any other value fails closed.
COORDINATOR_PLACEMENT_MODES: frozenset[str] = frozenset(
    {PER_PROJECT_SPACE, ROLE_GROUPED_SPACE, SHARED_SPACE}
)

#: The behavior-preserving default (file absent / mode unset): the historical
#: per-project placement, byte-for-byte the pre-#14139 launch.
DEFAULT_COORDINATOR_PLACEMENT_MODE = PER_PROJECT_SPACE

#: The supported record version. Kept small and self-contained (like the sibling
#: ``lane_placement`` version); any other value is rejected so a future schema
#: never reads as version 1.
COORDINATOR_PLACEMENT_CONFIG_VERSION: int = 1

#: The closed set of recognized top-level keys inside the operator placement
#: record: an optional ``version``, the ``mode`` knob, and the stable logical
#: workspace id that identifies the one top coordinator in role-grouped mode.
#: Deliberately minimal — this operator file carries placement identity only and
#: never any routing / target / credential / approval surface (and it never adopts a
#: ``pane``-shaped live-addressing key).
COORDINATOR_PLACEMENT_KEYS: frozenset[str] = frozenset(
    {"version", "mode", "top_workspace_id"}
)


class CoordinatorPlacementError(ValueError):
    """An operator placement record violates the closed schema (fail-closed).

    Inherits :class:`ValueError` for fail-closed semantics, matching the sibling
    repo-local domain errors. The application loader re-raises its own IO / parse
    failures as a subclass so a single ``except CoordinatorPlacementError`` at the
    call site catches schema, parse, and IO failures alike.
    """


@dataclass(frozen=True)
class CoordinatorPlacementConfig:
    """The operator-scoped coordinator placement mode (Redmine #14139) — field contract.

    Value field:

    - :attr:`mode` — a :data:`COORDINATOR_PLACEMENT_MODES` value. Defaults to
      :data:`DEFAULT_COORDINATOR_PLACEMENT_MODE` (``per_project_space``), the
      historical placement, so an operator with no file launches unchanged.
    - :attr:`top_workspace_id` — the exact stable logical workspace-registry id
      of the one top coordinator. Required only by ``role_grouped_space`` and
      rejected as an inert value in every other mode.

    Boundary, kept enforced in code (this is *placement intent*, not authority):

    - **Launch-time only.** The mode decides where a fresh launch / adopt lands;
      it never moves an already-live pair (Non-goal: no live relayout).
    - **Placement only.** ``mode`` names a placement strategy; the record can
      never address a live pane / target / route, name an executable, or grant
      approval / close / send authority.
    - **Default-preserving.** No file ⇒ default ⇒ ``per_project_space``, so an
      operator who never opts in launches exactly as before.
    """

    version: int = COORDINATOR_PLACEMENT_CONFIG_VERSION
    mode: str = DEFAULT_COORDINATOR_PLACEMENT_MODE
    top_workspace_id: str = ""

    def __post_init__(self) -> None:
        # Validate on construction too, so a directly-built config is checked as
        # thoroughly as one parsed from a record (no dataclass back door — review
        # j#83383 F3 / Design Answer j#83385 Decision 3: a direct
        # ``CoordinatorPlacementConfig(version=2)`` must fail closed exactly like a
        # record carrying that version, so both the version and the mode are checked
        # here, not only in ``from_record``).
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise CoordinatorPlacementError(
                f"operator coordinator placement 'version' must be an integer, got "
                f"{self.version!r}"
            )
        if self.version != COORDINATOR_PLACEMENT_CONFIG_VERSION:
            raise CoordinatorPlacementError(
                f"unsupported operator coordinator placement version {self.version!r}; "
                f"this build understands version {COORDINATOR_PLACEMENT_CONFIG_VERSION}"
            )
        if self.mode not in COORDINATOR_PLACEMENT_MODES:
            raise CoordinatorPlacementError(
                f"operator coordinator placement 'mode' must be one of "
                f"{sorted(COORDINATOR_PLACEMENT_MODES)}, got {self.mode!r}"
            )
        if not isinstance(self.top_workspace_id, str):
            raise CoordinatorPlacementError(
                "operator coordinator placement 'top_workspace_id' must be a string, "
                f"got {type(self.top_workspace_id).__name__}"
            )
        if self.top_workspace_id != self.top_workspace_id.strip():
            raise CoordinatorPlacementError(
                "operator coordinator placement 'top_workspace_id' must be the exact "
                "stable workspace id without surrounding whitespace"
            )
        if self.mode == ROLE_GROUPED_SPACE and not self.top_workspace_id:
            raise CoordinatorPlacementError(
                "operator coordinator placement mode 'role_grouped_space' requires "
                "non-empty 'top_workspace_id'; obtain it from `mozyo-bridge workspace "
                "inspect --repo <top-repo> --json`"
            )
        if self.mode != ROLE_GROUPED_SPACE and self.top_workspace_id:
            raise CoordinatorPlacementError(
                "operator coordinator placement 'top_workspace_id' is valid only when "
                "mode is 'role_grouped_space'; remove the inert authority or select that "
                "mode"
            )

    @classmethod
    def default(cls) -> "CoordinatorPlacementConfig":
        """The behavior-preserving default: the historical per-project placement."""
        return cls()

    @classmethod
    def from_record(
        cls, record: "Mapping[str, object] | None" = None
    ) -> "CoordinatorPlacementConfig":
        """Normalize an operator placement record into a typed policy (fail-closed).

        ``None`` or an empty mapping yields the behavior-preserving default. A
        non-mapping record, an unknown key, an unsupported / non-integer
        ``version``, or an unknown ``mode`` value raises
        :class:`CoordinatorPlacementError`.
        """
        if record is None:
            return cls.default()
        if not isinstance(record, Mapping):
            raise CoordinatorPlacementError(
                "operator coordinator placement record must be a mapping (a YAML "
                f"table), got {type(record).__name__}"
            )
        for key in record:
            if not isinstance(key, str) or key not in COORDINATOR_PLACEMENT_KEYS:
                raise CoordinatorPlacementError(
                    f"operator coordinator placement record has unknown key {key!r}; "
                    f"allowed keys: {sorted(COORDINATOR_PLACEMENT_KEYS)}"
                )
        version = record.get("version", COORDINATOR_PLACEMENT_CONFIG_VERSION)
        if isinstance(version, bool) or not isinstance(version, int):
            raise CoordinatorPlacementError(
                f"operator coordinator placement record 'version' must be an integer, "
                f"got {version!r}"
            )
        if version != COORDINATOR_PLACEMENT_CONFIG_VERSION:
            raise CoordinatorPlacementError(
                f"unsupported operator coordinator placement record version {version!r}; "
                f"this build understands version {COORDINATOR_PLACEMENT_CONFIG_VERSION}"
            )
        mode = record.get("mode", DEFAULT_COORDINATOR_PLACEMENT_MODE)
        if not isinstance(mode, str) or mode not in COORDINATOR_PLACEMENT_MODES:
            raise CoordinatorPlacementError(
                f"operator coordinator placement 'mode' must be one of "
                f"{sorted(COORDINATOR_PLACEMENT_MODES)}, got {mode!r}"
            )
        top_workspace_id = record.get("top_workspace_id", "")
        if not isinstance(top_workspace_id, str):
            raise CoordinatorPlacementError(
                "operator coordinator placement 'top_workspace_id' must be a string, "
                f"got {type(top_workspace_id).__name__}"
            )
        return cls(
            version=version,
            mode=mode,
            top_workspace_id=top_workspace_id,
        )


__all__ = (
    "COORDINATOR_PLACEMENT_CONFIG_VERSION",
    "COORDINATOR_PLACEMENT_KEYS",
    "COORDINATOR_PLACEMENT_MODES",
    "DEFAULT_COORDINATOR_PLACEMENT_MODE",
    "PER_PROJECT_SPACE",
    "ROLE_GROUPED_SPACE",
    "SHARED_SPACE",
    "CoordinatorPlacementConfig",
    "CoordinatorPlacementError",
)
