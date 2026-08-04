"""Credential-safe Redmine issue/journal ownership read (Redmine #14246)."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from mozyo_bridge.core.state.lane_lifecycle_model import is_redmine_id
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_anchor_authority import (
    RedmineAnchorRead,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_credentials import (
    resolve_redmine_credentials,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_read_transport import (
    no_redirect_read,
)
from mozyo_bridge.redmine_context import normalize_base_url


class RedmineAnchorSourceError(RuntimeError):
    """The ownership facts could not be read without guessing."""


@dataclass(frozen=True)
class LiveRedmineAnchorSource:
    """Read ``GET /issues/<id>.json?include=journals`` from the trusted origin."""

    base_url: str
    api_key: str
    timeout: float = 5.0
    opener: Callable[[urllib.request.Request, float], object] = no_redirect_read

    @classmethod
    def from_environment(
        cls,
        *,
        home: Path | None = None,
        environ: Mapping[str, str] | None = None,
        opener: Callable[[urllib.request.Request, float], object] | None = None,
        timeout: float = 5.0,
    ) -> "LiveRedmineAnchorSource":
        credentials = resolve_redmine_credentials(home, environ=environ)
        base_url = normalize_base_url(credentials.base_url)
        if not credentials.api_key or not base_url:
            raise RedmineAnchorSourceError(
                "trusted Redmine credentials are unavailable"
            )
        return cls(
            base_url=base_url,
            api_key=credentials.api_key,
            timeout=timeout,
            opener=opener or no_redirect_read,
        )

    def read_anchor(self, issue: str, journal: str) -> RedmineAnchorRead:
        requested_issue = str(issue or "").strip()
        requested_journal = str(journal or "").strip()
        if not is_redmine_id(requested_issue) or not is_redmine_id(requested_journal):
            raise RedmineAnchorSourceError("invalid Redmine anchor identifiers")
        url = (
            f"{self.base_url}/issues/"
            f"{urllib.parse.quote(requested_issue, safe='')}.json?include=journals"
        )
        request = urllib.request.Request(
            url, headers={"X-Redmine-API-Key": self.api_key}
        )
        try:
            response = self.opener(request, self.timeout)
            try:
                raw = response.read()
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            body = json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return RedmineAnchorRead(
                    requested_issue, requested_journal, False, None, ()
                )
            raise RedmineAnchorSourceError("Redmine ownership read failed") from None
        except (urllib.error.URLError, OSError, TypeError, UnicodeError, ValueError):
            raise RedmineAnchorSourceError("Redmine ownership read failed") from None
        if not isinstance(body, Mapping):
            raise RedmineAnchorSourceError("malformed Redmine ownership response")
        issue_payload = body.get("issue")
        if not isinstance(issue_payload, Mapping):
            raise RedmineAnchorSourceError("malformed Redmine issue response")
        observed_issue = issue_payload.get("id")
        if isinstance(observed_issue, bool) or not isinstance(observed_issue, (int, str)):
            raise RedmineAnchorSourceError("malformed Redmine issue identity")
        observed_issue_text = str(observed_issue).strip()
        if not is_redmine_id(observed_issue_text):
            raise RedmineAnchorSourceError("malformed Redmine issue identity")
        journals = issue_payload.get("journals")
        if not isinstance(journals, list):
            raise RedmineAnchorSourceError("malformed Redmine journal response")
        journal_ids: list[str] = []
        for entry in journals:
            if not isinstance(entry, Mapping):
                raise RedmineAnchorSourceError("malformed Redmine journal entry")
            journal_id = entry.get("id")
            if isinstance(journal_id, bool) or not isinstance(journal_id, (int, str)):
                raise RedmineAnchorSourceError("malformed Redmine journal identity")
            journal_id_text = str(journal_id).strip()
            if not is_redmine_id(journal_id_text):
                raise RedmineAnchorSourceError("malformed Redmine journal identity")
            journal_ids.append(journal_id_text)
        return RedmineAnchorRead(
            requested_issue,
            requested_journal,
            True,
            observed_issue_text,
            tuple(journal_ids),
        )


__all__ = (
    "LiveRedmineAnchorSource",
    "RedmineAnchorSourceError",
)
