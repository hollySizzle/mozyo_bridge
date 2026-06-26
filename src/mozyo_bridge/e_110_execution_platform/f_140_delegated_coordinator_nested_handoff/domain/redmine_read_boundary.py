"""Redmine read-boundary classifier for delegated-coordinator inference (Redmine #12474).

The #12474 minimal-context smoke exposed a *read* failure mode distinct from the
routing failure modes: before a parent coordinator decides to delegate work to a
child project, it reads the durable Redmine anchor. If it reads **too little** it
cannot soundly infer the delegation, and if it reads **outside the bounded set**
(parent journals, sibling / related / recent scans, the management issue, prior
smoke journals) its inference is contaminated by context that the minimal-anchor
acceptance was meant to exclude (#12474 j#64160 / j#64172 / j#64185).

This module is the pure classifier that fixes that boundary. Given the surfaces a
receiver actually read, it classifies the read as :data:`CLASS_ALLOWED`,
:data:`CLASS_INSUFFICIENT`, or :data:`CLASS_CONTAMINATED`. The route planner
(:mod:`mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegated_coordinator_route_plan`) consumes the verdict
to **stop before the route decision** whenever the read is not ``allowed`` — a
contaminated or insufficient read can never be promoted into a PASS/route.

Bounded-read contract (Redmine #12474 j#64172, restated):

- the receiver may read **only** the sparse target issue body + its journals, and
  the target's **direct parent description** (description only); and
- it must **not** read the management issue, sibling / related / recent issue
  scans, the parent's journals or children, or prior smoke journals — reading any
  of those exceeds the parent-description-only boundary and classifies as
  ``contaminated``, never PASS/FAIL.

The module is pure (dataclasses + a classifier); it performs no I/O and never
reads Redmine itself — the caller passes the surfaces it observed, exactly as the
sibling delegation domains take their inputs from the durable record.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

# --- read surfaces ------------------------------------------------------------
# A surface names *what* was read, abstracted away from concrete issue ids so the
# classifier stays pure and reusable. The caller tags each read with one surface.

#: The target (dispatch-anchor) issue body — the sparse anchor the receiver must
#: read before acting ("read it from the source-of-truth system before acting").
SURFACE_TARGET_BODY = "target_body"
#: The target issue's own journals (in-scope: the dispatch journal lives here).
SURFACE_TARGET_JOURNALS = "target_journals"
#: The target's direct parent **description** only (in-scope, description-only).
SURFACE_PARENT_DESCRIPTION = "parent_description"

#: The bounded set of surfaces a context-free delegation inference may read.
ALLOWED_SURFACES: frozenset[str] = frozenset(
    {SURFACE_TARGET_BODY, SURFACE_TARGET_JOURNALS, SURFACE_PARENT_DESCRIPTION}
)

#: The parent issue's journals — beyond the parent-description-only boundary
#: (Redmine #12474 j#64185: a parent-detail response that includes parent journals
#: exceeds the boundary).
SURFACE_PARENT_JOURNALS = "parent_journals"
#: The parent issue's child list — same boundary violation as parent journals.
SURFACE_PARENT_CHILDREN = "parent_children"
#: A sibling-issue scan (e.g. other children of the same parent).
SURFACE_SIBLING_SCAN = "sibling_scan"
#: A related-issue scan (Redmine relations).
SURFACE_RELATED_SCAN = "related_scan"
#: A recently-updated-issues scan.
SURFACE_RECENT_SCAN = "recent_scan"
#: The management / coordination issue (e.g. #12474 itself) whose context-rich
#: journals the minimal-anchor acceptance excludes (Redmine #12474 j#64160).
SURFACE_MANAGEMENT_ISSUE = "management_issue"
#: Prior smoke / decision journals (#12473 / #12460 / earlier runs).
SURFACE_PRIOR_SMOKE = "prior_smoke"

#: Reading any of these contaminates the inference; the run is neither PASS nor
#: FAIL — it is ``contaminated`` and must not feed a route decision.
CONTAMINATING_SURFACES: frozenset[str] = frozenset(
    {
        SURFACE_PARENT_JOURNALS,
        SURFACE_PARENT_CHILDREN,
        SURFACE_SIBLING_SCAN,
        SURFACE_RELATED_SCAN,
        SURFACE_RECENT_SCAN,
        SURFACE_MANAGEMENT_ISSUE,
        SURFACE_PRIOR_SMOKE,
    }
)

#: The minimum a receiver must read to soundly infer the delegation: the target
#: anchor body. Reading less than this is ``insufficient`` — the durable anchor
#: was not actually read before acting.
REQUIRED_SURFACES: tuple[str, ...] = (SURFACE_TARGET_BODY,)

KNOWN_SURFACES: frozenset[str] = ALLOWED_SURFACES | CONTAMINATING_SURFACES

# --- classifications ----------------------------------------------------------

#: The read stayed within the bounded set and covered the required minimum: the
#: route decision may proceed on a sound, context-free inference.
CLASS_ALLOWED = "allowed"
#: The read stayed within the bounded set but missed a required surface: the
#: anchor was under-read, so the route decision must not proceed.
CLASS_INSUFFICIENT = "insufficient"
#: The read reached outside the bounded set: the inference is contaminated and
#: must not be promoted to PASS/FAIL or a route decision.
CLASS_CONTAMINATED = "contaminated"


class ReadBoundaryError(ValueError):
    """A read-access record references an unknown surface (fail closed)."""


@dataclass(frozen=True)
class ReadAccess:
    """One observed read, tagged with the :mod:`surface <redmine_read_boundary>`.

    ``surface`` is one of :data:`KNOWN_SURFACES`. ``issue_ref`` is an optional
    audit pointer (e.g. ``#12474`` / ``#12474 j#64131``) carried for the record
    only; it never affects classification.
    """

    surface: str
    issue_ref: str = ""


@dataclass(frozen=True)
class ReadBoundaryVerdict:
    """The classified read boundary for a delegation inference.

    ``classification`` is one of :data:`CLASS_ALLOWED` / :data:`CLASS_INSUFFICIENT`
    / :data:`CLASS_CONTAMINATED`. ``contaminating_surfaces`` lists the
    out-of-bounds surfaces that were read (sorted, stable) when contaminated;
    ``missing_required`` lists the required surfaces that were not read when
    insufficient. ``reason`` is a short replayable phrase.
    """

    classification: str
    reason: str
    contaminating_surfaces: tuple[str, ...] = ()
    missing_required: tuple[str, ...] = ()

    @property
    def is_allowed(self) -> bool:
        """True only when the read may feed a route decision."""
        return self.classification == CLASS_ALLOWED

    @property
    def is_contaminated(self) -> bool:
        return self.classification == CLASS_CONTAMINATED

    @property
    def is_insufficient(self) -> bool:
        return self.classification == CLASS_INSUFFICIENT

    def to_dict(self) -> dict[str, object]:
        return {
            "classification": self.classification,
            "reason": self.reason,
            "contaminating_surfaces": list(self.contaminating_surfaces),
            "missing_required": list(self.missing_required),
        }


def classify_read_boundary(reads: Sequence[ReadAccess]) -> ReadBoundaryVerdict:
    """Classify the surfaces a receiver read into the read-boundary verdict.

    Fail-closed precedence (contamination dominates):

    1. Any unknown surface raises :class:`ReadBoundaryError` — an unrecognized
       read cannot be silently treated as in-bounds.
    2. **Contaminated** wins over insufficient: if any out-of-bounds surface was
       read (:data:`CONTAMINATING_SURFACES`), the inference is contaminated even
       if the required minimum was also read — the out-of-bounds context has
       already polluted the decision (Redmine #12474 j#64160 / j#64185).
    3. **Insufficient**: every read was in-bounds but a :data:`REQUIRED_SURFACES`
       surface (the target anchor body) was not read — the durable anchor was not
       actually read before acting.
    4. **Allowed**: the read stayed in-bounds and covered the required minimum.

    Pure and deterministic over its inputs.
    """
    surfaces = [r.surface for r in reads]
    unknown = sorted({s for s in surfaces if s not in KNOWN_SURFACES})
    if unknown:
        raise ReadBoundaryError(
            "unknown read surface(s): "
            + ", ".join(unknown)
            + f"; expected one of {sorted(KNOWN_SURFACES)}"
        )

    present = set(surfaces)
    contaminating = tuple(s for s in sorted(CONTAMINATING_SURFACES) if s in present)
    if contaminating:
        return ReadBoundaryVerdict(
            classification=CLASS_CONTAMINATED,
            reason=(
                "read reached outside the bounded set (target body/journals + "
                "parent description only): "
                + ", ".join(contaminating)
                + ". The inference is contaminated and must not feed a route "
                "decision (Redmine #12474 j#64172/j#64185)."
            ),
            contaminating_surfaces=contaminating,
        )

    missing = tuple(s for s in REQUIRED_SURFACES if s not in present)
    if missing:
        return ReadBoundaryVerdict(
            classification=CLASS_INSUFFICIENT,
            reason=(
                "the durable anchor was under-read; missing required surface(s): "
                + ", ".join(missing)
                + ". Read the target anchor before any route decision."
            ),
            missing_required=missing,
        )

    return ReadBoundaryVerdict(
        classification=CLASS_ALLOWED,
        reason=(
            "read stayed within the bounded set and covered the required anchor; "
            "a context-free delegation inference may proceed"
        ),
    )


__all__ = (
    "SURFACE_TARGET_BODY",
    "SURFACE_TARGET_JOURNALS",
    "SURFACE_PARENT_DESCRIPTION",
    "SURFACE_PARENT_JOURNALS",
    "SURFACE_PARENT_CHILDREN",
    "SURFACE_SIBLING_SCAN",
    "SURFACE_RELATED_SCAN",
    "SURFACE_RECENT_SCAN",
    "SURFACE_MANAGEMENT_ISSUE",
    "SURFACE_PRIOR_SMOKE",
    "ALLOWED_SURFACES",
    "CONTAMINATING_SURFACES",
    "REQUIRED_SURFACES",
    "KNOWN_SURFACES",
    "CLASS_ALLOWED",
    "CLASS_INSUFFICIENT",
    "CLASS_CONTAMINATED",
    "ReadBoundaryError",
    "ReadAccess",
    "ReadBoundaryVerdict",
    "classify_read_boundary",
)
