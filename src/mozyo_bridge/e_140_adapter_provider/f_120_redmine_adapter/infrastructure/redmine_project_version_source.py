"""Read-only live source for a project's Redmine Versions (Redmine #13687 Increment 1).

The sibling :mod:`redmine_version_issue_source` reads a Version's *issues*; nothing in
this adapter reads a Version's *metadata*, so the ``status`` (``open`` / ``locked`` /
``closed``) that gates a lane bucket has, until now, only ever come from an operator's
``--versions-json`` snapshot. Live-reading issues without it would silently drop the
:func:`...lane_bucket_provider.version_status_skip_reason` gate and let a closed or
locked Version yield dispatch candidates — a fail-open regression, not a missing
feature (#13687 j#76646 Finding 2).

This module is that missing read port: the smallest credential-safe, **read-only**
second endpoint on the *same* trusted client — same
:func:`resolve_redmine_credentials` boundary, same redirect-refusing transport, same
:class:`RedmineVersionReadUnavailable` reason vocabulary as the issue source. It is not
a second HTTP client.

Endpoint (j#76650 correction; official REST contract
https://www.redmine.org/projects/redmine/wiki/Rest_Versions):

    GET /projects/<project_identifier>/versions.json

The project-scoped list — not a generic ``/versions.json`` — because it returns exactly
the Versions *available to that project*, including shared ones. Asking the project
which Versions it can see is what makes "this Version belongs to another project" a
detectable mismatch rather than an invisible cross-project read.

Fail-closed posture, identical in spirit to the issue source: every inability to read
raises with an explicit reason, so "the project has no Versions" (HTTP 200 with an
empty list) is the only path on which an empty result is legitimate. A partial page
walk is refused rather than understated.
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
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_version_issue_source import (
    MAX_PAGES,
    PAGE_LIMIT,
    READ_CREDENTIAL_MISSING,
    READ_PROVIDER_UNAVAILABLE,
    READ_TIMEOUT_SECONDS,
    READ_TRANSPORT_ERROR,
    READ_UNAUTHORIZED,
    RedmineVersionReadUnavailable,
)
from mozyo_bridge.redmine_context import (
    API_KEY_ENV as _API_KEY_HINT,
    BASE_URL_ENV as _BASE_URL_HINT,
    normalize_base_url,
)


class LiveRedmineProjectVersionSource:
    """Read-only reader of ``GET /projects/<identifier>/versions.json``.

    Constructed with already-resolved credentials (see
    :func:`live_project_version_source_from_env`). Returns the raw REST version entries
    for the caller to resolve and status-gate; raises
    :class:`RedmineVersionReadUnavailable` on every credential / network / shape problem
    so an unreadable project is never rendered as a project with no Versions.
    """

    name = "redmine"

    def __init__(
        self,
        *,
        api_key: Optional[str],
        base_url: Optional[str],
        timeout: float = READ_TIMEOUT_SECONDS,
        page_limit: int = PAGE_LIMIT,
        max_pages: int = MAX_PAGES,
        opener: Optional[Callable[[urllib.request.Request, float], object]] = None,
    ):
        self._api_key = (api_key or "").strip() or None
        self._base_url = normalize_base_url(base_url)
        self._timeout = timeout
        self._page_limit = max(1, page_limit)
        self._max_pages = max(1, max_pages)
        self._opener = opener or no_redirect_read

    def read_project_versions(
        self, project_identifier: str
    ) -> Sequence[Mapping[str, object]]:
        """Fetch every Version available to ``project_identifier`` (shared ones included).

        The identifier is a *path segment value* sent to the trusted base URL, never the
        host, and is percent-encoded so it can never traverse out of the endpoint.
        """
        if not self._base_url:
            raise RedmineVersionReadUnavailable(
                f"no trusted Redmine base URL configured (set {_BASE_URL_HINT})",
                reason=READ_PROVIDER_UNAVAILABLE,
            )
        if not self._api_key:
            raise RedmineVersionReadUnavailable(
                f"no Redmine API key in the trusted environment (set {_API_KEY_HINT})",
                reason=READ_CREDENTIAL_MISSING,
            )
        identifier = str(project_identifier or "").strip()
        if not identifier:
            raise RedmineVersionReadUnavailable(
                "project identifier is required for a live version read",
                reason=READ_TRANSPORT_ERROR,
            )

        collected: list[Mapping[str, object]] = []
        offset = 0
        for _ in range(self._max_pages):
            body = self._get_page(identifier, offset)
            versions = body.get("versions")
            if not isinstance(versions, list):
                raise RedmineVersionReadUnavailable(
                    "Redmine returned a malformed versions page (no versions list)",
                    reason=READ_TRANSPORT_ERROR,
                )
            page_versions = [v for v in versions if isinstance(v, Mapping)]
            collected.extend(page_versions)
            total = body.get("total_count")
            if total is None:
                # The versions endpoint is not always paginated; without a
                # total_count the single page IS the complete list.
                return collected
            # Mirrors the issue source: a negative / non-integer total_count is a
            # malformed shape and must never be trusted as "already covered", which
            # would short-circuit a partial read to success.
            if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                raise RedmineVersionReadUnavailable(
                    "Redmine returned a malformed versions page "
                    "(negative or non-integer total_count)",
                    reason=READ_TRANSPORT_ERROR,
                )
            if len(collected) >= total:
                return collected
            if not page_versions:
                raise RedmineVersionReadUnavailable(
                    "Redmine returned an empty versions page before total_count was "
                    "covered; refusing a partial snapshot",
                    reason=READ_TRANSPORT_ERROR,
                )
            offset += self._page_limit
        raise RedmineVersionReadUnavailable(
            "Redmine project version list exceeded the page-walk guard; "
            "refusing a partial snapshot",
            reason=READ_TRANSPORT_ERROR,
        )

    def _get_page(self, identifier: str, offset: int) -> Mapping[str, object]:
        """One read-only versions query against the TRUSTED base URL only."""
        query = urllib.parse.urlencode(
            {"limit": str(self._page_limit), "offset": str(offset)}
        )
        url = (
            f"{self._base_url}/projects/"
            f"{urllib.parse.quote(identifier, safe='')}/versions.json?{query}"
        )
        request = urllib.request.Request(
            url, headers={"X-Redmine-API-Key": self._api_key or ""}
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
                    f"Redmine rejected the version read (HTTP {exc.code})",
                    reason=READ_UNAUTHORIZED,
                ) from None
            raise RedmineVersionReadUnavailable(
                f"Redmine version read failed (HTTP {exc.code})",
                reason=READ_TRANSPORT_ERROR,
            ) from None
        except (urllib.error.URLError, OSError, ValueError):
            raise RedmineVersionReadUnavailable(
                "Redmine version read failed (network error)",
                reason=READ_TRANSPORT_ERROR,
            ) from None
        if not isinstance(body, Mapping):
            raise RedmineVersionReadUnavailable(
                "Redmine returned a non-object versions response",
                reason=READ_TRANSPORT_ERROR,
            )
        return body


def live_project_version_source_from_env(
    *,
    environ: "object | None" = None,
    home: "object | None" = None,
    opener: Optional[Callable[[urllib.request.Request, float], object]] = None,
) -> LiveRedmineProjectVersionSource:
    """Build the read-only project-version source bound to the trusted-env credentials.

    Same credential resolution as :func:`live_version_issue_source_from_env`: daemon
    environment first, then the home-scoped permission-gated file — never a repo-local
    one. Credential absence is not an error here; it surfaces as an explicit
    :class:`RedmineVersionReadUnavailable` when the read is finally attempted.
    """
    creds = resolve_redmine_credentials(home, environ=environ)
    return LiveRedmineProjectVersionSource(
        api_key=creds.api_key,
        base_url=creds.base_url,
        opener=opener,
    )


__all__ = (
    "LiveRedmineProjectVersionSource",
    "live_project_version_source_from_env",
)
