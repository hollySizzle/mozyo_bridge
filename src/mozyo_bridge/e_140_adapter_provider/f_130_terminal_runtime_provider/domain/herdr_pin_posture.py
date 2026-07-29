"""herdr supply-chain **pin posture**: generate + validate (Redmine #13249).

The #13175 PoC (``vibes/docs/logics/herdr-poc-13175-experiment-log.md`` E2 / E3)
enumerated herdr's *entire* remote surface and proved it can be shut off. herdr's
only unattended egress is its update layer — two config switches under the
``[update]`` table:

- ``version_check`` — a bounded GET to ``herdr.dev`` for a newer herdr version;
- ``manifest_check`` — a bounded GET that refreshes the agent-detection manifest
  catalog. Its catalog URL can be pointed at an operator-run mirror through the
  trusted-environment variable ``HERDR_AGENT_DETECTION_MANIFEST_CATALOG_URL``.

E3 measured that with **both** switches ``false`` herdr runs completely offline —
zero network sockets, zero remote cache, every agent manifest served ``bundled``.
This module is the pure core that turns that measured fact into a fail-closed
**pin posture**: it *renders* the herdr config that pins the posture and it
*validates* an already-parsed herdr config so an operator (and the opt-in hook
installer, :mod:`...application.herdr_integration_install_ops`) can prove a
posture is pinned before anything mutates operator home.

Two pinned modes, and nothing else counts as pinned:

- :data:`PIN_MODE_OFFLINE` — both switches ``false``. No egress at all (the E3
  "完全オフライン動作" posture). This is the default the installer requires.
- :data:`PIN_MODE_PINNED_MIRROR` — ``version_check`` still ``false`` (there is *no*
  mirror override for the version endpoint, so leaving it on is always an
  un-pinnable ``herdr.dev`` egress), but ``manifest_check`` may stay ``true``
  **iff** an explicit pinned ``https`` catalog URL is supplied. Without that URL a
  ``true`` ``manifest_check`` egresses to herdr's default catalog host, so it is
  rejected as unpinned.

The single most important fail-closed rule, straight from herdr's behaviour: an
**absent** switch is herdr's default, and herdr's default is *checks on* (egress).
A config that simply omits ``version_check`` / ``manifest_check`` is therefore
**unpinned**, not "probably fine" — :func:`validate_pin_record` treats a missing
key exactly like ``true``. Generated code / update fetching is never implicitly
allowed (issue #13249 review focus: "pin posture が remote code/update を暗黙許可
しないこと").

Boundary (kept enforced in code):

- **Pure.** No file IO, no TOML parsing, no network. :func:`render_pin_config`
  emits deterministic config *text*; :func:`validate_pin_record` normalises an
  *already-parsed* mapping (the shape ``tomllib.load`` of the herdr config yields).
  Disk reading / atomic writing is the application ops layer.
- **The herdr binary and the mirror URL are trusted-environment values, never a
  repo-local field** — the same posture the transport resolver (#13496) and the
  ``terminal_transport`` config (#13245) already hold. This module only *describes*
  and *checks* the posture; it never resolves an executable or opens a socket.
- **Closed vocabulary, fail-closed.** Every rejection carries one
  :data:`PIN_FAILURE_REASONS` reason so a caller branches on a stable token rather
  than parsing a message.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Optional

# --- pinned-mode vocabulary (core-owned) -------------------------------------
#: Both update switches off — herdr runs fully offline (PoC E3). The posture the
#: opt-in hook installer requires before it will touch operator home.
PIN_MODE_OFFLINE = "offline"
#: ``manifest_check`` stays on but resolves through an explicit pinned ``https``
#: mirror (``HERDR_AGENT_DETECTION_MANIFEST_CATALOG_URL``); ``version_check`` is
#: still off (no mirror exists for the version endpoint).
PIN_MODE_PINNED_MIRROR = "pinned_mirror"

PIN_MODES: frozenset[str] = frozenset({PIN_MODE_OFFLINE, PIN_MODE_PINNED_MIRROR})

# --- fail-closed reason vocabulary (core-owned) ------------------------------
#: ``version_check`` is on (or absent → herdr default on). There is no mirror
#: override for the version endpoint, so an on ``version_check`` is always an
#: un-pinnable egress to herdr's default host.
REASON_VERSION_CHECK_ENABLED = "version_check_enabled"
#: ``manifest_check`` is on (or absent) and no explicit pinned catalog URL was
#: supplied — herdr would fetch the manifest catalog from its default host.
REASON_MANIFEST_CHECK_UNPINNED = "manifest_check_unpinned"
#: A supplied manifest catalog URL is not an absolute ``https://`` URL (an
#: ``http`` / relative / empty / non-string value is rejected — a pinned mirror
#: must be a fully-qualified TLS endpoint the operator controls).
REASON_MIRROR_URL_INSECURE = "mirror_url_insecure"
#: The ``[update]`` table (or a switch value) has a non-mapping / non-boolean
#: shape — a config we cannot read as a posture at all fails closed rather than
#: being guessed.
REASON_UPDATE_TABLE_MALFORMED = "update_table_malformed"
#: A pinned_mirror posture was requested with no catalog URL, or an offline
#: posture was requested *with* one — the requested mode and its arguments
#: disagree, so the posture is ambiguous.
REASON_MODE_ARGS_MISMATCH = "mode_args_mismatch"

PIN_FAILURE_REASONS: frozenset[str] = frozenset(
    {
        REASON_VERSION_CHECK_ENABLED,
        REASON_MANIFEST_CHECK_UNPINNED,
        REASON_MIRROR_URL_INSECURE,
        REASON_UPDATE_TABLE_MALFORMED,
        REASON_MODE_ARGS_MISMATCH,
    }
)

#: The trusted-environment variable herdr reads for a pinned manifest-catalog
#: mirror (PoC E2). It is surfaced by :meth:`HerdrPinPosture.env_directives` so an
#: operator (or a managed launch) knows exactly what to export; this module never
#: reads or writes it.
MANIFEST_CATALOG_URL_ENV = "HERDR_AGENT_DETECTION_MANIFEST_CATALOG_URL"


class HerdrPinPostureError(ValueError):
    """A requested pin posture is self-contradictory or carries an unsafe value.

    Inherits :class:`ValueError` for fail-closed semantics, matching the sibling
    adapter-boundary errors (``TerminalTransportError`` / ``RepoLocalConfigError``).
    Carries a ``reason`` from :data:`PIN_FAILURE_REASONS` when it stands for a
    rejected posture, so the caller branches on the same closed vocabulary the
    verdict record uses.
    """

    def __init__(self, message: str, *, reason: Optional[str] = None):
        super().__init__(message)
        if reason is not None and reason not in PIN_FAILURE_REASONS:
            raise HerdrPinPostureError(
                f"unknown pin failure reason {reason!r}; expected one of "
                f"{sorted(PIN_FAILURE_REASONS)}"
            )
        self.reason = reason


def _valid_https_url(value: object) -> bool:
    """True iff ``value`` is an absolute ``https://`` URL with a host.

    Deliberately strict and dependency-free: a pinned mirror must be a
    fully-qualified TLS endpoint. ``http://`` (cleartext), a relative value, an
    empty value, whitespace, or a bare host are all rejected — the mirror is a
    supply-chain trust root, so anything a shell / URL library could reinterpret
    fails closed.
    """
    if not isinstance(value, str):
        return False
    prefix = "https://"
    if not value.startswith(prefix):
        return False
    host = value[len(prefix):]
    # A host must be present, contain no whitespace, and not start with another
    # slash (``https:///path`` has an empty authority).
    return bool(host) and host[0] != "/" and not any(ch.isspace() for ch in value)


@dataclass(frozen=True)
class HerdrPinPosture:
    """A pinned herdr supply-chain posture (a rendered config + its env needs).

    Constructed through :meth:`offline` / :meth:`pinned_mirror` so an invalid
    combination (a mirror URL on an offline posture, a missing / cleartext URL on
    a mirror posture) can never exist. The two rendered switches are fixed by the
    mode: ``version_check`` is *always* ``false`` (no mirror override exists), and
    ``manifest_check`` is ``false`` for offline / ``true`` for a pinned mirror.
    """

    mode: str
    manifest_catalog_url: Optional[str] = None

    def __post_init__(self) -> None:
        if self.mode not in PIN_MODES:
            raise HerdrPinPostureError(
                f"pin posture mode {self.mode!r} is not recognised; allowed: "
                f"{sorted(PIN_MODES)}"
            )
        if self.mode == PIN_MODE_OFFLINE and self.manifest_catalog_url is not None:
            raise HerdrPinPostureError(
                "an offline pin posture takes no manifest catalog URL (offline "
                "disables the manifest check entirely)",
                reason=REASON_MODE_ARGS_MISMATCH,
            )
        if self.mode == PIN_MODE_PINNED_MIRROR:
            if self.manifest_catalog_url is None:
                raise HerdrPinPostureError(
                    "a pinned_mirror posture requires an explicit manifest catalog "
                    "URL; without it herdr fetches from its default host (unpinned)",
                    reason=REASON_MODE_ARGS_MISMATCH,
                )
            if not _valid_https_url(self.manifest_catalog_url):
                raise HerdrPinPostureError(
                    f"manifest catalog mirror {self.manifest_catalog_url!r} must be "
                    f"an absolute https:// URL the operator controls",
                    reason=REASON_MIRROR_URL_INSECURE,
                )

    @classmethod
    def offline(cls) -> "HerdrPinPosture":
        """The fully-offline posture (PoC E3): both update switches off."""
        return cls(mode=PIN_MODE_OFFLINE)

    @classmethod
    def pinned_mirror(cls, manifest_catalog_url: str) -> "HerdrPinPosture":
        """A posture that keeps ``manifest_check`` on but pinned to ``url``."""
        return cls(mode=PIN_MODE_PINNED_MIRROR, manifest_catalog_url=manifest_catalog_url)

    @property
    def manifest_check(self) -> bool:
        """The rendered ``manifest_check`` value for this mode."""
        return self.mode == PIN_MODE_PINNED_MIRROR

    @property
    def version_check(self) -> bool:
        """The rendered ``version_check`` value — always off (no mirror exists)."""
        return False

    def env_directives(self) -> "tuple[tuple[str, str], ...]":
        """The trusted-env exports this posture needs (``()`` for offline).

        A pinned mirror needs :data:`MANIFEST_CATALOG_URL_ENV` exported for herdr
        to resolve the manifest catalog from the mirror rather than its default
        host. Offline needs nothing. Returned as ``(name, value)`` pairs so the
        caller can surface them; this module never mutates any environment.
        """
        if self.mode == PIN_MODE_PINNED_MIRROR and self.manifest_catalog_url:
            return ((MANIFEST_CATALOG_URL_ENV, self.manifest_catalog_url),)
        return ()


def render_pin_config(posture: HerdrPinPosture) -> str:
    """Render the herdr config text that pins ``posture`` (deterministic).

    Emits just the ``[update]`` table with the two switches this posture fixes —
    intentionally minimal so the generated file is auditable at a glance and a
    diff against a hand-written config is small. TOML booleans are lowercase
    ``true`` / ``false``. A trailing header comment records the provenance and,
    for a pinned mirror, the env directive the operator must also export (herdr
    reads the mirror from the environment, not this file — PoC E2).

    The output is stable for a given posture (no timestamps / host data), so a
    dry-run preview is byte-identical to what an apply would write.
    """
    lines = [
        "# herdr supply-chain pin posture (mozyo-bridge, Redmine #13249)",
        f"# mode: {posture.mode}",
    ]
    for name, value in posture.env_directives():
        lines.append(f"# requires trusted-env export: {name}={value}")
    lines.append("")
    lines.append("[update]")
    lines.append(f"version_check = {_toml_bool(posture.version_check)}")
    lines.append(f"manifest_check = {_toml_bool(posture.manifest_check)}")
    lines.append("")
    return "\n".join(lines)


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"


@dataclass(frozen=True)
class PinVerdict:
    """The structured outcome of validating a parsed herdr config's posture.

    ``pinned`` is the sole authority on whether the config is safe to run without
    unattended egress. On a pinned verdict ``mode`` names which posture was proven
    and ``reason`` is ``None``; on an unpinned / malformed verdict ``mode`` is
    ``None`` and ``reason`` is one of :data:`PIN_FAILURE_REASONS`. ``detail`` is a
    short, credential-free diagnostic.
    """

    pinned: bool
    mode: Optional[str] = None
    reason: Optional[str] = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.pinned:
            if self.mode not in PIN_MODES:
                raise HerdrPinPostureError(
                    f"a pinned verdict must name a mode from {sorted(PIN_MODES)}, "
                    f"got {self.mode!r}"
                )
            if self.reason is not None:
                raise HerdrPinPostureError(
                    "a pinned verdict may not carry a failure reason"
                )
        else:
            if self.reason not in PIN_FAILURE_REASONS:
                raise HerdrPinPostureError(
                    f"an unpinned verdict must carry a reason from "
                    f"{sorted(PIN_FAILURE_REASONS)}, got {self.reason!r}"
                )
            if self.mode is not None:
                raise HerdrPinPostureError(
                    "an unpinned verdict may not name a pinned mode"
                )

    @classmethod
    def ok(cls, mode: str, detail: str = "") -> "PinVerdict":
        return cls(pinned=True, mode=mode, detail=detail)

    @classmethod
    def unpinned(cls, reason: str, detail: str = "") -> "PinVerdict":
        return cls(pinned=False, reason=reason, detail=detail)


def _read_switch(update_table: "Mapping[object, object]", key: str) -> Optional[bool]:
    """Read a strict boolean switch, or ``None`` when the key is absent.

    A present-but-non-boolean value raises :class:`HerdrPinPostureError`
    (``update_table_malformed``): we will not guess whether ``manifest_check = 0``
    means off. ``None`` (absent) is returned so the caller can apply herdr's
    default (on) explicitly.
    """
    if key not in update_table:
        return None
    value = update_table[key]
    if not isinstance(value, bool):
        raise HerdrPinPostureError(
            f"herdr config [update].{key} must be a boolean, got {value!r}",
            reason=REASON_UPDATE_TABLE_MALFORMED,
        )
    return value


def validate_pin_record(
    update_table: "Optional[Mapping[object, object]]",
    *,
    manifest_catalog_url: Optional[str] = None,
) -> PinVerdict:
    """Validate a parsed herdr ``[update]`` table's pin posture (fail-closed).

    ``update_table`` is the mapping ``tomllib.load(herdr_config)["update"]`` yields
    (``None`` when the config has no ``[update]`` table at all). ``manifest_catalog_url``
    is the trusted-env ``HERDR_AGENT_DETECTION_MANIFEST_CATALOG_URL`` value the
    caller observed, or ``None`` — it is what lets a ``manifest_check = true`` config
    read as a pinned mirror rather than a default-host egress.

    The rules, in fail-closed order (an absent switch is herdr's default = **on**):

    1. a non-mapping ``[update]`` table fails closed (``update_table_malformed``);
    2. ``version_check`` on (or absent) → ``version_check_enabled`` (no mirror
       exists for the version endpoint, so it is always un-pinnable egress);
    3. ``manifest_check`` off (or absent-and-then-treated-on but explicitly off) →
       nothing left to egress → **offline** pinned;
    4. ``manifest_check`` on (or absent) → pinned **only** with an absolute
       ``https`` mirror URL (``pinned_mirror``); otherwise ``manifest_check_unpinned``
       (no URL) or ``mirror_url_insecure`` (a non-https URL).
    """
    if update_table is None:
        # No [update] table → both switches take herdr's on-by-default. Unpinned.
        return PinVerdict.unpinned(
            REASON_VERSION_CHECK_ENABLED,
            "herdr config has no [update] table; version/manifest checks default on",
        )
    if not isinstance(update_table, Mapping):
        return PinVerdict.unpinned(
            REASON_UPDATE_TABLE_MALFORMED,
            f"[update] must be a table, got {type(update_table).__name__}",
        )
    try:
        version_check = _read_switch(update_table, "version_check")
        manifest_check = _read_switch(update_table, "manifest_check")
    except HerdrPinPostureError as exc:
        return PinVerdict.unpinned(exc.reason or REASON_UPDATE_TABLE_MALFORMED, str(exc))

    # An absent switch is herdr's default (on): treat None as True for the egress
    # decision, so a config that simply omits a switch is never read as pinned.
    if version_check is None or version_check is True:
        return PinVerdict.unpinned(
            REASON_VERSION_CHECK_ENABLED,
            "version_check is on (or absent → herdr default on); it has no mirror "
            "override and must be false",
        )
    # version_check is explicitly False here.
    if manifest_check is False:
        return PinVerdict.ok(
            PIN_MODE_OFFLINE, "version_check=false and manifest_check=false (offline)"
        )
    # manifest_check is on (True) or absent (herdr default on): a pinned mirror is
    # the only way this stays pinned.
    if manifest_catalog_url is None:
        return PinVerdict.unpinned(
            REASON_MANIFEST_CHECK_UNPINNED,
            "manifest_check is on (or absent → herdr default on) with no pinned "
            f"{MANIFEST_CATALOG_URL_ENV}; it would fetch from herdr's default host",
        )
    if not _valid_https_url(manifest_catalog_url):
        return PinVerdict.unpinned(
            REASON_MIRROR_URL_INSECURE,
            f"{MANIFEST_CATALOG_URL_ENV} must be an absolute https:// mirror URL",
        )
    return PinVerdict.ok(
        PIN_MODE_PINNED_MIRROR,
        "version_check=false and manifest_check pinned to an https mirror",
    )


__all__ = (
    "MANIFEST_CATALOG_URL_ENV",
    "PIN_FAILURE_REASONS",
    "PIN_MODES",
    "PIN_MODE_OFFLINE",
    "PIN_MODE_PINNED_MIRROR",
    "REASON_MANIFEST_CHECK_UNPINNED",
    "REASON_MIRROR_URL_INSECURE",
    "REASON_MODE_ARGS_MISMATCH",
    "REASON_UPDATE_TABLE_MALFORMED",
    "REASON_VERSION_CHECK_ENABLED",
    "HerdrPinPosture",
    "HerdrPinPostureError",
    "PinVerdict",
    "render_pin_config",
    "validate_pin_record",
)
