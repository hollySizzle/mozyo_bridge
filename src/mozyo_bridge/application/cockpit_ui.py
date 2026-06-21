"""Cockpit Web UI served by the mozyo-bridge daemon (Redmine #11679/#11680).

Behavior-preserving facade (Redmine #12323): the served cockpit UI was one
oversized module mixing API payload assembly, HTML rendering, action endpoint
handling, and the action-time preflight bridge. #12323 split those concerns into
three focused modules so a UI / rendering change and a domain / read-model change
no longer land in the same giant file:

- :mod:`mozyo_bridge.application.cockpit_page` — the served HTML / static page
  (``INDEX_HTML_TEMPLATE``);
- :mod:`mozyo_bridge.application.cockpit_payload` — read-only served-API payload
  assembly + reload / freshness presentation (``units_payload`` /
  ``attach_attention`` / ``attach_observation`` / ``observed_units_from_inventory``
  / ``grouped_units_payload``);
- :mod:`mozyo_bridge.application.cockpit_actions` — the side-effecting action
  endpoints + action-time live preflight bridge (``CockpitActionError`` /
  ``reveal_in_finder`` / ``jump_to_unit`` / the grouped ``_resolve_unit_target``
  / ``grouped_reveal`` / ``grouped_jump`` / ``grouped_action_preview`` /
  ``candidate_unit_selector``).

This module re-exports the public surface so existing importers and the
documentation cross-references keep resolving. New code should import from the
focused module that owns the concern.

Owner decision #11639 journal #56164: the cockpit is a localhost Web UI
served by the same daemon process that already receives OTLP — the iTerm2
Toolbelt webview is the default host but any browser shows the identical
UI, so the GUI investment is terminal-independent. The constraints (127.0.0.1
only, no auto-foregrounding, structured commands only, stale-safe, jump v1 =
``switch-client``, no secrets to Redmine) are documented on the focused modules
that enforce them.
"""

from __future__ import annotations

from mozyo_bridge.application.cockpit_actions import (
    DEFAULT_HOST,
    DEFAULT_LANE,
    CockpitActionError,
    _pick_attached_client,
    _resolve_record,
    _resolve_unit_target,
    candidate_unit_selector,
    grouped_action_preview,
    grouped_jump,
    grouped_reveal,
    jump_to_unit,
    reveal_in_finder,
)
from mozyo_bridge.application.cockpit_page import INDEX_HTML_TEMPLATE
from mozyo_bridge.application.cockpit_payload import (
    attach_attention,
    attach_observation,
    grouped_units_payload,
    observed_units_from_inventory,
    units_payload,
)

__all__ = [
    "CockpitActionError",
    "DEFAULT_HOST",
    "DEFAULT_LANE",
    "INDEX_HTML_TEMPLATE",
    "attach_attention",
    "attach_observation",
    "candidate_unit_selector",
    "grouped_action_preview",
    "grouped_jump",
    "grouped_reveal",
    "grouped_units_payload",
    "jump_to_unit",
    "observed_units_from_inventory",
    "reveal_in_finder",
    "units_payload",
    # Re-exported for the action-preflight tests / callers that patched these
    # internals on the original module.
    "_pick_attached_client",
    "_resolve_record",
    "_resolve_unit_target",
]
