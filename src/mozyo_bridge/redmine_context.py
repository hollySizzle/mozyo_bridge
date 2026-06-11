"""Redmine gate-context read model for the cockpit (Redmine #11686).

The third join layer (US #11639 constraint 3): Redmine stays the source
of truth for gate / workflow context ("whose turn is it"), the cockpit
only *reads* it for display, and nothing here ever writes to Redmine —
runtime heartbeats in journals are forbidden by design.

Credential boundary (review #56232): repo-local files must never control
where the API key is sent — a hostile or poisoned checkout could
otherwise declare an attacker host in its ``workspace-defaults.yaml``
and exfiltrate the key. Therefore:

- the **trusted base URL comes only from the daemon environment**
  (``MOZYO_REDMINE_URL``); every request goes to that URL and nothing
  else, ever;
- the workspace's own ``.mozyo-bridge/workspace-defaults.yaml``
  contributes the project identifier (a query parameter sent to the
  trusted host) and a declared URL that must HOST-MATCH the trusted base
  — a mismatch means the workspace belongs to some other Redmine and is
  treated as ``unconfigured`` with no fetch at all;
- the API key comes from the daemon's environment
  (``MOZYO_REDMINE_API_KEY``), never from repo files and never echoed
  into payloads, logs, or journals.

Degradation states (additive ``redmine`` field on unit payloads):

- ``available`` — context fetched; carries the open-issue count and the
  most recently updated open issue (id / subject / status / updated_on).
- ``unconfigured`` — no API key in the daemon env, or the workspace has
  no Redmine project mapping. Not an error; the other two layers stand.
- ``unavailable`` — the workspace is configured but the fetch failed or
  has not happened yet (budgeted). The cockpit shows the gap honestly
  instead of stale certainty.

Fetches are TTL-cached per project and budgeted per call so the
single-threaded daemon can never stall its OTLP ingestion behind a slow
or unreachable Redmine.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import yaml

from mozyo_bridge.workspace_defaults import WORKSPACE_DEFAULTS_INPUT_RELATIVE

API_KEY_ENV = "MOZYO_REDMINE_API_KEY"
BASE_URL_ENV = "MOZYO_REDMINE_URL"


def normalize_base_url(url: str | None) -> str | None:
    """Reduce a URL to its trusted ``scheme://netloc`` form, or ``None``."""
    if not isinstance(url, str) or not url.strip():
        return None
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"

STATE_AVAILABLE = "available"
STATE_UNCONFIGURED = "unconfigured"
STATE_UNAVAILABLE = "unavailable"

# Successful contexts stay fresh for a minute (the UI polls every 5s);
# failures retry sooner but not per-poll.
SUCCESS_TTL_SECONDS = 60
FAILURE_TTL_SECONDS = 30
FETCH_TIMEOUT_SECONDS = 2
# At most this many uncached projects are fetched per attach call, so a
# cold cache over many workspaces warms across polls instead of blocking
# one request for seconds.
DEFAULT_FETCH_BUDGET = 2


def read_redmine_project(repo_root: str | Path) -> tuple[str | None, str | None]:
    """Best-effort ``(identifier, base_url)`` from workspace-defaults.

    ``base_url`` is the scheme+host of ``redmine.default_project.url`` —
    the same host-derivation pattern as ``runtime-config install``, so no
    project-specific host is baked into distributed code. Returns
    ``(None, None)`` on any shape problem; the cockpit degrades to
    ``unconfigured``.
    """
    source = Path(repo_root) / WORKSPACE_DEFAULTS_INPUT_RELATIVE
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None, None
    if not isinstance(raw, dict):
        return None, None
    project = (raw.get("redmine") or {}).get("default_project")
    if not isinstance(project, dict):
        return None, None
    identifier = project.get("identifier")
    url = project.get("url")
    if not isinstance(identifier, str) or not identifier.strip():
        return None, None
    return identifier.strip(), normalize_base_url(
        url if isinstance(url, str) else None
    )


@dataclass
class _CacheEntry:
    payload: dict
    expires_at: float


class RedmineContextCache:
    """TTL-cached, budgeted, read-only Redmine context per project.

    ``base_url`` is the trusted Redmine origin from the daemon env; it is
    the ONLY destination this cache ever sends the API key to. Repo-local
    input can select a project identifier on that host or opt out via a
    host mismatch — it can never select a destination.
    """

    def __init__(self, *, api_key: str | None, base_url: str | None = None):
        self._api_key = api_key
        self._base_url = normalize_base_url(base_url)
        self._cache: dict[str, _CacheEntry] = {}

    def context_for_repo(
        self, repo_root: str | None, *, budget: list[int]
    ) -> dict:
        """The ``redmine`` payload for one unit. Never raises.

        ``budget`` is a single-element mutable counter shared across one
        attach pass; a fetch decrements it and a depleted budget yields
        ``unavailable`` (retried on a later poll once cached entries
        expire or slots free up).
        """
        if not self._api_key or not self._base_url:
            return {"state": STATE_UNCONFIGURED, "project": None}
        if not repo_root:
            return {"state": STATE_UNCONFIGURED, "project": None}
        identifier, workspace_base = read_redmine_project(repo_root)
        if not identifier:
            return {"state": STATE_UNCONFIGURED, "project": None}
        # Credential boundary (review #56232): the workspace's declared
        # host must match the trusted base. A mismatch means this
        # workspace's project lives on some other Redmine — refuse without
        # any fetch, so a hostile workspace-defaults URL can never draw a
        # request (let alone the key) anywhere.
        if workspace_base != self._base_url:
            return {"state": STATE_UNCONFIGURED, "project": identifier}
        now = time.monotonic()
        entry = self._cache.get(identifier)
        if entry is not None and entry.expires_at > now:
            return entry.payload
        if budget[0] <= 0:
            return {"state": STATE_UNAVAILABLE, "project": identifier}
        budget[0] -= 1
        payload = self._fetch(identifier)
        ttl = (
            SUCCESS_TTL_SECONDS
            if payload["state"] == STATE_AVAILABLE
            else FAILURE_TTL_SECONDS
        )
        self._cache[identifier] = _CacheEntry(
            payload=payload, expires_at=now + ttl
        )
        return payload

    def _fetch(self, identifier: str) -> dict:
        """One read-only issues query against the TRUSTED base URL only.

        The API key never leaves the request header, and the destination
        is ``self._base_url`` by construction — no caller-supplied URL
        reaches this method.
        """
        base_url = self._base_url or ""
        query = urllib.parse.urlencode(
            {
                "project_id": identifier,
                "status_id": "open",
                "sort": "updated_on:desc",
                "limit": "1",
            }
        )
        request = urllib.request.Request(
            f"{base_url}/issues.json?{query}",
            headers={"X-Redmine-API-Key": self._api_key or ""},
        )
        try:
            with urllib.request.urlopen(
                request, timeout=FETCH_TIMEOUT_SECONDS
            ) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError):
            return {"state": STATE_UNAVAILABLE, "project": identifier}
        issues = body.get("issues") if isinstance(body, dict) else None
        if not isinstance(issues, list):
            return {"state": STATE_UNAVAILABLE, "project": identifier}
        latest = None
        if issues and isinstance(issues[0], dict):
            top = issues[0]
            status = top.get("status")
            # Surface minimization (review #56232): subjects can carry
            # personal or confidential summaries and the v1 UI does not
            # display them, so they are never put on the payload.
            latest = {
                "id": top.get("id"),
                "status": (
                    status.get("name") if isinstance(status, dict) else None
                ),
                "updated_on": top.get("updated_on"),
            }
        return {
            "state": STATE_AVAILABLE,
            "project": identifier,
            "open_total": body.get("total_count"),
            "latest_issue": latest,
        }


def attach_redmine_context(
    payload: dict,
    cache: RedmineContextCache,
    *,
    fetch_budget: int = DEFAULT_FETCH_BUDGET,
) -> dict:
    """Enrich a units payload's panes with the additive ``redmine`` field.

    Cockpit-layer concern only: the `session list` CLI payload itself
    stays Redmine-free so listing never blocks on the network. Identity
    keys are untouched; degradation never removes or alters the OTel /
    tmux layers.
    """
    budget = [fetch_budget]
    for pane in payload.get("panes") or []:
        if isinstance(pane, dict):
            pane["redmine"] = cache.context_for_repo(
                pane.get("repo_root"), budget=budget
            )
    return payload
