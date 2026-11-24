"""The CLI, stdio MCP server and optional browser share this workspace-bound API."""

from contextlib import closing
import json

from .index import synchronize
from .model import connect
from .query import View


def call(workspace, operation, arguments=None):
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    if operation not in {"context", "resolve", "trace", "impact"}:
        raise ValueError("Unknown operation")
    synchronize(workspace)
    with closing(connect(workspace)) as db:
        db.execute("BEGIN")
        generation = db.execute(
            "SELECT value FROM meta WHERE key='generation'"
        ).fetchone()
        if operation == "impact":
            from .impact import analyze

            result = analyze(db, workspace, arguments)
        else:
            view = View(db)
            result = getattr(view, operation)(arguments)
        gap_row = db.execute("SELECT value FROM meta WHERE key='gaps'").fetchone()
        stored_gaps = json.loads(gap_row[0]) if gap_row else []
        if stored_gaps:
            existing = result.setdefault("gaps", [])
            for gap in stored_gaps:
                message = (
                    gap if isinstance(gap, str) else json.dumps(gap, ensure_ascii=False)
                )
                if message not in existing:
                    existing.append(message)
        result.update(
            root=str(workspace.root), generation=int(generation[0]) if generation else 0
        )
        return result
