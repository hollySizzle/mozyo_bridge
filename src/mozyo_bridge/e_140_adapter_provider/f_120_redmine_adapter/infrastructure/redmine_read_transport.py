"""Redirect-refusing read transport for the f_120 Redmine REST reads (Redmine #13687).

Every credentialed Redmine read in this adapter sends ``X-Redmine-API-Key`` in the
request header. The stdlib
:meth:`urllib.request.HTTPRedirectHandler.redirect_request` copies non-content
request headers — the API key among them — onto the redirect target ``Request``, so a
30x from Redmine, an intermediary, or a poisoned config would carry the key to the
untrusted ``Location`` host. A read-only GET has no legitimate reason to redirect, so
this module refuses the redirect *before* the next request is built: the key only ever
reaches the trusted base URL.

The refusal is raised as :class:`RedmineRedirectRefused`, a :class:`urllib.error.URLError`
subclass, so a caller that already maps ``URLError`` onto its fail-closed reason
vocabulary (``transport_error``) blocks the read without a new branch — a refused
redirect is a read that could not be performed, never an empty result.

Boundary note (#13687 Increment 1 / j#76650): the equivalent guard in
``f_140 live_redmine_journal_source`` is intentionally left in place and untouched here
to keep this increment's same-file overlap at zero. Converging the two onto this shared
helper is a recorded follow-up, not part of this increment.
"""

from __future__ import annotations

import urllib.error
import urllib.request


class RedmineRedirectRefused(urllib.error.URLError):
    """A Redmine read was redirected and the redirect was refused.

    Raised instead of following the 30x, so the API key never leaves the trusted base
    URL. The message names only the status code and the operator remedy — never the
    key, the destination, or the ``Location`` header.
    """

    def __init__(self, code: int):
        super().__init__(
            f"redmine read received an unexpected HTTP {code} redirect; refusing to "
            "follow it so the API key never leaves the trusted base URL. Point "
            "MOZYO_REDMINE_URL directly at the final Redmine origin."
        )
        self.code = code


class _RefuseRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Fail closed on any HTTP redirect so the API key never follows a 30x off the base."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise RedmineRedirectRefused(code)


#: Built once; holds no per-request state and performs no I/O at construction.
_NO_REDIRECT_OPENER = urllib.request.build_opener(_RefuseRedirectHandler())


def no_redirect_read(request: urllib.request.Request, timeout: float):
    """Perform one read-only request that never follows a redirect.

    Signature-compatible with the ``opener`` seam the f_120 live read sources inject in
    tests (``(request, timeout) -> response``), so a redirect-refusing transport is the
    default without changing how those sources are faked.
    """
    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


__all__ = ("RedmineRedirectRefused", "no_redirect_read")
