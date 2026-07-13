"""Read-only live adapter for Redmine Version open-leaf enumeration (Redmine #12923).

#12651 confirmed the *judgement* surface (fail-closed preflight) and the
open-leaf *read model* (``domain/redmine_version_enumeration``) but deliberately
left the live REST read unwired: no Version-scoped issue credential/adapter was
available in that shell, so the residual split (#12651 j#69306 / j#69369 /
``redmine-version-operation-surface.md`` §4) named "flat issue-by-version 読み
adapter" as follow-up #1. This module is exactly that follow-up — the smallest
credential-safe, **read-only** adapter that satisfies the
:class:`~mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.domain.redmine_version_enumeration.RedmineVersionIssueSource`
port so a real Redmine becomes the issue source behind the same seam, while the
pure leaf rule (``enumerate_open_leaf_issues``) is reused, never re-implemented.

Read-only by construction. This adapter performs **only** ``GET /issues.json``;
it never writes, and it shares no code path with any Version metadata mutation.
The destructive ``RedmineVersionWrites`` port stays unwired (#12651 §2 / §4
follow-up #2); nothing here advances it.

Credential boundary (reused verbatim from ``redmine_context`` / review #56232):

- the **trusted base URL comes only from the daemon environment**
  (``MOZYO_REDMINE_URL``) or the home-scoped, permission-gated credential file —
  never from a repo-local file, CLI argument, or the Version id. The Version id
  is a *query parameter value* sent to that trusted host, never the host.
- the API key comes from the daemon environment / home credential file
  (:func:`resolve_redmine_credentials`), is sent only in the request header, and
  is never echoed into a payload, log, or the :class:`RedmineVersionReadUnavailable`
  reason.
- **no redirect is ever followed** (#13687 j#76650 Finding 1): the default opener is
  ``redmine_read_transport.no_redirect_read``, so a 30x from the trusted base can
  never carry ``X-Redmine-API-Key`` to the ``Location`` host. The refusal surfaces as
  ``transport_error`` — an unreadable Version, never an empty one.
- an optional ``project_id`` scopes the read (``GET /issues.json?project_id=<id>``).
  Redmine Versions can be **shared** across projects, so an unscoped
  ``fixed_version_id`` read can return another project's issues; the governed live
  dispatch path always scopes it, while the legacy ``--live`` debug caller does not.

Fail-closed posture (#12923 acceptance — live-read absence must never be read as
an *empty* Version):

- no trusted base URL -> :class:`RedmineVersionReadUnavailable` (``provider_unavailable``);
- no API key -> ``credential_missing``;
- HTTP 401 / 403 -> ``unauthorized``;
- any other HTTP status, network error, malformed body, or an incomplete page
  walk -> ``transport_error``.

A genuinely empty Version (HTTP 200 with ``{"issues": [], "total_count": 0}``)
returns ``[]`` — the *only* path on which an empty result is legitimate. Every
inability to read raises, so a caller can tell "Version has no open work" apart
from "I could not look", and never renders a network gap as an empty Version.

Explicit opt-in. This adapter is never constructed implicitly: the
``redmine-version list-open-leaf`` handler builds it only under the operator's
explicit ``--live`` flag, so a plain snapshot invocation touches no network. The
credential presence is the second gate, enforced here before any request.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Mapping, Optional, Sequence

from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_credentials import (
    resolve_redmine_credentials,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_read_transport import (
    no_redirect_read,
)
from mozyo_bridge.redmine_context import (
    API_KEY_ENV as _API_KEY_HINT,
    BASE_URL_ENV as _BASE_URL_HINT,
    normalize_base_url,
)

# Fail-closed reason vocabulary. Mirrors the delivery-transport reason names so
# the read and write surfaces degrade with a consistent, redacted vocabulary.
READ_PROVIDER_UNAVAILABLE = "provider_unavailable"
READ_CREDENTIAL_MISSING = "credential_missing"
READ_UNAUTHORIZED = "unauthorized"
READ_TRANSPORT_ERROR = "transport_error"

# How long a single page request may block. Kept short so a slow or unreachable
# Redmine fails closed quickly instead of stalling the advisory CLI.
READ_TIMEOUT_SECONDS = 5

# Redmine caps ``limit`` at 100 per page; walk pages until ``total_count`` is
# covered. ``MAX_PAGES`` is a guard so a hostile / runaway ``total_count`` can
# never spin an unbounded fetch loop — hitting it fails closed (transport_error),
# never silently truncates the snapshot.
PAGE_LIMIT = 100
MAX_PAGES = 100


class RedmineVersionReadUnavailable(Exception):
    """A read-only Version issue read could not be performed.

    Carries an explicit, credential-free ``reason`` (one of the ``READ_*``
    constants) so the CLI can surface a precise block message and a non-zero
    exit, instead of letting a network/credential gap masquerade as an empty
    Version.
    """

    def __init__(self, message: str, *, reason: str):
        super().__init__(message)
        self.reason = reason


def _default_opener(request: urllib.request.Request, timeout: float):
    """Open ``request`` without ever following a redirect. Indirected so tests inject a fake.

    Credential boundary (#13687 j#76650 Finding 1): a plain ``urlopen`` follows a 30x and
    the stdlib copies ``X-Redmine-API-Key`` onto the redirect target, leaking the key to
    the ``Location`` host. :func:`no_redirect_read` refuses the redirect before that request
    is built; the refusal is a ``URLError`` subclass, so ``_get_page`` already maps it onto
    ``transport_error`` — an unreadable Version, never an empty one.
    """
    return no_redirect_read(request, timeout)


class LiveRedmineVersionIssueSource:
    """Read-only ``RedmineVersionIssueSource`` backed by the Redmine REST API.

    Constructed with already-resolved credentials (see
    :func:`live_version_issue_source_from_env`), it performs ``GET
    /issues.json?fixed_version_id=<id>&status_id=*`` against the trusted base
    URL, paginating until the Version's whole issue set is read. Every failure
    raises :class:`RedmineVersionReadUnavailable`; only a successful read of a
    genuinely empty Version returns ``[]``. Satisfies the structural
    :class:`RedmineVersionIssueSource` protocol (``read_version_issues``).
    """

    name = "redmine"

    def __init__(
        self,
        *,
        api_key: Optional[str],
        base_url: Optional[str],
        project_id: Optional[str] = None,
        timeout: float = READ_TIMEOUT_SECONDS,
        page_limit: int = PAGE_LIMIT,
        max_pages: int = MAX_PAGES,
        opener: Optional[Callable[[urllib.request.Request, float], object]] = None,
    ):
        # ``base_url`` is routed through ``normalize_base_url`` so a destination
        # can never be a non-http(s) or path-bearing URL; ``None`` is kept as
        # the explicit "no trusted destination" sentinel (fail-closed at read).
        self._api_key = (api_key or "").strip() or None
        self._base_url = normalize_base_url(base_url)
        # A Redmine Version can be *shared* across projects, so a bare
        # fixed_version_id read can return issues belonging to other projects.
        # ``project_id`` (an identifier or numeric id) scopes the read to the one
        # project the caller declared (#13687 j#76650). ``None`` keeps the
        # pre-existing unscoped read for the snapshot/debug ``--live`` caller.
        self._project_id = (project_id or "").strip() or None
        self._timeout = timeout
        self._page_limit = max(1, page_limit)
        self._max_pages = max(1, max_pages)
        self._opener = opener or _default_opener

    def read_version_issues(
        self, version_id: str
    ) -> Sequence[Mapping[str, object]]:
        """Fetch every issue tagged to ``version_id`` (open and closed).

        Returns the raw REST issue entries for
        :func:`~...redmine_version_enumeration.enumerate_from_source` to parse
        and reduce to open leaves. Raises :class:`RedmineVersionReadUnavailable`
        with an explicit reason on any credential / network / shape problem;
        never returns ``[]`` to signal failure.
        """
        if not self._base_url:
            raise RedmineVersionReadUnavailable(
                "no trusted Redmine base URL configured "
                f"(set {_BASE_URL_HINT})",
                reason=READ_PROVIDER_UNAVAILABLE,
            )
        if not self._api_key:
            raise RedmineVersionReadUnavailable(
                "no Redmine API key in the trusted environment "
                f"(set {_API_KEY_HINT})",
                reason=READ_CREDENTIAL_MISSING,
            )
        vid = str(version_id or "").strip()
        if not vid:
            raise RedmineVersionReadUnavailable(
                "version id is required for a live read",
                reason=READ_TRANSPORT_ERROR,
            )

        collected: list[Mapping[str, object]] = []
        offset = 0
        for _ in range(self._max_pages):
            body = self._get_page(vid, offset)
            issues = body.get("issues")
            if not isinstance(issues, list):
                raise RedmineVersionReadUnavailable(
                    "Redmine returned a malformed issues page (no issues list)",
                    reason=READ_TRANSPORT_ERROR,
                )
            total = body.get("total_count")
            # total_count must be a non-negative integer. A negative count is a
            # malformed Redmine page shape, and crucially must not be trusted as
            # "already covered": `len(collected) >= total` would be true for any
            # negative total and short-circuit a partial/empty read to success
            # (the #12651 negative-count fail-open lineage, j#69343 / #12923
            # j#69440). bool is an int subclass, so reject it explicitly too.
            if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                raise RedmineVersionReadUnavailable(
                    "Redmine returned a malformed issues page "
                    "(missing or negative total_count)",
                    reason=READ_TRANSPORT_ERROR,
                )
            page_issues = [entry for entry in issues if isinstance(entry, Mapping)]
            collected.extend(page_issues)
            if len(collected) >= total:
                # Covered the reported total: a complete snapshot. This is the
                # ONLY success path — including the genuinely-empty Version,
                # where total_count == 0 and the first page returns [].
                return collected
            # total_count is not yet covered, so another page is required. If
            # this page yielded no usable rows the walk cannot make progress:
            # the server's total_count and page contents disagree. Refuse a
            # partial snapshot rather than report a short read as a complete
            # (or empty) Version — the #12923 fail-closed contract (j#69422).
            if not page_issues:
                raise RedmineVersionReadUnavailable(
                    "Redmine returned an empty page before total_count was "
                    "covered; refusing a partial snapshot",
                    reason=READ_TRANSPORT_ERROR,
                )
            offset += self._page_limit
        # Exhausted MAX_PAGES without covering total_count: refuse a partial
        # snapshot rather than understate the Version's open work.
        raise RedmineVersionReadUnavailable(
            "Redmine Version issue set exceeded the page-walk guard; "
            "refusing a partial snapshot",
            reason=READ_TRANSPORT_ERROR,
        )

    def _get_page(self, version_id: str, offset: int) -> Mapping[str, object]:
        """One read-only issues query against the TRUSTED base URL only.

        The API key never leaves the request header and the destination is
        ``self._base_url`` by construction; only the Version id and pagination
        travel as query-parameter values. Maps every failure to an explicit
        :class:`RedmineVersionReadUnavailable` reason.
        """
        params = {
            "fixed_version_id": version_id,
            "status_id": "*",
            "limit": str(self._page_limit),
            "offset": str(offset),
            "sort": "id:asc",
        }
        if self._project_id is not None:
            # Shared Versions are visible from several projects; scoping the read
            # keeps another project's issues out of this project's bucket.
            params["project_id"] = self._project_id
        query = urllib.parse.urlencode(params)
        request = urllib.request.Request(
            f"{self._base_url}/issues.json?{query}",
            headers={"X-Redmine-API-Key": self._api_key or ""},
        )
        try:
            response = self._opener(request, self._timeout)
            try:
                raw = response.read()
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            body = json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise RedmineVersionReadUnavailable(
                    f"Redmine rejected the read (HTTP {exc.code})",
                    reason=READ_UNAUTHORIZED,
                ) from None
            raise RedmineVersionReadUnavailable(
                f"Redmine read failed (HTTP {exc.code})",
                reason=READ_TRANSPORT_ERROR,
            ) from None
        except (urllib.error.URLError, OSError, ValueError):
            raise RedmineVersionReadUnavailable(
                "Redmine read failed (network error)",
                reason=READ_TRANSPORT_ERROR,
            ) from None
        if not isinstance(body, Mapping):
            raise RedmineVersionReadUnavailable(
                "Redmine returned a non-object issues response",
                reason=READ_TRANSPORT_ERROR,
            )
        return body


def live_version_issue_source_from_env(
    *,
    project_id: Optional[str] = None,
    environ: "object | None" = None,
    home: "object | None" = None,
    opener: Optional[Callable[[urllib.request.Request, float], object]] = None,
) -> LiveRedmineVersionIssueSource:
    """Build the read-only live source bound to the trusted-env credentials.

    Resolves credentials through :func:`resolve_redmine_credentials` (daemon
    environment first, then the home-scoped permission-gated credential file —
    never a repo-local file), and returns a configured
    :class:`LiveRedmineVersionIssueSource`. Credential *absence* is not an error
    here: it surfaces as an explicit :class:`RedmineVersionReadUnavailable`
    (``provider_unavailable`` / ``credential_missing``) when
    ``read_version_issues`` is finally called, which is more informative than a
    silent ``None``. ``project_id`` (optional) scopes the read to one project so a
    shared Version cannot pull in another project's issues (#13687); omitted, the
    read stays unscoped as before. ``environ`` / ``home`` / ``opener`` are
    injectable for hermetic tests.
    """
    creds = resolve_redmine_credentials(home, environ=environ)
    return LiveRedmineVersionIssueSource(
        api_key=creds.api_key,
        base_url=creds.base_url,
        project_id=project_id,
        opener=opener,
    )


__all__ = (
    "LiveRedmineVersionIssueSource",
    "RedmineVersionReadUnavailable",
    "READ_CREDENTIAL_MISSING",
    "READ_PROVIDER_UNAVAILABLE",
    "READ_TRANSPORT_ERROR",
    "READ_UNAUTHORIZED",
    "live_version_issue_source_from_env",
)
