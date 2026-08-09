"""JSON-RPC 2.0 envelope + MCP stdio framing (pure, Redmine #15151).

Primary specification consulted for this module (implementation decision recorded
on the issue): the MCP ``2025-06-18`` revision — ``basic/transports`` for the stdio
framing and ``basic/lifecycle`` for the initialize / initialized / shutdown
sequence. The two rules this module exists to make unbreakable:

- **messages are newline-delimited UTF-8 JSON and MUST NOT contain embedded
  newlines.** :func:`encode_message` guarantees that by construction and refuses
  to emit a frame that would violate it, so a malformed frame is a caught
  producer error rather than a silently desynchronized stream.
- **nothing that is not a valid MCP message may reach stdout.** This module never
  prints; it returns strings. The server writes them, and every diagnostic goes to
  stderr.

No dependency is taken on an MCP SDK. The package's runtime dependency set is
deliberately small (``pyproject.toml``: ``build`` / ``PyYAML`` / a ``tomli``
backport), the surface needed here is five methods over a newline-delimited
transport, and a published CLI should not grow an async client stack to answer
``tools/list``. The trade is that the wire contract is restated here — so it is
restated *once*, in one pure module, pinned by tests.

Everything is pure: parse takes a string, render returns a dict. There is no I/O,
no logging, and no global state, so every framing / protocol error path is
reachable from a unit test without a subprocess or a pipe.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Optional

#: JSON-RPC 2.0 version literal. Any other value is an invalid request.
JSONRPC_VERSION = "2.0"

# --- standard JSON-RPC 2.0 error codes ------------------------------------- #

#: Invalid JSON was received (the frame did not parse).
ERROR_PARSE = -32700
#: The payload parsed but is not a valid JSON-RPC request object.
ERROR_INVALID_REQUEST = -32600
#: The method does not exist / is not supported by this server.
ERROR_METHOD_NOT_FOUND = -32601
#: Invalid method parameters — including an unknown tool name and arguments that
#: fail the tool's declared input schema (MCP ``server/tools`` "Protocol Errors").
ERROR_INVALID_PARAMS = -32602
#: Internal server error.
ERROR_INTERNAL = -32603

#: Emitted when a request arrives before ``initialize`` completed. Server-defined
#: (the -32000..-32099 implementation-defined band), because the lifecycle spec
#: names the ordering requirement but reserves no standard code for it.
ERROR_NOT_INITIALIZED = -32002

#: The largest frame this server will accept, in characters. A single JSON-RPC
#: message over stdio is a tool call's arguments, not a data channel; refusing an
#: oversized frame fail-closed keeps an unbounded line from being buffered whole.
MAX_FRAME_CHARS = 1 << 20


class FrameEncodingError(RuntimeError):
    """A response could not be rendered as one legal stdio frame.

    Raised by :func:`encode_message` instead of writing a frame that would break
    the newline-delimited contract. The server turns it into a stderr diagnostic
    and an internal-error response, never a corrupt stdout write.
    """


@dataclass(frozen=True)
class JsonRpcRequest:
    """One well-formed inbound JSON-RPC request or notification.

    Notification-ness is decided by whether the ``id`` **member was present**, not
    by its value (review j#102186 finding_4). The spec is explicit on both halves:

    - "A Notification is a Request object *without an 'id' member*." /
      "If it is not included it is assumed to be a notification."
    - "An identifier ... MUST contain a String, Number, or NULL value *if
      included*."

    So an explicit ``"id": null`` is a **Request** — discouraged, but a Request —
    and it must be answered with a null-id response. Reading it as a notification
    silently drops a call the client is waiting on. Value and presence are
    therefore carried in separate fields: ``id`` alone cannot express the
    difference between "absent" and "present and null".
    """

    method: str
    #: The id value. Meaningless unless :attr:`has_id` is true.
    id: Optional[Any] = None
    params: Mapping[str, Any] = None  # type: ignore[assignment]
    #: Whether the ``id`` member was present in the frame at all.
    has_id: bool = False

    @property
    def is_notification(self) -> bool:
        """True when no response may be sent for this message."""
        return not self.has_id

    def arguments(self) -> Mapping[str, Any]:
        """``params`` as a mapping, never ``None``."""
        return self.params or {}


@dataclass(frozen=True)
class FrameError:
    """A frame that could not become a :class:`JsonRpcRequest`.

    ``id`` is the request id when one could still be recovered from the payload
    (so an invalid-request response can be correlated), else ``None``.

    ``respond`` is decided by the parser, not re-derived from ``id`` here: a
    refused frame that carried an explicit ``"id": null`` is answerable with a
    null-id response, and a refused frame that carried no id at all is a
    notification that must stay unanswered. Those two cases have the same ``id``
    value and opposite dispositions, so the flag has to be carried.
    """

    code: int
    message: str
    id: Optional[Any] = None
    #: Whether the peer expects an error response. Defaults to ``False`` so a
    #: caller that forgets to set it stays silent rather than injecting an
    #: uncorrelatable frame.
    respond: bool = False

    @property
    def respondable(self) -> bool:
        """True when the peer expects an error response for this frame.

        A parse error is always answered: the frame could not be read at all, so
        the peer cannot be assumed to have meant a notification, and leaving it
        unanswered would hang a request that merely had a syntax error.
        """
        return self.code == ERROR_PARSE or self.respond


def parse_frame(line: str) -> "JsonRpcRequest | FrameError":
    """Parse one stdio frame, fail-closed.

    Returns a :class:`JsonRpcRequest` or a :class:`FrameError`; never raises and
    never partially accepts. A batch (JSON array) is refused rather than silently
    processing its first element — MCP's stdio transport frames one message per
    line, and accepting a batch here would answer with a shape the peer's framing
    does not expect.
    """
    # For these four the id could not be read at all. The spec's rule for that
    # case is to answer with a null id rather than stay silent — the frame may
    # well have been a request, and dropping it would hang the peer.
    if len(line) > MAX_FRAME_CHARS:
        return FrameError(
            ERROR_INVALID_REQUEST,
            f"frame exceeds the {MAX_FRAME_CHARS}-character limit",
            respond=True,
        )
    try:
        payload = json.loads(line)
    except ValueError as exc:
        return FrameError(ERROR_PARSE, f"invalid JSON: {exc}", respond=True)
    if isinstance(payload, list):
        return FrameError(
            ERROR_INVALID_REQUEST,
            "JSON-RPC batches are not supported over the stdio transport",
            respond=True,
        )
    if not isinstance(payload, dict):
        return FrameError(
            ERROR_INVALID_REQUEST, "message must be a JSON object", respond=True
        )

    # Recover the id first so every subsequent refusal can be correlated. Presence
    # and value are read separately: `"id": null` is a Request with a null id, and
    # an absent `id` is a notification. Collapsing them loses that distinction.
    has_id = "id" in payload
    raw_id = payload.get("id")
    id_is_legal = raw_id is None or (
        isinstance(raw_id, (str, int)) and not isinstance(raw_id, bool)
    )
    request_id = raw_id if (has_id and id_is_legal) else None

    if payload.get("jsonrpc") != JSONRPC_VERSION:
        return FrameError(
            ERROR_INVALID_REQUEST,
            f'"jsonrpc" must be exactly "{JSONRPC_VERSION}"',
            request_id,
            respond=has_id and id_is_legal,
        )
    if has_id and not id_is_legal:
        # A float / bool / object id is outside the spec's "String, Number, or
        # NULL" value set. Refuse rather than coercing: the peer correlates on the
        # exact value, and `bool` in particular would slip through an
        # `isinstance(x, int)` check because it subclasses int.
        return FrameError(
            ERROR_INVALID_REQUEST,
            '"id" must be a string, a number, or null',
            None,
            respond=True,
        )
    method = payload.get("method")
    if not isinstance(method, str) or not method:
        return FrameError(
            ERROR_INVALID_REQUEST,
            '"method" must be a non-empty string',
            request_id,
            respond=has_id,
        )
    params = payload.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        # Positional (array) params have no meaning for any MCP method.
        return FrameError(
            ERROR_INVALID_PARAMS,
            '"params" must be an object',
            request_id,
            respond=has_id,
        )
    return JsonRpcRequest(
        method=method, id=request_id, params=params, has_id=has_id
    )


def success_response(request_id: Any, result: Mapping[str, Any]) -> dict:
    """Render a JSON-RPC success response envelope."""
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": dict(result)}


def error_response(
    request_id: Optional[Any],
    code: int,
    message: str,
    data: Optional[Mapping[str, Any]] = None,
) -> dict:
    """Render a JSON-RPC error response envelope."""
    error: dict[str, Any] = {"code": int(code), "message": message}
    if data is not None:
        error["data"] = dict(data)
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error}


def encode_message(message: Mapping[str, Any]) -> str:
    """Render one outbound frame: compact UTF-8 JSON with no embedded newline.

    ``ensure_ascii=False`` keeps non-ASCII text readable on the wire (the frame is
    UTF-8 by contract). ``json.dumps`` escapes ``\\n`` / ``\\r`` inside strings, so
    the only way a raw newline could appear is a non-serializable structure or a
    key type JSON cannot express — both of which raise here rather than corrupting
    the stream.
    """
    try:
        rendered = json.dumps(
            message, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise FrameEncodingError(f"response is not JSON-serializable: {exc}") from exc
    if "\n" in rendered or "\r" in rendered:
        # Unreachable with a conforming json module; asserted rather than assumed,
        # because a desynchronized stdout stream is silent and unrecoverable.
        raise FrameEncodingError("rendered frame contains an embedded newline")
    return rendered


__all__ = (
    "ERROR_INTERNAL",
    "ERROR_INVALID_PARAMS",
    "ERROR_INVALID_REQUEST",
    "ERROR_METHOD_NOT_FOUND",
    "ERROR_NOT_INITIALIZED",
    "ERROR_PARSE",
    "FrameEncodingError",
    "FrameError",
    "JSONRPC_VERSION",
    "JsonRpcRequest",
    "MAX_FRAME_CHARS",
    "encode_message",
    "error_response",
    "parse_frame",
    "success_response",
)
