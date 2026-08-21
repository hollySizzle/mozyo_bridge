"""Where a host-local exception-diagnostic sink may live — fail-closed (Redmine #15840).

Design record: ``vibes/docs/logics/exception-diagnostic-sink-boundary.md``.

The boundary that doc fixes is "raw may be held host-local; it may never be copied into a
durable record". A sink that lands *inside* a forbidden surface breaks that boundary before a
single byte is written, so the location has to be **enforced**, not merely documented.

Review j#109680 ``finding_xdgforbiddenoverlap`` is why this module exists. The first version of
the design named ``${XDG_STATE_HOME:-~/.local/state}/mozyo-bridge/diagnostics/`` and argued it
was outside the guarded home because ``ambient_homes()`` returns only ``~/.mozyo_bridge`` and
``$MOZYO_BRIDGE_HOME``. That argument only holds for the DEFAULT value of ``XDG_STATE_HOME``,
which is environment input. The review reproduced the consequence:

    MOZYO_BRIDGE_HOME=/tmp/review-15840/guarded
    XDG_STATE_HOME=/tmp/review-15840/guarded
    -> sink = /tmp/review-15840/guarded/mozyo-bridge/diagnostics   (inside a guarded home)

It is the same mistake as the one review j#109671 found in the class-name claim: an observation
about the typical case, written as if it were a structural guarantee. Enumerating forbidden
surfaces in a table does nothing unless something refuses to resolve into them.

**Pure, and deliberately without an opinion on who the forbidden roots are.** The roots arrive as
an argument rather than being read from :func:`...test_home_fence.ambient_homes` here, so the
runtime diagnostic path does not depend on the CI-verification machinery. The wiring is the sink
slice's job — and if that wiring is ever forgotten, the root set arrives empty and
:func:`resolve_diagnostic_sink_root` refuses (rule 3), which is the failure direction we want.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

#: No candidate was offered.
SINK_NO_CANDIDATE = "no_candidate"
#: The candidate is not an absolute path, so "is it inside a forbidden root" is not answerable.
SINK_NOT_ABSOLUTE = "candidate_not_absolute"
#: No forbidden roots were supplied. Refusing is the point: an empty set means the caller has not
#: told us what to stay out of, not that there is nothing to stay out of.
SINK_NO_FORBIDDEN_ROOTS = "forbidden_roots_unknown"
#: A path could not be canonicalized, so the comparison cannot be trusted.
SINK_UNCANONICALIZABLE = "path_not_canonicalizable"
#: A forbidden root is relative (Redmine #15840 review j#109685 ``finding_relativeforbiddenroot``).
#: ``Path("repo").resolve()`` silently anchors on the process working directory, so a caller that
#: passes a relative repo root gets a root that is NOT the one it meant — and candidates under the
#: real repo sail through. The candidate's absoluteness was required from the start; the roots'
#: was not. That asymmetry was fail-OPEN and the review reproduced it.
SINK_FORBIDDEN_ROOT_NOT_ABSOLUTE = "forbidden_root_not_absolute"
#: The candidate IS one of the forbidden roots.
SINK_IS_FORBIDDEN_ROOT = "candidate_is_forbidden_root"
#: The candidate lives under a forbidden root.
SINK_INSIDE_FORBIDDEN_ROOT = "candidate_inside_forbidden_root"


@dataclass(frozen=True)
class SinkLocation:
    """Where the diagnostic sink may be written, or why it may not be written at all.

    ``root`` is populated only when ``admissible`` is true. A refusal is not a degraded mode:
    the caller writes nothing. Losing a diagnostic is strictly less bad than writing raw
    exception text into a guarded home or a committable worktree.
    """

    admissible: bool
    root: Optional[Path] = None
    reason: str = ""
    detail: str = ""

    def as_payload(self) -> dict:
        return {
            "admissible": self.admissible,
            "root": str(self.root) if self.root is not None else "",
            "reason": self.reason,
            "detail": self.detail,
        }


def _refused(reason: str, detail: str) -> SinkLocation:
    return SinkLocation(admissible=False, reason=reason, detail=detail)


def _canonical(path: Path) -> Optional[Path]:
    """``path`` with ``~`` expanded and symlinks resolved, or ``None`` if that cannot be done.

    Resolution is non-strict, so a sink root that does not exist yet still canonicalizes — but
    any symlinked ancestor that DOES exist is followed. That matters: comparing unresolved
    strings would let a symlink point a "safe" candidate straight into a guarded home.
    """
    try:
        return Path(path).expanduser().resolve()
    except (OSError, ValueError, RuntimeError):
        return None


def _is_within(candidate: Path, root: Path) -> Optional[bool]:
    """Is ``candidate`` equal to, or under, ``root``? ``None`` when it cannot be decided."""
    try:
        if candidate == root:
            return True
        return root in candidate.parents
    except (OSError, ValueError):
        return None


def resolve_diagnostic_sink_root(
    candidate: Optional[Path], *, forbidden_roots: Iterable[Path]
) -> SinkLocation:
    """Admit ``candidate`` as the diagnostic sink root, or refuse — fail-closed (#15840).

    The four rules from the design record, in order:

    1. canonicalize the candidate and every forbidden root (``expanduser`` + ``resolve``), so
       comparison happens after symlinks are followed rather than on raw strings;
    2. refuse when the candidate equals, or lives under, ANY forbidden root;
    3. refuse when the answer cannot be established at all — no candidate, a relative candidate,
       an empty root set, or a path that will not canonicalize. "Probably outside" is not a
       basis for writing raw exception text;
    4. on refusal the caller writes nothing. There is no degraded "write it somewhere else"
       mode, because the whole point of the sink's location is the boundary it keeps.

    ``forbidden_roots`` is supplied by the caller — in production the guarded homes
    (:func:`...test_home_fence.ambient_homes`) plus the repo / worktree root. Passing an empty
    iterable refuses (rule 3) rather than admitting everything.
    """
    if candidate is None:
        return _refused(
            SINK_NO_CANDIDATE,
            "no sink root was offered; the diagnostic is dropped rather than written to a "
            "location nobody chose",
        )
    if not Path(candidate).is_absolute():
        return _refused(
            SINK_NOT_ABSOLUTE,
            f"the sink root {candidate!s} is not absolute, so whether it lands inside a "
            "forbidden surface depends on the process working directory and cannot be decided "
            "here",
        )

    roots = tuple(forbidden_roots)
    if not roots:
        return _refused(
            SINK_NO_FORBIDDEN_ROOTS,
            "no forbidden roots were supplied. An empty set means the caller has not said what "
            "to stay out of — not that there is nothing to stay out of — so the sink is refused",
        )

    resolved = _canonical(candidate)
    if resolved is None:
        return _refused(
            SINK_UNCANONICALIZABLE,
            f"the sink root {candidate!s} could not be canonicalized; a comparison against the "
            "forbidden roots would not be trustworthy",
        )

    for root in roots:
        if not Path(root).is_absolute():
            return _refused(
                SINK_FORBIDDEN_ROOT_NOT_ABSOLUTE,
                f"the forbidden root {root!s} is relative. Resolving it would anchor on the "
                "process working directory and silently name a different directory than the "
                "caller meant, so candidates under the intended root would be admitted",
            )
        resolved_root = _canonical(root)
        if resolved_root is None:
            return _refused(
                SINK_UNCANONICALIZABLE,
                f"a forbidden root ({root!s}) could not be canonicalized, so the sink cannot be "
                "proven to sit outside it",
            )
        within = _is_within(resolved, resolved_root)
        if within is None:
            return _refused(
                SINK_UNCANONICALIZABLE,
                f"whether the sink root sits inside {resolved_root!s} could not be decided",
            )
        if within:
            reason = (
                SINK_IS_FORBIDDEN_ROOT
                if resolved == resolved_root
                else SINK_INSIDE_FORBIDDEN_ROOT
            )
            return _refused(
                reason,
                f"the sink root resolves to {resolved!s}, which is the forbidden root "
                f"{resolved_root!s} or lives under it. Raw diagnostics there would land in a "
                "guarded home or a committable checkout, breaking the boundary the sink exists "
                "to keep",
            )
    return SinkLocation(admissible=True, root=resolved)


__all__ = (
    "SINK_FORBIDDEN_ROOT_NOT_ABSOLUTE",
    "SINK_INSIDE_FORBIDDEN_ROOT",
    "SINK_IS_FORBIDDEN_ROOT",
    "SINK_NOT_ABSOLUTE",
    "SINK_NO_CANDIDATE",
    "SINK_NO_FORBIDDEN_ROOTS",
    "SINK_UNCANONICALIZABLE",
    "SinkLocation",
    "resolve_diagnostic_sink_root",
)
