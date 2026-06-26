"""Compatibility facade — real implementation relocated to
:mod:`mozyo_bridge.features.adapter_provider.domain.ticket_adapter`.

US #12595 (parent US #12590, Feature #12533 `140_ソース配置管理`) expands the
#12570 source-layout pilot to the ``adapter_provider`` bounded context (Redmine
Epic #12504). The ticket-adapter common contract moved out of the technical-layer
``domain/`` package into the Redmine Epic-slug package
``features/adapter_provider/domain/`` (layer-leaf shape, #12591 j#65435). This
legacy import path is preserved per the migration plan
(``vibes/docs/logics/source-layout-bounded-context-migration.md``); the relocated
module object is re-bound here via ``sys.modules`` so that
``mozyo_bridge.domain.ticket_adapter`` and the new
``mozyo_bridge.features.adapter_provider.domain.ticket_adapter`` refer to the exact
same module object — attribute access and monkeypatch on either path stay
equivalent. Do not remove this facade outside the fallback-retirement-ledger
process.
"""

import sys as _sys

from mozyo_bridge.features.adapter_provider.domain import (
    ticket_adapter as _impl,
)

_sys.modules[__name__] = _impl
