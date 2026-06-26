"""Compatibility facade — real implementation relocated to
:mod:`mozyo_bridge.features.quality_architecture.domain.module_registry`.

The US #12596 quality_architecture source-layout slice (parent US #12590, Feature
#12533 `140_ソース配置管理`) moved the built-in CLI module registry / composition
classification out of the technical-layer ``domain/`` package and into the Redmine
Epic-slug package ``features/quality_architecture/domain/`` (Epic #12505
`150_品質・アーキテクチャ統治`). This legacy import path is preserved per the migration
plan (``vibes/docs/logics/source-layout-bounded-context-migration.md``) and the
#12591 j#65435 R1 layer-leaf decision; the relocated module object is re-bound
here via ``sys.modules`` so that ``mozyo_bridge.domain.module_registry`` and the
new ``mozyo_bridge.features.quality_architecture.domain.module_registry`` refer to
the exact same module object — attribute access and monkeypatch on either path
stay equivalent. Do not remove this facade outside the fallback-retirement-ledger
process.
"""

import sys as _sys

from mozyo_bridge.features.quality_architecture.domain import (
    module_registry as _impl,
)

_sys.modules[__name__] = _impl
