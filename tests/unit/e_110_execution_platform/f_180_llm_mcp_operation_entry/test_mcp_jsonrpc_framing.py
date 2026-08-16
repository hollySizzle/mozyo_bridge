"""JSON-RPC / stdio framing specifications (Redmine #15161 / #15163).

The framing rules a desynchronized stdout stream would silently break, pinned
against the MCP ``2025-06-18`` stdio transport: newline-delimited UTF-8 JSON with
no embedded newlines, one message per frame, and every refusal typed.
"""

from __future__ import annotations

import ast
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain import (  # noqa: E402,E501
    jsonrpc,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.jsonrpc import (  # noqa: E402,E501
    ERROR_INVALID_PARAMS,
    ERROR_INVALID_REQUEST,
    ERROR_PARSE,
    FrameEncodingError,
    FrameError,
    JsonRpcRequest,
    MAX_FRAME_CHARS,
    encode_message,
    error_response,
    parse_frame,
    success_response,
)


def frame(**payload) -> str:
    return json.dumps(payload)


class ParseFrameTests(unittest.TestCase):
    def test_well_formed_request_parses(self) -> None:
        parsed = parse_frame(
            frame(jsonrpc="2.0", id=1, method="tools/list", params={"cursor": "a"})
        )
        self.assertIsInstance(parsed, JsonRpcRequest)
        self.assertEqual(parsed.method, "tools/list")
        self.assertEqual(parsed.id, 1)
        self.assertFalse(parsed.is_notification)
        self.assertEqual(parsed.arguments(), {"cursor": "a"})

    def test_notification_has_no_id(self) -> None:
        parsed = parse_frame(frame(jsonrpc="2.0", method="notifications/initialized"))
        self.assertIsInstance(parsed, JsonRpcRequest)
        self.assertTrue(parsed.is_notification)

    def test_absent_params_becomes_empty_mapping(self) -> None:
        parsed = parse_frame(frame(jsonrpc="2.0", id=2, method="ping"))
        self.assertEqual(parsed.arguments(), {})

    def test_invalid_json_is_a_parse_error(self) -> None:
        parsed = parse_frame("{not json")
        self.assertIsInstance(parsed, FrameError)
        self.assertEqual(parsed.code, ERROR_PARSE)
        # A parse error is answerable even with no recoverable id: the peer cannot
        # correlate it, but it must learn the frame was rejected.
        self.assertTrue(parsed.respondable)

    def test_batch_is_refused_rather_than_partially_processed(self) -> None:
        parsed = parse_frame(json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"}]))
        self.assertIsInstance(parsed, FrameError)
        self.assertEqual(parsed.code, ERROR_INVALID_REQUEST)
        self.assertIn("batch", parsed.message)

    def test_non_object_message_is_refused(self) -> None:
        parsed = parse_frame(json.dumps("ping"))
        self.assertIsInstance(parsed, FrameError)
        self.assertEqual(parsed.code, ERROR_INVALID_REQUEST)

    def test_wrong_jsonrpc_version_is_refused_and_correlated(self) -> None:
        parsed = parse_frame(frame(jsonrpc="1.0", id=7, method="ping"))
        self.assertIsInstance(parsed, FrameError)
        self.assertEqual(parsed.code, ERROR_INVALID_REQUEST)
        self.assertEqual(parsed.id, 7)

    def test_missing_method_is_refused(self) -> None:
        parsed = parse_frame(frame(jsonrpc="2.0", id=3))
        self.assertIsInstance(parsed, FrameError)
        self.assertEqual(parsed.code, ERROR_INVALID_REQUEST)

    def test_array_params_are_refused(self) -> None:
        parsed = parse_frame(frame(jsonrpc="2.0", id=4, method="ping", params=[1, 2]))
        self.assertIsInstance(parsed, FrameError)
        self.assertEqual(parsed.code, ERROR_INVALID_PARAMS)

    def test_non_scalar_id_is_refused_and_not_coerced(self) -> None:
        parsed = parse_frame(frame(jsonrpc="2.0", id={"a": 1}, method="ping"))
        self.assertIsInstance(parsed, FrameError)
        # The id could not be recovered, so the refusal carries none rather than
        # inventing one the peer would not recognize.
        self.assertIsNone(parsed.id)

    def test_oversized_frame_is_refused_before_parsing(self) -> None:
        parsed = parse_frame("x" * (MAX_FRAME_CHARS + 1))
        self.assertIsInstance(parsed, FrameError)
        self.assertEqual(parsed.code, ERROR_INVALID_REQUEST)

    def test_parse_never_raises_for_arbitrary_input(self) -> None:
        for payload in ("", "   ", "null", "0", "[]", "{}", '{"jsonrpc": null}'):
            self.assertIsInstance(
                parse_frame(payload), (JsonRpcRequest, FrameError), payload
            )


class EncodeFrameTests(unittest.TestCase):
    def test_encoded_frame_has_no_embedded_newline(self) -> None:
        rendered = encode_message(
            success_response(1, {"text": "line one\nline two\r\nline three"})
        )
        self.assertNotIn("\n", rendered)
        self.assertNotIn("\r", rendered)
        self.assertEqual(
            json.loads(rendered)["result"]["text"], "line one\nline two\r\nline three"
        )

    def test_non_ascii_survives_round_trip(self) -> None:
        rendered = encode_message(success_response(1, {"text": "日本語 ✓"}))
        self.assertEqual(json.loads(rendered)["result"]["text"], "日本語 ✓")

    def test_unserializable_response_raises_rather_than_corrupting(self) -> None:
        with self.assertRaises(FrameEncodingError):
            encode_message({"jsonrpc": "2.0", "id": 1, "result": {"x": object()}})

    def test_nan_is_refused(self) -> None:
        with self.assertRaises(FrameEncodingError):
            encode_message({"jsonrpc": "2.0", "id": 1, "result": {"x": float("nan")}})

    def test_error_response_shape(self) -> None:
        rendered = json.loads(
            encode_message(error_response(9, ERROR_INVALID_PARAMS, "bad", {"why": "x"}))
        )
        self.assertEqual(rendered["jsonrpc"], "2.0")
        self.assertEqual(rendered["id"], 9)
        self.assertEqual(rendered["error"]["code"], ERROR_INVALID_PARAMS)
        self.assertEqual(rendered["error"]["data"], {"why": "x"})
        self.assertNotIn("result", rendered)

    def test_success_response_carries_no_error_key(self) -> None:
        rendered = json.loads(encode_message(success_response(9, {"ok": True})))
        self.assertNotIn("error", rendered)


class ModuleBoundaryTests(unittest.TestCase):
    def test_module_imports_nothing_that_performs_io(self) -> None:
        """The framing module is pure: it imports no I/O or process machinery.

        Checked on the parsed AST rather than the file text, so the module may
        *describe* what it does not do (the docstring names ``subprocess``) without
        the guard mistaking prose for a dependency.
        """
        tree = ast.parse(Path(jsonrpc.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported & {"subprocess", "os", "sys", "shutil", "socket"}, set())

    def test_module_never_calls_print(self) -> None:
        tree = ast.parse(Path(jsonrpc.__file__).read_text(encoding="utf-8"))
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertNotIn("print", called)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
