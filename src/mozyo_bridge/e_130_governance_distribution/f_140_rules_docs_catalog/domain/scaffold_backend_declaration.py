"""`scaffold apply --backend` — declare the terminal backend on day one (Redmine #15527).

Measured before this issue: `scaffold apply` had no backend surface at all. It wrote
`.mozyo-bridge/tmux/agent-ui.conf` but never a `config.yaml`, so every freshly
scaffolded target selected the tmux default, and adopting herdr meant hand-writing
`terminal_transport.backend: herdr` into a config file the scaffold had only shipped
as an `.example`. The owner's question — "does scaffolding give you herdr?" — had the
answer "no, and there is no flag to ask for it".

This module is the pure half of that flag. Three deliberate properties:

- **Omitting the flag changes nothing.** No config is written and the runtime
  default applies — tmux when this flag shipped (1.0.x), herdr since the 2.0
  flip (Redmine #15531). Staying on tmux under 2.0 therefore means declaring it:
  `--backend tmux` here, or `terminal_transport.backend: tmux` by hand.
- **The declaration is not part of the scaffold manifest.** `config.yaml` is the
  coordinator-owned operational config; operators edit it afterwards, and a
  manifest-tracked copy would make `scaffold status` report drift on every legitimate
  edit. It is a one-time bootstrap, like the skip-category artifacts that also stay
  out of the manifest.
- **An existing config is never overwritten.** A target that already carries
  `.mozyo-bridge/config.yaml` has an operator-owned declaration; the flag refuses
  rather than silently replacing it.

Placement: this feature owns the scaffold command surface, so the concern lives in
its domain layer (per the source-layout contract, applied the same way in #15526
review j#105978 finding_1).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

__all__ = (
    "BACKEND_CHOICES",
    "BackendDeclaration",
    "backend_declaration",
)

#: The backends `--backend` accepts — the same closed set the runtime recognises.
BACKEND_CHOICES = ("herdr", "tmux")

#: Repo-relative location of the repo-local config, spelled here rather than
#: imported so this domain module stays free of application-layer imports; the
#: regression suite pins it against the runtime's real reader.
_CONFIG_RELATIVE = PurePosixPath(".mozyo-bridge/config.yaml")


@dataclass(frozen=True)
class BackendDeclaration:
    """What `--backend <value>` will do at ``target``: one write, or one refusal."""

    path: Path
    content: str
    refusal: str | None = None

    @property
    def ok(self) -> bool:
        return self.refusal is None


def backend_declaration(
    target: Path, backend: str, *, config_exists: bool
) -> BackendDeclaration:
    """Decide the declaration write for ``target``, failing closed on operator config.

    ``config_exists`` is supplied by the caller (the application layer owns the
    filesystem read) so this decision — including the exact refusal wording — is
    testable without a disk.
    """
    path = target / Path(_CONFIG_RELATIVE)
    if backend not in BACKEND_CHOICES:
        # The CLI's `choices=` already rejects this; kept for non-CLI callers.
        return BackendDeclaration(
            path,
            "",
            refusal=(
                f"unknown backend {backend!r}: choose one of "
                + " / ".join(BACKEND_CHOICES)
            ),
        )
    if config_exists:
        return BackendDeclaration(
            path,
            "",
            refusal=(
                f"refusing --backend {backend}: {path} already exists and is the "
                "operator-owned terminal-transport declaration. Edit "
                "`terminal_transport.backend` there directly instead of letting "
                "the scaffold overwrite it."
            ),
        )
    content = (
        "# Written by `mozyo-bridge scaffold apply --backend "
        f"{backend}` (Redmine #15527).\n"
        "# Operator-owned from here on: edit directly, the scaffold never "
        "rewrites it\n"
        "# and `scaffold status` does not track it.\n"
        "terminal_transport:\n"
        f"  backend: {backend}\n"
    )
    return BackendDeclaration(path, content)
