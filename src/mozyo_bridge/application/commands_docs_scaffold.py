"""Compatibility facade — real implementation relocated to
:mod:`mozyo_bridge.features.governance_distribution.application.commands_docs_scaffold`.

The #12590 Source/Test slug-layout expansion (child task #12594) moved the
governance docs/scaffold command handlers out of the technical-layer
``application/`` package and into the Redmine Epic-slug package
``features/governance_distribution/application/`` (Epic #12503 `130_統治・Scaffold配布`),
using the R1 layer-leaf shape recorded in Redmine #12591 j#65435. This legacy
import path is preserved per the migration plan
(``vibes/docs/logics/source-layout-bounded-context-migration.md``); the relocated
module object is re-bound here via ``sys.modules`` so that
``mozyo_bridge.application.commands_docs_scaffold`` and the new
``mozyo_bridge.features.governance_distribution.application.commands_docs_scaffold``
refer to the exact same module object — attribute access and monkeypatch on
either path (including the ``application.commands`` re-export) stay equivalent. Do
not remove this facade outside the fallback-retirement-ledger process.
"""

import sys as _sys

from mozyo_bridge.features.governance_distribution.application import (
    commands_docs_scaffold as _impl,
)

_sys.modules[__name__] = _impl
