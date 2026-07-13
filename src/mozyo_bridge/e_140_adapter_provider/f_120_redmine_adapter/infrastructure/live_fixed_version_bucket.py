"""Live, project-scoped fixed-version lane-bucket read (Redmine #13687 Increment 1).

Composes the two read-only f_120 live sources —
:class:`LiveRedmineProjectVersionSource` (Version metadata) and
:class:`LiveRedmineVersionIssueSource` (the Version's issues) — into the input the pure
:class:`RedmineFixedVersionLaneBucketProvider` already consumes, so ``workflow
dispatch-plan --live-redmine`` plans against a real Redmine without re-implementing the
bucket / leaf rule. The pure provider is used **unchanged**: its snapshot semantics stay
byte-identical, and every live-only strictness lives here.

Read-only by construction: two ``GET``s and no write, no actuation, no handoff. This is
Increment 1's whole surface (j#76650); selection and dispatch are Increment 3.

Why this layer is stricter than the pure provider (j#76646 Finding 2 / j#76650)
------------------------------------------------------------------------------
The pure provider treats an *unknown* Version status as "proceed" — correct for an
advisory snapshot plan, but fail-open on a path that feeds a governed dispatch. So the
live path refuses anything it cannot positively confirm, and every refusal is a
**blocked read** (a raised :class:`RedmineVersionReadUnavailable`, exit non-zero), never
a zero-candidate plan:

- the project identifier / declared host cannot be read from the repo's cataloged
  defaults -> ``project_unresolved``;
- the declared host does not match the trusted credential base URL -> ``project_host_mismatch``
  (raised **before any network call**, so a hostile checkout can never draw the key to
  its own host);
- the requested Version is not among the ones the project can see -> ``version_not_found``
  (this is also how a cross-project Version id is caught);
- a Version *name* matches several ids -> ``version_ambiguous`` (never guessed);
- the Version's status is not positively ``open`` — closed, locked, or absent/unknown ->
  ``version_not_open``.

"Could not look" is thereby never reported as "nothing to do".
"""

from __future__ import annotations

import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.domain.fixed_version_lane_bucket_provider import (
    RedmineFixedVersionLaneBucketProvider,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_context import (
    normalize_base_url,
    read_redmine_project,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_credentials import (
    resolve_redmine_credentials,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_project_version_source import (
    LiveRedmineProjectVersionSource,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_version_issue_source import (
    READ_CREDENTIAL_MISSING,
    READ_PROVIDER_UNAVAILABLE,
    LiveRedmineVersionIssueSource,
    RedmineVersionReadUnavailable,
)

# Live-composition refusal reasons. They extend the issue source's transport-level
# vocabulary (``provider_unavailable`` / ``credential_missing`` / ``unauthorized`` /
# ``transport_error``) with the governance-level blocks this layer adds; they are carried
# by the same exception so a caller has one fail-closed branch, not two.
LIVE_PROJECT_UNRESOLVED = "project_unresolved"
LIVE_PROJECT_HOST_MISMATCH = "project_host_mismatch"
LIVE_VERSION_NOT_FOUND = "version_not_found"
LIVE_VERSION_AMBIGUOUS = "version_ambiguous"
LIVE_VERSION_NOT_OPEN = "version_not_open"

#: The only Version status a live dispatch plan may be built from.
_OPEN_STATUS = "open"

_Opener = Callable[[urllib.request.Request, float], object]


def _text(value: object) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass(frozen=True)
class LiveBucketRead:
    """The outcome of a successful live bucket read.

    ``provider`` is the pure #12919 provider loaded with exactly the live-read payloads,
    so the caller resolves the bucket through the same code path a snapshot run uses.
    ``project_identifier`` / ``version_id`` / ``version_name`` describe what was actually
    read, for the coordinator's durable dispatch record.
    """

    provider: RedmineFixedVersionLaneBucketProvider
    project_identifier: str
    version_id: str
    version_name: Optional[str]
    issue_count: int


def _resolve_project(repo_root: Path) -> tuple[str, str]:
    """The repo's declared ``(project identifier, host)``, or fail closed."""
    identifier, declared_base = read_redmine_project(repo_root)
    if not identifier:
        raise RedmineVersionReadUnavailable(
            "no Redmine project identifier in the repo's project defaults; a live read "
            "cannot know which project to scope to",
            reason=LIVE_PROJECT_UNRESOLVED,
        )
    if not declared_base:
        raise RedmineVersionReadUnavailable(
            f"the repo's project defaults declare no usable Redmine URL for "
            f"project {identifier!r}; a live read cannot verify the destination",
            reason=LIVE_PROJECT_UNRESOLVED,
        )
    return identifier, declared_base


def _resolve_trusted_base(environ, home) -> tuple[str, str]:
    """The daemon-trusted ``(base URL, api key)``, or fail closed."""
    creds = resolve_redmine_credentials(home, environ=environ)
    base_url = normalize_base_url(creds.base_url)
    if not base_url:
        raise RedmineVersionReadUnavailable(
            "no trusted Redmine base URL configured (set MOZYO_REDMINE_URL)",
            reason=READ_PROVIDER_UNAVAILABLE,
        )
    if not creds.api_key:
        raise RedmineVersionReadUnavailable(
            "no Redmine API key in the trusted environment (set MOZYO_REDMINE_API_KEY)",
            reason=READ_CREDENTIAL_MISSING,
        )
    return base_url, creds.api_key


def _select_version(
    versions: Sequence[Mapping[str, object]],
    *,
    bucket_id: Optional[str],
    bucket_name: Optional[str],
) -> Mapping[str, object]:
    """The one project-visible Version the caller asked for, or fail closed.

    Selecting from the *project's* version list is what turns "this Version belongs to
    another project" into an explicit ``version_not_found`` block instead of a silent
    cross-project read.
    """
    if bucket_id:
        for entry in versions:
            if _text(entry.get("id")) == bucket_id:
                return entry
        raise RedmineVersionReadUnavailable(
            f"version id {bucket_id!r} is not available in this project",
            reason=LIVE_VERSION_NOT_FOUND,
        )

    matches = [entry for entry in versions if _text(entry.get("name")) == bucket_name]
    if not matches:
        raise RedmineVersionReadUnavailable(
            f"no version named {bucket_name!r} is available in this project",
            reason=LIVE_VERSION_NOT_FOUND,
        )
    ids = sorted({_text(entry.get("id")) or "" for entry in matches})
    if len(ids) > 1:
        raise RedmineVersionReadUnavailable(
            f"version name {bucket_name!r} matches multiple ids: {', '.join(ids)}",
            reason=LIVE_VERSION_AMBIGUOUS,
        )
    return matches[0]


def _require_open(entry: Mapping[str, object], version_id: str) -> None:
    """Refuse any Version whose status is not positively ``open``."""
    status = _text(entry.get("status"))
    if status is None or status.lower() != _OPEN_STATUS:
        raise RedmineVersionReadUnavailable(
            f"version #{version_id} status is {status or 'unknown'!r}, not 'open'; "
            "a live dispatch plan is only built from a confirmed-open version",
            reason=LIVE_VERSION_NOT_OPEN,
        )


def read_live_fixed_version_bucket(
    *,
    repo_root: Path,
    bucket_id: Optional[str] = None,
    bucket_name: Optional[str] = None,
    environ: "object | None" = None,
    home: "object | None" = None,
    opener: Optional[_Opener] = None,
) -> LiveBucketRead:
    """Read one project-scoped, confirmed-open Version bucket live (read-only).

    Raises :class:`RedmineVersionReadUnavailable` on every credential / host / project /
    version / transport problem, so the caller blocks with an explicit reason and a
    non-zero exit rather than emitting an empty-looking plan.
    """
    identifier, declared_base = _resolve_project(repo_root)
    trusted_base, api_key = _resolve_trusted_base(environ, home)
    if declared_base != trusted_base:
        # The repo declares a host; the credential env owns the trusted one. A mismatch
        # means this checkout is not the workspace the key belongs to — refuse before
        # any request is issued, so the key is never sent anywhere on this run.
        raise RedmineVersionReadUnavailable(
            f"the repo's declared Redmine host for project {identifier!r} does not match "
            "the trusted credential host; refusing a live read",
            reason=LIVE_PROJECT_HOST_MISMATCH,
        )

    selector_id = _text(bucket_id)
    selector_name = _text(bucket_name)
    if selector_id is None and selector_name is None:
        raise RedmineVersionReadUnavailable(
            "a live read needs --bucket-id or --bucket-name to select the version",
            reason=LIVE_VERSION_NOT_FOUND,
        )

    version_source = LiveRedmineProjectVersionSource(
        api_key=api_key, base_url=trusted_base, opener=opener
    )
    versions = version_source.read_project_versions(identifier)
    entry = _select_version(
        versions, bucket_id=selector_id, bucket_name=selector_name
    )
    version_id = _text(entry.get("id"))
    if version_id is None:
        raise RedmineVersionReadUnavailable(
            "the resolved version carries no id; refusing an unidentifiable bucket",
            reason=LIVE_VERSION_NOT_FOUND,
        )
    _require_open(entry, version_id)

    issue_source = LiveRedmineVersionIssueSource(
        api_key=api_key,
        base_url=trusted_base,
        project_id=identifier,
        opener=opener,
    )
    issues = list(issue_source.read_version_issues(version_id))

    # The pure provider is fed the live payloads in the exact snapshot shapes it already
    # parses, so the bucket / leaf / umbrella rules stay the one tested authority. The
    # versions payload carries only the resolved (confirmed-open) entry.
    provider = RedmineFixedVersionLaneBucketProvider(
        issues_payload={"issues": issues},
        versions_payload={"versions": [entry]},
    )
    return LiveBucketRead(
        provider=provider,
        project_identifier=identifier,
        version_id=version_id,
        version_name=_text(entry.get("name")),
        issue_count=len(issues),
    )


__all__ = (
    "LIVE_PROJECT_HOST_MISMATCH",
    "LIVE_PROJECT_UNRESOLVED",
    "LIVE_VERSION_AMBIGUOUS",
    "LIVE_VERSION_NOT_FOUND",
    "LIVE_VERSION_NOT_OPEN",
    "LiveBucketRead",
    "read_live_fixed_version_bucket",
)
