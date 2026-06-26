"""Authority / routing leak guard for the grouping config.

The desired presentation grouping config is **display-only** — it may never load
code, address a target / pane / route, grant authority / approval, or carry a
credential. This module owns the forbidden-token vocabulary and the guards that
fail closed when an *unknown key* or an *identity / diagnostic value* is shaped
like one of those boundaries.

It is deliberately separate from the generic shape validators in
:mod:`mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.presentation_grouping.validation`: those check *types*;
this checks *authority boundaries*. Free public display prose (``label`` /
``description`` / ``label_override``) is curated author-asserted public-safe and
is **not** token-scanned here, so a legitimate label such as "Code Review" is
preserved.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Optional

from .errors import PresentationGroupingConfigError
from .validation import _optional_str

#: Substrings in a config key that signal an attempt to cross a boundary this
#: surface does not own: load / execute code, name a module / callable / entry
#: point, grant or alter authority / approval / routing / send safety, address a
#: target / pane / route, or carry a credential. Scanned only against *unknown*
#: keys (every allowed key in this package is curated boundary-safe), so it gives
#: a deliberate-looking audit message without false-positiving on legitimate
#: display keys such as ``description``.
_FORBIDDEN_KEY_PARTS: tuple[str, ...] = (
    "import",
    "module",
    "callable",
    "entry",
    "plugin",
    "exec",
    "eval",
    "load",
    "authority",
    "approval",
    "approve",
    "grant",
    "owner",
    "review",
    "close",
    "routing",
    "route",
    "send",
    "target",
    "pane",
    "secret",
    "token",
    "password",
    "credential",
    "command",
    "script",
)


def _reject_unknown_keys(
    record: "Mapping[object, object]", *, allowed: "frozenset[str]", source: str
) -> None:
    """Fail closed on a non-string / boundary-shaped / unknown record key.

    Keys must be non-empty strings; a key outside ``allowed`` whose name carries
    a :data:`_FORBIDDEN_KEY_PARTS` token is rejected with a boundary-specific
    message (so smuggling a ``target`` / ``route`` / ``approval`` key reads as a
    deliberate rejection in an audit); any other key outside ``allowed`` is a
    plain unknown-key rejection (closed schema / typo protection).
    """
    for key in record:
        if not isinstance(key, str) or not key:
            raise PresentationGroupingConfigError(
                f"{source} record keys must be non-empty strings; got {key!r}"
            )
        if key in allowed:
            continue
        lowered = key.lower()
        for part in _FORBIDDEN_KEY_PARTS:
            if part in lowered:
                raise PresentationGroupingConfigError(
                    f"{source} record key {key!r} may not carry a boundary token: "
                    f"grouping config is display-only and may never load code, "
                    f"address a target / pane / route, grant authority, or carry a "
                    f"credential (matched forbidden token {part!r})."
                )
        raise PresentationGroupingConfigError(
            f"{source} record has unknown key {key!r}; allowed keys: {sorted(allowed)}"
        )


def _reject_boundary_value(value: object, *, source: str, field_name: str) -> None:
    """Fail closed on a boundary-shaped string *value* in an identity / diagnostic field.

    ``unit-presentation-state-db.md`` validation marks a boundary-shaped
    *key/value* — not only a key — invalid config. The portable group keys
    (``group_id`` / ``preferred_group`` / ``missing_group`` /
    ``unknown_unit_group``) are stable join / pointer keys, and
    ``degraded_display`` is operator-facing diagnostic text; like a key, none of
    them may name a ``target`` / ``pane`` / ``route`` / ``send`` / ``approval`` /
    ``credential`` / ``module`` boundary (the same :data:`_FORBIDDEN_KEY_PARTS`
    vocabulary). A non-string value is ignored (the field's own type check
    handles it). Free public display text — ``label`` / ``description`` /
    ``label_override`` — is deliberately *not* scanned here: it is inert prose,
    not an identity or diagnostic key, and legitimately contains words such as
    "review" or "closed".
    """
    if not isinstance(value, str):
        return
    lowered = value.lower()
    for part in _FORBIDDEN_KEY_PARTS:
        if part in lowered:
            raise PresentationGroupingConfigError(
                f"{source} '{field_name}' value {value!r} may not carry a boundary "
                f"token: a grouping key / diagnostic is display-only and may never "
                f"name a target / pane / route, grant authority, or carry a "
                f"credential (matched forbidden token {part!r})."
            )


def _optional_guarded_str(
    value: object, *, source: str, field_name: str
) -> Optional[str]:
    """An optional non-empty string that is also boundary-token guarded.

    For identity / pointer keys (``group_id`` and the group references) and
    operator-facing diagnostic text (``degraded_display``): the value must be a
    public-safe display string carrying no boundary token. Free naming prose
    (``label`` / ``description`` / ``label_override``) uses the plain
    :func:`_optional_str` instead — see :func:`_reject_boundary_value`.
    """
    text = _optional_str(value, source=source, field_name=field_name)
    _reject_boundary_value(text, source=source, field_name=field_name)
    return text
