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

Two disclosure rules are enforced rather than assumed:

- the normalized :class:`PluginObservation` has no field that can hold a path, so
  nothing this module formats can carry one out of herdr's payload (which does
  carry three absolute operator-home paths);
- text that is *not* ours — a subprocess's stderr, a parser's message about a
  malformed record — is passed through :func:`redact_probe_paths` before it
  reaches a report, because its shape is not under our control.

An unreadable plugin record is reported as a malformed entry and fails the
report; it is never skipped. Skipping the one record that could not be read, and
then reporting on the rest, is how an inventory silently becomes "everything is
fine".
"""

from __future__ import annotations

import json
import subprocess
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
    REASON_MALFORMED_RECORD,
    SCOPE_ISOLATION_MECHANISM,
    SCOPE_ROOT_DETERMINANTS,
    SOURCE_KIND_GITHUB,
    HerdrPluginPolicyError,
    PluginSourceRef,
    PluginVerdict,
    PolicyDecision,
    classify_plugin,
    observe_plugin,
    plan_install,
    resolve_review,
)

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
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _redact(text: object) -> str:
    """Redact any absolute path out of text this module did not author."""
    return redact_probe_paths(str(text or ""))


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
        "version": observation.version,
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

    @property
    def breaches(self) -> "tuple[PluginVerdict, ...]":
        return tuple(verdict for verdict in self.verdicts if verdict.breach)

    @property
    def ok(self) -> bool:
        """Clean iff every record read and no enabled plugin is inadmissible.

        A *denied* plugin that is not enabled is the policy working, so it does not
        fail the report. A record that could not be read does, because an inventory
        with an unreadable entry has not been fully classified.
        """
        return not self.malformed and not self.breaches

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


@dataclass(frozen=True)
class EnablePlan:
    """The answer to "may I enable this plugin?" — a decision, never an action."""

    plugin_id: str
    found: bool
    verdict: Optional[PluginVerdict]

    @property
    def ok(self) -> bool:
        """A plugin that is not installed is not admissible to enable either."""
        return bool(self.verdict and self.verdict.enable.admitted)

    def as_payload(self) -> dict:
        return {
            "ok": self.ok,
            "plugin_id": self.plugin_id,
            "found": self.found,
            "enable_scope": _scope_payload(),
            "plugin": _verdict_payload(self.verdict) if self.verdict else None,
        }


def plan_enable(status: PolicyStatus, plugin_id: str) -> EnablePlan:
    """Decide whether ``plugin_id`` may be enabled. Enables nothing.

    A plugin id that is absent from the inventory is reported as not found and is
    not admissible — there is nothing whose identity could have been established.
    """
    for verdict in status.verdicts:
        if verdict.observation.plugin_id == plugin_id:
            return EnablePlan(plugin_id=plugin_id, found=True, verdict=verdict)
    return EnablePlan(plugin_id=plugin_id, found=False, verdict=None)


@dataclass(frozen=True)
class InstallPlan:
    """The answer to "may I run this install?" — a decision, never an action."""

    spec: str
    ref: Optional[PluginSourceRef]
    decision: PolicyDecision

    @property
    def ok(self) -> bool:
        return self.decision.admitted

    def as_payload(self) -> dict:
        review = resolve_review(self.ref)
        return {
            "ok": self.ok,
            "spec": self.spec,
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
    ``ref=None`` and is denied as ``unpinned_source``, which is the same answer an
    unpinnable installed plugin gets.
    """
    source_ref: Optional[PluginSourceRef] = None
    owner, separator, repo = spec.partition("/")
    if separator and owner and repo and ref:
        try:
            source_ref = PluginSourceRef.pinned(SOURCE_KIND_GITHUB, owner, repo, ref)
        except HerdrPluginPolicyError:
            source_ref = None
    if source_ref is None and separator and owner and repo:
        # A well-formed repository with no usable commit still resolves to a
        # repository-scoped deny entry when one exists, so a known-inadmissible
        # project is named as such instead of as a generic unpinned source.
        try:
            source_ref = PluginSourceRef.repository(SOURCE_KIND_GITHUB, owner, repo)
        except HerdrPluginPolicyError:
            source_ref = None
    return InstallPlan(
        spec=spec, ref=source_ref, decision=plan_install(source_ref)
    )


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


def _format_verdict(verdict: PluginVerdict) -> "list[str]":
    observation = verdict.observation
    state = "enabled" if observation.enabled else "disabled"
    lines = [
        f"- {observation.plugin_id} ({observation.version or 'no version'}) "
        f"[{state}, {ENABLE_SCOPE}]",
        f"  source: {observation.ref.describe() if observation.ref else 'unpinned'}",
        f"  class: {verdict.plugin_class}  build: {verdict.build_provenance}",
    ]
    if verdict.review_anchor:
        lines.append(f"  reviewed: {verdict.review_anchor}")
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
        f"herdr plugin policy — enable plan for {plan.plugin_id} "
        f"(read-only; nothing was enabled)"
    ]
    lines.extend(_format_scope())
    lines.append("")
    if plan.verdict is None:
        lines.append(
            f"- {plan.plugin_id}: NOT INSTALLED — nothing to establish an identity "
            f"from, so the enable is not admissible."
        )
        return "\n".join(lines)
    lines.extend(_format_verdict(plan.verdict))
    return "\n".join(lines)


def format_install_plan_text(plan: InstallPlan) -> str:
    """Human-readable install plan. This never installs anything."""
    review = resolve_review(plan.ref)
    lines = [
        f"herdr plugin policy — install plan for {plan.spec} "
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
    "classify_inventory",
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
