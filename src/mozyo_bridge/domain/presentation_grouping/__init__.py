"""Compatibility facade package — real implementation relocated to
:mod:`mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.presentation_grouping`.

US #12625 (parent US #12622, Feature #12533) moves the desired-presentation
grouping subpackage into the numbered ``f_140_presentation_grouping_layout``
Feature package (Redmine #12516). The subpackage keeps its internal relative
imports intact under the new path; this legacy ``mozyo_bridge.domain.presentation_grouping``
import path is preserved as a ``sys.modules`` package facade re-binding the exact
same package object, so attribute access (``from mozyo_bridge.domain.presentation_grouping
import ...``) and monkeypatch stay equivalent. Held as a fixed surface in the #12590
round (relative-import subpackage); moved here per the #12623 migration map. Do not
remove outside the fallback-retirement-ledger process.
"""

import sys as _sys

from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain import (
    presentation_grouping as _impl,
)

_sys.modules[__name__] = _impl
