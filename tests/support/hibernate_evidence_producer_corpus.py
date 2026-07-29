"""Shared corpora and builders for the hibernate-evidence PRODUCER surface.

Two files judge that surface and must agree about what "invalid" and "unusual but valid" mean:
the #14694 regression pin (does the fixed defect recur?) and the producer contract unit tests
(does the public grammar still say what it says?). Splitting those two kinds of claim across
files is required by ``tests-placement-discovery-policy.md`` ``### regressions``; keeping their
inputs in one place is what stops the split from becoming a drift generator — a token added to
one file's "invalid" list and not the other would leave a hole in exactly one of them.

Not a test module: no ``test_*`` name, so ``unittest discover`` never collects it.
"""

from __future__ import annotations

import inspect

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain import (  # noqa: E501
    hibernate_evidence_marker as ev,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_envelope import (  # noqa: E501
    LaneEvidenceEnvelope,
)

WS = "ws-1"
LANE = "lane-abc"
HEAD = "a" * 40
INTEGRATION_HEAD = "b" * 40

#: Raw values a producer must refuse AS WRITTEN. The whitespace rows are the point: every one of
#: them was ``strip()``-ed into a perfectly clean token by the old validator (Redmine #14694).
INVALID_TOKENS = (
    " check ",  # the reported symptom (#14667 j#93230)
    "check ",
    " check",
    "che ck",
    "che\tck",
    "che\nck",  # a marker scanned per line never closes → unreadable durable evidence
    "che\rck",
    "che\xa0ck",  # "空白" is not two characters
    "che:ck",  # splits into a bogus extra field
    "che]ck",  # truncates the marker
    "che[ck",
    " ",
    "\n",
    "",  # an explicit empty value is something the caller WROTE (review j#93646 finding 3)
)

#: Values that are not strings at all. ``None`` and ``0`` are the dangerous pair: ``or ""`` turned
#: both into "the caller supplied nothing", so a type error was reported as a missing field.
INVALID_TYPES = (None, 0, 1, 12345, True, False, 3.5, b"check", ["check"], {"check": 1})

#: Tokens that merely LOOK unusual and are perfectly renderable — the control that kills an
#: over-correction. A guard that also refuses these has stopped being a marker-grammar rule.
VALID_TOKENS = (
    "test.yml",
    "29860030313",
    "a-b_c",
    "#14184",
    "チェック",
    "a/b",
    "a=b",
    "%2F",
    "ci(main)",
    "v1.0.0-rc.1",
)

#: The kind-specific producer parameters, DERIVED from the renderer's own signature so a new
#: parameter joins every sweep automatically instead of quietly sitting outside them.
PRODUCER_FIELDS = tuple(
    name
    for name in inspect.signature(ev.render_hibernate_evidence).parameters
    if name not in ("kind", "envelope")
)

#: What each kind's marker CARRIES. Written out as the ORACLE rather than read off the producer's
#: own table: an expectation derived from the implementation cannot catch the implementation
#: getting it wrong. :func:`assert_population_is_closed` ties it back to the signature and the
#: kind vocabulary, so a new field or a new kind cannot slip past either reader of this module.
CARRIED_BY_KIND = {
    ev.EVIDENCE_REQUIRED_CI_GREEN: (ev.FIELD_WORKFLOW, ev.FIELD_RUN, ev.FIELD_CONCLUSION),
    ev.EVIDENCE_DOGFOOD_DELEGATED: (ev.FIELD_RELEASE_ISSUE, ev.FIELD_ACCEPTANCE),
    ev.EVIDENCE_PARK_DECLARED: (),
}

#: ``conclusion`` is carried by CI but is producer-STATED rather than caller-supplied, so it is
#: never part of a clean call.
REQUIRED_BY_KIND = {
    kind: tuple(f for f in fields if f != ev.FIELD_CONCLUSION)
    for kind, fields in CARRIED_BY_KIND.items()
}


def envelope(**over) -> LaneEvidenceEnvelope:
    """A clean lane envelope, with any field overridden."""
    fields = dict(workspace=WS, lane=LANE, lane_generation=3, head=HEAD)
    fields.update(over)
    return LaneEvidenceEnvelope(**fields)


def envelope_for(kind: str, **over) -> LaneEvidenceEnvelope:
    """A clean envelope carrying a head only when ``kind`` is head-bearing."""
    fields = {"head": HEAD if kind in ev._HEAD_BEARING_EVIDENCE else ""}
    fields.update(over)
    return envelope(**fields)


def clean_kwargs(kind: str) -> dict:
    """The minimal CLEAN call for ``kind`` — every field it requires, nothing else."""
    return {field: "clean" for field in REQUIRED_BY_KIND[kind]}


def assert_population_is_closed(case) -> None:
    """Fail unless this module's oracle still covers the whole producer surface.

    Called from both readers rather than living as a test of its own: it is a guard on the SWEEPS,
    not a claim about the module, so it belongs wherever a sweep runs.
    """
    case.assertEqual(
        set(PRODUCER_FIELDS),
        {
            ev.FIELD_RUN,
            ev.FIELD_WORKFLOW,
            ev.FIELD_CONCLUSION,
            ev.FIELD_RELEASE_ISSUE,
            ev.FIELD_ACCEPTANCE,
        },
    )
    case.assertEqual(set(CARRIED_BY_KIND), set(ev.HIBERNATE_EVIDENCE_KINDS))
    case.assertEqual(
        {f for fields in CARRIED_BY_KIND.values() for f in fields}, set(PRODUCER_FIELDS)
    )
