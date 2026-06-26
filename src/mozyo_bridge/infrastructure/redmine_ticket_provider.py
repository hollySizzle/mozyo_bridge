"""Compatibility facade — real implementation relocated to
:mod:`mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_ticket_provider`.

US #12627 (parent US #12622, Redmine Epic #12504 `140_Adapter・Provider基盤`) re-homes
the Redmine ticket provider to the Redmine-numbered Feature package ``src/mozyo_bridge/e_140_adapter_provider/f_120_redmine_adapter/infrastructure/`` (Feature #12525
`120_RedmineAdapter`), superseding the #12595 ``features/adapter_provider/`` epic-slug pilot
(``features/`` root abolished, ``vibes/docs/logics/source-layout-bounded-context-migration.md``
`## #12622 Redmine-Numbered Layout Correction`). No external write / network behavior is added by the move. The legacy import path
``mozyo_bridge.infrastructure.redmine_ticket_provider`` is preserved; the relocated module object is re-bound here via
``sys.modules`` so both paths refer to the exact same module object (attribute access /
monkeypatch stay equivalent). Do not remove this facade outside the
fallback-retirement-ledger process.
"""

import sys as _sys

from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure import (
    redmine_ticket_provider as _impl,
)

_sys.modules[__name__] = _impl
