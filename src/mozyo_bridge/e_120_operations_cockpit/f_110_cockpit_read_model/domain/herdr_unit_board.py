"""Public-safe read model for the Herdr coordinator Unit board.

The board is a presentation consumer.  It groups already-resolved managed Herdr
agent observations by the durable ``(workspace_id, lane_id)`` Unit identity and
renders only short, operator-facing labels.  Runtime pane locators are retained
inside the process solely so the adapter can refresh Herdr display metadata; they
are deliberately absent from the public payload.

No value in this module is workflow, routing, review, approval, or close
authority.  Missing or contradictory inputs stay visible as ``unknown`` /
``ambiguous`` instead of being completed from pane position or provider guesses.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.absolute_path_rule import (
    contains_absolute_path,
)


SOURCE_LIVE = "live"
SOURCE_UNAVAILABLE = "unavailable"
SOURCE_RELOAD_REQUIRED = "reload_required"
SOURCE_STATES = frozenset({SOURCE_LIVE, SOURCE_UNAVAILABLE, SOURCE_RELOAD_REQUIRED})

AUTHORITY_RESOLVED = "resolved"
AUTHORITY_MISSING = "missing"
AUTHORITY_INVALID = "invalid"
AUTHORITY_STATES = frozenset(
    {AUTHORITY_RESOLVED, AUTHORITY_MISSING, AUTHORITY_INVALID}
)

MAX_PRESENTATION_TEXT = 80
REDACTED_TEXT = "[redacted]"
_SPACE_RE = re.compile(r"\s+")
_ISSUE_LANE_RE = re.compile(r"^issue_(\d+)(?:_(.*))?$")
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
    r"(?:\bgh[pousr]_[A-Za-z0-9]{20,}\b|"
    r"\bsk-[A-Za-z0-9_-]{16,}\b|"
    r"\bAKIA[A-Z0-9]{16}\b|"
    r"\bbearer\s+\S{8,}|"
    r"\bbasic\s+[A-Za-z0-9+/=._-]{8,}|"
    r"://[^/\s:@]+:[^/@\s]+@)",
    re.IGNORECASE,
)
_COMPACT_JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?P<header>[A-Za-z0-9_-]+)\."
    r"(?P<claims>[A-Za-z0-9_-]+)\.[A-Za-z0-9_-]*"
    r"(?![A-Za-z0-9_-])"
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
            or unicodedata.category(char) == "Cf"
        ):
            continue
        projected.append(char)
    return "".join(projected)


def _normalized_untrusted_text(value: str) -> str:
    """Apply the same classification normalization at every decoding depth."""
    normalized = unicodedata.normalize("NFKC", _without_terminal_controls(value))
    return _SPACE_RE.sub(" ", normalized).strip()


def _is_json_object_segment(segment: str) -> bool:
    """Return whether one base64url segment decodes to a JSON object."""
    padded = segment + ("=" * (-len(segment) % 4))
    try:
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        return False
    try:
        text = decoded.decode("utf-8")
    except UnicodeDecodeError:
        return False
    try:
        return isinstance(json.loads(text), dict)
    except json.JSONDecodeError:
        return False
    except (RecursionError, ValueError):
        return True


def _contains_compact_jwt(value: str) -> bool:
    """Recognize compact JWTs without assuming a minimum encoded JSON length."""
    return any(
        _is_json_object_segment(match.group("header"))
        and _is_json_object_segment(match.group("claims"))
        for match in _COMPACT_JWT_RE.finditer(value)
    )


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
    return len(components) == 1 and components[0] in _CREDENTIAL_COMPACT_KEYS


def _contains_non_json_credential(value: str) -> bool:
    """Classify credential shapes after any surrounding JSON is decoded."""
    if (
        _CREDENTIAL_FILE_RE.search(value)
        or _OPAQUE_CREDENTIAL_RE.search(value)
        or _contains_compact_jwt(value)
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
    if not stripped or not any(marker in stripped for marker in "[{"):
        return False
    if len(stripped) > _MAX_CREDENTIAL_JSON_LENGTH:
        return True

    decoder = json.JSONDecoder()
    pending_text: list[tuple[str, int]] = [(stripped, 0)]
    pending_nodes: list[tuple[object, int]] = []
    parse_attempts = 0
    scanned_chars = 0
    visited = 0

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
                    decoded_key = _normalized_untrusted_text(key)
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
            decoded = _normalized_untrusted_text(node)
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
    normalized = _normalized_untrusted_text(value)
    if not normalized:
        return fallback
    if contains_absolute_path(normalized) or _is_credential_shaped(normalized):
        return REDACTED_TEXT
    return normalized[:MAX_PRESENTATION_TEXT]


def _unit_public_id(workspace_id: str, lane_id: str) -> str:
    """Return a bounded opaque key without truncating or disclosing identity input."""
    digest = hashlib.sha256()
    for component in (workspace_id, lane_id):
        encoded = component.encode("utf-8", errors="surrogatepass")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"unit-{digest.hexdigest()[:32]}"


def lane_work_label(lane_id: object, issue_id: object = "", label: object = "") -> str:
    """Return a readable work label without pretending it is a ticket subject.

    ``lane_metadata`` is display metadata, not durable ticket truth.  The label is
    therefore rendered as a lane label.  When it includes the conventional issue
    prefix, the readable suffix is kept beside the id so the UI never shows a bare
    ticket number as if that explained the work.
    """
    lane = safe_text(lane_id, fallback="unknown-lane")
    if lane == "default":
        return "default lane"
    display = safe_text(label, fallback=lane)
    match = _ISSUE_LANE_RE.fullmatch(display)
    if match is None:
        match = _ISSUE_LANE_RE.fullmatch(lane)
    if match is not None:
        number, suffix = match.groups()
        words = safe_text((suffix or "").replace("_", " "), fallback="lane")
        return safe_text(f"#{number} {words}")
    issue = safe_text(issue_id, fallback="")
    if issue:
        return safe_text(f"#{issue} {display.replace('_', ' ')}")
    return safe_text(display.replace("_", " "))


@dataclass(frozen=True)
class AgentObservation:
    """One managed live agent plus its resolved display authority.

    ``pane_id`` is a transient action-time locator.  It is intentionally private
    to this value object and omitted by :meth:`AgentCell.as_payload`.
    """

    workspace_id: str
    lane_id: str
    provider: str
    pane_id: str
    runtime_state: str
    interactive_ready: bool
    project_label: str
    workflow_role: str
    responsibility: str
    work_label: str
    authority_state: str

    def __post_init__(self) -> None:
        if not self.workspace_id or not self.lane_id or not self.provider:
            raise ValueError("managed Unit observation requires workspace, lane, and provider")
        if self.authority_state not in AUTHORITY_STATES:
            raise ValueError(f"unknown authority state: {self.authority_state!r}")


@dataclass(frozen=True)
class AgentCell:
    provider: str
    runtime_state: str
    interactive_ready: bool
    pane_id: str

    def as_payload(self) -> dict[str, object]:
        return {
            "provider": safe_text(self.provider),
            "runtime_state": safe_text(self.runtime_state),
            "interactive_ready": self.interactive_ready,
        }


@dataclass(frozen=True)
class UnitBoardRow:
    unit_id: str
    workspace_id: str
    lane_id: str
    project_label: str
    workflow_role: str
    responsibility: str
    work_label: str
    authority_state: str
    identity_state: str
    agents: tuple[AgentCell, ...]

    def as_payload(self) -> dict[str, object]:
        return {
            "unit_id": safe_text(self.unit_id),
            "workspace_id": safe_text(self.workspace_id),
            "lane_id": safe_text(self.lane_id),
            "project_label": safe_text(self.project_label),
            "workflow_role": safe_text(self.workflow_role),
            "responsibility": safe_text(self.responsibility),
            "work_label": safe_text(self.work_label),
            "authority_state": safe_text(self.authority_state),
            "identity_state": safe_text(self.identity_state),
            "agents": [agent.as_payload() for agent in self.agents],
        }


@dataclass(frozen=True)
class UnitBoardSnapshot:
    source_state: str
    observed_at: str
    units: tuple[UnitBoardRow, ...]
    unmanaged_agents: int = 0
    detail: str = ""

    def __post_init__(self) -> None:
        if self.source_state not in SOURCE_STATES:
            raise ValueError(f"unknown Unit board source state: {self.source_state!r}")

    @property
    def ok(self) -> bool:
        return self.source_state == SOURCE_LIVE

    def as_payload(self) -> dict[str, object]:
        return {
            "source_state": self.source_state,
            "observed_at": safe_text(self.observed_at, fallback=""),
            "unmanaged_agents": self.unmanaged_agents,
            "detail": safe_text(self.detail, fallback="") if self.detail else "",
            "units": [unit.as_payload() for unit in self.units],
        }


def _choose_unit_field(values: Iterable[str], *, fallback: str) -> tuple[str, bool]:
    distinct = {safe_text(value, fallback=fallback) for value in values}
    if not distinct:
        return fallback, False
    if len(distinct) == 1:
        return next(iter(distinct)), False
    return "ambiguous", True


def build_unit_board(
    observations: Sequence[AgentObservation],
    *,
    observed_at: str,
    unmanaged_agents: int = 0,
) -> UnitBoardSnapshot:
    """Group managed observations into a deterministic, ambiguity-visible board."""
    grouped: dict[tuple[str, str], list[AgentObservation]] = {}
    for observation in observations:
        grouped.setdefault(
            (observation.workspace_id, observation.lane_id), []
        ).append(observation)

    rows: list[UnitBoardRow] = []
    for (workspace_id, lane_id), members in grouped.items():
        project, project_ambiguous = _choose_unit_field(
            (member.project_label for member in members), fallback="unknown-project"
        )
        role, role_ambiguous = _choose_unit_field(
            (member.workflow_role for member in members), fallback="unknown"
        )
        responsibility, responsibility_ambiguous = _choose_unit_field(
            (member.responsibility for member in members), fallback="unknown"
        )
        work, work_ambiguous = _choose_unit_field(
            (member.work_label for member in members), fallback="unknown"
        )
        authority, authority_ambiguous = _choose_unit_field(
            (member.authority_state for member in members), fallback=AUTHORITY_MISSING
        )
        provider_counts: dict[str, int] = {}
        for member in members:
            provider_counts[member.provider] = provider_counts.get(member.provider, 0) + 1
        duplicate_provider = any(count > 1 for count in provider_counts.values())
        identity_state = (
            "ambiguous"
            if any(
                (
                    project_ambiguous,
                    role_ambiguous,
                    responsibility_ambiguous,
                    work_ambiguous,
                    authority_ambiguous,
                    duplicate_provider,
                )
            )
            else "resolved"
        )
        cells = tuple(
            AgentCell(
                provider=safe_text(member.provider),
                runtime_state=safe_text(member.runtime_state),
                interactive_ready=bool(member.interactive_ready),
                pane_id=member.pane_id,
            )
            for member in sorted(members, key=lambda item: (item.provider, item.pane_id))
        )
        rows.append(
            UnitBoardRow(
                unit_id=_unit_public_id(workspace_id, lane_id),
                workspace_id=safe_text(workspace_id),
                lane_id=safe_text(lane_id),
                project_label=project,
                workflow_role=role,
                responsibility=responsibility,
                work_label=work,
                authority_state=authority,
                identity_state=identity_state,
                agents=cells,
            )
        )

    rows.sort(key=lambda row: (row.project_label.casefold(), row.lane_id, row.unit_id))
    return UnitBoardSnapshot(
        source_state=SOURCE_LIVE,
        observed_at=observed_at,
        units=tuple(rows),
        unmanaged_agents=max(0, int(unmanaged_agents)),
    )


def unavailable_snapshot(source_state: str, *, observed_at: str, detail: str) -> UnitBoardSnapshot:
    if source_state not in {SOURCE_UNAVAILABLE, SOURCE_RELOAD_REQUIRED}:
        raise ValueError("an unavailable snapshot needs an unavailable source state")
    return UnitBoardSnapshot(
        source_state=source_state,
        observed_at=observed_at,
        units=(),
        detail=safe_text(detail),
    )


def metadata_for_unit(unit: UnitBoardRow) -> tuple[Mapping[str, str], str]:
    """Render the bounded metadata patch shared by every pane in one Unit."""
    tokens = {
        "mozyo_project": safe_text(unit.project_label),
        "mozyo_role": safe_text(unit.workflow_role),
        "mozyo_responsibility": safe_text(unit.responsibility),
        "mozyo_work": safe_text(unit.work_label),
        "mozyo_unit": safe_text(unit.unit_id),
        "mozyo_identity": safe_text(unit.identity_state),
    }
    title = safe_text(
        f"{unit.project_label} · {unit.workflow_role} · {unit.work_label}"
    )
    return tokens, title


def _cell_width(value: str) -> int:
    return sum(
        2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        for char in value
    )


def _pad_display(value: object, width: int) -> str:
    clipped = clip_display(value, width)
    return clipped + (" " * max(0, width - _cell_width(clipped)))


def clip_display(value: object, width: int) -> str:
    """Clip text by terminal cells without cutting a control sequence or codepoint."""
    text = safe_text(value)
    if width <= 0:
        return ""
    if _cell_width(text) <= width:
        return text
    if width == 1:
        return "…"
    budget = width - 1
    used = 0
    chars: list[str] = []
    for char in text:
        cells = 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        if used + cells > budget:
            break
        chars.append(char)
        used += cells
    return "".join(chars) + "…"


def format_board(snapshot: UnitBoardSnapshot, *, width: int = 120) -> str:
    """Render a compact terminal table.  JSON callers use ``as_payload`` instead."""
    usable = max(1, int(width))
    heading = (
        f"mozyo Unit board  source={snapshot.source_state}  "
        f"observed={snapshot.observed_at or 'unknown'}"
    )
    if not snapshot.ok:
        return "\n".join(
            (
                clip_display(heading, usable),
                clip_display(f"detail: {snapshot.detail}", usable),
            )
        )
    if not snapshot.units:
        return "\n".join(
            (clip_display(heading, usable), clip_display("no managed Units", usable))
        )

    if usable < 90:
        lines = [clip_display(heading, usable), "-" * usable]
        for unit in snapshot.units:
            agents = ",".join(
                f"{agent.provider}:{agent.runtime_state}" for agent in unit.agents
            ) or "none"
            lines.extend(
                (
                    clip_display(
                        f"{unit.project_label} · {unit.workflow_role}", usable
                    ),
                    clip_display(
                        f"  responsibility: {unit.responsibility}", usable
                    ),
                    clip_display(f"  work: {unit.work_label}", usable),
                    clip_display(f"  agents: {agents}", usable),
                )
            )
            if unit.identity_state != "resolved" or unit.authority_state != "resolved":
                lines.append(
                    clip_display(
                        f"  ! identity={unit.identity_state} "
                        f"authority={unit.authority_state}",
                        usable,
                    )
                )
        if snapshot.unmanaged_agents:
            lines.append(
                clip_display(
                    f"unmanaged agents omitted: {snapshot.unmanaged_agents}", usable
                )
            )
        return "\n".join(lines)

    project_w = min(24, max(16, usable // 6))
    role_w = min(22, max(14, usable // 7))
    responsibility_w = min(26, max(16, usable // 5))
    state_w = 18
    work_w = max(
        16,
        usable - project_w - role_w - responsibility_w - state_w - 12,
    )
    header = (
        f"{'PROJECT':<{project_w}}  {'ROLE':<{role_w}}  "
        f"{'RESPONSIBILITY':<{responsibility_w}}  "
        f"{'AGENTS':<{state_w}}  WORK"
    )
    lines = [clip_display(heading, usable), header, "-" * usable]
    for unit in snapshot.units:
        agents = ",".join(
            f"{agent.provider}:{agent.runtime_state}"
            for agent in unit.agents
        ) or "none"
        lines.append(
            f"{_pad_display(unit.project_label, project_w)}  "
            f"{_pad_display(unit.workflow_role, role_w)}  "
            f"{_pad_display(unit.responsibility, responsibility_w)}  "
            f"{_pad_display(agents, state_w)}  "
            f"{clip_display(unit.work_label, work_w)}"
        )
        if unit.identity_state != "resolved" or unit.authority_state != "resolved":
            lines.append(
                clip_display(
                    f"  ! identity={unit.identity_state} "
                    f"authority={unit.authority_state}",
                    usable,
                )
            )
    if snapshot.unmanaged_agents:
        lines.append(
            clip_display(
                f"unmanaged agents omitted: {snapshot.unmanaged_agents}", usable
            )
        )
    return "\n".join(lines)


__all__ = (
    "AUTHORITY_INVALID",
    "AUTHORITY_MISSING",
    "AUTHORITY_RESOLVED",
    "REDACTED_TEXT",
    "AgentObservation",
    "SOURCE_LIVE",
    "SOURCE_RELOAD_REQUIRED",
    "SOURCE_UNAVAILABLE",
    "UnitBoardRow",
    "UnitBoardSnapshot",
    "build_unit_board",
    "clip_display",
    "format_board",
    "lane_work_label",
    "metadata_for_unit",
    "safe_text",
    "unavailable_snapshot",
)
