"""Compatibility facade — real implementation relocated to
:mod:`mozyo_bridge.e_150_quality_architecture.f_130_module_health.domain.module_registry`.

#12628 (parent US #12622, Feature #12533 `140_ソース配置管理`) moved the built-in CLI
module registry / composition classification into the Redmine-numbered Epic/Feature
package ``e_150_quality_architecture/f_130_module_health/domain/`` (Epic #12505
`150_品質・アーキテクチャ統治`, Feature #12532 `130_モジュール健全性管理`), superseding the
earlier ``features/quality_architecture/domain/`` slug pilot (US #12596). This legacy
import path is preserved per the migration plan
(``vibes/docs/logics/source-layout-bounded-context-migration.md``) and the #12591
j#65435 R1 layer-leaf decision; the relocated module object is re-bound here via
``sys.modules`` so that ``mozyo_bridge.domain.module_registry`` and the new
``mozyo_bridge.e_150_quality_architecture.f_130_module_health.domain.module_registry``
refer to the exact same module object — attribute access and monkeypatch on either
path stay equivalent. Do not remove this facade outside the
fallback-retirement-ledger process.
"""

import sys as _sys

from mozyo_bridge.e_150_quality_architecture.f_130_module_health.domain import (
    module_registry as _impl,
)

_sys.modules[__name__] = _impl
