"""Read-only live read of a Redmine project's numeric id (Redmine #13687 R1-F1).

The Issues REST contract states the list filter explicitly:

    project_id: get issues from the project with the given id
                (a numeric value, **not** a project identifier)

    https://www.redmine.org/projects/redmine/wiki/Rest_Issues

The repo's project defaults declare a project *identifier* (a slug), which is what the
Versions endpoint accepts as a path segment. The issues filter does not. Passing the
identifier there would leave the whole point of the project scoping — keeping another
project's issues out of a *shared* Version's bucket — resting on undocumented server
permissiveness: if the server ignored the filter, other projects' issues would enter the
bucket; if it rejected it into an empty set, an empty result would be misread as "no
work". Neither is acceptable on a governed live path (R1 j#76747).

This module closes that gap with the documented resolution step: the Projects endpoint
accepts an id *or an identifier* and returns the project object, whose numeric ``id`` is
what the issues filter requires.

    GET /projects/<identifier>.json  ->  project.id  (numeric)

    https://www.redmine.org/projects/redmine/wiki/Rest_Projects

Same trusted client as its siblings: the same :func:`resolve_redmine_credentials`
boundary, the same redirect-refusing transport, and the same
:class:`RedmineVersionReadUnavailable` reason vocabulary — one fail-closed branch for the
caller, not a third. It is a third endpoint on that client, not a new HTTP client.

Fail-closed: a project that cannot be read, whose id is missing / non-integer / boolean /
non-positive, or whose returned ``identifier`` does not match the one requested, blocks the
read. A project id is never guessed and never derived from a Version's owning project (a
*shared* Version's project is its owner, which is not necessarily the project being scoped).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Mapping, Optional

from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_credentials import (
    resolve_redmine_credentials,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_read_transport import (
    no_redirect_read,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_version_issue_source import (
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

#: Reason for a project whose numeric identity cannot be established from the live read.
READ_PROJECT_UNRESOLVED = "project_unresolved"


class LiveRedmineProjectSource:
    """Read-only reader of ``GET /projects/<identifier>.json``, resolving the numeric id."""

    name = "redmine"

    def __init__(
        self,
        *,
        api_key: Optional[str],
        base_url: Optional[str],
        timeout: float = READ_TIMEOUT_SECONDS,
        opener: Optional[Callable[[urllib.request.Request, float], object]] = None,
    ):
        self._api_key = (api_key or "").strip() or None
        self._base_url = normalize_base_url(base_url)
        self._timeout = timeout
        self._opener = opener or no_redirect_read

    def read_project_id(self, project_identifier: str) -> int:
        """The project's numeric ``id``, or fail closed.

        The identifier is a *path segment value* sent to the trusted base URL, never the
        host, and is percent-encoded so it cannot traverse out of the endpoint. The
        returned ``identifier`` is checked against the requested one, so a redirected /
        substituted project can never silently become the scope of the issues read.
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
                "project identifier is required for a live project read",
                reason=READ_TRANSPORT_ERROR,
            )

        body = self._get(identifier)
        project = body.get("project")
        if not isinstance(project, Mapping):
            raise RedmineVersionReadUnavailable(
                "Redmine returned a malformed project response (no project object)",
                reason=READ_PROJECT_UNRESOLVED,
            )

        returned = project.get("identifier")
        if not isinstance(returned, str) or returned.strip() != identifier:
            # The endpoint accepts an id or an identifier, so a mismatch means the server
            # resolved something other than the project this repo declares. Refuse rather
            # than scope the issues read to a project nobody asked for.
            raise RedmineVersionReadUnavailable(
                f"Redmine resolved project {identifier!r} to a different identifier; "
                "refusing to scope the read to an unexpected project",
                reason=READ_PROJECT_UNRESOLVED,
            )

        project_id = project.get("id")
        # bool is an int subclass: reject it explicitly, along with a missing / negative /
        # zero id. The issues filter needs a real, positive numeric id or nothing at all.
        if (
            not isinstance(project_id, int)
            or isinstance(project_id, bool)
            or project_id <= 0
        ):
            raise RedmineVersionReadUnavailable(
                f"Redmine returned no usable numeric id for project {identifier!r}; "
                "the issues filter requires a numeric project id",
                reason=READ_PROJECT_UNRESOLVED,
            )
        return project_id

    def _get(self, identifier: str) -> Mapping[str, object]:
        """One read-only project query against the TRUSTED base URL only."""
        url = (
            f"{self._base_url}/projects/"
            f"{urllib.parse.quote(identifier, safe='')}.json"
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
                    f"Redmine rejected the project read (HTTP {exc.code})",
                    reason=READ_UNAUTHORIZED,
                ) from None
            raise RedmineVersionReadUnavailable(
                f"Redmine project read failed (HTTP {exc.code})",
                reason=READ_TRANSPORT_ERROR,
            ) from None
        except (urllib.error.URLError, OSError, ValueError):
            raise RedmineVersionReadUnavailable(
                "Redmine project read failed (network error)",
                reason=READ_TRANSPORT_ERROR,
            ) from None
        if not isinstance(body, Mapping):
            raise RedmineVersionReadUnavailable(
                "Redmine returned a non-object project response",
                reason=READ_TRANSPORT_ERROR,
            )
        return body


def live_project_source_from_env(
    *,
    environ: "object | None" = None,
    home: "object | None" = None,
    opener: Optional[Callable[[urllib.request.Request, float], object]] = None,
) -> LiveRedmineProjectSource:
    """Build the read-only project source bound to the trusted-env credentials."""
    creds = resolve_redmine_credentials(home, environ=environ)
    return LiveRedmineProjectSource(
        api_key=creds.api_key, base_url=creds.base_url, opener=opener
    )


__all__ = (
    "READ_PROJECT_UNRESOLVED",
    "LiveRedmineProjectSource",
    "live_project_source_from_env",
)
