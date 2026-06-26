"""Compatibility facade — real implementation relocated to
:mod:`mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application.release`.

The #12590 Source/Test slug-layout expansion (child task #12594) first moved the
governance release helper surfaces out of the technical-layer ``application/``
package into the Epic-slug package ``features/governance_distribution/``. The
#12622 Redmine-numbered layout correction (child task #12626) then relocated them
to the numbered Epic/Feature package
``e_130_governance_distribution/f_160_release_version_governance/application/``
(Epic #12503 `130_統治・Scaffold配布`, Feature #12523 `160_Release・Version統治`),
per ``vibes/docs/specs/bounded-context-map.md`` (``## Redmine-numbered package
path map (#12622)``) and ``vibes/docs/logics/source-layout-bounded-context-migration.md``.
This module is allowlisted in ``module_health.yaml`` (oversized). This legacy
import path is preserved per the migration plan; the relocated module object is
re-bound here via ``sys.modules`` so that ``mozyo_bridge.application.release`` and
the new
``mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application.release``
refer to the exact same module object — attribute access and monkeypatch on
either path stay equivalent. Do not remove this facade outside the
fallback-retirement-ledger process.
"""

import sys as _sys

from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import (
    release as _impl,
)

_sys.modules[__name__] = _impl
