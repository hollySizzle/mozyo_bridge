"""Live, credential-safe Redmine journal-write transport (Redmine #12347).

The delivery-record persistence seam (Redmine #12311) defined the narrow
:class:`~mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.delivery_record_sink.RedmineNoteTransport` write
seam in core but deliberately left the live transport unwired: ticket-write is a
per-task-review surface (``vibes/docs/logics/plugin-ready-adapter-boundary.md``
Implementation Guardrail #6), and ``redmine_context`` is read-only by design, so
production resolved to a fail-closed staged receipt (``write_optin_unset`` when the
live-write opt-in is unset, Redmine #13262). #12347 wires the real transport — the
smallest credential-safe Redmine journal write that keeps every #12311 invariant.

What core still owns (this module never touches): the record class
(``delivery_notification`` is a notification pointer, never a workflow gate or
owner approval), source semantics (a Redmine note is a journal note, never an
Asana comment), and the secret / private-data rule. This module only performs
the provider-owned network write through the protocol seam; the sink
(:class:`~mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.delivery_record_sink.RedmineDeliveryRecordSink`)
owns source/anchor validation and receipt shaping.

Credential boundary (reused verbatim from ``redmine_context``, review #56232):

- the **trusted base URL comes only from the daemon environment**
  (``MOZYO_REDMINE_URL``). The write destination is that host and nothing else,
  ever; no repo-local file, CLI argument, or delivery anchor can redirect where
  the API key is sent. The issue id (the *path*, not the host) comes from the
  durable handoff anchor, which is an issue on that same trusted Redmine.
- the API key comes from the daemon environment (``MOZYO_REDMINE_API_KEY``),
  is sent only in the request header, and is never echoed into a payload, log,
  receipt, or the :class:`DeliveryTransportError` reason.

Explicit opt-in (the "明示 opt-in" of the #12347 acceptance criteria): the live
network write is gated *twice*. ``--persist-delivery`` selects the persistence
seam (existing #12311 CLI opt-in); separately,
``MOZYO_REDMINE_DELIVERY_WRITE`` must be set to an explicit truthy value in the
trusted environment before any live journal write is attempted. Without the
explicit env opt-in :func:`redmine_delivery_transport_from_env` returns ``None``
so resolution stays the byte-compatible staged ``write_optin_unset`` posture (the
unwired sink's reason, Redmine #13262).
Putting the second gate in the environment (not a repo file) keeps it inside the
same trusted boundary as the credentials, so a hostile checkout can never turn a
plain ``--persist-delivery`` into a live write.

Fail-closed reasons (all normalized to
:data:`~mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.delivery_record_sink.PERSIST_FAILURE_REASONS`):

- no/invalid trusted base URL -> ``base_url_unset`` (Redmine #13262; distinct from
  the unwired sink's ``write_optin_unset`` so a missing-URL misconfiguration is not
  confused with the opt-in simply being unset);
- no API key -> ``credential_missing``;
- HTTP 401 / 403 -> ``unauthorized``;
- any other HTTP status, network error, or unexpected failure ->
  ``transport_error``.

There is no dynamic provider loading and no public plugin contract; this is the
single built-in write provider for v0.8.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.delivery_record_sink import (
    PERSIST_BASE_URL_UNSET,
    PERSIST_CREDENTIAL_MISSING,
    PERSIST_TRANSPORT_ERROR,
    PERSIST_UNAUTHORIZED,
    DeliveryTransportError,
)
from mozyo_bridge.redmine_context import (
    API_KEY_ENV,
    BASE_URL_ENV,
    normalize_base_url,
)

# The explicit live-write opt-in. Separate from ``--persist-delivery`` so the
# live network write is a deliberate, trusted-environment decision and a plain
# ``--persist-delivery`` stays the byte-compatible staged seam.
DELIVERY_WRITE_ENV = "MOZYO_REDMINE_DELIVERY_WRITE"

# How long the single journal-write request may block. Kept short so a slow or
# unreachable Redmine fails closed quickly and never stalls the handoff (which
# has already completed by the time persistence runs).
WRITE_TIMEOUT_SECONDS = 5


def _env_flag(value: Optional[str]) -> bool:
    """True only for an explicit truthy opt-in token (case-insensitive)."""
    if value is None:
        return False
    return value.strip().lower() in ("1", "true", "yes", "on")


class RedmineNoteHttpTransport:
    """Post a Redmine journal note via the trusted-base credential boundary.

    Credentials are read from the trusted environment *lazily*, at write time,
    so a transport can be constructed cheaply and the credential state is
    re-evaluated per write. The destination host is always the trusted
    ``MOZYO_REDMINE_URL``; only the issue id (the URL path) comes from the
    caller. Every failure is surfaced as a :class:`DeliveryTransportError` with
    an explicit reason and never carries the API key.
    """

    name = "redmine"

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = WRITE_TIMEOUT_SECONDS,
    ):
        # ``None`` defers to the trusted environment at write time (the normal
        # path). Explicit values are for tests / a future trusted caller; they
        # are still routed only through ``normalize_base_url`` so a destination
        # can never be a non-http(s) or path-bearing URL.
        self._api_key = api_key
        self._base_url = base_url
        self._timeout = timeout

    def _resolved_base_url(self) -> Optional[str]:
        raw = self._base_url if self._base_url is not None else os.environ.get(BASE_URL_ENV)
        return normalize_base_url(raw)

    def _resolved_api_key(self) -> Optional[str]:
        key = self._api_key if self._api_key is not None else os.environ.get(API_KEY_ENV)
        if key is None:
            return None
        key = key.strip()
        return key or None

    def post_issue_note(self, issue_id: str, notes: str) -> str:
        """Append ``notes`` as a journal note on ``issue_id``; fail closed.

        Uses Redmine's ``PUT /issues/<id>.json`` with ``{"issue": {"notes":
        ...}}``, which creates a journal entry. A successful update returns
        ``204 No Content`` (no journal id in the body), so the returned id is the
        empty string per the protocol contract; the sink then records a
        ``redmine:issue=<id>`` location pointer. Raises
        :class:`DeliveryTransportError` with an explicit, credential-free reason
        on any failure.
        """
        base_url = self._resolved_base_url()
        if not base_url:
            # Missing or non-http(s)/host-only base: no trusted destination.
            # Redmine #13262: this is the opt-in-set-but-misconfigured case
            # (``base_url_unset``), distinct from the unwired sink's
            # ``write_optin_unset`` (the opt-in was never set at all).
            raise DeliveryTransportError(
                "no trusted Redmine base URL configured",
                reason=PERSIST_BASE_URL_UNSET,
            )
        api_key = self._resolved_api_key()
        if not api_key:
            raise DeliveryTransportError(
                "no Redmine API key in the trusted environment",
                reason=PERSIST_CREDENTIAL_MISSING,
            )
        # The issue id is the only caller-supplied part, and it is the URL path,
        # never the host. Quote it so it can never inject a query/host segment.
        safe_issue = urllib.parse.quote(str(issue_id), safe="")
        payload = json.dumps({"issue": {"notes": notes}}).encode("utf-8")
        request = urllib.request.Request(
            f"{base_url}/issues/{safe_issue}.json",
            data=payload,
            method="PUT",
            headers={
                "Content-Type": "application/json",
                "X-Redmine-API-Key": api_key,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout):
                # 2xx (Redmine returns 204 No Content for a notes-only update).
                # No journal id is reported; the protocol allows the empty id.
                return ""
        except urllib.error.HTTPError as exc:
            # Map auth failures explicitly; everything else is a transport error.
            # The exception message is for transport diagnostics only and is
            # never copied onto a receipt, but keep the key out of it regardless.
            if exc.code in (401, 403):
                raise DeliveryTransportError(
                    f"Redmine rejected the write (HTTP {exc.code})",
                    reason=PERSIST_UNAUTHORIZED,
                ) from None
            raise DeliveryTransportError(
                f"Redmine write failed (HTTP {exc.code})",
                reason=PERSIST_TRANSPORT_ERROR,
            ) from None
        except (urllib.error.URLError, OSError, ValueError):
            raise DeliveryTransportError(
                "Redmine write failed (network error)",
                reason=PERSIST_TRANSPORT_ERROR,
            ) from None


def redmine_delivery_transport_from_env() -> Optional[RedmineNoteHttpTransport]:
    """Build the live transport iff the explicit live-write opt-in is set.

    Returns a :class:`RedmineNoteHttpTransport` only when
    ``MOZYO_REDMINE_DELIVERY_WRITE`` is an explicit truthy value in the trusted
    environment; otherwise returns ``None`` so the sink resolver stays on the
    byte-compatible staged ``write_optin_unset`` posture (the unwired sink's
    reason, Redmine #13262). Credential presence is intentionally NOT checked here:
    when the operator has opted in but the credentials are missing, the transport
    fails closed at write time with the explicit ``credential_missing`` reason, and
    a missing/invalid base URL fails closed with ``base_url_unset`` — both more
    informative than the old collapsed ``provider_unavailable``.
    """
    if not _env_flag(os.environ.get(DELIVERY_WRITE_ENV)):
        return None
    return RedmineNoteHttpTransport()


__all__ = (
    "DELIVERY_WRITE_ENV",
    "RedmineNoteHttpTransport",
    "redmine_delivery_transport_from_env",
)
