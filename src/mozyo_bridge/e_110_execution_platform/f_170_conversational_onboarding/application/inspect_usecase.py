"""``onboarding.inspect`` — build the preflight + drift facts from the tree.

This is the single deterministic probing site. It classifies the path, resolves
the herdr binary from the trusted environment, reads the on-disk onboarding
receipt and existing config readability, hashes the adoption-relevant files for
drift binding, and assembles the closed :class:`OnboardingPreflight` plus the
:class:`OnboardingFacts` a plan binds to.

``onboarding.plan`` calls this itself and builds the plan from *these* facts —
it never accepts model-supplied path / risk / adoption / binary facts. That is
the whole point of re-inspecting: the model is a UI, not a source of truth.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from mozyo_bridge.application.repo_local_config_loader import (
    load_repo_local_config_from_path,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (
    RepoLocalConfigError,
)
from mozyo_bridge.shared.paths import REPO_LOCAL_CONFIG_MARKER, WORKSPACE_MARKERS

from ..domain.path_safety import (
    ONBOARDING_RECEIPT_MARKER,
    MountProbe,
    classify_path_safety,
)
from ..domain.plan import OnboardingFacts
from ..domain.preflight import (
    RECEIPT_STATE_BROKEN,
    RECEIPT_STATE_NONE,
    HerdrBinary,
    OnboardingPreflight,
    assemble_preflight,
)
from ..domain.receipt import OnboardingReceipt, ReceiptError, parse_receipt
from .herdr_binary import resolve_herdr_binary
from .mount_probe import LiveMountProbe

__all__ = ("InspectResult", "inspect_onboarding")

# Adoption-relevant files whose content is hashed into the drift fingerprint.
_DRIFT_FILES: tuple[str, ...] = (
    REPO_LOCAL_CONFIG_MARKER,
    ONBOARDING_RECEIPT_MARKER,
) + WORKSPACE_MARKERS


@dataclass(frozen=True)
class InspectResult:
    preflight: OnboardingPreflight
    facts: OnboardingFacts
    receipt: OnboardingReceipt | None


def _sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _read_receipt(
    root: Path, secret: str | None
) -> tuple[str, OnboardingReceipt | None]:
    """Return ``(receipt_state, receipt)`` for the on-disk onboarding receipt.

    A present receipt is verified against the trusted ``secret`` (signature +
    coherence). A missing secret, an unreadable file, or a failed validation all
    classify as ``broken`` — the preflight then blocks the root (a forged /
    unverifiable receipt is never trusted as resume authority; Redmine #13501 F3).
    """
    receipt_path = root / ONBOARDING_RECEIPT_MARKER
    if not receipt_path.exists():
        return RECEIPT_STATE_NONE, None
    if not isinstance(secret, str) or not secret.strip():
        return RECEIPT_STATE_BROKEN, None
    try:
        receipt = parse_receipt(receipt_path.read_text(encoding="utf-8"), secret=secret)
    except (OSError, ReceiptError):
        return RECEIPT_STATE_BROKEN, None
    return receipt.state, receipt


def _config_readable(root: Path) -> bool:
    config_path = root / REPO_LOCAL_CONFIG_MARKER
    if not config_path.exists():
        return True
    try:
        load_repo_local_config_from_path(config_path)
        return True
    except RepoLocalConfigError:
        return False


def inspect_onboarding(
    raw_root: str | Path,
    *,
    home: Path | None = None,
    sync_roots: Sequence[Path] | None = None,
    env: Mapping[str, str] | None = None,
    mount_probe: MountProbe | None = None,
    gate_secret: str | None = None,
) -> InspectResult:
    """Deterministically inspect ``raw_root`` into preflight + drift facts.

    A ``MountProbe`` is **always** supplied to the classifier (defaulting to the
    live :class:`LiveMountProbe`): the classifier fails closed to ``ambiguous``
    without positive mount evidence (Redmine #13508 F3), so public onboarding
    inspect never succeeds without a mount adapter. Tests inject a fake probe for
    determinism. ``gate_secret`` (the trusted onboarding secret) is required to
    verify a present onboarding receipt's signature; without it a present receipt
    is treated as broken (blocked).
    """
    home = Path(home) if home is not None else Path.home()
    if mount_probe is None:
        mount_probe = LiveMountProbe()
    safety = classify_path_safety(
        raw_root, home=home, sync_roots=sync_roots, mount_probe=mount_probe
    )
    herdr = resolve_herdr_binary(env)

    # For an ambiguous/unresolved root the canonical path is best-effort; skip
    # filesystem-dependent probes (receipt / config / hashes) — the preflight is
    # a hard block regardless.
    if safety.is_hard_block and not safety.root.is_dir():
        preflight = assemble_preflight(safety, herdr)
        facts = _facts_from(safety, preflight, herdr, {})
        return InspectResult(preflight=preflight, facts=facts, receipt=None)

    root = safety.root
    receipt_state, receipt = _read_receipt(root, gate_secret)
    config_readable = _config_readable(root)
    preflight = assemble_preflight(
        safety, herdr, receipt_state=receipt_state, config_readable=config_readable
    )

    file_hashes: dict[str, str] = {}
    for rel in _DRIFT_FILES:
        digest = _sha256_file(root / rel)
        if digest is not None:
            file_hashes[rel] = digest

    facts = _facts_from(safety, preflight, herdr, file_hashes)
    return InspectResult(preflight=preflight, facts=facts, receipt=receipt)


def _facts_from(
    safety,
    preflight: OnboardingPreflight,
    herdr: HerdrBinary,
    file_hashes: Mapping[str, str],
) -> OnboardingFacts:
    return OnboardingFacts(
        canonical_root=str(safety.root),
        state=preflight.state,
        root_kind=safety.root_kind,
        path_risk=safety.path_risk,
        adoption_marker=safety.adoption_marker,
        herdr_binary_realpath=herdr.path,
        existing_file_hashes=dict(file_hashes),
    )
