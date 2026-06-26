"""Compatibility facade — real implementation relocated to
:mod:`mozyo_bridge.features.adapter_provider.domain.plugin_manifest`.

US #12595 (parent US #12590) moved the plugin manifest / marketplace records out
of the technical-layer ``domain/`` package into the ``adapter_provider`` Epic-slug
package ``features/adapter_provider/domain/`` (layer-leaf shape, #12591 j#65435).
The legacy import path ``mozyo_bridge.domain.plugin_manifest`` is preserved per
``vibes/docs/logics/source-layout-bounded-context-migration.md``; the relocated
module object is re-bound here via ``sys.modules`` so both paths refer to the exact
same module object (attribute access / monkeypatch stay equivalent). Do not remove
this facade outside the fallback-retirement-ledger process.
"""

import sys as _sys

from mozyo_bridge.features.adapter_provider.domain import (
    plugin_manifest as _impl,
)

_sys.modules[__name__] = _impl
