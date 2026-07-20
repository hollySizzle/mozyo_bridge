"""Operator-scoped coordinator pair placement mode (Redmine #14139).

The closed vocabulary + fail-closed field contract for *where the coordinator
pair (default lane) is placed* on the herdr terminal — an **operator-scoped
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

Any other ``mode`` string — or an unsupported ``version`` / an unknown key / a
non-mapping record — raises :class:`CoordinatorPlacementError` (fail-closed): a
future, not-yet-understood shape never reads as ``per_project_space`` by
accident.

Launch-time only
----------------
The mode is a launch/adopt-time policy: it decides where a *fresh* coordinator
pair launch or an adopt lands. It never moves an already-live pair (herdr
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

#: The closed placement-mode vocabulary. Any other value fails closed.
COORDINATOR_PLACEMENT_MODES: frozenset[str] = frozenset({PER_PROJECT_SPACE, SHARED_SPACE})

#: The behavior-preserving default (file absent / mode unset): the historical
#: per-project placement, byte-for-byte the pre-#14139 launch.
DEFAULT_COORDINATOR_PLACEMENT_MODE = PER_PROJECT_SPACE

#: The supported record version. Kept small and self-contained (like the sibling
#: ``lane_placement`` version); any other value is rejected so a future schema
#: never reads as version 1.
COORDINATOR_PLACEMENT_CONFIG_VERSION: int = 1

#: The closed set of recognized top-level keys inside the operator placement
#: record: an optional ``version`` plus the single ``mode`` knob. Deliberately
#: minimal — this operator file carries the placement mode ONLY and never any
#: routing / target / credential / approval surface (and it never adopts a
#: ``pane``-shaped live-addressing key).
COORDINATOR_PLACEMENT_KEYS: frozenset[str] = frozenset({"version", "mode"})


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
        return cls(version=version, mode=mode)


__all__ = (
    "COORDINATOR_PLACEMENT_CONFIG_VERSION",
    "COORDINATOR_PLACEMENT_KEYS",
    "COORDINATOR_PLACEMENT_MODES",
    "DEFAULT_COORDINATOR_PLACEMENT_MODE",
    "PER_PROJECT_SPACE",
    "SHARED_SPACE",
    "CoordinatorPlacementConfig",
    "CoordinatorPlacementError",
)
