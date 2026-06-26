"""Compatibility facade — real implementation relocated to
:mod:`mozyo_bridge.features.governance_distribution.application.release`.

The #12590 Source/Test slug-layout expansion (child task #12594) moved the
governance release helper surfaces out of the technical-layer ``application/``
package and into the Redmine Epic-slug package
``features/governance_distribution/application/`` (Epic #12503 `130_統治・Scaffold配布`),
using the R1 layer-leaf shape recorded in Redmine #12591 j#65435. This module was
the deferred 4th governance slice (it is allowlisted in ``module_health.yaml`` at
1470 lines, so the move and the allowlist path re-key had to land atomically per
#12591 j#65488); it was completed under #12591 integration. This legacy import
path is preserved per the migration plan
(``vibes/docs/logics/source-layout-bounded-context-migration.md``); the relocated
module object is re-bound here via ``sys.modules`` so that
``mozyo_bridge.application.release`` and the new
``mozyo_bridge.features.governance_distribution.application.release`` refer to the
exact same module object — attribute access and monkeypatch on either path stay
equivalent. Do not remove this facade outside the fallback-retirement-ledger
process.
"""

import sys as _sys

from mozyo_bridge.features.governance_distribution.application import (
    release as _impl,
)

_sys.modules[__name__] = _impl
