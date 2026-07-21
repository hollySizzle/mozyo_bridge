"""Mount-independent adoption classification for the bare-entry gate (#13497 R1).

The bare-`mozyo` gate must NOT run the fresh-adoption mount / path classifier
against an *already-adopted* project: that classifier fails closed to
``ambiguous`` (→ ``blocked``) without positive mount evidence (#13508 F3), which
would break a currently-working adopted launch on an inconclusive mount. j#74919
R1 approves bypassing it **only when adoption is validly complete** — a mere
marker file with a broken config / receipt must not be treated as adopted.

This probe is the mount-independent half of the routing decision: it reads only
the on-disk adoption evidence (typed config readability, the signed onboarding
receipt, scaffold / workspace anchors) and returns a closed status. The full
:func:`inspect_onboarding` (with mount safety) is still used for the
``ABSENT`` / fresh-onboarding branch.

Precedence mirrors :func:`~..domain.preflight.assemble_preflight` **minus** the
mount hard-block: any broken config / receipt → ``broken`` (fail-closed, never
launched over); else an in-progress receipt → ``in_progress``; else a complete
receipt / readable config / anchor → ``complete``; else ``absent``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mozyo_bridge.application.repo_local_config_loader import (
    load_repo_local_config_from_path,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (
    RepoLocalConfigError,
)
from mozyo_bridge.shared.paths import REPO_LOCAL_CONFIG_MARKER, WORKSPACE_MARKERS

from ..domain.path_safety import ONBOARDING_RECEIPT_MARKER
from ..domain.receipt import (
    RECEIPT_STATE_COMPLETE,
    RECEIPT_STATE_IN_PROGRESS,
    OnboardingReceipt,
    ReceiptError,
    parse_receipt,
)

__all__ = (
    "ADOPTION_COMPLETE",
    "ADOPTION_IN_PROGRESS",
    "ADOPTION_BROKEN",
    "ADOPTION_ABSENT",
    "AdoptionStatus",
    "classify_adoption",
)

ADOPTION_COMPLETE = "complete"
ADOPTION_IN_PROGRESS = "in_progress"
ADOPTION_BROKEN = "broken"
ADOPTION_ABSENT = "absent"

_SCAFFOLD_MARKER = ".mozyo-bridge/scaffold.json"


@dataclass(frozen=True)
class AdoptionStatus:
    """The mount-independent adoption classification of a root."""

    status: str
    canonical_root: Path
    receipt: OnboardingReceipt | None = None
    reason: str | None = None

    @property
    def is_complete(self) -> bool:
        return self.status == ADOPTION_COMPLETE

    @property
    def is_in_progress(self) -> bool:
        return self.status == ADOPTION_IN_PROGRESS

    @property
    def is_broken(self) -> bool:
        return self.status == ADOPTION_BROKEN

    @property
    def is_absent(self) -> bool:
        return self.status == ADOPTION_ABSENT


def _config_state(root: Path) -> str:
    """``absent`` / ``readable`` / ``broken`` for the repo-local config."""
    config_path = root / REPO_LOCAL_CONFIG_MARKER
    if not config_path.exists():
        return "absent"
    try:
        load_repo_local_config_from_path(config_path)
        return "readable"
    except (RepoLocalConfigError, OSError):
        return "broken"


def _receipt_state(
    root: Path, secret: str | None
) -> tuple[str, OnboardingReceipt | None]:
    """``none`` / ``complete`` / ``in_progress`` / ``broken`` for the receipt.

    A present receipt with no verifying secret, an unreadable file, or a failed
    signature / coherence check is ``broken`` — a forged / unverifiable receipt
    is never trusted (mirrors ``inspect_usecase`` / #13501 F3).
    """
    receipt_path = root / ONBOARDING_RECEIPT_MARKER
    if not receipt_path.exists():
        return "none", None
    if not isinstance(secret, str) or not secret.strip():
        return ADOPTION_BROKEN, None
    try:
        receipt = parse_receipt(receipt_path.read_text(encoding="utf-8"), secret=secret)
    except (OSError, ReceiptError):
        return ADOPTION_BROKEN, None
    if receipt.state == RECEIPT_STATE_COMPLETE:
        return ADOPTION_COMPLETE, receipt
    if receipt.state == RECEIPT_STATE_IN_PROGRESS:
        return ADOPTION_IN_PROGRESS, receipt
    return ADOPTION_BROKEN, receipt


def _anchor_present(root: Path) -> bool:
    if (root / _SCAFFOLD_MARKER).exists():
        return True
    return any((root / marker).exists() for marker in WORKSPACE_MARKERS)


def classify_adoption(root: str | Path, *, gate_secret: str | None) -> AdoptionStatus:
    """Classify a root's adoption *without* the fresh-adoption mount classifier."""
    canonical = Path(root).expanduser().resolve()

    config_state = _config_state(canonical)
    receipt_state, receipt = _receipt_state(canonical, gate_secret)

    # Any broken adoption evidence fails closed — never launched / adopted over.
    if config_state == "broken":
        return AdoptionStatus(
            ADOPTION_BROKEN,
            canonical,
            receipt,
            reason="existing .mozyo-bridge/config.yaml is present but unreadable",
        )
    if receipt_state == ADOPTION_BROKEN:
        return AdoptionStatus(
            ADOPTION_BROKEN,
            canonical,
            receipt,
            reason="onboarding receipt is present but does not verify / parse",
        )

    if receipt_state == ADOPTION_IN_PROGRESS:
        return AdoptionStatus(ADOPTION_IN_PROGRESS, canonical, receipt)
    if receipt_state == ADOPTION_COMPLETE:
        return AdoptionStatus(ADOPTION_COMPLETE, canonical, receipt)
    if config_state == "readable" or _anchor_present(canonical):
        return AdoptionStatus(ADOPTION_COMPLETE, canonical, receipt)

    return AdoptionStatus(ADOPTION_ABSENT, canonical, receipt)
