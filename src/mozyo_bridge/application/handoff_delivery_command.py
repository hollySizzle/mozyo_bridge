"""OOP-first boundary for the handoff delivery-rendering residual (Redmine #12981).

The handoff strict-rail command body in ``application/commands.py`` historically
carried a tail of *delivery rendering / persistence* residual inline:

- ``_emit_outcome`` — print the durable delivery-record markdown and/or the
  single-line JSON outcome (the shape every ``orchestrate_handoff`` terminal path
  emits).
- ``_emit_receipt`` — print the opt-in durable-persistence receipt (Redmine
  #12311).
- ``_maybe_persist_delivery_record`` — the opt-in durable persistence wiring
  (Redmine #12311 / #12347): build the note, pick the credential-safe live
  Redmine transport by source, resolve the sink, persist, and emit the receipt.
- ``_emit_handoff_marker_timeout_guidance`` — the stderr fallback trailer printed
  after a strict-rail ``handoff send`` ``marker_timeout`` (Asana task
  1214779823377861).

This module carves that residual into an OOP-first boundary under #12638,
aligning with the existing ``handoff_command.py`` entry boundary (#12936) and the
``domain/delivery_record_sink`` delivery-record boundary, **without touching**
:func:`orchestrate_handoff` itself, the strict rail / marker-timeout / queue-enter
semantics, the transport rail, or the receiver-binding gate (all out of #12981
scope):

- :func:`marker_timeout_guidance_lines` is *pure* — it builds the three stderr
  hint strings with no ``print`` — so the fallback wording is unit tested with a
  plain assertion instead of a stderr capture.
- :class:`DeliveryRecordOps` is the port for the two dependencies the persistence
  path needs from its environment (the ``die`` error-exit and the credential-safe
  live transport / sink resolution), so :meth:`DeliveryRecordUseCase.maybe_persist`
  is exercisable with a synthetic fake (source-routed transport selection,
  best-effort ``transport_error`` receipt on any sink failure) with no live
  Redmine.
- :class:`DeliveryRecordUseCase` holds the four bodies (``emit_outcome`` /
  ``emit_receipt`` / ``maybe_persist`` / ``emit_marker_timeout_guidance``).
- :class:`LiveDeliveryRecordOps` routes ``die`` through the :mod:`commands` module
  *at call time* and lazily imports the sink / infra transport at call time, so
  the existing CLI delivery-record / delivery-sink integration tests (which drive
  ``orchestrate_handoff`` for real and patch the low-level ``commands.*`` seams)
  keep intercepting the side effects unchanged and the application layer does not
  import the ``e_140`` infra transport at module load.

The pure-domain rendering helpers (``build_delivery_record`` /
``build_delivery_record_note`` / ``DeliveryReceipt`` / the ``RECORD_FORMAT_*``
constants / ``SOURCE_REDMINE``) are imported directly from the handoff domain,
exactly as ``commands.py`` did. This is a behavior-preserving restructuring: the
stdout record block, the JSON outcome line, the persistence receipt, and the
stderr marker-timeout trailer are byte-for-byte identical to the original bodies.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any, List, Protocol, runtime_checkable

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    NO_SUBMIT_RETRY_BUDGET,
    RECORD_FORMAT_BOTH,
    RECORD_FORMAT_JSON,
    RECORD_FORMAT_TEXT,
    RECORD_FORMATS,
    SOURCE_REDMINE,
    QueueEnterRetryOutcome,
    TargetActivationOutcome,
    build_delivery_record,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.delivery_record_sink import (
    DeliveryReceipt,
    PERSIST_TRANSPORT_ERROR,
    build_delivery_record_note,
)

if TYPE_CHECKING:  # avoid importing argparse on the hot path
    import argparse


# --------------------------------------------------------------------------- #
# Pure domain (no stdout / stderr)
# --------------------------------------------------------------------------- #


def marker_timeout_guidance_lines(receiver: str, retry_budget: int) -> List[str]:
    """The stderr trailer after a strict-rail `handoff send` marker_timeout (pure).

    Required by Asana task 1214779823377861 to keep agents from collapsing a
    single transient ``marker_timeout`` into the preset's ``Notification fails``
    branch. The structured outcome and durable record already enumerate the
    fallback path; this trailer surfaces it on the failure stream so the agent
    sees it even when the durable record is consumed by a downstream process and
    not re-read. ``retry_budget`` is the ``--no-submit`` attempt cap
    (``NO_SUBMIT_RETRY_BUDGET``); the wording is returned as discrete lines so the
    caller owns the ``print(..., file=sys.stderr)`` side effect.
    """
    cap = retry_budget
    return [
        (
            f"hint: fallback path: `mozyo-bridge read {receiver}` then "
            f"`mozyo-bridge message {receiver} \"<resubmit text>\" --no-submit "
            f"--attempt <N>` (up to {cap} attempts per preset contract; track "
            "remaining with `--attempt N`)."
        ),
        (
            "hint: --no-submit retry budget and the `mozyo-bridge handoff send` "
            "retry pool are separate budgets; do not borrow attempts across them."
        ),
        (
            f"hint: only after the {cap}-attempt --no-submit budget is exhausted "
            "AND the last gate error lacks a literal next-action verb (`read "
            "target again`, `retry`, `refresh`) may the preset's `Notification "
            "fails` branch fire. Record every attempted command and observed "
            "error verbatim in the durable record before escalating."
        ),
    ]


# --------------------------------------------------------------------------- #
# Port
# --------------------------------------------------------------------------- #


@runtime_checkable
class DeliveryRecordOps(Protocol):
    """The environment dependencies the delivery-record use case needs.

    Only the two non-pure dependencies are injected: the ``die`` error-exit (kept
    routed through ``commands`` so a bad ``--record-format`` fails exactly as
    before) and the credential-safe live transport / sink resolution (kept behind
    the port so persistence is testable with a fake sink and no live Redmine).
    """

    def die(self, message: str) -> None:
        """Emit ``message`` and exit non-zero, matching ``commands.die``."""
        ...

    def redmine_delivery_transport_from_env(self) -> Any:
        """The credential-safe live Redmine delivery transport, or ``None``.

        Built only when the trusted ``MOZYO_REDMINE_DELIVERY_WRITE`` opt-in is set
        in the environment; otherwise ``None`` so resolution stays the
        byte-compatible staged ``provider_unavailable`` posture.
        """
        ...

    def resolve_delivery_record_sink(
        self, *, enabled: bool, source: str, redmine_transport: Any
    ) -> Any:
        """Resolve the durable delivery-record sink for ``source``."""
        ...


# --------------------------------------------------------------------------- #
# Use case
# --------------------------------------------------------------------------- #


class DeliveryRecordUseCase:
    """The handoff delivery-rendering / persistence residual behind the port.

    Each method encodes one of the original ``commands`` bodies; behavior is
    byte-for-byte identical to the procedural originals.
    """

    def __init__(self, ops: DeliveryRecordOps) -> None:
        self._ops = ops

    def emit_outcome(
        self,
        outcome,
        *,
        record_format: str = RECORD_FORMAT_BOTH,
        command: str | None = None,
        recovery_command: str | None = None,
        duplicate_lane_panes: list[str] | None = None,
        role_profile_contract: str | None = None,
        retry: QueueEnterRetryOutcome | None = None,
        activation: TargetActivationOutcome | None = None,
        submit_lines: list[str] | None = None,
    ) -> None:
        """Emit the structured outcome and/or the durable delivery-record text.

        ``record_format=both`` (default) prints the multi-line record first, a
        blank separator line, and the single-line JSON outcome last so existing
        callers that scrape the last JSON-looking line keep working while humans
        can paste the record block verbatim into the source-of-truth ticket
        system. ``json`` preserves the prior CLI shape for scripts; ``text`` is
        for callers that only want the markdown.

        ``recovery_command`` (Redmine #12162), ``duplicate_lane_panes`` (Redmine
        #12229), and ``role_profile_contract`` (Redmine #12388) are optional
        markdown-record enrichments threaded into ``build_delivery_record``; none
        of them affects the ``json`` outcome shape that scripts scrape.
        """
        if record_format not in RECORD_FORMATS:
            self._ops.die(
                f"--record-format must be one of {sorted(RECORD_FORMATS)}; got {record_format!r}"
            )
        if record_format in (RECORD_FORMAT_TEXT, RECORD_FORMAT_BOTH):
            print(
                build_delivery_record(
                    outcome,
                    command=command,
                    recovery_command=recovery_command,
                    duplicate_lane_panes=duplicate_lane_panes,
                    role_profile_contract=role_profile_contract,
                    retry=retry,
                    activation=activation,
                    submit_lines=submit_lines,
                )
            )
            if record_format == RECORD_FORMAT_BOTH:
                print("")
        if record_format in (RECORD_FORMAT_JSON, RECORD_FORMAT_BOTH):
            print(outcome.to_json())

    def emit_receipt(self, receipt, *, record_format: str) -> None:
        """Emit the durable delivery-record persistence receipt (Redmine #12311).

        Carries no credential by construction — only the provider id, the
        persisted flag, an explicit reason, an optional ``issue/journal`` location
        pointer, and the record class. ``text`` / ``both`` print a one-line human
        summary; ``json`` / ``both`` print the receipt JSON last so a script can
        scrape it.
        """
        if record_format in (RECORD_FORMAT_TEXT, RECORD_FORMAT_BOTH):
            if receipt.persisted:
                print(
                    f"- Durable delivery record persisted to {receipt.location} "
                    f"(class: {receipt.record_class})"
                )
            else:
                print(
                    "- Durable delivery record not persisted "
                    f"(reason: {receipt.reason})"
                )
        if record_format in (RECORD_FORMAT_JSON, RECORD_FORMAT_BOTH):
            print(receipt.to_json())

    def maybe_persist(
        self,
        args: "argparse.Namespace",
        outcome,
        *,
        duplicate_lane_panes: list[str] | None,
        record_format: str,
        retry: QueueEnterRetryOutcome | None = None,
        activation: TargetActivationOutcome | None = None,
    ) -> None:
        """Best-effort durable persistence of the delivery record (Redmine #12311).

        Opt-in via ``--persist-delivery`` and a no-op otherwise, so the default
        handoff behavior is byte-identical. Called only on the *typed* terminal
        paths (``pending_input`` / ``sent``): a blocked-before-typing outcome has
        no delivery to durably record, and its pasteable record already prints to
        stdout.

        The persisted body is rendered WITHOUT the free-text ``--record-command``
        (Finding 1, j#62549): that field is user-supplied and can carry a private
        path or a credential-shaped argument, so the opt-in durable sink must not
        auto-journal it. The printed stdout record (via :meth:`emit_outcome`)
        still includes ``- Command:`` for human audit-replay; only the
        auto-persisted body omits it. Every other body field is already redacted
        (``execution_root`` / duplicate-pane rows carry no absolute paths), so the
        persisted note carries no unvetted free text.

        This NEVER alters the pane-send outcome: persistence runs after the
        outcome is emitted and any failure — including an unexpected sink error —
        is swallowed and reported as a ``transport_error`` receipt. The live
        Redmine journal-write transport (Redmine #12347) is wired behind a second
        explicit opt-in: ``--persist-delivery`` selects the seam, and the trusted
        ``MOZYO_REDMINE_DELIVERY_WRITE`` env flag enables the live write (resolved
        through the port). Without the env opt-in the transport is ``None`` and
        resolution stays the byte-compatible staged ``provider_unavailable``
        posture; with it the credential-safe transport reads the trusted base URL
        / API key from the env at write time and fails closed
        (``credential_missing`` / ``unauthorized`` / ``provider_unavailable`` /
        ``transport_error``) without ever carrying a credential
        (``vibes/docs/logics/plugin-ready-adapter-boundary.md`` Implementation
        Guardrail #6; the credential boundary is reused verbatim from
        ``redmine_context``).
        """
        if not getattr(args, "persist_delivery", False):
            return
        try:
            # `command=None`: the durable sink path must not auto-journal the
            # user-supplied free-text `--record-command` (Finding 1, j#62549). The
            # stdout record built by `emit_outcome` keeps it for audit-replay.
            record_markdown = build_delivery_record(
                outcome,
                command=None,
                duplicate_lane_panes=duplicate_lane_panes or None,
                retry=retry,
                activation=activation,
            )
            note = build_delivery_record_note(
                outcome,
                record_markdown=record_markdown,
                has_duplicate_advisory=bool(duplicate_lane_panes),
            )
            # Live Redmine journal-write transport (Redmine #12347): resolved
            # through the port, which returns `None` unless the explicit
            # `MOZYO_REDMINE_DELIVERY_WRITE` opt-in is set in the trusted
            # environment; otherwise resolution stays the byte-compatible staged
            # `provider_unavailable` posture. The transport reads the trusted base
            # URL / API key from the env at write time and fails closed without
            # ever carrying a credential.
            redmine_transport = None
            if (outcome.source or "") == SOURCE_REDMINE:
                redmine_transport = self._ops.redmine_delivery_transport_from_env()
            sink = self._ops.resolve_delivery_record_sink(
                enabled=True,
                source=outcome.source or "",
                redmine_transport=redmine_transport,
            )
            receipt = sink.persist(note)
        except Exception:
            # Best-effort: durable persistence must never break or alter the pane
            # send (the delivery already happened). Surface an explicit
            # transport_error receipt instead of raising.
            receipt = DeliveryReceipt(
                provider=getattr(outcome, "source", None),
                persisted=False,
                reason=PERSIST_TRANSPORT_ERROR,
            )
        self.emit_receipt(receipt, record_format=record_format)

    def emit_marker_timeout_guidance(self, receiver: str) -> None:
        """Print the pure :func:`marker_timeout_guidance_lines` to stderr."""
        for line in marker_timeout_guidance_lines(receiver, NO_SUBMIT_RETRY_BUDGET):
            print(line, file=sys.stderr)


# --------------------------------------------------------------------------- #
# Live adapter
# --------------------------------------------------------------------------- #


class LiveDeliveryRecordOps:
    """Live :class:`DeliveryRecordOps`.

    ``die`` routes through the :mod:`commands` module *at call time* so a
    monkeypatched ``commands.die`` still intercepts; the sink / infra transport
    are imported lazily at call time so the application layer never imports the
    ``e_140`` infra transport at module load and the existing delivery-record /
    delivery-sink integration tests keep intercepting unchanged.
    """

    def die(self, message: str) -> None:
        from mozyo_bridge.application import commands as _commands

        _commands.die(message)

    def redmine_delivery_transport_from_env(self) -> Any:
        from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_note_transport import (
            redmine_delivery_transport_from_env,
        )

        return redmine_delivery_transport_from_env()

    def resolve_delivery_record_sink(
        self, *, enabled: bool, source: str, redmine_transport: Any
    ) -> Any:
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.delivery_record_sink import (
            resolve_delivery_record_sink,
        )

        return resolve_delivery_record_sink(
            enabled=enabled,
            source=source,
            redmine_transport=redmine_transport,
        )
