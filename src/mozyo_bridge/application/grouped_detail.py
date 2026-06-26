"""Compatibility facade — real implementation relocated to
:mod:`mozyo_bridge.e_120_operations_cockpit.f_120_cockpit_web_ui.application.grouped_detail`.

US #12625 (parent US #12622, Feature #12533 `140_ソース配置管理`) migrates the
``operations_cockpit`` bounded context (Redmine Epic #12502) to the Redmine-numbered
Epic/Feature layout ``e_120_operations_cockpit/f_120_cockpit_web_ui/application/`` (#12623 migration map). The real
module moved out of the technical-layer ``application/`` package into the numbered
Feature package; this legacy import path is preserved as a ``sys.modules`` facade
per the migration plan
(``vibes/docs/logics/source-layout-bounded-context-migration.md``) so that
``mozyo_bridge.application.grouped_detail`` and the new numbered path refer to the exact same
module object — attribute access and monkeypatch on either path stay equivalent.
Do not remove this facade outside the fallback-retirement-ledger process.
"""

import sys as _sys

from mozyo_bridge.e_120_operations_cockpit.f_120_cockpit_web_ui.application import (
    grouped_detail as _impl,
)

_sys.modules[__name__] = _impl
