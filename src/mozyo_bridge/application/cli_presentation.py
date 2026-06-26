"""Compatibility facade — real implementation relocated to
:mod:`mozyo_bridge.features.operations_cockpit.application.cli_presentation`.

US #12593 (parent US #12590, Feature #12533 `140_ソース配置管理`) expands the
#12570 source-layout pilot to the ``operations_cockpit`` bounded context (Redmine
Epic #12502). The presentation CLI subcommands moved out of the technical-layer ``application/`` package into
the Redmine Epic-slug package ``features/operations_cockpit/application/`` (layer-leaf
shape, #12591 j#65435). This legacy import path is preserved per the migration
plan (``vibes/docs/logics/source-layout-bounded-context-migration.md``); the
relocated module object is re-bound here via ``sys.modules`` so that
``mozyo_bridge.application.cli_presentation`` and the new
``mozyo_bridge.features.operations_cockpit.application.cli_presentation`` refer to the exact same
module object — attribute access and monkeypatch on either path stay equivalent.
Do not remove this facade outside the fallback-retirement-ledger process.
"""

import sys as _sys

from mozyo_bridge.features.operations_cockpit.application import (
    cli_presentation as _impl,
)

_sys.modules[__name__] = _impl
