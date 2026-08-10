"""The public-safe text projection shared by every Unit-board surface.

One projection, used everywhere a value that came from outside this process is
about to be displayed, put in a payload, or written into Herdr's pane metadata.
Extracted from :mod:`...herdr_unit_board` so the Unit read model and the
operator source schema can both depend on it without depending on each other,
and so the read model stays inside the module-health line budget.

The rules it enforces are the ones the board's callers rely on: control and
direction codepoints are removed *before* classification so they cannot split an
unsafe shape; an absolute path or a credential-shaped value collapses to one
fixed token whose basename, key, and value are never reflected; and the result
is length-bounded.

It is a **display** projection, not an identity normalizer.  It folds Unicode
form and collapses whitespace, so a padded or full-width identity comes out
looking canonical — which is exactly why an action must read a raw identity
instead of the value this produces (Redmine #15138 review j#101928 finding_2).
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import unicodedata
from typing import Mapping

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.absolute_path_rule import (
    contains_absolute_path,
)


#: The bound every projected value is clipped to.
MAX_PRESENTATION_TEXT = 80
#: The single token an absolute path or a credential-shaped value collapses to.
#: Its basename, key, and value are never reflected.
REDACTED_TEXT = "[redacted]"
_SPACE_RE = re.compile(r"\s+")
_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?P<quote>[\"']?)(?P<key>[A-Za-z0-9_.-]+"
    r"(?:[ \t]+[A-Za-z0-9_.-]+){0,5})(?P=quote)"
    r"[ \t]*[:=][ \t]*(?P<value>\S+)",
    re.IGNORECASE,
)
_CREDENTIAL_SINGLE_COMPONENTS = frozenset(
    {
        "auth",
        "authorization",
        "bearer",
        "cookie",
        "credential",
        "jwt",
        "pass",
        "password",
        "passphrase",
        "passwd",
        "pwd",
        "secret",
        "session",
        "token",
    }
)
_CREDENTIAL_COMPONENT_SEQUENCES = (
    ("access", "key"),
    ("access", "token"),
    ("api", "key"),
    ("api", "token"),
    ("auth", "token"),
    ("client", "secret"),
    ("private", "key"),
    ("refresh", "token"),
    ("secret", "key"),
    ("session", "id"),
    ("session", "token"),
)
_CREDENTIAL_COMPACT_KEYS = frozenset(
    {
        "accesskey",
        "accesstoken",
        "apikey",
        "apitoken",
        "authtoken",
        "awsaccesskeyid",
        "awssecretaccesskey",
        "clientsecret",
        "privatekey",
        "refreshtoken",
        "secretkey",
        "sessionid",
        "sessiontoken",
    }
)
_CREDENTIAL_COMPACT_SUFFIXES = frozenset(
    {
        "accesskey",
        "accesstoken",
        "apikey",
        "apitoken",
        "authtoken",
        "clientsecret",
        "password",
        "privatekey",
        "refreshtoken",
        "secretkey",
        "sessionid",
        "sessiontoken",
        "token",
    }
)
_MAX_CREDENTIAL_JSON_LENGTH = 16_384
_MAX_CREDENTIAL_JSON_DEPTH = 8
_MAX_CREDENTIAL_JSON_NODES = 256
_MAX_CREDENTIAL_JSON_PARSE_ATTEMPTS = 64
_MAX_CREDENTIAL_JSON_SCANNED_CHARS = _MAX_CREDENTIAL_JSON_LENGTH * 2
_CREDENTIAL_FILE_RE = re.compile(
    r"(?:"
    r"(?<![A-Za-z0-9])\.credentials(?![A-Za-z0-9])|"
    r"\.pem(?![A-Za-z0-9])|"
    r"(?<![A-Za-z0-9])id_rsa(?![A-Za-z0-9])|"
    r"(?<![A-Za-z0-9])private[ _-]+key(?![A-Za-z0-9])"
    r")",
    re.IGNORECASE,
)
_OPAQUE_CREDENTIAL_RE = re.compile(
    r"(?:\bgithub_pat_[A-Za-z0-9_]{20,}\b|"
    r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b|"
    r"\bglpat-[A-Za-z0-9_-]{20,}\b|"
    r"\bnpm_[A-Za-z0-9_-]{20,}\b|"
    r"\bpypi-[A-Za-z0-9_-]{20,}\b|"
    r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9_-]{16,}\b|"
    r"\bx(?:ox[baprs]|app)-[A-Za-z0-9-]{10,}\b|"
    r"\bAIza[0-9A-Za-z_-]{30,}\b|"
    r"\bsk-[A-Za-z0-9_-]{16,}\b|"
    r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|"
    r"\bbasic\s+[A-Za-z0-9+/=._-]{8,}|"
    r"://[^/\s:@]+:[^/@\s]+@)",
    re.IGNORECASE,
)
_BEARER_CREDENTIAL_RE = re.compile(
    r"(?<![A-Za-z0-9])bearer[ \t]+[A-Za-z0-9._~+/-]+=*"
    r"(?![A-Za-z0-9._~+/=-])",
    re.IGNORECASE,
)
_BASIC_CREDENTIAL_RE = re.compile(
    r"(?<![A-Za-z0-9])basic[ \t]+(?P<token>[A-Za-z0-9+/]+={0,2})"
    r"(?![A-Za-z0-9+/=])",
    re.IGNORECASE,
)
_COMPACT_JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?P<header>[A-Za-z0-9_-]+)\."
    r"(?P<claims>[A-Za-z0-9_-]+)\.[A-Za-z0-9_-]*"
    r"(?![A-Za-z0-9_-])"
)
_COMPACT_JWE_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?P<header>[A-Za-z0-9_-]+)\."
    r"[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\."
    r"[A-Za-z0-9_-]+(?![A-Za-z0-9_-])"
)
_BIDI_CONTROLS = frozenset(
    {
        0x061C,
        0x200E,
        0x200F,
        0x202A,
        0x202B,
        0x202C,
        0x202D,
        0x202E,
        0x2066,
        0x2067,
        0x2068,
        0x2069,
    }
)


def _without_terminal_controls(value: str) -> str:
    """Make whitespace inert and remove other terminal/direction controls."""
    projected: list[str] = []
    for char in value:
        if char.isspace():
            projected.append(" ")
            continue
        if (
            ord(char) < 0x20
            or 0x7F <= ord(char) <= 0x9F
            or ord(char) in _BIDI_CONTROLS
            or unicodedata.category(char) in {"Cf", "Cs"}
        ):
            continue
        projected.append(char)
    return "".join(projected)


def normalized_untrusted_text(value: str) -> str:
    """Apply the same classification normalization at every decoding depth.

    Public because the read model compares untrusted field values for agreement
    and must normalize them exactly the way this module does before deciding
    whether two of them differ.  A second, slightly different normalizer there
    would let a conflict hide behind a whitespace or Unicode-form difference.
    """
    normalized = unicodedata.normalize("NFKC", _without_terminal_controls(value))
    return _SPACE_RE.sub(" ", normalized).strip()


class _DuplicateJsonKey(ValueError):
    """A JSON object repeated a key, so it makes two claims about one field."""


def _reject_duplicate_pairs(pairs):
    seen: dict = {}
    for key, value in pairs:
        if key in seen:
            raise _DuplicateJsonKey(key)
        seen[key] = value
    return seen


def _decode_json_object_segment(
    segment: str,
) -> tuple[Mapping[str, object] | None, bool]:
    """Return a decoded object and whether the segment must be treated as unsafe.

    The second element is the fail-closed flag.  It covers a resource limit and,
    equally, a repeated key: ``json.loads`` keeps the last value, so a header of
    ``{"cty":"JWT","cty":"text"}`` would read as innocuous and the credential it
    heads would print verbatim (review j#102129 finding_1).  A segment making two
    claims about one field is not one this classifier can clear, so it degrades
    to unsafe rather than to "not a credential".
    """
    padded = segment + ("=" * (-len(segment) % 4))
    try:
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        return None, False
    try:
        text = decoded.decode("utf-8")
    except UnicodeDecodeError:
        return None, False
    try:
        parsed = json.loads(text, object_pairs_hook=_reject_duplicate_pairs)
    except _DuplicateJsonKey:
        return None, True
    except json.JSONDecodeError:
        return None, False
    except (RecursionError, ValueError):
        return None, True
    return (parsed, False) if isinstance(parsed, dict) else (None, False)


def _contains_compact_jwt(value: str) -> bool:
    """Recognize plain and nested compact JWTs without encoded-length guesses."""
    for match in _COMPACT_JWT_RE.finditer(value):
        header, header_unknown = _decode_json_object_segment(match.group("header"))
        claims, claims_unknown = _decode_json_object_segment(match.group("claims"))
        if header_unknown or claims_unknown:
            return True
        if header is None:
            continue
        if claims is not None:
            return True
        for marker in (header.get("cty"), header.get("typ")):
            if isinstance(marker, str) and marker.casefold() == "jwt":
                return True
    return False


def _contains_compact_jwe(value: str) -> bool:
    """Recognize 5-part encrypted JWTs from their protected ``enc`` header."""
    for match in _COMPACT_JWE_RE.finditer(value):
        header, resource_unknown = _decode_json_object_segment(
            match.group("header")
        )
        if resource_unknown or (header is not None and "enc" in header):
            return True
    return False


def _credential_key(key: str) -> bool:
    normalized = unicodedata.normalize("NFKC", key)
    camel_split = re.sub(
        r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", normalized
    )
    components = tuple(
        part for part in re.split(r"[^a-z0-9]+", camel_split.casefold()) if part
    )
    if not components:
        return False
    if any(part in _CREDENTIAL_SINGLE_COMPONENTS for part in components):
        return True
    for sequence in _CREDENTIAL_COMPONENT_SEQUENCES:
        width = len(sequence)
        if any(
            components[index : index + width] == sequence
            for index in range(len(components) - width + 1)
        ):
            return True
    if len(components) != 1:
        return False
    compact = components[0]
    if compact in _CREDENTIAL_COMPACT_KEYS:
        return True
    return any(
        len(compact) > len(suffix) and compact.endswith(suffix)
        for suffix in _CREDENTIAL_COMPACT_SUFFIXES
    )


def _contains_authorization_credential(value: str) -> bool:
    """Recognize standards-valid authorization credentials without length guesses."""
    if _BEARER_CREDENTIAL_RE.search(value):
        return True
    for match in _BASIC_CREDENTIAL_RE.finditer(value):
        token = match.group("token")
        if len(token) % 4 == 1:
            continue
        try:
            decoded = base64.b64decode(
                token + ("=" * (-len(token) % 4)), validate=True
            )
        except (binascii.Error, ValueError):
            continue
        if b":" in decoded:
            return True
    return False


def _contains_non_json_credential(value: str) -> bool:
    """Classify credential shapes after any surrounding JSON is decoded."""
    if (
        _CREDENTIAL_FILE_RE.search(value)
        or _OPAQUE_CREDENTIAL_RE.search(value)
        or _contains_authorization_credential(value)
        or _contains_compact_jwt(value)
        or _contains_compact_jwe(value)
    ):
        return True
    for match in _CREDENTIAL_ASSIGNMENT_RE.finditer(value):
        if _credential_key(match.group("key")):
            return True
    return False


def _contains_credential_json(value: str) -> bool:
    """Inspect decoded JSON with one shared, bounded, fail-closed budget.

    Successful roots advance the scanner to their end, so nested objects are not
    decoded and counted twice.  JSON strings are scanned again only after decoding;
    this catches escaped assignments and encoded child JSON without recursive calls
    or a fresh budget.
    """
    stripped = value.strip()
    if not stripped:
        return False
    if len(stripped) > _MAX_CREDENTIAL_JSON_LENGTH:
        return True

    decoder = json.JSONDecoder(object_pairs_hook=_reject_duplicate_pairs)
    pending_text: list[tuple[str, int]] = []
    pending_nodes: list[tuple[object, int]] = []
    parse_attempts = 0
    scanned_chars = 0
    visited = 0
    if stripped.startswith('"'):
        try:
            root, end = decoder.raw_decode(stripped, 0)
        except _DuplicateJsonKey:
            return True
        except json.JSONDecodeError:
            root, end = None, -1
        except (RecursionError, ValueError):
            return True
        if end == len(stripped) and isinstance(root, str):
            pending_nodes.append((root, 0))
            parse_attempts = 1
            scanned_chars = len(stripped)
    if not pending_nodes:
        if not any(marker in stripped for marker in "[{"):
            return False
        pending_text.append((stripped, 0))

    while pending_text or pending_nodes:
        while pending_text:
            text, depth = pending_text.pop()
            if depth > _MAX_CREDENTIAL_JSON_DEPTH:
                return True
            scanned_chars += len(text)
            if scanned_chars > _MAX_CREDENTIAL_JSON_SCANNED_CHARS:
                return True
            index = 0
            while index < len(text):
                object_at = text.find("{", index)
                array_at = text.find("[", index)
                candidates = [at for at in (object_at, array_at) if at >= 0]
                if not candidates:
                    break
                candidate = min(candidates)
                parse_attempts += 1
                if parse_attempts > _MAX_CREDENTIAL_JSON_PARSE_ATTEMPTS:
                    return True
                try:
                    root, end = decoder.raw_decode(text, candidate)
                except _DuplicateJsonKey:
                    return True
                except json.JSONDecodeError:
                    index = candidate + 1
                    continue
                except (RecursionError, ValueError):
                    return True
                if isinstance(root, (dict, list)):
                    pending_nodes.append((root, depth))
                    index = max(end, candidate + 1)
                else:
                    index = candidate + 1

        if not pending_nodes:
            continue
        node, depth = pending_nodes.pop()
        visited += 1
        if visited > _MAX_CREDENTIAL_JSON_NODES or depth > _MAX_CREDENTIAL_JSON_DEPTH:
            return True
        if isinstance(node, dict):
            for key, child in node.items():
                if isinstance(key, str):
                    decoded_key = normalized_untrusted_text(key)
                    if (
                        contains_absolute_path(decoded_key)
                        or _credential_key(decoded_key)
                        or _contains_non_json_credential(decoded_key)
                    ):
                        return True
                    if any(marker in decoded_key for marker in "[{"):
                        pending_text.append((decoded_key, depth + 1))
                pending_nodes.append((child, depth + 1))
        elif isinstance(node, list):
            pending_nodes.extend((child, depth + 1) for child in node)
        elif isinstance(node, str):
            decoded = normalized_untrusted_text(node)
            if contains_absolute_path(decoded) or _contains_non_json_credential(decoded):
                return True
            if any(marker in decoded for marker in "[{"):
                pending_text.append((decoded, depth + 1))
    return False


def _is_credential_shaped(value: str) -> bool:
    return _contains_non_json_credential(value) or _contains_credential_json(value)


def safe_text(value: object, *, fallback: str = "unknown") -> str:
    """Project one untrusted value into bounded, inert, public-safe text.

    Control and bidi codepoints are removed before classification so they cannot
    split an unsafe shape.  Absolute paths and credential-shaped values collapse
    to one fixed token; their basename, key, and value are never reflected.
    """
    if not isinstance(value, str):
        return fallback
    normalized = normalized_untrusted_text(value)
    if not normalized:
        return fallback
    if contains_absolute_path(normalized) or _is_credential_shaped(normalized):
        return REDACTED_TEXT
    return normalized[:MAX_PRESENTATION_TEXT]


__all__ = (
    "MAX_PRESENTATION_TEXT",
    "REDACTED_TEXT",
    "normalized_untrusted_text",
    "safe_text",
)
