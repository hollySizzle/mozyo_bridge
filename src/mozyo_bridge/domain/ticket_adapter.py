"""Compatibility facade — real implementation relocated to
:mod:`mozyo_bridge.e_140_adapter_provider.f_110_ticket_adapter_common.domain.ticket_adapter`.

US #12627 (parent US #12622, Redmine Epic #12504 `140_Adapter・Provider基盤`) re-homes
the ticket-adapter common contract to the Redmine-numbered Feature package ``src/mozyo_bridge/e_140_adapter_provider/f_110_ticket_adapter_common/domain/`` (Feature #12524
`110_TicketAdapter共通`), superseding the #12595 ``features/adapter_provider/`` epic-slug pilot
(``features/`` root abolished, ``vibes/docs/logics/source-layout-bounded-context-migration.md``
`## #12622 Redmine-Numbered Layout Correction`). The legacy import path
``mozyo_bridge.domain.ticket_adapter`` is preserved; the relocated module object is re-bound here via
``sys.modules`` so both paths refer to the exact same module object (attribute access /
monkeypatch stay equivalent). Do not remove this facade outside the
fallback-retirement-ledger process.
"""

import sys as _sys

from mozyo_bridge.e_140_adapter_provider.f_110_ticket_adapter_common.domain import (
    ticket_adapter as _impl,
)

_sys.modules[__name__] = _impl
