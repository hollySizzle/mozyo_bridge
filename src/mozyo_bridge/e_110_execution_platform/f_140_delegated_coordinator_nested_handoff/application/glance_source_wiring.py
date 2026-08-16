"""Namespace-free source wiring for the glance projection (Redmine #15151).

``workflow glance`` reads five adapters: the workflow-runtime advisory store, the
reconcile-state store, the herdr delivery ledger, the glance Redmine source, and
the authority / execution-surface index. Which concrete adapter each resolves to
is *adapter construction*, not workflow judgement — but if the CLI and the local
MCP server each built them, the two entries could silently resolve to different
stores and produce different projections from the same repo. That is exactly the
duplication ``cli-mcp-shared-application-api.md`` closed for the handoff family.

So the construction lives here, once, in a form that reads no ``argparse``
Namespace: :func:`build_glance_sources` takes typed overrides and returns a
:class:`GlanceSources` record. The CLI passes the values it parsed from flags; the
MCP tool passes none and gets the same defaults. Neither can end up on a different
store than the other.

Fail-open at every construction, matching the read-only glance's existing posture:
an unavailable / unreadable adapter degrades to ``None`` (fewer joined facts) and
never raises. The one exception is an explicitly supplied Redmine fixture path
that cannot be read — an operator who named a file expects to hear that it is
unusable, not to get a silently live projection instead.
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


@dataclass(frozen=True)
class GlanceSources:
    """The five resolved glance adapters. Any of them may be ``None`` (degraded)."""

    store: Any = None
    reconcile_store: Any = None
    ledger: Any = None
    redmine_source: Any = None
    authority_index: Mapping[str, Any] = None  # type: ignore[assignment]

    def index(self) -> Mapping[str, Any]:
        """``authority_index``, never ``None``."""
        return self.authority_index or {}


class GlanceFixtureError(ValueError):
    """An explicitly supplied Redmine fixture path could not be read."""


def build_workflow_runtime_store(store_path: Optional[str] = None):
    """The workflow-runtime advisory store (fail-open)."""
    try:
        from mozyo_bridge.core.state.workflow_runtime_store import (
            WorkflowRuntimeStore,
            workflow_runtime_store_path,
        )

        raw = (store_path or "").strip()
        path = Path(raw) if raw else workflow_runtime_store_path()
        return WorkflowRuntimeStore(path=path)
    except Exception:  # noqa: BLE001 - an unavailable store degrades the read-only glance
        return None


def build_reconcile_store(store_path: Optional[str] = None):
    """The reconcile-state store for the central projection (fail-open)."""
    try:
        from mozyo_bridge.core.state.reconcile_state import (
            ReconcileStateStore,
            reconcile_state_path,
        )

        raw = (store_path or "").strip()
        path = Path(raw) if raw else reconcile_state_path()
        return ReconcileStateStore(path=path)
    except Exception:  # noqa: BLE001 - an unreadable store degrades to no reconcile join
        return None


def build_delivery_ledger(
    ledger_path: Optional[str] = None, *, enabled: bool = True
):
    """The herdr delivery ledger, or ``None`` when disabled / unavailable."""
    if not enabled:
        return None
    try:
        from mozyo_bridge.core.state.herdr_delivery_ledger import (
            HerdrDeliveryLedger,
            herdr_delivery_ledger_path,
        )

        raw = (ledger_path or "").strip()
        path = Path(raw) if raw else herdr_delivery_ledger_path()
        return HerdrDeliveryLedger(path=path)
    except Exception:  # noqa: BLE001 - a missing/unreadable ledger degrades to no join
        return None


def build_redmine_source(
    fixture_path: Optional[str] = None, *, live: bool = True
):
    """The glance Redmine source: a fixture when named, else the live adapter.

    Raises :class:`GlanceFixtureError` when ``fixture_path`` was supplied but could
    not be read as JSON. A *live* adapter that is unconfigured or unreachable
    degrades to ``None`` instead — the glance never fails because Redmine is down.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.glance_snapshot_source import (  # noqa: E501
        GlanceLiveRedmineSource,
        MappingGlanceRedmineSource,
    )

    raw = (fixture_path or "").strip()
    if raw:
        try:
            data = _json.loads(Path(raw).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise GlanceFixtureError(
                f"{raw!r} could not be read as JSON: {exc}"
            ) from exc
        payloads = data.get("issues", data) if isinstance(data, dict) else {}
        return MappingGlanceRedmineSource(payloads if isinstance(payloads, dict) else {})
    if not live:
        return None
    try:
        return GlanceLiveRedmineSource.from_environment()
    except Exception:  # noqa: BLE001 - unconfigured / unreachable Redmine degrades
        return None


def build_authority_index() -> Mapping[str, Any]:
    """The non-live authority / execution-surface index (fail-open)."""
    try:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.glance_snapshot_source import (  # noqa: E501
            authority_execution_index,
        )

        return authority_execution_index()
    except Exception:  # noqa: BLE001 - a lifecycle read never breaks the read-only glance
        return {}


def build_glance_sources(
    *,
    store_path: Optional[str] = None,
    reconcile_store_path: Optional[str] = None,
    ledger_path: Optional[str] = None,
    ledger_enabled: bool = True,
    redmine_fixture_path: Optional[str] = None,
    redmine_live: bool = True,
) -> GlanceSources:
    """Build every glance adapter from typed overrides (no Namespace, no argv)."""
    return GlanceSources(
        store=build_workflow_runtime_store(store_path),
        reconcile_store=build_reconcile_store(reconcile_store_path),
        ledger=build_delivery_ledger(ledger_path, enabled=ledger_enabled),
        redmine_source=build_redmine_source(redmine_fixture_path, live=redmine_live),
        authority_index=build_authority_index(),
    )


def roster_for(
    issues: Sequence[str], repo_root: Path
) -> "tuple[tuple[tuple[str, str], ...], Optional[str]]":
    """The active-lane roster + enumeration error: ``(roster, error)``.

    Explicit issue ids form the roster directly. Otherwise the roster is
    enumerated **scoped to this repo's workspace** — a lane owned by another
    workspace on the same host is not this repo's capacity, and an unresolved
    scope degrades rather than falling back to the host-global enumerator, because
    a silent fallback is the leak (#14813 R1-F1).
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.glance_snapshot_source import (  # noqa: E501
        enumerate_active_lanes_for_repo,
    )

    named = [str(i).strip() for i in (issues or []) if str(i).strip()]
    if named:
        return tuple((issue, "") for issue in named), None
    return enumerate_active_lanes_for_repo(repo_root)


__all__ = (
    "GlanceFixtureError",
    "GlanceSources",
    "build_authority_index",
    "build_delivery_ledger",
    "build_glance_sources",
    "build_redmine_source",
    "build_reconcile_store",
    "build_workflow_runtime_store",
    "roster_for",
)
