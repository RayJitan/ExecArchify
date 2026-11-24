"""Render the current workspace's group projection as Archify architecture IR v1."""

from contextlib import closing
from hashlib import sha256

from .index import synchronize
from .model import connect
from .query import View


def group_id(name):
    return "group-" + sha256(name.encode()).hexdigest()[:12]


def overview(workspace):
    synchronize(workspace)
    with closing(connect(workspace)) as db:
        db.execute("BEGIN")
        view = View(db)
        groups = view.context({})["groups"]
        components = [
            {
                "id": group_id(g["name"]),
                "type": "backend",
                "label": g["name"],
                "sublabel": f"{g['files']} files / {g['symbols']} symbols",
            }
            for g in groups
        ]
        if not components:
            components = [
                {
                    "id": "empty",
                    "type": "backend",
                    "label": workspace.root.name or "Repository",
                    "sublabel": "No indexed text files",
                }
            ]
        connections = [
            {
                "from": group_id(edge["source_group"]),
                "to": group_id(edge["target_group"]),
                "label": f"{edge['kind']} ({edge['count']})",
            }
            for edge in view.group_edges()["group_edges"]
            if edge["confidence"] in ("parsed", "declared")
        ]
    return {
        "schema_version": 1,
        "diagram_type": "architecture",
        "meta": {
            "title": workspace.root.name or "Repository",
            "locale": "en",
            "output": "architecture.html",
            "subtitle": "Workspace source groups and evidenced dependencies",
            "feedback_links": False,
        },
        "layout": {"mode": "auto"},
        "components": components,
        "connections": connections,
    }
