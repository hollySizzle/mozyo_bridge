"""Application ops for the managed-lane herdr plugin policy (Redmine #14619).

The pure model (:mod:`...domain.herdr_plugin_policy`) decides what a herdr plugin
is allowed to be; this ops layer is the thin IO edge the CLI calls:

- **read** the plugin inventory — either from an already-captured
  ``herdr plugin list --json`` document (``--from-json``) or by running the
  trusted-environment herdr binary with exactly that read-only subcommand;
- **classify** every observed plugin through the pure model;
- **render** a status report, or a single enable / install *plan*.

Everything here is read-only by construction. :data:`INVENTORY_ARGV` is the only
argv this module ever builds, it is a literal constant rather than a computed
list, and there is no code path that installs, enables, disables, or uninstalls a
plugin — planning an enable answers whether it *would* be admissible and stops.

This surface has **three** untrusted inputs, and each needs its own rule. Naming
only the first two is what let review j#92092 finding 2 through: the operand a
plan is asked about never passes through the inventory, so the whole-surface
inventory oracle could not see it.

1. **The inventory** — normalized into :class:`PluginObservation`, a closed
   representation whose every field is a core-owned value (see
   :mod:`...domain.herdr_plugin_identity`). Nothing formatted from it can carry a
   path out of herdr's payload.
2. **Text this module did not author** — a subprocess's stderr, a parser's message
   about a malformed record — passed through :func:`redact_probe_paths`, because
   its shape is not under our control.
3. **The plan operand** (``--plan-enable <id>`` / ``--plan-install <spec>``) —
   normalized by :func:`normalize_operand` and echoed only when it is already a
   bounded identifier. "The operator typed it" is not a reason to echo it: this
   report is written to be pasted into a durable record, so an operand can publish
   a path, and an operand containing a newline can **forge a line** in that record.

An unreadable plugin record is reported as a malformed entry and fails the
report; it is never skipped. Skipping the one record that could not be read, and
then reporting on the rest, is how an inventory silently becomes "everything is
fine".
"""

from __future__ import annotations

import json
import re
import subprocess
from types import MappingProxyType
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_probe_redaction import (
    redact_probe_paths,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_plugin_policy import (
    CLASS_UNKNOWN,
    ENABLE_SCOPE,
    ENABLE_SCOPE_STATEMENT,
    REASON_AMBIGUOUS_TARGET,
    REASON_INVALID_TARGET_ID,
    REASON_INVENTORY_INCOMPLETE,
    REASON_MALFORMED_RECORD,
    REASON_TARGET_NOT_INSTALLED,
    SCOPE_ISOLATION_MECHANISM,
    SCOPE_ROOT_DETERMINANTS,
    MAX_RENDERED_FIELD_LENGTH,
    REDACTED_TOKEN,
    SOURCE_KIND_GITHUB,
    HerdrPluginPolicyError,
    PluginSourceRef,
    PluginVerdict,
    PolicyDecision,
    classify_plugin,
    observe_plugin,
    plan_install,
    contains_absolute_path,
    require_renderable_field,
    require_segment,
    resolve_review,
    source_ref_from_parts,
)

#: The enable denials reachable without a verdict, each with the **state it
#: means**: ``reason -> (is the plugin id echoed?, was something found?)``.
#:
#: Closing the reason *set* alone was not enough (review j#92285 F1): the
#: constructor accepted ``found=True`` beside ``target_not_installed`` and an
#: absent id beside ``found=True``, so a public plan could report "found but not
#: installed" or "withheld but found" — states the planner cannot produce.
#:
#: This table is the planner's own; :func:`plan_enable` reads its states from here
#: rather than restating them, and a regression drives the planner and requires
#: the set it reaches to equal this table. A hand-written list on the checking
#: side is what drifts.
#: **Read-only.** Collecting the planner and the constructor onto one table was
#: right, and it made a mutable table worse: both readers move together, so a
#: state outside the closed vocabulary gets justified on both sides at once.
#: Review j#92330 measured it — injecting a per-plugin reason made a forbidden
#: verdictless plan constructible while the public reason view stayed stale.
#: "One authority" and "an authority nobody can rewrite" are different properties.
VERDICTLESS_ENABLE_STATES: "MappingProxyType[str, tuple[bool, bool]]" = MappingProxyType(
    {
        REASON_INVALID_TARGET_ID: (False, False),
        REASON_INVENTORY_INCOMPLETE: (True, False),
        REASON_AMBIGUOUS_TARGET: (True, True),
        REASON_TARGET_NOT_INSTALLED: (True, False),
    }
)

#: The set view, derived from the table rather than snapshotted beside it — the
#: snapshot could disagree with the table it claimed to summarise.
VERDICTLESS_ENABLE_REASONS: frozenset[str] = frozenset(VERDICTLESS_ENABLE_STATES)

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
#: Same set minus the newline, which is what structures the text report.
_CONTROL_CHARS_EXCEPT_NEWLINE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")

#: The **only** herdr invocation this surface makes: a read-only inventory query.
#: A literal constant rather than something assembled per call, so a test can pin
#: the exact argv and no future edit can grow a mutating subcommand into it.
INVENTORY_ARGV: "tuple[str, ...]" = ("plugin", "list", "--json")

#: Seconds to wait for the inventory query before giving up.
INVENTORY_TIMEOUT_SECONDS = 20

# --- inventory-read failure vocabulary (closed) ------------------------------
#: The herdr binary could not be resolved from the trusted environment.
READ_HERDR_UNRESOLVED = "herdr_unresolved"
#: herdr ran but exited non-zero, or could not be executed at all.
READ_HERDR_ERROR = "herdr_error"
#: The named inventory document could not be read.
READ_SOURCE_UNREADABLE = "inventory_unreadable"
#: The inventory is not valid JSON, or not the ``plugin_list`` envelope.
READ_MALFORMED_INVENTORY = "inventory_malformed"

READ_FAILURE_REASONS: frozenset[str] = frozenset(
    {
        READ_HERDR_UNRESOLVED,
        READ_HERDR_ERROR,
        READ_SOURCE_UNREADABLE,
        READ_MALFORMED_INVENTORY,
    }
)


class InventoryReadError(Exception):
    """The plugin inventory could not be obtained (carries a closed reason)."""

    def __init__(self, reason: str, detail: str):
        if reason not in READ_FAILURE_REASONS:
            raise InventoryReadError(
                READ_MALFORMED_INVENTORY,
                f"unknown inventory read reason {reason!r}",
            )
        safe_detail = sanitize_renderable(detail)
        super().__init__(safe_detail)
        self.reason = reason
        self.detail = safe_detail


def sanitize_renderable(text: object) -> str:
    """Make third-party-derived text safe to render (redact, flatten, bound).

    The *sanitizing* half of the boundary described in
    ``require_renderable_field``. Applied where the content comes from outside —
    a subprocess's stderr, a parser's message quoting a hostile record — because
    there a violation is expected input rather than a bug in our own text, so the
    boundary must produce a usable value instead of refusing.

    Three transformations, each closing a measured hole: absolute paths are
    redacted (review j#92053 F1), control characters are flattened to spaces so a
    field cannot forge a line of the record it is pasted into (j#92092 F2), and the
    result is bounded (an unbounded field is a channel).
    """
    redacted = redact_probe_paths(str(text or ""))
    flattened = _CONTROL_CHARS.sub(" ", redacted)
    if len(flattened) > MAX_RENDERED_FIELD_LENGTH:
        flattened = flattened[: MAX_RENDERED_FIELD_LENGTH - 1] + "…"
    if contains_absolute_path(flattened):
        # redact_probe_paths is line-oriented; if anything survives the flattening
        # we withhold the whole detail rather than emit a partial path.
        return REDACTED_TOKEN
    return flattened


#: In-module alias kept for the existing call sites.
_redact = sanitize_renderable


def read_inventory_document(path: Path) -> str:
    """Read a captured ``herdr plugin list --json`` document (read-only)."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InventoryReadError(
            READ_SOURCE_UNREADABLE,
            f"inventory document is unreadable ({exc.__class__.__name__})",
        ) from exc


def query_inventory(env: "Mapping[str, str]") -> str:
    """Run the trusted herdr binary's read-only inventory query.

    The binary comes only from the shared trusted-environment resolver
    (``resolve_herdr_binary``, #13496) — never from repo-local config or the cwd —
    and is invoked with :data:`INVENTORY_ARGV` and nothing else.
    """
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (
        TerminalTransportError,
        resolve_herdr_binary,
    )

    try:
        resolution = resolve_herdr_binary(env)
    except TerminalTransportError as exc:
        raise InventoryReadError(READ_HERDR_UNRESOLVED, _redact(exc)) from exc
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, trusted-env binary
            [resolution.path, *INVENTORY_ARGV],
            capture_output=True,
            text=True,
            timeout=INVENTORY_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InventoryReadError(
            READ_HERDR_ERROR,
            f"herdr inventory query failed ({exc.__class__.__name__})",
        ) from exc
    if completed.returncode != 0:
        raise InventoryReadError(
            READ_HERDR_ERROR,
            f"herdr exited {completed.returncode}: {_redact(completed.stderr)[:400]}",
        )
    return completed.stdout


def parse_inventory(document: str) -> "list[object]":
    """Extract the plugin records from a ``plugin list --json`` envelope.

    Fails closed on anything that is not that envelope. An envelope whose
    ``result`` is present but carries no ``plugins`` list is malformed rather than
    empty: "herdr told us nothing" and "herdr told us there are none" are different
    facts, and only the second may be reported as a clean inventory.
    """
    try:
        payload = json.loads(document)
    except (json.JSONDecodeError, TypeError) as exc:
        raise InventoryReadError(
            READ_MALFORMED_INVENTORY,
            f"inventory is not valid JSON ({exc.__class__.__name__})",
        ) from exc
    if not isinstance(payload, Mapping):
        raise InventoryReadError(
            READ_MALFORMED_INVENTORY,
            f"inventory root must be an object, got {type(payload).__name__}",
        )
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise InventoryReadError(
            READ_MALFORMED_INVENTORY, "inventory has no 'result' object"
        )
    plugins = result.get("plugins")
    if isinstance(plugins, (str, bytes)) or not isinstance(plugins, Sequence):
        raise InventoryReadError(
            READ_MALFORMED_INVENTORY,
            f"inventory 'result.plugins' must be a list, got "
            f"{type(plugins).__name__}",
        )
    return list(plugins)


@dataclass(frozen=True)
class MalformedEntry:
    """A record in the inventory that could not be read as a plugin at all.

    Carries the position and a redacted parse reason, never the record itself: the
    record is third-party data whose fields can hold a private path.
    """

    index: int
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.index, int) or isinstance(self.index, bool):
            raise HerdrPluginPolicyError("malformed-entry index must be an int")
        # Third-party derived: sanitize rather than refuse, so a hostile record
        # still yields a reportable entry (frozen dataclass -> object.__setattr__).
        object.__setattr__(self, "detail", sanitize_renderable(self.detail))

    def as_payload(self) -> dict:
        return {
            "index": self.index,
            "reason": REASON_MALFORMED_RECORD,
            "detail": self.detail,
        }


def _decision_payload(decision: PolicyDecision) -> dict:
    return {
        "admitted": decision.admitted,
        "reason": decision.reason,
        "detail": decision.detail,
    }


def _verdict_payload(verdict: PluginVerdict) -> dict:
    observation = verdict.observation
    return {
        "plugin_id": observation.plugin_id,
        "enabled": observation.enabled,
        "enable_scope": ENABLE_SCOPE,
        "source": observation.ref.describe() if observation.ref else None,
        "source_kind": observation.source_kind,
        "class": verdict.plugin_class,
        "build_provenance": verdict.build_provenance,
        "review_anchor": verdict.review_anchor,
        "declares_build": observation.declares_build,
        "declares_panes": observation.declares_panes,
        "declares_actions": observation.declares_actions,
        "enable": _decision_payload(verdict.enable),
        "install": _decision_payload(verdict.install),
        "breach": verdict.breach,
    }


def _scope_payload() -> dict:
    return {
        "scope": ENABLE_SCOPE,
        "statement": ENABLE_SCOPE_STATEMENT,
        "root_determinants": list(SCOPE_ROOT_DETERMINANTS),
        "isolation": SCOPE_ISOLATION_MECHANISM,
    }


@dataclass(frozen=True)
class PolicyStatus:
    """The classified inventory: every plugin, both decisions, plus the scope truth."""

    verdicts: "tuple[PluginVerdict, ...]"
    malformed: "tuple[MalformedEntry, ...]"

    def __post_init__(self) -> None:
        for verdict in self.verdicts:
            if not isinstance(verdict, PluginVerdict):
                raise HerdrPluginPolicyError("verdicts must be PluginVerdict values")
        for entry in self.malformed:
            if not isinstance(entry, MalformedEntry):
                raise HerdrPluginPolicyError("malformed must be MalformedEntry values")

    @property
    def breaches(self) -> "tuple[PluginVerdict, ...]":
        return tuple(verdict for verdict in self.verdicts if verdict.breach)

    @property
    def fully_read(self) -> bool:
        """Whether every record in the inventory was readable.

        The single predicate every consumer of this inventory asks, rather than a
        condition each one re-derives. Review j#92053 finding 2 measured the cost of
        having only ``ok`` express it: the reasoning ("an inventory with an
        unreadable entry has not been fully classified") was written into ``ok``
        and *not* applied to :func:`plan_enable`, so the reporting side failed
        closed while the admission side — the one that actually gates an
        operator's action — failed open.
        """
        return not self.malformed

    @property
    def ok(self) -> bool:
        """Clean iff every record read and no enabled plugin is inadmissible.

        A *denied* plugin that is not enabled is the policy working, so it does not
        fail the report. A record that could not be read does.
        """
        return self.fully_read and not self.breaches

    def as_payload(self) -> dict:
        return {
            "ok": self.ok,
            "enable_scope": _scope_payload(),
            "plugins": [_verdict_payload(verdict) for verdict in self.verdicts],
            "malformed": [entry.as_payload() for entry in self.malformed],
            "breach_count": len(self.breaches),
        }


def classify_inventory(records: "Sequence[object]") -> PolicyStatus:
    """Classify every record, collecting — never skipping — the unreadable ones."""
    verdicts: "list[PluginVerdict]" = []
    malformed: "list[MalformedEntry]" = []
    for index, record in enumerate(records):
        try:
            observation = observe_plugin(record)
        except HerdrPluginPolicyError as exc:
            malformed.append(MalformedEntry(index=index, detail=_redact(exc)))
            continue
        verdicts.append(classify_plugin(observation))
    return PolicyStatus(verdicts=tuple(verdicts), malformed=tuple(malformed))


def normalize_operand(raw: object) -> Optional[str]:
    """Return an operand safe to echo, or ``None`` when it is not (review j#92092 F2).

    An operand reaching this surface is *typed by the operator*, which is why the
    original version echoed it back without a thought. That reasoning was wrong on
    two counts. This surface's text output exists to be pasted into a durable
    record, so an operand carrying an absolute path publishes it; and an operand
    carrying a newline can **forge a line** in that record — measured: an operand
    of ``a\\nBREACH: fake\\nb`` rendered ``BREACH: fake`` as its own line, which is
    the shape this report uses to announce a live policy violation. That is a
    record-integrity problem, not merely a disclosure one.

    So an operand is echoed only if it is already a valid bounded identifier;
    anything else is reported as :data:`REDACTED_TOKEN`. The operator loses no
    information they did not already have — they typed it.
    """
    try:
        return require_segment(raw, "operand")
    except HerdrPluginPolicyError:
        return None


def _operand_label(operand: Optional[str]) -> str:
    """How an operand is rendered: itself when safe, a closed token otherwise."""
    return operand if operand is not None else REDACTED_TOKEN


@dataclass(frozen=True)
class EnablePlan:
    """The answer to "may I enable this plugin?" — a decision, never an action.

    Carries an explicit :class:`PolicyDecision` rather than deriving admissibility
    from whether a verdict happened to be found. The derived form (review j#92053
    finding 2) could only say "admitted" or "not found", so every *other* way the
    question can be unanswerable — an inventory that did not fully read, an
    ambiguous id, an operand we will not echo — had nowhere to be represented and
    silently became "admitted".

    ``plugin_id`` is the *normalized* operand: ``None`` when the operand was not a
    valid bounded identifier, in which case nothing derived from it is echoed.
    """

    plugin_id: Optional[str]
    found: bool
    verdict: Optional[PluginVerdict]
    decision: PolicyDecision

    def __post_init__(self) -> None:
        # The factory normalizes; the record *closes*. Review j#92141 F1: closing
        # only the factory left `EnablePlan(...)` and `dataclasses.replace(...)`
        # as open paths into the very object that gets rendered.
        if self.plugin_id is not None:
            require_segment(self.plugin_id, "enable plan target")
        if not isinstance(self.found, bool):
            raise HerdrPluginPolicyError("found must be a boolean")
        if not isinstance(self.decision, PolicyDecision):
            raise HerdrPluginPolicyError("decision must be a PolicyDecision")
        if self.verdict is not None and not isinstance(self.verdict, PluginVerdict):
            raise HerdrPluginPolicyError("verdict must be a PluginVerdict or None")
        # Relational invariant. The first version (review j#92194 F2) constrained
        # only the admitted case, on the reasoning that "a denial needs nothing
        # behind it". That reasoning is right about *preconditions* and wrong about
        # *consistency*: j#92241 F2 measured a plan carrying an enable-admitted
        # verdict while its own decision denied, and the two renderers then
        # answered the same question oppositely.
        #
        # So a plan with a verdict is checked the way `PluginVerdict` is checked —
        # against what produced it — rather than against a weaker hand-written
        # rule. The strong technique was already in this module for verdicts; using
        # it here too is the point.
        if self.verdict is not None:
            if (
                not self.found
                or self.plugin_id != self.verdict.observation.plugin_id
                or self.decision != self.verdict.enable
            ):
                raise HerdrPluginPolicyError(
                    "an enable plan carrying a verdict must report that verdict's "
                    "plugin, decision and found state"
                )
        elif self.decision.admitted or self.decision.reason not in (
            VERDICTLESS_ENABLE_STATES
        ):
            raise HerdrPluginPolicyError(
                f"without a verdict an enable plan may only deny for one of "
                f"{sorted(VERDICTLESS_ENABLE_STATES)}; a per-plugin reason cannot "
                f"be reached without the plugin it judged"
            )
        else:
            expected = VERDICTLESS_ENABLE_STATES[self.decision.reason]
            if (self.plugin_id is not None, self.found) != expected:
                raise HerdrPluginPolicyError(
                    f"a {self.decision.reason!r} denial reports "
                    f"(id echoed, found) = {expected}; this plan reports "
                    f"{(self.plugin_id is not None, self.found)}"
                )

    @property
    def ok(self) -> bool:
        return self.decision.admitted

    def as_payload(self) -> dict:
        return {
            "ok": self.ok,
            "plugin_id": _operand_label(self.plugin_id),
            "found": self.found,
            "enable_scope": _scope_payload(),
            "decision": _decision_payload(self.decision),
            "plugin": _verdict_payload(self.verdict) if self.verdict else None,
        }


def _verdictless_plan(reason: str, detail: str, operand: Optional[str]) -> EnablePlan:
    """Build a verdictless enable plan, deriving its state from the shared table.

    The planner does not restate `plugin_id` / `found` per branch; it reads them
    from :data:`VERDICTLESS_ENABLE_STATES`, which is the same table the constructor
    checks against. Two hand-written copies of "what this reason means" is how the
    constructor came to accept states the planner cannot produce (review j#92285 F1).
    """
    id_present, found = VERDICTLESS_ENABLE_STATES[reason]
    return EnablePlan(
        plugin_id=operand if id_present else None,
        found=found,
        verdict=None,
        decision=PolicyDecision.deny(reason, detail),
    )


def plan_enable(status: PolicyStatus, plugin_id: object) -> EnablePlan:
    """Decide whether ``plugin_id`` may be enabled. Enables nothing.

    Fails closed on every way the question can lack a single trustworthy answer,
    checked before the plugin's own verdict is consulted:

    - **the inventory did not fully read** — a record we could not classify may be
      the very plugin being asked about, or a second claimant to its id, so no
      answer drawn from the remainder is trustworthy;
    - **more than one installed plugin answers to the id** — herdr's id is what an
      operator would type at ``herdr plugin enable``, and picking the first match
      silently answers about a different plugin than the one that would be enabled;
    - **nothing answers to the id** — there is no identity to establish.

    An operand that is not a valid bounded identifier is refused before any of
    that, and is never echoed (:func:`normalize_operand`).
    """
    operand = normalize_operand(plugin_id)
    if operand is None:
        return _verdictless_plan(
            REASON_INVALID_TARGET_ID,
            "the named plugin id is not a bounded identifier; no installed "
            "plugin could carry it, and it is not echoed back",
            operand,
        )
    if not status.fully_read:
        return _verdictless_plan(
            REASON_INVENTORY_INCOMPLETE,
            f"{len(status.malformed)} inventory record(s) could not be read, so "
            f"the inventory has not been fully classified; no enable answer "
            f"drawn from it is trustworthy",
            operand,
        )
    matches = [
        verdict
        for verdict in status.verdicts
        if verdict.observation.plugin_id == operand
    ]
    if len(matches) > 1:
        return _verdictless_plan(
            REASON_AMBIGUOUS_TARGET,
            f"{len(matches)} installed plugins answer to this id; which one an "
            f"enable would affect has no single answer",
            operand,
        )
    if not matches:
        return _verdictless_plan(
            REASON_TARGET_NOT_INSTALLED,
            "no installed plugin answers to this id, so there is nothing whose "
            "identity could be established",
            operand,
        )
    verdict = matches[0]
    return EnablePlan(
        plugin_id=operand, found=True, verdict=verdict, decision=verdict.enable
    )


@dataclass(frozen=True)
class InstallPlan:
    """The answer to "may I run this install?" — a decision, never an action."""

    spec: Optional[str]
    ref: Optional[PluginSourceRef]
    decision: PolicyDecision

    def __post_init__(self) -> None:
        if self.spec is not None:
            owner, separator, repo = self.spec.partition("/")
            if not separator:
                raise HerdrPluginPolicyError("install plan spec must be owner/repo")
            require_segment(owner, "install plan spec owner")
            require_segment(repo, "install plan spec repo")
        if self.ref is not None and not isinstance(self.ref, PluginSourceRef):
            raise HerdrPluginPolicyError("ref must be a PluginSourceRef or None")
        if not isinstance(self.decision, PolicyDecision):
            raise HerdrPluginPolicyError("decision must be a PolicyDecision")
        # Recomputed, not merely preconditioned (review j#92241 F1). Requiring only
        # "admitted implies a pinned ref" let a reference the policy DENIES
        # (`unpinned_remote_build`) be handed an invented admit and rendered
        # `ok=true` — a supply-chain preflight inverting its own answer. The
        # decision must be the policy's decision for this reference.
        if self.decision != plan_install(self.ref):
            raise HerdrPluginPolicyError(
                "install plan decision disagrees with the policy for this reference"
            )
        # Presence is coupled, not independent (review j#92285 F2). The factory
        # derives both from the same owner/repo validation, so it produces both or
        # neither; one-sided states let a record withhold its target while
        # disclosing that target's exact source, or name a target it reports as
        # unknown.
        if (self.spec is None) != (self.ref is None):
            raise HerdrPluginPolicyError(
                "an install plan names a target and its reference, or neither"
            )
        if self.spec is not None and self.ref is not None:
            if self.spec != f"{self.ref.owner}/{self.ref.repo}":
                raise HerdrPluginPolicyError(
                    "install plan spec must name the repository its reference "
                    "resolves to"
                )

    @property
    def ok(self) -> bool:
        return self.decision.admitted

    def as_payload(self) -> dict:
        review = resolve_review(self.ref)
        return {
            "ok": self.ok,
            "spec": _operand_label(self.spec),
            "source": self.ref.describe() if self.ref else None,
            "class": review.plugin_class if review else CLASS_UNKNOWN,
            "build_provenance": review.build_provenance if review else None,
            "review_anchor": review.review_anchor if review else "",
            "install": _decision_payload(self.decision),
        }


def plan_candidate_install(spec: str, ref: Optional[str]) -> InstallPlan:
    """Decide a candidate ``herdr plugin install <owner>/<repo> --ref <commit>``.

    ``spec`` mirrors herdr's own ``<owner>/<repo>`` argument. A spec or ref that
    cannot be read as an exact pinned identity is not an error here: it resolves to
    the strongest reference the parts support (possibly repository-scoped, possibly
    ``None``) and is denied accordingly — the same answer an installed plugin with
    the same parts gets, because both go through ``source_ref_from_parts``. That
    sharing is the point: the pinned-then-repository fallback used to live only
    here, which is how the observed path lost a repository identity to a malformed
    commit (review j#92053 finding 3).

    The operand itself is echoed only when both halves are valid bounded
    identifiers; otherwise the plan reports :data:`REDACTED_TOKEN` in its place
    (review j#92092 finding 2 — the raw ``spec`` reached both the text and the JSON,
    so a path or a newline in it reached a pasteable record).
    """
    owner, separator, repo = str(spec).partition("/") if isinstance(spec, str) else ("", "", "")
    source_ref: Optional[PluginSourceRef] = (
        source_ref_from_parts(SOURCE_KIND_GITHUB, owner, repo, ref)
        if separator
        else None
    )
    safe_owner = normalize_operand(owner)
    safe_repo = normalize_operand(repo)
    safe_spec = (
        f"{safe_owner}/{safe_repo}"
        if separator and safe_owner is not None and safe_repo is not None
        else None
    )
    return InstallPlan(spec=safe_spec, ref=source_ref, decision=plan_install(source_ref))


# --- text rendering ----------------------------------------------------------


def _format_scope() -> "list[str]":
    return [
        f"scope: {ENABLE_SCOPE} — {ENABLE_SCOPE_STATEMENT}",
        f"  affected config root resolves from: "
        f"{', '.join(SCOPE_ROOT_DETERMINANTS)}",
        f"  isolation: {SCOPE_ISOLATION_MECHANISM}",
    ]


def _format_decision(label: str, decision: PolicyDecision) -> str:
    head = "ADMITTED" if decision.admitted else f"DENIED [{decision.reason}]"
    line = f"  {label}: {head}"
    return f"{line}\n      {decision.detail}" if decision.detail else line


def _format_verdict(
    verdict: PluginVerdict, *, include_enable: bool = True
) -> "list[str]":
    """Render one plugin's block. ``include_enable=False`` omits the enable line.

    The enable plan suppresses it because the plan's own ``decision`` is the answer
    there, and printing a second enable line beside it puts two authorities on one
    question — which is how the JSON and the text came to disagree (review j#92241
    F2). The install line stays: it is context the plan does not answer.
    """
    observation = verdict.observation
    state = "enabled" if observation.enabled else "disabled"
    lines = [
        f"- {observation.plugin_id} [{state}, {ENABLE_SCOPE}]",
        f"  source: {observation.ref.describe() if observation.ref else 'unpinned'}",
        f"  class: {verdict.plugin_class}  build: {verdict.build_provenance}",
    ]
    if verdict.review_anchor:
        lines.append(f"  reviewed: {verdict.review_anchor}")
    if include_enable:
        lines.append(_format_decision("enable ", verdict.enable))
    lines.append(_format_decision("install", verdict.install))
    if verdict.breach:
        lines.append(
            "  BREACH: this plugin is enabled now and is not admissible; the enable "
            "is user-global, so it affects every managed lane."
        )
    return lines


def format_status_text(status: PolicyStatus) -> str:
    """Human-readable status: the scope truth first, then every plugin."""
    lines = ["herdr plugin policy — status (read-only; nothing was changed)"]
    lines.extend(_format_scope())
    lines.append("")
    if not status.verdicts and not status.malformed:
        lines.append("no plugins installed")
    for verdict in status.verdicts:
        lines.extend(_format_verdict(verdict))
    for entry in status.malformed:
        lines.append(
            f"- <record {entry.index}>: UNREADABLE [{REASON_MALFORMED_RECORD}] "
            f"{entry.detail}"
        )
    if status.breaches:
        lines.append("")
        lines.append(
            f"{len(status.breaches)} enabled plugin(s) are not admissible in a managed "
            f"lane."
        )
    return "\n".join(lines)


def format_enable_plan_text(plan: EnablePlan) -> str:
    """Human-readable enable plan. This never enables anything."""
    lines = [
        f"herdr plugin policy — enable plan for {_operand_label(plan.plugin_id)} "
        f"(read-only; nothing was enabled)"
    ]
    lines.extend(_format_scope())
    lines.append("")
    # The plan's own decision is the single answer, whether or not a verdict
    # supplied it. The verdict block below is context, with its enable line
    # suppressed so this stays the only place the question is answered.
    lines.append(_format_decision("enable ", plan.decision).lstrip())
    if plan.verdict is not None:
        lines.extend(_format_verdict(plan.verdict, include_enable=False))
    return "\n".join(lines)


def format_install_plan_text(plan: InstallPlan) -> str:
    """Human-readable install plan. This never installs anything."""
    review = resolve_review(plan.ref)
    lines = [
        f"herdr plugin policy — install plan for {_operand_label(plan.spec)} "
        f"(read-only; nothing was installed)",
        f"  source: {plan.ref.describe() if plan.ref else 'unpinned'}",
        f"  class: {review.plugin_class if review else CLASS_UNKNOWN}  "
        f"build: {review.build_provenance if review else 'unknown'}",
    ]
    if review is not None:
        lines.append(f"  reviewed: {review.review_anchor}")
    lines.append(_format_decision("install", plan.decision))
    return "\n".join(lines)


def format_read_error_text(error: InventoryReadError) -> str:
    return f"error [{error.reason}]: {error.detail}"


# --- the sink guard ----------------------------------------------------------


class RenderGuardError(HerdrPluginPolicyError):
    """An assembled artifact violated the disclosure boundary at the output sink."""


def guard_rendered_text(text: str) -> str:
    """Verify an assembled text artifact, or refuse to emit it.

    The *second* layer, and the reason it exists is empirical rather than
    theoretical. Four review rounds each closed a surface and each left the
    surface next to it open: fields meant to hold a path, then fields not checked
    at all, then a narrowed alphabet, then the value objects behind the factories.
    Every one of those was found by someone enumerating surfaces — and the
    enumeration was wrong every time, mine included.

    So this check is not attached to a surface. It is attached to the **one place
    everything leaves through**, and it asks about the finished artifact: does it
    carry an absolute path, or a control character other than the newlines that
    structure it? A future field, DTO, or formatter is covered without anyone
    noticing it needs to be.

    Fail-closed by raising: emitting nothing is the correct outcome for a report
    that would carry a private path or a forged line into a durable record.
    """
    if contains_absolute_path(text):
        raise RenderGuardError(
            "refusing to emit a report carrying an absolute filesystem path"
        )
    if _CONTROL_CHARS_EXCEPT_NEWLINE.search(text):
        raise RenderGuardError(
            "refusing to emit a report carrying a control character"
        )
    return text


def guard_rendered_payload(payload: object) -> object:
    """Verify every string in an assembled JSON payload, or refuse to emit it.

    The payload counterpart of :func:`guard_rendered_text`. A payload string is a
    *field*, never an assembled artifact, so a newline is not structural here and
    is refused along with every other control character.
    """
    if isinstance(payload, str):
        if contains_absolute_path(payload):
            raise RenderGuardError(
                "refusing to emit a payload carrying an absolute filesystem path"
            )
        if _CONTROL_CHARS.search(payload):
            raise RenderGuardError(
                "refusing to emit a payload carrying a control character"
            )
    elif isinstance(payload, Mapping):
        for key, value in payload.items():
            guard_rendered_payload(key)
            guard_rendered_payload(value)
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        for item in payload:
            guard_rendered_payload(item)
    return payload


__all__ = (
    "INVENTORY_ARGV",
    "INVENTORY_TIMEOUT_SECONDS",
    "READ_FAILURE_REASONS",
    "READ_HERDR_ERROR",
    "READ_HERDR_UNRESOLVED",
    "READ_MALFORMED_INVENTORY",
    "READ_SOURCE_UNREADABLE",
    "EnablePlan",
    "InstallPlan",
    "InventoryReadError",
    "MalformedEntry",
    "PolicyStatus",
    "RenderGuardError",
    "guard_rendered_payload",
    "guard_rendered_text",
    "sanitize_renderable",
    "VERDICTLESS_ENABLE_REASONS",
    "VERDICTLESS_ENABLE_STATES",
    "classify_inventory",
    "normalize_operand",
    "format_enable_plan_text",
    "format_install_plan_text",
    "format_read_error_text",
    "format_status_text",
    "parse_inventory",
    "plan_candidate_install",
    "plan_enable",
    "query_inventory",
    "read_inventory_document",
)
