"""Compatibility facade — real implementation relocated to
:mod:`mozyo_bridge.features.execution_platform.delegated_coordinator_nested_handoff.delegation_route_executor`.

The US #12570 source-layout pilot (parent Feature #12533 `140_ソース配置管理`) moved
the delegated-coordinator live executor out of the technical-layer ``domain/``
package and into the Redmine Feature-slug package
``features/execution_platform/delegated_coordinator_nested_handoff/`` (Feature
#12510). This legacy import path is preserved per the migration plan
(``vibes/docs/logics/source-layout-bounded-context-migration.md``); the relocated
module object is re-bound here via ``sys.modules`` so that
``mozyo_bridge.domain.delegation_route_executor`` and the new
``mozyo_bridge.features.execution_platform.delegated_coordinator_nested_handoff.delegation_route_executor``
refer to the exact same module object — attribute access and monkeypatch on either
path stay equivalent. Do not remove this facade outside the
fallback-retirement-ledger process.
"""

import sys as _sys

from mozyo_bridge.features.execution_platform.delegated_coordinator_nested_handoff import (
    delegation_route_executor as _impl,
)

_sys.modules[__name__] = _impl
