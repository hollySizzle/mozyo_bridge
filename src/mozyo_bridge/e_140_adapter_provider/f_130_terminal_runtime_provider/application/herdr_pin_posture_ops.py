"""Application ops for the herdr pin posture: render + verify (Redmine #13249).

The pure model (:mod:`...domain.herdr_pin_posture`) decides what a pinned posture
*is*; this ops layer is the thin IO edge the CLI calls:

- **render** — turn a requested mode (offline / pinned-mirror) into the config text
  an operator pins into their herdr config. No file IO: it prints, it does not write
  operator config (writing herdr's config is a home mutation, and this US keeps the
  only opt-in home mutation in the hook installer). ``render`` is a read-only
  generator.
- **verify** — read an *existing* herdr config file, parse it with the stdlib
  ``tomllib`` (read-only, no third-party dependency), and validate its ``[update]``
  posture. Every way reading can fail — a missing file, a non-file, unreadable
  bytes, invalid TOML, an ``[update]`` that is not a table — resolves to a
  fail-closed unpinned verdict, never a silent pass.

Both surfaces are pure of side effects on operator state: verify only *reads*, and it
reads only the small ``[update]`` table it needs (it never echoes the file's other
contents, so a herdr config carrying anything sensitive is not surfaced).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_pin_posture import (
    MANIFEST_CATALOG_URL_ENV,
    PIN_MODE_OFFLINE,
    PIN_MODE_PINNED_MIRROR,
    REASON_UPDATE_TABLE_MALFORMED,
    HerdrPinPosture,
    HerdrPinPostureError,
    PinVerdict,
    render_pin_config,
    validate_pin_record,
)


def build_posture(
    mode: str, *, manifest_catalog_url: Optional[str] = None
) -> HerdrPinPosture:
    """Construct the requested posture, raising :class:`HerdrPinPostureError` on a bad combo."""
    if mode == PIN_MODE_PINNED_MIRROR:
        return HerdrPinPosture.pinned_mirror(manifest_catalog_url or "")
    if mode == PIN_MODE_OFFLINE:
        return HerdrPinPosture.offline()
    raise HerdrPinPostureError(
        f"unknown pin posture mode {mode!r}; allowed: "
        f"{sorted((PIN_MODE_OFFLINE, PIN_MODE_PINNED_MIRROR))}"
    )


@dataclass(frozen=True)
class PinRenderResult:
    """The rendered config text plus the env exports the posture needs."""

    mode: str
    config_text: str
    env_directives: "tuple[tuple[str, str], ...]"

    def as_payload(self) -> dict:
        return {
            "mode": self.mode,
            "config_text": self.config_text,
            "env_directives": [
                {"name": name, "value": value} for name, value in self.env_directives
            ],
        }


def render_posture(
    mode: str, *, manifest_catalog_url: Optional[str] = None
) -> PinRenderResult:
    """Render the pin config for ``mode`` (read-only; raises on an invalid combo)."""
    posture = build_posture(mode, manifest_catalog_url=manifest_catalog_url)
    return PinRenderResult(
        mode=posture.mode,
        config_text=render_pin_config(posture),
        env_directives=posture.env_directives(),
    )


@dataclass(frozen=True)
class PinVerifyResult:
    """The verdict of verifying a herdr config file's pin posture."""

    config_path: str
    verdict: PinVerdict

    @property
    def ok(self) -> bool:
        return self.verdict.pinned

    def as_payload(self) -> dict:
        return {
            "config_path": self.config_path,
            "pinned": self.verdict.pinned,
            "mode": self.verdict.mode,
            "reason": self.verdict.reason,
            "detail": self.verdict.detail,
        }


def verify_config(
    config_path: Path, *, manifest_catalog_url: Optional[str] = None
) -> PinVerifyResult:
    """Read + validate a herdr config file's ``[update]`` posture (fail-closed).

    ``manifest_catalog_url`` is the trusted-env ``HERDR_AGENT_DETECTION_MANIFEST_CATALOG_URL``
    the caller observed (or ``None``); it is what lets a ``manifest_check = true`` config
    read as a pinned mirror. A missing / unreadable / non-TOML file, or an ``[update]``
    that is not a table, yields an unpinned verdict with a specific reason.
    """
    verdict = _verify_verdict(config_path, manifest_catalog_url=manifest_catalog_url)
    return PinVerifyResult(config_path=str(config_path), verdict=verdict)


def _verify_verdict(
    config_path: Path, *, manifest_catalog_url: Optional[str]
) -> PinVerdict:
    try:
        raw = config_path.read_bytes()
    except FileNotFoundError:
        return PinVerdict.unpinned(
            REASON_UPDATE_TABLE_MALFORMED,
            f"herdr config not found at {config_path}; nothing pins the posture",
        )
    except OSError as exc:
        return PinVerdict.unpinned(
            REASON_UPDATE_TABLE_MALFORMED,
            f"herdr config at {config_path} is unreadable ({exc.__class__.__name__})",
        )
    try:
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        return PinVerdict.unpinned(
            REASON_UPDATE_TABLE_MALFORMED,
            f"herdr config at {config_path} is not valid TOML ({exc.__class__.__name__})",
        )
    update_table = parsed.get("update") if isinstance(parsed, dict) else None
    try:
        return validate_pin_record(
            update_table, manifest_catalog_url=manifest_catalog_url
        )
    except HerdrPinPostureError as exc:
        # validate_pin_record itself never raises (it returns a verdict), but a
        # malformed switch inside a well-formed table is surfaced defensively.
        return PinVerdict.unpinned(
            exc.reason or REASON_UPDATE_TABLE_MALFORMED, str(exc)
        )


def format_render_text(result: PinRenderResult) -> str:
    """Human-readable render output: the config text + any required env exports."""
    lines = [result.config_text.rstrip("\n")]
    if result.env_directives:
        lines.append("")
        lines.append("# Also export in the trusted environment:")
        for name, value in result.env_directives:
            lines.append(f"#   export {name}={value}")
    return "\n".join(lines)


def format_verify_text(result: PinVerifyResult) -> str:
    """Human-readable verify verdict."""
    verdict = result.verdict
    if verdict.pinned:
        head = f"PINNED ({verdict.mode})"
    else:
        head = f"UNPINNED [{verdict.reason}]"
    lines = [f"{head}: {result.config_path}"]
    if verdict.detail:
        lines.append(f"  {verdict.detail}")
    if not verdict.pinned:
        lines.append(
            f"  fix: pin version_check=false / manifest_check=false, or set a pinned "
            f"{MANIFEST_CATALOG_URL_ENV} https mirror"
        )
    return "\n".join(lines)


__all__ = (
    "PinRenderResult",
    "PinVerifyResult",
    "build_posture",
    "format_render_text",
    "format_verify_text",
    "render_posture",
    "verify_config",
)
