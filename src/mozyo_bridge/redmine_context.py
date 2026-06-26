"""Compatibility facade — real implementation relocated to
:mod:`mozyo_bridge.features.adapter_provider.infrastructure.redmine_context`.

US #12595 (parent US #12590) moved the Redmine API context / env resolution out of
the loose top-level ``mozyo_bridge`` namespace into the ``adapter_provider``
Epic-slug package ``features/adapter_provider/infrastructure/`` (layer-leaf shape,
#12591 j#65435), grouping it with the rest of the Redmine adapter seam. The seam
stays read-only-by-design; no external write / network behavior is added by the
move. The legacy import path ``mozyo_bridge.redmine_context`` is preserved per
``vibes/docs/logics/source-layout-bounded-context-migration.md``; the relocated
module object is re-bound here via ``sys.modules`` so both paths refer to the exact
same module object (attribute access / monkeypatch stay equivalent). Do not remove
this facade outside the fallback-retirement-ledger process.
"""

import sys as _sys

from mozyo_bridge.features.adapter_provider.infrastructure import (
    redmine_context as _impl,
)

_sys.modules[__name__] = _impl
