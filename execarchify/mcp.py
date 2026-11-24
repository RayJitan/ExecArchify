"""Serve the workspace-bound query API over MCP stdio."""

import json
import logging
import sqlite3
import sys

from . import __version__
from .api import call

MODERN_VERSION = "2026-07-28"
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
STRUCTURED_VERSIONS = (MODERN_VERSION, *LEGACY_VERSIONS[:2])
META = "io.modelcontextprotocol/"
SERVER_INFO = {"name": "execarchify", "version": __version__}
CAPABILITIES = {"tools": {}}
INSTRUCTIONS = (
    "Read-only code graph for one Git worktree and its explicitly configured dependencies. "
    "Start with context to verify the root and dependency versions. Use resolve for a term or "
    "symbol, then pass a returned ID to context for source or trace for connections. "
    "Use impact for a Git comparison. Cite paths, lines, and revisions. Check confidence, "
    "gaps, and truncation before concluding that a dependency or signal path is covered."
)
REQUEST_BYTES = 1024 * 1024
RESULT_BYTES = 55_000


def input_schema(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def string(description, **constraints):
    return {"type": "string", "description": description, **constraints}


def positive_integer(description, default, cap):
    return {
        "type": "integer",
        "minimum": 1,
        "default": default,
        "description": f"{description} Values above {cap} use {cap}.",
    }


ID = string(
    "An ID returned by resolve or trace. A unique root-relative path or "
    "path#qualified_symbol also works.",
)
REVISION = "A locally available Git commit, branch, tag, or revision expression."
OBJECT = {"type": "object"}
OBJECTS = {"type": "array", "items": OBJECT}
STRINGS = {"type": "array", "items": {"type": "string"}}
INTEGER = {"type": "integer"}
BOOL = {"type": "boolean"}


def output_schema(**properties):
    return {
        "type": "object",
        "required": ["root", "generation"],
        "properties": {
            "root": string("Absolute path to the bound Git worktree."),
            "generation": {
                **INTEGER,
                "description": "Generation of the workspace index.",
            },
            "gaps": {
                **STRINGS,
                "description": "Known indexing or relationship coverage gaps.",
            },
            "truncated": {
                **BOOL,
                "description": "A query or response budget limited the result.",
            },
            "response_truncated": {
                **BOOL,
                "description": "The MCP response byte limit reduced the query result.",
            },
            "message": {"type": "string"},
            **properties,
        },
    }


TOOLS = {
    "context": (
        (
            "Inspect the bound repository, dependency versions, groups, and indexing gaps. "
            "Use architecture=true for cross-group relationships with sample evidence, "
            "or pass an ID to inspect one indexed source excerpt."
        ),
        input_schema(
            {
                "id": ID,
                "architecture": {
                    **BOOL,
                    "default": False,
                    "description": "Include a bounded scan of cross-group edges when id is omitted.",
                },
            }
        ),
        output_schema(
            item=OBJECT,
            repositories=OBJECTS,
            groups=OBJECTS,
            stats=OBJECT,
            languages=OBJECT,
            terms=STRINGS,
            group_edges=OBJECTS,
            group_edges_truncated=BOOL,
            reference_scan_count=INTEGER,
        ),
    ),
    "resolve": (
        (
            "Find symbols, indexed file paths, repository-specific terms, and source text. "
            "Results include a match reason, source lines, and IDs for follow-up queries."
        ),
        input_schema(
            {
                "query": string(
                    "Symbol, path, term, or short source text. Omit or use an empty "
                    "string to browse the selected scope.",
                    maxLength=2048,
                    default="",
                ),
                "group": string("Exact group name from context."),
                "repo": string(
                    "Repository ID from context. Use root for the main worktree."
                ),
                "limit": positive_integer(
                    "Maximum number of returned matches.", 12, 100
                ),
            }
        ),
        output_schema(results=OBJECTS),
    ),
    "trace": (
        (
            "Walk incoming or outgoing relationships from a known symbol or file. "
            "Use each edge's path, line, revision, and confidence to verify hops. "
            "Read gaps for unresolved targets."
        ),
        input_schema(
            {
                "id": {**ID, "minLength": 1},
                "direction": {
                    **string(
                        "in finds incoming references and out follows outgoing edges."
                    ),
                    "enum": ["in", "out", "both"],
                    "default": "both",
                },
                "depth": positive_integer("Maximum number of graph hops.", 1, 5),
                "limit": positive_integer(
                    "Maximum number of graph nodes, including the start.", 35, 150
                ),
            },
            ["id"],
        ),
        output_schema(nodes=OBJECTS, edges=OBJECTS),
    ),
    "impact": (
        (
            "Compare Git revisions or inspect changes in the current working tree. "
            "Find changed ranges, affected dependents, possible impacts, and candidate tests "
            "with evidence for each hop and its base or head side."
        ),
        input_schema(
            {
                "base": string(f"{REVISION} Omit to use HEAD.", maxLength=512),
                "head": string(
                    f"{REVISION} Omit to use the working tree.", maxLength=512
                ),
                "depth": positive_integer("Maximum number of propagation hops.", 2, 5),
                "limit": positive_integer(
                    "Cap for changed files and each set of dependents.", 50, 200
                ),
            }
        ),
        output_schema(
            base={"type": "string"},
            head={"type": "string"},
            depth=INTEGER,
            external_consumers={"type": "string"},
            changes=OBJECTS,
            changed_symbols=OBJECTS,
            direct=OBJECTS,
            indirect=OBJECTS,
            possible=OBJECTS,
            tests=OBJECTS,
            edges=OBJECTS,
        ),
    ),
}


def encode(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8", errors="backslashreplace")


def shorten_snippets(value):
    if isinstance(value, dict):
        shortened = {key: shorten_snippets(item) for key, item in value.items()}
        snippet = value.get("snippet", "")
        if len(snippet) > 400:
            lines = snippet.split("\n")
            offset = value["line"] - value["snippet_line"]
            if 0 <= offset < len(lines):
                shortened.update(
                    snippet=lines[offset][:400], snippet_line=value["line"]
                )
            else:
                shortened["snippet"] = snippet[:400]
        return shortened
    if isinstance(value, list):
        return [shorten_snippets(item) for item in value]
    return value


def bounded_result(value, budget=RESULT_BYTES):
    """Trim excerpts and whole records while keeping source and graph anchors."""
    if len(encode(value)) <= budget:
        return value
    trimmed = shorten_snippets(value)
    trimmed.update(truncated=True, response_truncated=True)
    while len(encode(trimmed)) > budget:
        lists = {
            name: len(encode(rows))
            for name, rows in trimmed.items()
            if isinstance(rows, list) and len(rows) > 1
        }
        if not lists:
            return {
                "root": value["root"],
                "generation": value["generation"],
                "truncated": True,
                "response_truncated": True,
                "message": "Result exceeds the response budget. Narrow the query or reduce depth and limit.",
            }
        name = max(lists, key=lists.get)
        rows = trimmed[name]
        del rows[len(rows) // 2 :]
        if name == "nodes":
            ids = {node["id"] for node in rows}
            trimmed["edges"] = [
                edge
                for edge in trimmed["edges"]
                if edge["source"] in ids and edge["target"] in ids
            ]
        elif name == "groups" and "group_edges" in trimmed:
            groups = {group["name"] for group in rows}
            trimmed["group_edges"] = [
                edge
                for edge in trimmed["group_edges"]
                if edge["source_group"] in groups and edge["target_group"] in groups
            ]
            trimmed["group_edges_truncated"] = True
        elif name == "group_edges":
            trimmed["group_edges_truncated"] = True
    return trimmed


def validate_arguments(arguments, schema):
    properties = schema["properties"]
    for key in schema["required"]:
        if key not in arguments:
            raise ValueError(f"Missing required argument {key!r}.")
    for key, value in arguments.items():
        prop = properties.get(key)
        if prop is None:
            raise ValueError(f"Unknown argument {key[:80]!r}.")
        kind = prop["type"]
        if kind == "string":
            if not isinstance(value, str):
                raise ValueError(f"{key} must be a string.")
            if len(value) < prop.get("minLength", 0):
                raise ValueError(f"Provide a non-empty {key}.")
            if "maxLength" in prop and len(value) > prop["maxLength"]:
                raise ValueError(
                    f"{key} must have at most {prop['maxLength']} characters."
                )
            if "enum" in prop and value not in prop["enum"]:
                raise ValueError(f"{key} must be one of {', '.join(prop['enum'])}.")
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                raise ValueError(f"{key} must be valid Unicode text.") from None
        elif kind == "integer" and (type(value) is not int or value < prop["minimum"]):
            raise ValueError(f"{key} must be a positive integer.")
        elif kind == "boolean" and type(value) is not bool:
            raise ValueError(f"{key} must be a boolean.")


class ProtocolError(Exception):
    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code = code
        self.data = data


def error_reply(request_id, code, message, data=None):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


class Session:
    def __init__(self, workspace):
        self.workspace = workspace
        self.legacy_version = None

    def handle(self, message):
        if isinstance(message, list) and self.legacy_version == "2025-03-26":
            if not 1 <= len(message) <= 32:
                return error_reply(
                    None, -32600, "Expected 1 to 32 requests in the batch."
                )
            return [
                reply
                for item in message
                if (reply := self.handle_one(item)) is not None
            ] or None
        return self.handle_one(message)

    def handle_one(self, message):
        request_id = message.get("id") if isinstance(message, dict) else None
        valid_id = type(request_id) in (str, int)
        if (
            not isinstance(message, dict)
            or message.get("jsonrpc") != "2.0"
            or not isinstance(message.get("method"), str)
            or ("id" in message and not valid_id)
        ):
            return error_reply(
                request_id if valid_id else None, -32600, "Invalid Request"
            )
        if "id" not in message:
            return None
        try:
            result = self.dispatch(message["method"], message.get("params", {}))
        except ProtocolError as exc:
            return error_reply(request_id, exc.code, str(exc), exc.data)
        except Exception:
            logging.getLogger(__name__).exception("MCP request failed")
            return error_reply(request_id, -32603, "Internal error")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def dispatch(self, method, params):
        if not isinstance(params, dict):
            raise ProtocolError(-32602, "Expected params object.")
        if method == "initialize":
            return self.initialize(params)

        meta = params.get("_meta", {})
        if not isinstance(meta, dict):
            raise ProtocolError(-32602, "Expected _meta object.")
        version_key = META + "protocolVersion"
        if method == "server/discover" or version_key in meta:
            requested = meta.get(version_key)
            if not isinstance(requested, str):
                raise ProtocolError(-32602, f"Expected _meta.{version_key}.")
            if requested != MODERN_VERSION:
                raise ProtocolError(
                    -32022,
                    "Unsupported protocol version",
                    {"supported": [MODERN_VERSION], "requested": requested},
                )
            if not isinstance(meta.get(META + "clientCapabilities"), dict):
                raise ProtocolError(-32602, f"Expected _meta.{META}clientCapabilities.")
            result = self.request(method, params, MODERN_VERSION)
            if method in ("server/discover", "tools/list"):
                result.update(cacheScope="public", ttlMs=300_000)
            return {
                "resultType": "complete",
                "_meta": {META + "serverInfo": SERVER_INFO},
                **result,
            }
        if self.legacy_version is None and method != "ping":
            raise ProtocolError(
                -32602, "Use per-request MCP metadata or initialize first."
            )
        return self.request(method, params, self.legacy_version)

    def initialize(self, params):
        if self.legacy_version is not None:
            raise ProtocolError(-32600, "This MCP session is already initialized.")
        version = params.get("protocolVersion")
        if not isinstance(version, str):
            raise ProtocolError(-32602, "Expected protocolVersion string.")
        self.legacy_version = (
            version if version in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
        )
        return {
            "protocolVersion": self.legacy_version,
            "serverInfo": SERVER_INFO,
            "capabilities": CAPABILITIES,
            "instructions": INSTRUCTIONS,
        }

    def request(self, method, params, version):
        if method == "server/discover":
            return {
                "supportedVersions": [MODERN_VERSION],
                "capabilities": CAPABILITIES,
                "instructions": INSTRUCTIONS,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            if "cursor" in params:
                raise ProtocolError(-32602, "The tool list fits in a single page.")
            return {
                "tools": [
                    {
                        "name": name,
                        "description": description,
                        "inputSchema": inputs,
                        "annotations": {"readOnlyHint": True, "openWorldHint": False},
                        **(
                            {"outputSchema": outputs}
                            if version in STRUCTURED_VERSIONS
                            else {}
                        ),
                    }
                    for name, (description, inputs, outputs) in TOOLS.items()
                ]
            }
        if method == "tools/call":
            return self.call_tool(params, version)
        raise ProtocolError(-32601, "Method not found")

    def call_tool(self, params, version):
        name = params.get("name")
        if not isinstance(name, str) or name not in TOOLS:
            raise ProtocolError(-32602, "Unknown tool. See tools/list.")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ProtocolError(-32602, "Expected tool arguments object.")
        try:
            validate_arguments(arguments, TOOLS[name][1])
        except ValueError as exc:
            if version in ("2025-11-25", MODERN_VERSION):
                return tool_result(str(exc), is_error=True)
            raise ProtocolError(-32602, str(exc)) from exc
        try:
            value = bounded_result(call(self.workspace, name, arguments))
        except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
            return tool_result(str(exc), is_error=True)
        result = tool_result(encode(value).decode("utf-8"))
        if version in STRUCTURED_VERSIONS:
            result["structuredContent"] = value
        return result


def tool_result(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def reject_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


def serve_mcp(workspace):
    session = Session(workspace)
    source, destination = sys.stdin.buffer, sys.stdout.buffer
    while line := source.readline(REQUEST_BYTES + 1):
        if len(line) > REQUEST_BYTES:
            while line and not line.endswith(b"\n"):
                line = source.readline(REQUEST_BYTES + 1)
            reply = error_reply(
                None, -32600, "Request exceeds the stdio message limit."
            )
        else:
            try:
                message = json.loads(
                    line.decode("utf-8"), parse_constant=reject_constant
                )
            except (ValueError, RecursionError):
                reply = error_reply(None, -32700, "Parse error")
            else:
                reply = session.handle(message)
        if reply is not None:
            try:
                destination.write(encode(reply) + b"\n")
                destination.flush()
            except BrokenPipeError:
                return
