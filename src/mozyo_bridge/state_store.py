"""Compatibility facade — real implementation relocated to
:mod:`mozyo_bridge.core.state.state_store`.

Unit A of the source-layout bounded-context migration (Redmine #12493) moved the
former top-level managed-state module into the core.state package. This
legacy import path is preserved per the migration plan
(`vibes/docs/logics/source-layout-bounded-context-migration.md`); the relocated
module object is re-bound here via ``sys.modules`` so that
mozyo_bridge.state_store and mozyo_bridge.core.state.state_store refer to the
exact same module object — attribute access and monkeypatch on either path stay
equivalent. Do not remove this facade outside the fallback-retirement-ledger
process.
"""

import sys as _sys

from mozyo_bridge.core.state import state_store as _impl

_sys.modules[__name__] = _impl
