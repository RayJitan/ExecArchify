"""Wire errors, legacy compatibility, and bounded MCP result payloads."""

import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from execarchify.mcp import (
    META,
    MODERN_VERSION,
    REQUEST_BYTES,
    Session,
    bounded_result,
    encode,
    serve_mcp,
)


def request(method, params=None, request_id=1):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": {} if params is None else params,
    }


class McpProtocolTests(unittest.TestCase):
    def test_modern_metadata_and_tool_errors(self):
        session = Session(None)
        meta = {
            META + "protocolVersion": MODERN_VERSION,
            META + "clientCapabilities": {},
        }
        self.assertEqual(
            session.handle(request("server/discover"))["error"]["code"], -32602
        )
        mismatch = session.handle(
            request(
                "server/discover",
                {"_meta": {**meta, META + "protocolVersion": "2100-01-01"}},
            )
        )["error"]
        self.assertEqual(mismatch["code"], -32022)
        self.assertIn(MODERN_VERSION, mismatch["data"]["supported"])
        listing = session.handle(request("tools/list", {"_meta": meta}))["result"]
        discovery = session.handle(request("server/discover", {"_meta": meta}))[
            "result"
        ]
        for result in (discovery, listing):
            self.assertEqual(result["cacheScope"], "public")
            self.assertGreaterEqual(result["ttlMs"], 0)
        self.assertEqual(
            {tool["name"] for tool in listing["tools"]},
            {"context", "resolve", "trace", "impact"},
        )
        self.assertTrue(all("outputSchema" in tool for tool in listing["tools"]))

        with patch("execarchify.mcp.call") as call:
            for name, arguments, error_text in (
                ("trace", {}, "Missing required"),
                ("trace", {"id": "x", "depth": True}, "positive integer"),
                ("trace", {"id": "x", "direction": "sideways"}, "in, out, both"),
                ("resolve", {"query": "\ud800"}, "Unicode"),
                ("context", {"workspace": "somewhere else"}, "Unknown argument"),
            ):
                with self.subTest(name=name, arguments=arguments):
                    result = session.handle(
                        request(
                            "tools/call",
                            {"_meta": meta, "name": name, "arguments": arguments},
                        )
                    )["result"]
                    self.assertTrue(result["isError"])
                    self.assertIn(error_text, result["content"][0]["text"])
            call.assert_not_called()
        unknown = session.handle(request("tools/call", {"_meta": meta, "name": "init"}))
        self.assertEqual(unknown["error"]["code"], -32602)

    def test_legacy_batches_and_text_only_results(self):
        session = Session(None)
        init = session.handle(request("initialize", {"protocolVersion": "2025-03-26"}))
        self.assertEqual(init["result"]["protocolVersion"], "2025-03-26")
        with patch(
            "execarchify.mcp.call", return_value={"root": "/repo", "generation": 1}
        ):
            replies = session.handle(
                [
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    request("tools/list", request_id=2),
                    request("tools/call", {"name": "context"}, request_id=3),
                ]
            )
        self.assertEqual([reply["id"] for reply in replies], [2, 3])
        self.assertTrue(
            all("outputSchema" not in tool for tool in replies[0]["result"]["tools"])
        )
        self.assertNotIn("structuredContent", replies[1]["result"])
        self.assertEqual(
            json.loads(replies[1]["result"]["content"][0]["text"]),
            {"root": "/repo", "generation": 1},
        )

    def test_stdio_recovers_after_bad_and_oversized_messages(self):
        source = io.BytesIO(
            b"{\n"
            + b" " * REQUEST_BYTES
            + b"\n"
            + encode(request("ping", request_id=False))
            + b"\n"
            + encode(request("ping", request_id="ok"))
            + b"\n"
        )
        destination = io.BytesIO()
        with (
            patch("execarchify.mcp.sys.stdin", SimpleNamespace(buffer=source)),
            patch("execarchify.mcp.sys.stdout", SimpleNamespace(buffer=destination)),
        ):
            serve_mcp(None)
        *errors, ping = map(json.loads, destination.getvalue().splitlines())
        self.assertEqual([reply["id"] for reply in errors], [None, None, None])
        self.assertEqual(
            [reply["error"]["code"] for reply in errors], [-32700, -32600, -32600]
        )
        self.assertEqual(ping, {"jsonrpc": "2.0", "id": "ok", "result": {}})

    def test_utf8_budget_keeps_graph_edge_endpoints_and_evidence(self):
        value = {
            "root": "含空格的仓库",
            "generation": 6,
            "nodes": [{"id": str(n), "snippet": "界" * 400} for n in range(75)],
            "edges": [
                {"source": "0", "target": str(n), "path": "code.py", "line": n}
                for n in range(1, 75)
            ],
        }
        self.assertLess(len(encode(value).decode("utf-8")), 55_000)
        self.assertGreater(len(encode(value)), 55_000)
        result = bounded_result(value)
        self.assertTrue(result["truncated"])
        self.assertTrue(result["response_truncated"])
        self.assertLessEqual(len(encode(result)), 55_000)
        self.assertEqual(len(value["nodes"]), 75)
        self.assertEqual((result["root"], result["generation"]), (value["root"], 6))
        self.assertTrue(result["edges"])
        ids = {node["id"] for node in result["nodes"]}
        self.assertTrue(
            all({edge["source"], edge["target"]} <= ids for edge in result["edges"])
        )

    def test_shortened_excerpt_starts_at_the_evidence_line(self):
        result = bounded_result(
            {
                "root": "/repo",
                "generation": 2,
                "item": {
                    "path": "source.py",
                    "line": 7,
                    "snippet_line": 6,
                    "snippet": "#" * 400 + "\nreturn lookup()",
                },
            },
            budget=250,
        )
        self.assertTrue(result["response_truncated"])
        self.assertEqual(result["item"]["snippet"], "return lookup()")
        self.assertEqual(result["item"]["snippet_line"], 7)
