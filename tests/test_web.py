"""Exercise the browser's HTTP contract on a real ephemeral loopback socket."""

from contextlib import closing
from http.client import HTTPConnection
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from execarchify.model import Workspace
from execarchify.web import ASSETS, MAX_BODY, _Handler, _server


class ExplorerHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = Workspace(Path("sample"), Path("sample/.git"), Path("index.sqlite"))
        cls.server = _server(cls.workspace, "127.0.0.1", 0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": .01})
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, method, path, body=None, **headers):
        with closing(HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)) as client:
            with patch.object(_Handler, "log_message"):
                client.request(method, path, body=body, headers=headers)
                response = client.getresponse()
                return response.status, response.headers, response.read()

    def test_only_bundled_assets_are_served(self):
        for path, (_, media) in ASSETS.items():
            with self.subTest(path=path):
                status, headers, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(headers.get_content_type(), media)
                self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
                self.assertTrue(body)
                self.assertEqual(self.request("HEAD", path)[2], b"")
        self.assertEqual(self.request("GET", "/.git/config")[0], 404)
        self.assertEqual(self.request("GET", "/static/../web.py")[0], 404)

    def test_queries_call_the_same_bound_api(self):
        answer = {"generation": 8, "root": "sample", "results": ["本地词"]}
        queries = {
            "context": {"architecture": True},
            "resolve": {"query": "本地词", "limit": 4},
            "trace": {"id": "item", "direction": "in"},
            "impact": {"base": "HEAD~1", "head": "HEAD"},
        }
        with patch("execarchify.api.call", return_value=answer) as api:
            for op, args in queries.items():
                with self.subTest(operation=op):
                    status, _, body = self.request("POST", "/api/" + op, json.dumps(args), **{"Content-Type": "application/json; charset=utf-8"})
                    self.assertEqual(status, 200)
                    self.assertEqual(json.loads(body), answer)
                    api.assert_called_with(self.workspace, op, args)

    def test_methods_and_payload_validation(self):
        with patch("execarchify.api.call") as api:
            self.assertEqual(self.request("GET", "/api/trace")[0], 405)
            self.assertEqual(self.request("POST", "/api/init", "{}")[0], 404)
            self.assertEqual(self.request("POST", "/api/context", "{}")[0], 415)
            for body in (b"{", b"[]", b"\xfe"):
                with self.subTest(body=body):
                    self.assertEqual(self.request("POST", "/api/context", body, **{"Content-Type": "application/json"})[0], 400)
            self.assertEqual(self.request("POST", "/api/context", b"x" * (MAX_BODY + 1), **{"Content-Type": "application/json"})[0], 413)
            api.assert_not_called()

    def test_rejects_external_origins_and_serves_api_errors(self):
        for extra in ({"Origin": "http://example.invalid"}, {"Host": "example.invalid"}):
            self.assertEqual(self.request("POST", "/api/context", "{}", **{"Content-Type": "application/json", **extra})[0], 403)
        with patch("execarchify.api.call", side_effect=ValueError("Unknown revision")):
            status, _, body = self.request("POST", "/api/impact", "{}", **{"Content-Type": "application/json"})
            self.assertEqual((status, json.loads(body)), (400, {"error": "Unknown revision"}))


if __name__ == "__main__":
    unittest.main()
