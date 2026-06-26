"""Compatibility facade — real implementation relocated to
:mod:`mozyo_bridge.features.execution_platform.handoff_routing.application.cli_handoff`.

Redmine #12592 (parent US #12590 source-layout full expansion) moved this
execution_platform ``application/`` real module into the Feature-slug package
``features/execution_platform/handoff_routing/application/`` under the R1 layer-leaf shape
(#12591 j#65435). The legacy import path ``mozyo_bridge.application.cli_handoff`` is
preserved here via the ``sys.modules`` facade idiom so both paths refer to the same
module object (attribute access / monkeypatch stay equivalent). Do not remove this
facade outside the fallback-retirement-ledger process.
"""

import sys as _sys

from mozyo_bridge.features.execution_platform.handoff_routing.application import (
    cli_handoff as _impl,
)

_sys.modules[__name__] = _impl
