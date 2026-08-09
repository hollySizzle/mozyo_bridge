"""Operator-scoped Herdr observation sources for the Unit board (Redmine #15138).

One Herdr server owns exactly one host's sockets, database, and workspace state.
That boundary is deliberate and stays physical: this module does **not** merge
servers.  It describes the operator-scoped set of servers a *local client* may
observe, so the board can render local, remote-host, and Dev-Container Units in
one view while every server keeps its own runtime.

The schema is pure and fail-closed.  It owns two things and nothing else:

- **source identity** — the ``host_id`` that joins ``workspace_id`` and
  ``lane_id`` into a cross-host Unit identity, plus the operator-chosen public
  label the board is allowed to display;
- **command shape** — the exact argv used to reach one source.  Connection
  values (ssh target, container name) are private to the operator home file:
  they are inputs to argv construction and are never part of any payload,
  rendered row, or refusal detail.

There is no arbitrary remote shell here.  A source resolves to one of three
fixed argv shapes (local / ssh / container-exec, optionally container-over-ssh),
each built as an argv list from validated tokens.  The only place a string is
handed to a shell is the ssh remote command, and that string is composed with
:func:`shlex.quote` per token, so an operator value can never widen into a
second command.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Sequence


#: The implicit source every client always has: the Herdr server this process
#: can already reach.  Its id is fixed so a Unit observed with no configured
#: source keeps the identity it had before multi-source observation existed.
LOCAL_HOST_ID = "local"

HOST_KIND_LOCAL = "local"
HOST_KIND_SSH = "ssh"
HOST_KIND_CONTAINER = "container"
HOST_KINDS = (HOST_KIND_LOCAL, HOST_KIND_SSH, HOST_KIND_CONTAINER)

#: Container runtimes whose ``exec`` argv shape this module knows.  A Dev
#: Container is an ordinary container to us; we deliberately do not shell out to
#: a devcontainer CLI, because that surface takes a workspace folder rather than
#: a stable container identity.
CONTAINER_RUNTIMES = ("docker", "podman")

#: Bound on how many servers one operator view may fan out to.  Every source
#: costs one subprocess (often one SSH round trip) per refresh, so an unbounded
#: list would silently turn ``watch`` into a connection storm.
MAX_SOURCES = 8

#: Default seconds before a source's connection attempt is abandoned.  Short on
#: purpose: an unreachable host must degrade to a visible ``unavailable`` row
#: quickly rather than stalling the whole board.
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10
MAX_CONNECT_TIMEOUT_SECONDS = 120

#: The mozyo-bridge entry point invoked on a remote source.  A remote source is
#: asked for its *own* public-safe projection rather than its raw Herdr
#: inventory, so each host resolves its own registry, role bindings, and lane
#: metadata and the client never needs a cross-host registry.
DEFAULT_MOZYO_BINARY = "mozyo-bridge"

_HOST_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
#: A short display label with no control codepoints and no surrounding blank
#: run, so the rendered column cannot be padded into a different-looking name.
_LABEL_RE = re.compile(
    r"^[^\s\x00-\x1f\x7f](?:[^\x00-\x1f\x7f]{0,38}[^\s\x00-\x1f\x7f])?$"
)
#: An ssh destination (``host``, ``user@host``, or a ``~/.ssh/config`` alias).
#: No whitespace, no shell metacharacters, and never a leading ``-`` so a
#: destination can never be read as an ssh option.
_SSH_TARGET_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@%:-]{0,127}$")
_CONTAINER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
#: A command name on the trusted PATH of the target host, or an absolute POSIX
#: path to it.  Both are operator-home values; neither is ever displayed.
_BINARY_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
_BINARY_PATH_RE = re.compile(r"^/[A-Za-z0-9_./-]{1,255}$")


class UnitBoardSourceError(ValueError):
    """An operator source declaration is missing, unknown, or contradictory."""


def _require_str(record: Mapping[str, object], key: str, *, where: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise UnitBoardSourceError(
            f"{where} requires a non-empty string '{key}'"
        )
    return value


def _validated(
    value: str, pattern: "re.Pattern[str]", *, key: str, where: str
) -> str:
    if not pattern.fullmatch(value):
        raise UnitBoardSourceError(
            f"{where} has an unsupported '{key}' value shape"
        )
    return value


def _validated_binary(value: str, *, key: str, where: str) -> str:
    if _BINARY_NAME_RE.fullmatch(value) or _BINARY_PATH_RE.fullmatch(value):
        return value
    raise UnitBoardSourceError(
        f"{where} '{key}' must be a plain command name or an absolute path"
    )


def _reject_unknown_keys(
    record: Mapping[str, object], allowed: Iterable[str], *, where: str
) -> None:
    unknown = sorted(str(key) for key in record if key not in set(allowed))
    if unknown:
        raise UnitBoardSourceError(
            f"{where} has unsupported key(s): {', '.join(unknown)}"
        )


@dataclass(frozen=True)
class UnitBoardSource:
    """One observable Herdr server plus the fixed argv shape that reaches it.

    ``label`` is the only operator-supplied value the board may render.  Every
    other field here is connection detail and stays inside argv construction.
    """

    host_id: str
    label: str
    kind: str
    ssh_target: str = ""
    container: str = ""
    container_runtime: str = "docker"
    via: str = ""
    mozyo_binary: str = DEFAULT_MOZYO_BINARY
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if self.kind not in HOST_KINDS:
            raise UnitBoardSourceError(f"unknown source kind: {self.kind!r}")
        if not _HOST_ID_RE.fullmatch(self.host_id):
            raise UnitBoardSourceError(
                "source 'host_id' must be a short lowercase slug"
            )
        # The reserved id is what keeps local Units on their historical opaque
        # key.  A remote source wearing it would mint keys in the local key
        # space and collide with genuinely local Units; a local source under a
        # different id would move every local Unit to a new key.  Rename with
        # ``label``, which is the field the board actually displays.
        if self.is_local and self.host_id != LOCAL_HOST_ID:
            raise UnitBoardSourceError(
                f"the local source must use host_id {LOCAL_HOST_ID!r}; "
                "use 'label' to give it a different display name"
            )
        if not self.is_local and self.host_id == LOCAL_HOST_ID:
            raise UnitBoardSourceError(
                f"host_id {LOCAL_HOST_ID!r} is reserved for the local source"
            )

    @property
    def is_local(self) -> bool:
        return self.kind == HOST_KIND_LOCAL

    def as_payload(self) -> dict[str, str]:
        """Public-safe projection: identity and display label only.

        Deliberately omits ``ssh_target`` / ``container`` / ``mozyo_binary``.
        Those are the private connection values the close conditions forbid on
        every public surface, and a payload is the easiest place to leak them.
        """
        return {
            "host_id": self.host_id,
            "host_label": self.label,
            "host_kind": self.kind,
        }

    @classmethod
    def local_default(cls) -> "UnitBoardSource":
        return cls(host_id=LOCAL_HOST_ID, label=LOCAL_HOST_ID, kind=HOST_KIND_LOCAL)

    @classmethod
    def from_record(cls, record: object) -> "UnitBoardSource":
        """Build one validated source, failing closed on any surprise."""
        if not isinstance(record, Mapping):
            raise UnitBoardSourceError("each Unit board source must be a mapping")
        host_id = _validated(
            _require_str(record, "host_id", where="a Unit board source"),
            _HOST_ID_RE,
            key="host_id",
            where="a Unit board source",
        )
        where = f"Unit board source {host_id!r}"
        kind = _require_str(record, "kind", where=where)
        if kind not in HOST_KINDS:
            raise UnitBoardSourceError(
                f"{where} has an unknown kind {kind!r}; "
                f"expected one of {', '.join(HOST_KINDS)}"
            )

        common = ("host_id", "kind", "label", "mozyo_binary", "connect_timeout")
        allowed = {
            HOST_KIND_LOCAL: common,
            HOST_KIND_SSH: common + ("ssh_target",),
            HOST_KIND_CONTAINER: common + ("container", "container_runtime", "via"),
        }[kind]
        _reject_unknown_keys(record, allowed, where=where)

        raw_label = record.get("label", host_id)
        if not isinstance(raw_label, str) or not raw_label:
            raise UnitBoardSourceError(f"{where} 'label' must be a non-empty string")
        label = _validated(raw_label, _LABEL_RE, key="label", where=where)

        mozyo_binary = DEFAULT_MOZYO_BINARY
        if "mozyo_binary" in record:
            mozyo_binary = _validated_binary(
                _require_str(record, "mozyo_binary", where=where),
                key="mozyo_binary",
                where=where,
            )

        connect_timeout = DEFAULT_CONNECT_TIMEOUT_SECONDS
        if "connect_timeout" in record:
            raw_timeout = record.get("connect_timeout")
            # ``bool`` is an ``int`` subclass; a YAML ``true`` is not a timeout.
            if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, int):
                raise UnitBoardSourceError(
                    f"{where} 'connect_timeout' must be an integer number of seconds"
                )
            if not 1 <= raw_timeout <= MAX_CONNECT_TIMEOUT_SECONDS:
                raise UnitBoardSourceError(
                    f"{where} 'connect_timeout' must be between 1 and "
                    f"{MAX_CONNECT_TIMEOUT_SECONDS} seconds"
                )
            connect_timeout = raw_timeout

        ssh_target = ""
        container = ""
        container_runtime = "docker"
        via = ""
        if kind == HOST_KIND_SSH:
            ssh_target = _validated(
                _require_str(record, "ssh_target", where=where),
                _SSH_TARGET_RE,
                key="ssh_target",
                where=where,
            )
        elif kind == HOST_KIND_CONTAINER:
            container = _validated(
                _require_str(record, "container", where=where),
                _CONTAINER_RE,
                key="container",
                where=where,
            )
            if "container_runtime" in record:
                container_runtime = _require_str(
                    record, "container_runtime", where=where
                )
                if container_runtime not in CONTAINER_RUNTIMES:
                    raise UnitBoardSourceError(
                        f"{where} 'container_runtime' must be one of "
                        f"{', '.join(CONTAINER_RUNTIMES)}"
                    )
            if "via" in record:
                via = _validated(
                    _require_str(record, "via", where=where),
                    _HOST_ID_RE,
                    key="via",
                    where=where,
                )
                if via == host_id:
                    raise UnitBoardSourceError(f"{where} 'via' cannot reference itself")

        return cls(
            host_id=host_id,
            label=label,
            kind=kind,
            ssh_target=ssh_target,
            container=container,
            container_runtime=container_runtime,
            via=via,
            mozyo_binary=mozyo_binary,
            connect_timeout=connect_timeout,
        )


@dataclass(frozen=True)
class UnitBoardSourcesConfig:
    """The operator's whole observable set, always including the local server."""

    sources: tuple[UnitBoardSource, ...] = field(
        default_factory=lambda: (UnitBoardSource.local_default(),)
    )

    def __post_init__(self) -> None:
        if not self.sources:
            raise UnitBoardSourceError("at least the local source is required")
        locals_ = [source for source in self.sources if source.is_local]
        if len(locals_) != 1:
            raise UnitBoardSourceError(
                "exactly one local source is required; the local Herdr server is "
                "always observable and cannot be declared twice"
            )
        seen: set[str] = set()
        for source in self.sources:
            if source.host_id in seen:
                raise UnitBoardSourceError(
                    f"duplicate Unit board source host_id {source.host_id!r}"
                )
            seen.add(source.host_id)
        for source in self.sources:
            if source.kind != HOST_KIND_CONTAINER or not source.via:
                continue
            parent = self.by_id.get(source.via)
            if parent is None:
                raise UnitBoardSourceError(
                    f"Unit board source {source.host_id!r} declares an unknown "
                    f"'via' host_id {source.via!r}"
                )
            if parent.kind == HOST_KIND_CONTAINER:
                # One hop only.  A chain of container hops would make the argv
                # shape unbounded and the failure mode impossible to explain.
                raise UnitBoardSourceError(
                    f"Unit board source {source.host_id!r} cannot route 'via' "
                    "another container source"
                )
        if len(self.sources) > MAX_SOURCES:
            raise UnitBoardSourceError(
                f"at most {MAX_SOURCES} Unit board sources are supported"
            )

    @property
    def by_id(self) -> dict[str, UnitBoardSource]:
        return {source.host_id: source for source in self.sources}

    @property
    def local(self) -> UnitBoardSource:
        return next(source for source in self.sources if source.is_local)

    @property
    def remote_sources(self) -> tuple[UnitBoardSource, ...]:
        return tuple(source for source in self.sources if not source.is_local)

    @property
    def is_local_only(self) -> bool:
        """True when nothing has been configured beyond the local server.

        The board keeps its pre-multi-source rendering in this case, so an
        operator who never opts in sees byte-identical output.
        """
        return not self.remote_sources

    @classmethod
    def default(cls) -> "UnitBoardSourcesConfig":
        return cls(sources=(UnitBoardSource.local_default(),))

    @classmethod
    def from_record(cls, record: object) -> "UnitBoardSourcesConfig":
        """Validate a whole operator document, or fail closed."""
        if not isinstance(record, Mapping):
            raise UnitBoardSourceError(
                "the Unit board sources document must be a mapping"
            )
        _reject_unknown_keys(
            record, ("version", "sources"), where="the Unit board sources document"
        )
        version = record.get("version", 1)
        if isinstance(version, bool) or not isinstance(version, int) or version != 1:
            raise UnitBoardSourceError(
                "the Unit board sources document must declare 'version: 1'"
            )
        raw_sources = record.get("sources", [])
        if raw_sources is None:
            raw_sources = []
        if not isinstance(raw_sources, list):
            raise UnitBoardSourceError("'sources' must be a list")
        if len(raw_sources) > MAX_SOURCES:
            raise UnitBoardSourceError(
                f"at most {MAX_SOURCES} Unit board sources are supported"
            )
        declared = tuple(UnitBoardSource.from_record(item) for item in raw_sources)
        if not any(source.is_local for source in declared):
            # The local server is not optional; an operator who only lists remote
            # hosts still sees their own Units first.
            declared = (UnitBoardSource.local_default(),) + declared
        ordered = tuple(source for source in declared if source.is_local) + tuple(
            source for source in declared if not source.is_local
        )
        return cls(sources=ordered)


def ssh_argv(source: UnitBoardSource, command: Sequence[str]) -> tuple[str, ...]:
    """Build the argv that runs ``command`` on one ssh source.

    ``BatchMode`` keeps an unreachable or unauthenticated host from blocking on
    a password prompt — the board must degrade to a visible ``unavailable`` row
    instead of hanging.  ``-T`` avoids allocating a TTY the board never reads.
    ``--`` ends option parsing, and the remote command is quoted per token so a
    connection value can never become a second command.
    """
    if source.kind != HOST_KIND_SSH:
        raise UnitBoardSourceError("ssh argv requires an ssh source")
    return (
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={source.connect_timeout}",
        "-T",
        "--",
        source.ssh_target,
        " ".join(shlex.quote(part) for part in command),
    )


def source_command_argv(
    source: UnitBoardSource,
    args: Sequence[str],
    *,
    by_id: Optional[Mapping[str, UnitBoardSource]] = None,
) -> tuple[str, ...]:
    """Return the argv that runs ``mozyo-bridge <args>`` on ``source``.

    A local source is the caller's own process boundary and gets no argv here;
    the runtime observes it in-process instead, so this raises rather than
    inventing a subprocess shape for it.
    """
    if not args:
        raise UnitBoardSourceError("a source command requires arguments")
    if any(not isinstance(arg, str) or not arg for arg in args):
        raise UnitBoardSourceError("source command arguments must be non-empty strings")
    if source.is_local:
        raise UnitBoardSourceError(
            "the local source is observed in-process, not through an argv"
        )
    command = [source.mozyo_binary, *args]
    if source.kind == HOST_KIND_SSH:
        return ssh_argv(source, command)
    exec_argv = [source.container_runtime, "exec", source.container, *command]
    if not source.via:
        return tuple(exec_argv)
    parent = (by_id or {}).get(source.via)
    if parent is None:
        raise UnitBoardSourceError(
            f"source {source.host_id!r} routes via an unknown host_id"
        )
    if parent.kind == HOST_KIND_LOCAL:
        return tuple(exec_argv)
    if parent.kind != HOST_KIND_SSH:
        raise UnitBoardSourceError(
            f"source {source.host_id!r} cannot route via a container source"
        )
    return ssh_argv(parent, exec_argv)


__all__ = (
    "CONTAINER_RUNTIMES",
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_MOZYO_BINARY",
    "HOST_KINDS",
    "HOST_KIND_CONTAINER",
    "HOST_KIND_LOCAL",
    "HOST_KIND_SSH",
    "LOCAL_HOST_ID",
    "MAX_CONNECT_TIMEOUT_SECONDS",
    "MAX_SOURCES",
    "UnitBoardSource",
    "UnitBoardSourceError",
    "UnitBoardSourcesConfig",
    "source_command_argv",
    "ssh_argv",
)
