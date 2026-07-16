"""Live, credentialed Redmine journal poll adapter for `workflow watch` (Redmine #13289).

The domain read boundary (:mod:`...domain.redmine_journal_source`) ships two collaborators:
a pure :class:`RedmineJournalSource` **port** and one implementation over an *already
fetched* snapshot (:class:`MappingRedmineJournalSource`). Its docstring names the live,
credentialed auto-poll adapter as an explicit follow-up — "a network read using the existing
read-only ``redmine_context`` machinery + a since/updated_on cursor". This module is that
follow-up: a :class:`RedmineJournalSource` that *reads Redmine over the network* so
``workflow watch --poll`` ingests real journal history without an operator hand-fetching an
``--redmine-json`` snapshot first.

Design (kept inside the tested boundary):

- the **read / extract / convert path is not reimplemented** — the adapter fetches the
  issue-detail JSON and hands it to the pure :class:`MappingRedmineJournalSource`, so the
  both-shapes parsing, empty-note dropping and structured-marker extraction that #12672
  already pins are reused verbatim;
- the **network seam is injected** (:class:`LiveRedmineTransport`), so every test drives a
  fake transport and no unit / integration test ever touches a real Redmine;
- the **credential boundary is the daemon-trusted one** (review #56232 / #12306): the API key
  and base URL come only from :func:`resolve_redmine_credentials` (env, then the home-scoped
  credential file), never from repo-local files, and the key travels only in the request
  header to that trusted base — it is never echoed into output, logs, or journals;
- the **cursor** is a supplied ``since`` (an ISO ``updated_on`` timestamp). It is applied
  client-side: a journal strictly newer than the cursor is kept, one at/older than it is
  skipped, and a journal with no ``created_on`` is kept (the cursor is an efficiency filter,
  not a correctness gate — the intake's ``redmine:<issue>:<journal>`` dedup already makes a
  re-read idempotent, so the cursor never has to be perfectly precise). The cursor is
  argument-supplied, not persisted: this adapter stays a stateless read and leaves the
  workflow-runtime store schema untouched.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Mapping, Protocol, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    MappingRedmineJournalSource,
    RedmineJournalEntry,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_context import (
    normalize_base_url,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_credentials import (
    resolve_redmine_credentials,
)

#: Read-only issue-detail fetch timeout. A live poll is an interactive command, so a slightly
#: more patient budget than the cockpit's 2s is fine, but it stays bounded so an unreachable
#: Redmine fails closed rather than hanging.
DEFAULT_FETCH_TIMEOUT_SECONDS = 5


class LiveRedmineJournalError(RuntimeError):
    """A live poll could not complete (unconfigured credentials / transport failure).

    The message references only env-var names, the issue id, and exception classes — never
    the API key or the resolved URL — so it is safe to surface to stderr / a journal.
    """


class _RefuseRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Fail closed on any HTTP redirect so the API key never follows a 30x off the base.

    Credential-boundary defense (review #13289 j#72712): stdlib
    :meth:`urllib.request.HTTPRedirectHandler.redirect_request` copies every non-content
    request header — including ``X-Redmine-API-Key`` — onto the redirect target ``Request``,
    so a 30x from Redmine / an intermediary / a poisoned config would carry the key to the
    untrusted ``Location`` host. Refusing the redirect *before* the next request is built means
    that request is never sent: the key only ever reaches the trusted base URL. Any redirect is
    unexpected for a read-only issue-detail GET, so failing closed (rather than following
    same-origin only) is the simplest posture that keeps the key on the trusted host.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise LiveRedmineJournalError(
            f"redmine issue fetch received an unexpected HTTP {code} redirect; refusing to "
            "follow it so the API key never leaves the trusted base URL. Point "
            "MOZYO_REDMINE_URL directly at the final Redmine origin."
        )


#: A read-only opener whose redirect handler fails closed (see :class:`_RefuseRedirectHandler`).
#: Built once; it holds no per-request state and performs no I/O at construction.
_NO_REDIRECT_OPENER = urllib.request.build_opener(_RefuseRedirectHandler())


class LiveRedmineTransport(Protocol):
    """The injected network seam: one read-only issue-detail GET.

    An implementation performs a single ``GET issues/<id>.json?include=journals`` against the
    trusted ``base_url`` with the API key in the request header, and returns the parsed JSON
    mapping. Declared as a Protocol so tests inject a fake transport and no real network is
    touched; the default is :func:`urllib_issue_detail_fetch`.
    """

    def __call__(
        self,
        *,
        base_url: str,
        api_key: str,
        issue_id: str,
        since: str | None,
    ) -> Mapping[str, object]: ...


def urllib_issue_detail_fetch(
    *,
    base_url: str,
    api_key: str,
    issue_id: str,
    since: str | None,
    timeout: float = DEFAULT_FETCH_TIMEOUT_SECONDS,
) -> Mapping[str, object]:
    """Default transport: one read-only ``issues/<id>.json?include=journals`` GET.

    The destination is the trusted ``base_url`` by construction and the API key travels only
    in the ``X-Redmine-API-Key`` header — no caller-supplied URL and no key in the query
    string. Redmine's issue-detail endpoint has no journal-level ``since`` filter, so the
    cursor is applied client-side by the adapter; ``since`` is accepted here only to satisfy
    the transport signature. The request goes through :data:`_NO_REDIRECT_OPENER` so a 30x can
    never carry the key off the trusted base URL (review #13289 j#72712). Any network / decode
    failure is raised as :class:`LiveRedmineJournalError` (its message never carries the key or
    the URL).
    """
    query = urllib.parse.urlencode({"include": "journals"})
    url = f"{base_url}/issues/{urllib.parse.quote(str(issue_id), safe='')}.json?{query}"
    request = urllib.request.Request(url, headers={"X-Redmine-API-Key": api_key})
    try:
        with _NO_REDIRECT_OPENER.open(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise LiveRedmineJournalError(
            f"redmine issue {issue_id} journal fetch failed "
            f"({exc.__class__.__name__}); the live poll is skipped"
        ) from exc
    if not isinstance(body, Mapping):
        raise LiveRedmineJournalError(
            f"redmine issue {issue_id} fetch returned a "
            f"{type(body).__name__}, not an issue-detail object"
        )
    return body


def _journal_mappings(payload: Mapping[str, object]) -> list[Mapping[str, object]]:
    """The journal objects from an issue-detail payload (both real shapes).

    Mirrors :meth:`MappingRedmineJournalSource._journals` at the *object* level so the cursor
    can read each journal's ``created_on`` before the entry projection drops it: the top-level
    ``journals`` list (MCP / export wrapper) wins, otherwise ``issue.journals`` (the REST
    ``?include=journals`` shape) is read. A bare string is never a journals list.
    """

    def _as_list(raw: object) -> list[Mapping[str, object]] | None:
        if isinstance(raw, str) or not isinstance(raw, Sequence):
            return None
        return [j for j in raw if isinstance(j, Mapping)]

    top = _as_list(payload.get("journals"))
    if top is not None:
        return top
    issue = payload.get("issue")
    if isinstance(issue, Mapping):
        nested = _as_list(issue.get("journals"))
        if nested is not None:
            return nested
    return []


def _after_cursor(journal: Mapping[str, object], since: str) -> bool:
    """True when ``journal`` is strictly newer than the ``since`` cursor (keep it).

    A journal with no ``created_on`` is kept: the cursor cannot place it, and the intake's
    anchor dedup makes an over-inclusive read idempotent, so the filter fails open rather than
    dropping a possibly-new event. ISO-8601 UTC timestamps sort lexically, so a string compare
    is a correct ordering for the Redmine ``created_on`` shape (``2026-07-05T07:55:57Z``).
    """
    created = str(journal.get("created_on", "")).strip()
    if not created:
        return True
    return created > since


def _apply_since(
    payload: Mapping[str, object], since: str | None
) -> Mapping[str, object]:
    """Return ``payload`` with journals at/older than the ``since`` cursor removed.

    When ``since`` is empty the payload is returned unchanged, so the no-cursor path is the
    exact snapshot path :class:`MappingRedmineJournalSource` already handles (both shapes,
    verbatim). With a cursor, the kept journals are re-emitted under the top-level ``journals``
    key (which the source treats as authoritative) alongside the issue id.
    """
    if not since:
        return payload
    kept = [j for j in _journal_mappings(payload) if _after_cursor(j, since)]
    issue = payload.get("issue")
    issue_id = issue.get("id") if isinstance(issue, Mapping) else None
    return {"issue": {"id": issue_id}, "journals": kept}


@dataclass(frozen=True)
class LiveRedmineJournalSource:
    """A :class:`RedmineJournalSource` that reads an issue's journals live over HTTP.

    ``base_url`` is the trusted Redmine origin and ``api_key`` the daemon-trusted key; both are
    normally resolved by :meth:`from_environment` from env / the home credential file, never a
    repo-local file. ``transport`` is the injected network seam (defaulting to
    :func:`urllib_issue_detail_fetch`); ``since`` is the optional ISO ``updated_on`` cursor.
    ``warnings`` carries any pre-redacted credential warnings for the caller to surface.

    :meth:`read_entries` fetches the issue-detail JSON, applies the cursor, and delegates the
    parse to the pure :class:`MappingRedmineJournalSource` — so the marker extraction stays the
    one tested boundary and this class only adds the network + cursor layer.
    """

    base_url: str
    api_key: str
    transport: LiveRedmineTransport = urllib_issue_detail_fetch
    since: str | None = None
    warnings: tuple[str, ...] = field(default=())

    #: Every :meth:`read_entries` performs a NEW network fetch, so two reads can legitimately
    #: differ and a re-read is a real guard. Actuating callers (the #13889 callback sweep) require
    #: this to be positively declared before they may mutate — a snapshot source declares it False,
    #: so its "re-read" can never be mistaken for a fresh observation (review R2-F1).
    fresh_read: ClassVar[bool] = True

    @classmethod
    def from_environment(
        cls,
        *,
        since: str | None = None,
        transport: LiveRedmineTransport | None = None,
        home: Path | None = None,
        environ: "Mapping[str, str] | None" = None,
    ) -> "LiveRedmineJournalSource":
        """Build the adapter from daemon-trusted credentials, or fail closed if unconfigured.

        The API key and base URL come only from :func:`resolve_redmine_credentials` (env first,
        then the home-scoped credential file). A missing key or an unusable base URL raises
        :class:`LiveRedmineJournalError` naming only the env vars — a repo-local file can never
        supply either. ``environ`` / ``home`` are injectable so tests resolve credentials
        hermetically without touching the real environment.
        """
        credentials = resolve_redmine_credentials(home, environ=environ)
        base_url = normalize_base_url(credentials.base_url)
        if not credentials.api_key or not base_url:
            raise LiveRedmineJournalError(
                "live Redmine poll is unconfigured: set MOZYO_REDMINE_API_KEY and "
                "MOZYO_REDMINE_URL (or the home-scoped redmine-credentials.yaml). "
                "A repo-local file can never supply the key or the destination."
            )
        return cls(
            base_url=base_url,
            api_key=credentials.api_key,
            transport=transport or urllib_issue_detail_fetch,
            since=(since or None),
            warnings=credentials.warnings,
        )

    def read_entries(self, issue_id: str) -> Sequence[RedmineJournalEntry]:
        """Fetch the issue's journals live and project them onto journal entries.

        Raises :class:`LiveRedmineJournalError` for a missing issue id or a transport failure;
        both are visible, fail-closed degradations rather than a silent empty read.
        """
        issue = str(issue_id or "").strip()
        if not issue:
            raise LiveRedmineJournalError(
                "live Redmine poll requires an issue id (pass --source-issue)"
            )
        payload = self.transport(
            base_url=self.base_url,
            api_key=self.api_key,
            issue_id=issue,
            since=self.since,
        )
        if not isinstance(payload, Mapping):
            raise LiveRedmineJournalError(
                f"live transport for issue {issue} returned a "
                f"{type(payload).__name__}, not an issue-detail mapping"
            )
        filtered = _apply_since(payload, self.since)
        return MappingRedmineJournalSource(payload=filtered).read_entries(issue)


__all__ = (
    "DEFAULT_FETCH_TIMEOUT_SECONDS",
    "LiveRedmineJournalError",
    "LiveRedmineTransport",
    "LiveRedmineJournalSource",
    "urllib_issue_detail_fetch",
)
