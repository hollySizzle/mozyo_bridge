"""Changed-work incremental reconcile (Redmine #14150 review F2).

The provider reconciliation leg must not full-fetch every roster issue every due pass. This module
builds the ``reconcile_incremental_fn`` the supervisor injects: it asks a **provider-neutral
changed-work port** which roster issues changed externally since a durable watermark, folds that with
LOCAL change detection (a per-issue snapshot the provider never sees — an owner resolving, a
generation advancing) and un-accounted local work, and returns the subset to provider-reconcile plus a
commit that advances the watermark + snapshots on a successful pass.

The core selection (:func:`...domain.workspace_supervisor.select_reconcile_issues`) is provider-neutral
and pure. Redmine-specific vocabulary (``updated_on``, ``issues.json``) lives ONLY in the adapter
helper here, never in the domain. The adapter FAILS OPEN: any credential / transport / parse problem
yields ``changed_ok=False`` so the selector reconciles the whole roster — a broken incremental read
never suppresses the provider fallback (the recovery contract).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional, Sequence

from mozyo_bridge.core.state.callback_outbox import CallbackOutbox
from mozyo_bridge.core.state.reconcile_cadence import ReconcileCadenceStore
from mozyo_bridge.core.state.workflow_runtime_store import CALLBACK_INFLIGHT, CALLBACK_PENDING, CALLBACK_UNCERTAIN  # noqa: E501
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (
    issue_reconcile_snapshot,
    select_reconcile_issues,
)

#: The per-issue overlap re-read window (seconds) is expressed as a coarse date margin: Redmine's
#: ``updated_on`` filter is date-granular over the REST API, so the changed-work read subtracts one day
#: from the watermark to guarantee a boundary / same-day update is never missed. The outbox UNIQUE key
#: + generation fence remain the correctness authority, so an over-inclusive changed set only costs a
#: redundant (idempotent) re-fetch, never a mis-delivery.
_CHANGED_WORK_TIMEOUT_SECONDS = 8.0


def _date_floor(iso_ts: str) -> str:
    """The date (YYYY-MM-DD) component of an ISO timestamp, or '' — Redmine ``updated_on`` is date-granular."""
    text = str(iso_ts or "").strip()
    return text[:10] if len(text) >= 10 else ""


def redmine_changed_issue_ids(
    issue_ids: Sequence[str],
    since: str,
    *,
    home: Optional[Path] = None,
    now: str,
    urlopen: Callable = urllib.request.urlopen,
) -> "tuple[frozenset[str], str, bool]":
    """Which of ``issue_ids`` changed on the provider since ``since`` (Redmine #14150 F2; fail-open).

    Queries ``issues.json?issue_id=<ids>&status_id=*&updated_on=>=<since-1day>`` against the TRUSTED
    base URL (the api key stays in the request header, the destination is the resolved base by
    construction), returning ``(changed_ids, next_watermark, ok)``. A credential / transport / parse
    failure returns ``ok=False`` -> the selector reconciles the whole roster (fail-open). ``next_watermark``
    is ``now`` (the query time), advanced by the caller only on a fully-successful pass, with the one-day
    overlap covering the date granularity.

    **Bootstrap (Redmine #14150 review F1)**: a blank ``since`` (never reconciled) does NOT query — it
    returns ``(all ids, now, True)`` so the first pass reconciles the whole roster AND the commit SEEDS
    the watermark to ``now``. The next pass then has a real ``since`` and queries incrementally, instead
    of blank-``since`` fail-open full-reconcile forever (the F1 defect: the watermark was never seeded).
    """
    ids = [str(i).strip() for i in (issue_ids or ()) if str(i).strip()]
    if not ids:
        return frozenset(), str(now or ""), True  # nothing to ask about; a trivially-successful read
    since_date = _date_floor(since)
    if not since_date:
        # Bootstrap (F1): reconcile all this pass AND seed the watermark so the next pass goes
        # incremental. changed=all ids (so the selector reconciles the whole roster), ok=True, so the
        # commit advances the watermark to ``now`` on success.
        return frozenset(ids), str(now or ""), True
    try:
        from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_context import (
            normalize_base_url,
        )
        from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_credentials import (
            resolve_redmine_credentials,
        )
        from mozyo_bridge.shared.paths import mozyo_bridge_home
    except Exception:  # noqa: BLE001 - import problems fail open
        return frozenset(), "", False
    try:
        creds = resolve_redmine_credentials(home or mozyo_bridge_home(), environ={})
        base_url = normalize_base_url(creds.base_url)
        api_key = creds.api_key
    except Exception:  # noqa: BLE001 - unresolvable credentials fail open
        return frozenset(), "", False
    if not base_url or not api_key:
        return frozenset(), "", False
    # A one-day overlap on the date-granular filter (see module note).
    from datetime import date, timedelta

    try:
        y, m, d = (int(x) for x in since_date.split("-"))
        overlap_date = (date(y, m, d) - timedelta(days=1)).isoformat()
    except (ValueError, TypeError):
        overlap_date = since_date
    query = urllib.parse.urlencode(
        {
            "issue_id": ",".join(ids),
            "status_id": "*",
            "updated_on": f">={overlap_date}",
            "limit": str(max(1, len(ids))),
        }
    )
    request = urllib.request.Request(
        f"{base_url}/issues.json?{query}",
        headers={"X-Redmine-API-Key": api_key},
    )
    try:
        with urlopen(request, timeout=_CHANGED_WORK_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, Exception):  # noqa: BLE001 - fail open
        return frozenset(), "", False
    rows = body.get("issues") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        return frozenset(), "", False
    changed = {
        str(r.get("id")).strip()
        for r in rows
        if isinstance(r, dict) and r.get("id") is not None
    }
    return frozenset(changed & set(ids)), str(now or ""), True


def _issues_with_local_work(
    outbox: CallbackOutbox, workspace_id: str
) -> "tuple[frozenset[str], bool]":
    """``(issues carrying un-accounted local outbox work, ok)`` — pending / inflight / uncertain.

    Such an issue MUST be reconciled even if unchanged, so its pending rows are delivered — skipping it
    would strand review_return / lane_gateway rows the local drain deliberately defers. Redmine #14150
    review F3: a read failure returns ``ok=False`` (NOT an empty set) so the caller fails OPEN
    (reconciles the whole roster) — an unreadable outbox must not be silently read as "no local work"
    (which would let an unchanged issue skip its reconcile while its local recovery is unverifiable).
    """
    wsid = str(workspace_id or "").strip()
    try:
        rows = outbox.read(states=[CALLBACK_PENDING, CALLBACK_INFLIGHT, CALLBACK_UNCERTAIN])
    except Exception:  # noqa: BLE001 - an unreadable outbox is a fail-OPEN signal, never "no work"
        return frozenset(), False
    return (
        frozenset(
            str(getattr(r, "issue", "") or "").strip()
            for r in rows
            if str(getattr(r, "workspace_id", "") or "").strip() == wsid
            and str(getattr(r, "issue", "") or "").strip()
        ),
        True,
    )


def build_reconcile_incremental_fn(
    *,
    cadence_store: ReconcileCadenceStore,
    lifecycle_store: object,
    outbox: CallbackOutbox,
    lane_facts_fn: Callable[[str, str], "tuple[str, int, str]"],
    authoritative_map_fn: Callable[[], dict],
    changed_work_fn: Callable[[Sequence[str], str], "tuple[frozenset[str], str, bool]"],
    now_fn: Callable[[], str],
):
    """Build the ``reconcile_incremental_fn(workspace_id, roster) -> (to_reconcile, skipped, commit)``.

    ``changed_work_fn(issue_ids, since) -> (changed_ids, next_watermark, ok)`` is the provider-neutral
    changed-work port (the Redmine adapter is :func:`redmine_changed_issue_ids`; tests inject a fake).
    ``lane_facts_fn`` / ``authoritative_map_fn`` build each issue's LOCAL snapshot; the outbox supplies
    the un-accounted-work set. ``commit`` persists snapshots for the reconciled-without-error issues and
    advances the scope watermark ONLY when EVERY selected reconcile target succeeded (Redmine #14150
    review F2) — a partial failure leaves the watermark so the next pass re-queries the still-unread
    change (a transient provider failure never advances past an un-read issue). An unreadable outbox
    (review F3) fails OPEN to a whole-roster reconcile.
    """

    def _reconcile_incremental_fn(workspace_id: str, roster: Sequence[str]):
        wsid = str(workspace_id or "").strip()
        issues = tuple(dict.fromkeys(str(i).strip() for i in (roster or ()) if str(i).strip()))
        since = cadence_store.read_changed_watermark(wsid)
        changed_ids, next_watermark, changed_ok = changed_work_fn(issues, since)
        try:
            owners = authoritative_map_fn() or {}
        except Exception:  # noqa: BLE001 - an owner-map failure just leaves owner blank in the snapshot
            owners = {}
        snapshot_by_issue: dict = {}
        for issue in issues:
            try:
                lane_id, generation, disposition = lane_facts_fn(wsid, issue)
            except Exception:  # noqa: BLE001 - an unreadable lane leaves a blank snapshot component
                lane_id, generation, disposition = "", 0, ""
            snapshot_by_issue[issue] = issue_reconcile_snapshot(
                lane_id, generation, disposition, owners.get(issue, "")
            )
        prior = cadence_store.read_issue_snapshots(wsid, issues)
        has_local_work, local_work_ok = _issues_with_local_work(outbox, wsid)
        # Redmine #14150 review F3: an unreadable outbox fails OPEN — force the selector to reconcile
        # the whole roster (never let an unchanged issue skip while its local recovery is unverifiable).
        effective_changed_ok = changed_ok and local_work_ok
        to_reconcile, skipped = select_reconcile_issues(
            issues, changed_ids=changed_ids, changed_ok=effective_changed_ok,
            snapshot_by_issue=snapshot_by_issue, prior_snapshot_by_issue=prior,
            has_local_work=has_local_work,
        )
        to_reconcile_set = set(to_reconcile)

        def _commit(reconciled_issues: Sequence[str]) -> None:
            now = now_fn()
            reconciled = {str(i).strip() for i in (reconciled_issues or ())}
            for iss in reconciled:
                if iss in snapshot_by_issue:
                    cadence_store.write_issue_snapshot(
                        wsid, iss, snapshot=snapshot_by_issue[iss], now=now
                    )
            # Redmine #14150 review F2: advance the scope watermark ONLY when EVERY selected target
            # reconciled without error (``to_reconcile_set <= reconciled`` — vacuously true when nothing
            # was selected, so an idle "nothing changed" pass still advances). A partial failure keeps
            # the old watermark so the next pass re-queries the still-unread change (success-only advance).
            if (
                changed_ok
                and local_work_ok
                and next_watermark
                and to_reconcile_set <= reconciled
            ):
                cadence_store.advance_changed_watermark(wsid, watermark=next_watermark, now=now)

        return to_reconcile, skipped, _commit

    return _reconcile_incremental_fn


def default_reconcile_incremental_fn(
    *,
    cadence_store: ReconcileCadenceStore,
    lifecycle_store: object,
    outbox: CallbackOutbox,
    lane_facts_fn: Callable[[str, str], "tuple[str, int, str]"],
    authoritative_map_fn: Callable[[], dict],
    home: Optional[Path],
    now_fn: Callable[[], str],
):
    """The production changed-work incremental selector: the Redmine ``updated_on`` adapter (fail-open)
    folded with local snapshots + un-accounted work. The composition root calls this one factory."""
    return build_reconcile_incremental_fn(
        cadence_store=cadence_store,
        lifecycle_store=lifecycle_store,
        outbox=outbox,
        lane_facts_fn=lane_facts_fn,
        authoritative_map_fn=authoritative_map_fn,
        changed_work_fn=lambda ids, since: redmine_changed_issue_ids(
            ids, since, home=home, now=now_fn()
        ),
        now_fn=now_fn,
    )


__all__ = (
    "redmine_changed_issue_ids",
    "build_reconcile_incremental_fn",
    "default_reconcile_incremental_fn",
)
