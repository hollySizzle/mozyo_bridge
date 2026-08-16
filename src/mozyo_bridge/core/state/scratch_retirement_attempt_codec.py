"""Small codecs for scratch retirement attempt detail and close progress."""

from __future__ import annotations

import json
from typing import Sequence

_DETAIL_ENVELOPE_PREFIX = "mozyo-retirement-attempt-v1:"


class ScratchRetirementAttemptCodecError(ValueError):
    pass


def encode_attempt_detail(*, approval_evidence: str, detail: str) -> str:
    if not approval_evidence:
        return detail
    return _DETAIL_ENVELOPE_PREFIX + json.dumps(
        {"approval_evidence": approval_evidence, "detail": detail},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def decode_attempt_detail(value: object) -> tuple[str, str]:
    raw = str(value or "")
    if not raw.startswith(_DETAIL_ENVELOPE_PREFIX):
        return "", raw
    try:
        payload = json.loads(raw[len(_DETAIL_ENVELOPE_PREFIX) :])
    except (TypeError, ValueError) as exc:
        raise ScratchRetirementAttemptCodecError("approval envelope is unreadable") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"approval_evidence", "detail"}
        or type(payload.get("approval_evidence")) is not str
        or not payload["approval_evidence"]
        or type(payload.get("detail")) is not str
    ):
        raise ScratchRetirementAttemptCodecError("approval envelope is not exact")
    return payload["approval_evidence"], payload["detail"]


def decode_close_pairs(blob: object) -> tuple[tuple[str, str], ...]:
    if type(blob) is not str:
        raise ScratchRetirementAttemptCodecError("close progress is not text")
    out: list[tuple[str, str]] = []
    for chunk in blob.split("\n"):
        if not chunk:
            continue
        role, separator, locator = chunk.partition("\t")
        if (
            not separator
            or not role
            or role.strip() != role
            or not locator
            or locator.strip() != locator
        ):
            raise ScratchRetirementAttemptCodecError("close progress is malformed")
        out.append((role, locator))
    if len({role for role, _ in out}) != len(out) or len(
        {locator for _, locator in out}
    ) != len(out):
        raise ScratchRetirementAttemptCodecError("close progress is duplicated")
    return tuple(out)


def encode_close_pairs(pairs: Sequence[tuple[str, str]]) -> str:
    wanted = tuple(pairs)
    # Encode through the decoder so writers cannot mint bytes readers reject.
    value = "\n".join(f"{role}\t{locator}" for role, locator in wanted)
    if decode_close_pairs(value) != wanted:
        raise ScratchRetirementAttemptCodecError("close progress is not canonical")
    return value


__all__ = (
    "ScratchRetirementAttemptCodecError",
    "decode_attempt_detail",
    "decode_close_pairs",
    "encode_attempt_detail",
    "encode_close_pairs",
)
