"""Cross-module acceptance: blank agent, two independent roots, and before/after evidence."""

from contextlib import closing
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import time
import unittest

from execarchify.api import call
from execarchify.export import overview
from execarchify.index import configure, discover, synchronize
from execarchify.mcp import META, MODERN_VERSION
from execarchify.model import connect
from execarchify import watch


def git(path, *args):
    return (
        subprocess.run(
            ["git", "-C", str(path), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        .stdout.decode("utf-8")
        .strip()
    )


def repository(path):
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.email", "test@invalid.example")
    git(path, "config", "user.name", "Fixture")
    git(path, "config", "core.autocrlf", "false")
    return path


def save(path, name, value):
    entry = path / name
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(value, encoding="utf-8")


def record(path):
    git(path, "add", "-A")
    git(path, "commit", "-qm", "fixture")
    return git(path, "rev-parse", "HEAD")


def stdio_exchange(project, messages):
    payload = "".join(json.dumps(message) + "\n" for message in messages).encode(
        "utf-8"
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "execarchify",
            "mcp",
            "--workspace",
            str(project),
            "--no-watch",
        ],
        input=payload,
        capture_output=True,
        timeout=30,
        check=True,
    )
    return list(map(json.loads, proc.stdout.splitlines()))


class Journeys(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = repository(self.base / "renderer")
        self.shared = repository(self.base / "formats")
        save(
            self.shared, "formats.py", "def convert(value):\n    return value.strip()\n"
        )
        self.public_v1 = record(self.shared)
        self.config = {
            "terms": {"规格换算": ["convert"]},
            "layers": [{"name": "Runtime", "paths": ["src/**"]}],
            "pins": {"../formats": self.public_v1},
            "relations": [
                {
                    "from": "src/consumer.py#render",
                    "to": "docs/guide.md",
                    "kind": "documents",
                }
            ],
        }
        save(
            self.project,
            ".execarchify.json",
            json.dumps(self.config, ensure_ascii=False),
        )
        save(
            self.project,
            "src/consumer.py",
            "from formats import convert\n\ndef render(value):\n    return convert(value)\n",
        )
        save(
            self.project,
            "tests/test_render.py",
            "from src.consumer import render\n\ndef test_output():\n    return render('foo')\n",
        )
        save(
            self.project,
            "docs/guide.md",
            "# Format reference\nThe spectrum marker is violet.\n",
        )
        save(self.project, "assets/cfg.exotic", "universal_payload_marker = 41\n")
        self.v1 = record(self.project)
        self.ws = discover(self.project)
        configure(self.ws, [])

    def test_one_root_configurable_concept_trace_and_opaque_file(self):
        roots = call(self.ws, "context")
        self.assertEqual([repo["role"] for repo in roots["repositories"]], ["root"])
        self.assertIn("Runtime", [g["name"] for g in roots["groups"]])
        novel = call(self.ws, "resolve", {"query": "universal_payload_marker"})[
            "results"
        ]
        self.assertIn("assets/cfg.exotic", [n["path"] for n in novel])
        single = call(
            self.ws, "trace", {"id": "src/consumer.py#render", "direction": "out"}
        )
        self.assertTrue(any("convert" in gap for gap in single["gaps"]))
        self.assertIn("documents", [edge["kind"] for edge in single["edges"]])
        configure(self.ws, ["../formats"])
        result = call(self.ws, "resolve", {"query": "规格换算"})["results"]
        self.assertEqual(result[0]["name"], "convert")
        self.assertEqual(result[0]["revision"], self.public_v1)
        linked = call(
            self.ws, "trace", {"id": "src/consumer.py#render", "direction": "out"}
        )
        edge = next(e for e in linked["edges"] if e["kind"] == "call")
        self.assertEqual(
            (edge["repo"], edge["path"], edge["line"]), ("root", "src/consumer.py", 4)
        )
        self.assertEqual(
            {n["name"] for n in linked["nodes"] if n["repo"] != "root"}, {"convert"}
        )
        other = repository(self.base / "separate")
        save(other, "app.py", "def another():\n    pass\n")
        self.assertNotIn(
            result[0]["id"],
            [
                n["id"]
                for n in call(discover(other), "resolve", {"query": "规格换算"})[
                    "results"
                ]
            ],
        )
        configure(self.ws, [])
        with self.assertRaises(ValueError):
            call(self.ws, "context", {"id": result[0]["id"]})

    def test_impact_deleted_public_field_and_historical_head(self):
        configure(self.ws, ["../formats"])
        call(self.ws, "context")
        save(
            self.shared,
            "formats.py",
            "def convert_new(value):\n    return value.upper()\n",
        )
        v2 = record(self.shared)
        self.config["pins"]["../formats"] = v2
        save(
            self.project,
            ".execarchify.json",
            json.dumps(self.config, ensure_ascii=False),
        )
        changes = call(self.ws, "impact")
        self.assertTrue(
            any(
                x["path"] == "formats.py" and x["repo"] != "root"
                for x in changes["changes"]
            )
        )
        before = [n for n in changes["direct"] if n["name"] == "render"]
        self.assertTrue(before, changes)
        self.assertEqual(before[0]["side"], "base")
        self.assertIn("convert(value)", before[0]["evidence"]["snippet"])
        v2_root = record(self.project)
        save(
            self.project,
            "ignored-by-history.py",
            "def transient_sentinel():\n    pass\n",
        )
        historical = call(self.ws, "impact", {"base": self.v1, "head": v2_root})
        self.assertNotIn(
            "ignored-by-history.py", [row["path"] for row in historical["changes"]]
        )
        self.assertEqual(historical["head"], v2_root)
        self.assertEqual(git(self.shared, "rev-parse", "HEAD"), v2)

    def test_standalone_shared_export_and_mcp_session(self):
        public = discover(self.shared)
        self.assertEqual(
            [r["role"] for r in call(public, "context")["repositories"]], ["root"]
        )
        self.assertEqual(call(public, "impact")["external_consumers"], "out_of_scope")
        result = overview(self.ws)
        self.assertEqual(result["diagram_type"], "architecture")
        self.assertTrue(result["connections"])
        commands = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25"},
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "context", "arguments": {}},
            },
        ]
        responses = stdio_exchange(self.project, commands)
        self.assertEqual([v["id"] for v in responses], [1, 2, 3])
        self.assertEqual(
            {t["name"] for t in responses[1]["result"]["tools"]},
            {"context", "resolve", "trace", "impact"},
        )
        context = json.loads(responses[2]["result"]["content"][0]["text"])
        self.assertEqual(Path(context["root"]), self.project)
        self.assertEqual(responses[2]["result"]["structuredContent"], context)

    def test_modern_mcp_workflow_preserves_source_versions(self):
        configure(self.ws, ["../formats"])
        source = "from formats import convert\n\ndef render(value):\n    return convert(value + 'x')\n"
        save(self.project, "src/consumer.py", source)
        head = record(self.project)
        save(self.project, "scratch.py", "marker = 1\n")
        meta = {
            META + "protocolVersion": MODERN_VERSION,
            META + "clientCapabilities": {},
        }
        requests = [
            ("server/discover", {}),
            (
                "tools/call",
                {"name": "context", "arguments": {"architecture": True}},
            ),
            ("tools/call", {"name": "resolve", "arguments": {"query": "规格换算"}}),
            (
                "tools/call",
                {
                    "name": "context",
                    "arguments": {"id": "src/consumer.py#render"},
                },
            ),
            (
                "tools/call",
                {
                    "name": "trace",
                    "arguments": {"id": "src/consumer.py#render", "direction": "out"},
                },
            ),
            (
                "tools/call",
                {"name": "impact", "arguments": {"base": self.v1, "head": head}},
            ),
        ]
        responses = stdio_exchange(
            self.project,
            [
                {
                    "jsonrpc": "2.0",
                    "id": index,
                    "method": method,
                    "params": {**params, "_meta": meta},
                }
                for index, (method, params) in enumerate(requests)
            ],
        )
        self.assertEqual([row["id"] for row in responses], list(range(len(requests))))
        discovery, *outputs = [row["result"] for row in responses]
        self.assertIn(MODERN_VERSION, discovery["supportedVersions"])
        for output in outputs:
            self.assertFalse(output["isError"], output)
            self.assertEqual(output["resultType"], "complete")
            self.assertEqual(
                output["structuredContent"], json.loads(output["content"][0]["text"])
            )
        context, search, excerpt, trace, impact = [
            output["structuredContent"] for output in outputs
        ]
        self.assertEqual(Path(context["root"]), self.project)
        self.assertTrue(
            any(
                repo["binding"] == "pinned" and repo["revision"] == self.public_v1
                for repo in context["repositories"]
            )
        )
        sample = next(
            edge["sample"]
            for edge in context["group_edges"]
            if edge["kind"] == "documents"
        )
        self.assertEqual(sample["pointer"], "/relations/0")
        self.assertEqual(sample["revision"], head)
        self.assertTrue(sample["digest"])
        self.assertEqual(search["results"][0]["revision"], self.public_v1)
        self.assertEqual(search["results"][0]["name"], "convert")
        edge = next(edge for edge in trace["edges"] if edge["kind"] == "call")
        self.assertEqual(
            (edge["line"], edge["revision"], edge["binding"]), (4, head, "worktree")
        )
        self.assertEqual(edge["digest"], excerpt["item"]["digest"])
        self.assertEqual(edge["target"], search["results"][0]["id"])
        self.assertEqual((impact["base"], impact["head"]), (self.v1, head))
        self.assertEqual(
            [row["path"] for row in impact["changes"]], ["src/consumer.py"]
        )
        affected = next(
            node for node in impact["direct"] if node["name"] == "test_output"
        )
        self.assertEqual(affected["evidence_path"][0]["binding"], "commit")
        self.assertEqual(
            affected["evidence_path"][0]["revision"],
            self.v1 if affected["side"] == "base" else head,
        )

    def test_generic_signal_directions_group_projection_and_change_path(self):
        save(self.project, "src/publish.py", "def emit():\n    return 1\n")
        save(self.project, "bus/contract.proto", "// Event contract for this fixture\n")
        save(self.project, "io/consumer.py", "def accept():\n    return True\n")
        self.config["relations"] = [
            {
                "from": "src/publish.py#emit",
                "to": "bus/contract.proto",
                "kind": "publishes",
                "impact": "forward",
            },
            {
                "from": "bus/contract.proto",
                "to": "io/consumer.py#accept",
                "kind": "routes_to",
                "impact": "forward",
            },
        ]
        save(
            self.project,
            ".execarchify.json",
            json.dumps(self.config, ensure_ascii=False, indent=2),
        )
        record(self.project)
        save(self.project, "src/publish.py", "def emit():\n    return 2\n")
        architecture = call(self.ws, "context", {"architecture": True})
        hop = next(e for e in architecture["group_edges"] if e["kind"] == "publishes")
        self.assertEqual((hop["source_group"], hop["target_group"]), ("Runtime", "bus"))
        self.assertGreater(hop["sample"]["line"], 1)
        diff = call(self.ws, "impact", {"depth": 2})
        self.assertIn("bus/contract.proto", [n["path"] for n in diff["direct"]])
        consumer = next(n for n in diff["indirect"] if n["name"] == "accept")
        self.assertEqual(
            [e["kind"] for e in consumer["evidence_path"]], ["publishes", "routes_to"]
        )
        shorter = call(self.ws, "impact", {"depth": 1})
        self.assertFalse(shorter["indirect"])

    def test_javascript_relative_import_and_git_line_endings(self):
        save(
            self.project,
            "frontend/main.ts",
            'import {run} from "./core";\nexport function main(){return run();}\n',
        )
        save(self.project, "frontend/core.ts", "export function run(){ return 7; }\n")
        save(self.project, ".gitattributes", "*.ts text eol=crlf\n")
        record(self.project)
        module = self.project / "frontend" / "core.ts"
        module.write_bytes(
            module.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        )
        arc = call(self.ws, "trace", {"id": "frontend/main.ts", "direction": "out"})
        self.assertIn("frontend/core.ts", [n["path"] for n in arc["nodes"]])
        back = call(self.ws, "trace", {"id": "frontend/core.ts", "direction": "in"})
        self.assertIn("frontend/main.ts", [n["path"] for n in back["nodes"]])
        self.assertNotIn(
            "frontend/core.ts", [n["path"] for n in call(self.ws, "impact")["changes"]]
        )

    def test_background_updates_without_queries_and_survives_second_start(self):
        synchronize(self.ws)
        self.addCleanup(watch.stop, self.ws)
        first = watch.start(self.ws, 0.25)
        self.assertTrue(first["running"], first)
        again = watch.start(self.ws, 0.25)
        self.assertEqual(first["pid"], again["pid"])
        save(self.project, "late.py", "def newly_added():\n    pass\n")

        def found():
            with closing(connect(self.ws)) as db:
                return db.execute(
                    "SELECT count(*) FROM symbols WHERE name='newly_added'"
                ).fetchone()[0]

        def wait_for(expected):
            until = time.monotonic() + 12
            while time.monotonic() < until:
                if found() == expected:
                    return
                time.sleep(0.1)
            self.fail(
                f"No-query observer did not converge to {expected}: {watch.status(self.ws)}"
            )

        wait_for(1)
        (self.project / "late.py").unlink()
        wait_for(0)
        self.assertFalse(watch.stop(self.ws)["running"])


if __name__ == "__main__":
    unittest.main()
