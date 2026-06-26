"""Compatibility facade — real implementation relocated to
:mod:`mozyo_bridge.features.adapter_provider.application.presentation_runtime`.

US #12595 (parent US #12590) moved the presentation-provider runtime out of the
technical-layer ``application/`` package into the ``adapter_provider`` Epic-slug
package ``features/adapter_provider/application/`` (layer-leaf shape, #12591
j#65435). The legacy import path ``mozyo_bridge.application.presentation_runtime``
is preserved per ``vibes/docs/logics/source-layout-bounded-context-migration.md``;
the relocated module object is re-bound here via ``sys.modules`` so both paths
refer to the exact same module object (attribute access / monkeypatch stay
equivalent). Do not remove this facade outside the fallback-retirement-ledger
process.
"""

import sys as _sys

from mozyo_bridge.features.adapter_provider.application import (
    presentation_runtime as _impl,
)

_sys.modules[__name__] = _impl
