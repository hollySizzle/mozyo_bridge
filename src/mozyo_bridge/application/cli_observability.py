"""Compatibility facade — real implementation relocated to
:mod:`mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.cli_observability`.

Redmine #12624 (US #12622 `Source/Test layout を Redmine 番号付き Epic/Feature
階層へ全面再移行する`) moved the execution_platform runtime body into the
Redmine-numbered Epic/Feature layout
``src/mozyo_bridge/e_110_execution_platform/f_150_runtime_observation_event_timeline/application/`` per the reviewed migration
map (`vibes/docs/specs/bounded-context-map.md`
`## Redmine-numbered package path map (#12622)`). The relocated module object
is re-bound here via ``sys.modules`` so the legacy import path
``mozyo_bridge.application.cli_observability`` and the new path refer to the exact same module object —
attribute access and monkeypatch on either path stay equivalent. Do not
remove this facade outside the fallback-retirement-ledger process.
"""

import sys as _sys

from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application import (
    cli_observability as _impl,
)

_sys.modules[__name__] = _impl
