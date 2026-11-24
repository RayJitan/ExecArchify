"""Compare indexed working files with immutable Git trees without changing the checkout."""

import difflib
import hashlib
from pathlib import Path
import re
import sqlite3

from .gitio import blob_reader, commit_at, git_bytes, in_scope_path
from .index import MAX_FILE_BYTES, decode_source, load_pins, put_text, source_binding
from .query import SYMBOL, View, bounded
from .propagation import propagate


def commit(repo, value, optional=False):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or value.startswith("-")
        or "\0" in value
    ):
        raise ValueError("Invalid Git revision")
    resolved = commit_at(Path(repo), value)
    if not resolved:
        if optional:
            return None
        raise ValueError("Unknown Git revision: " + value)
    return resolved


def tree(repo, revision):
    blobs, links = {}, {}
    if not revision:
        return blobs, links
    for line in git_bytes(Path(repo), "ls-tree", "-rlz", "--full-tree", revision).split(
        b"\0"
    ):
        if not line:
            continue
        head, name = line.split(b"\t", 1)
        mode, kind, oid, size = head.split()
        path = in_scope_path(name)
        if path is None:
            continue
        if mode == b"160000":
            links[path] = oid.decode()
        elif (
            kind == b"blob"
            and mode in (b"100644", b"100755")
            and int(size) <= MAX_FILE_BYTES
        ):
            blobs[path] = (oid.decode(), int(size))
    return blobs, links


def checkout_into(db, repo, revision):
    """Only differing text gets extracted. Matching rows keep their symbol and occurrence IDs."""
    blobs, links = tree(repo["path"], revision)
    pending = dict(blobs)
    for row in db.execute(
        "SELECT id,path,body FROM files WHERE repo=?", (repo["id"],)
    ).fetchall():
        path = row["path"]
        entry = blobs.get(path)
        if not entry:
            db.execute("DELETE FROM files WHERE id=?", (row["id"],))
            continue
        oid, _ = entry
        body = row["body"].encode("utf-8")
        algo = "sha1" if len(oid) == 40 else "sha256"
        content_hash = hashlib.new(
            algo, b"blob " + str(len(body)).encode() + b"\0" + body
        ).hexdigest()
        if content_hash == oid:
            del pending[path]
    if pending:
        with blob_reader(Path(repo["path"])) as read:
            for path, (oid, size) in pending.items():
                raw = read(oid, size)
                text = decode_source(raw)
                if text is None:
                    db.execute(
                        "DELETE FROM files WHERE repo=? AND path=?", (repo["id"], path)
                    )
                else:
                    put_text(
                        db, repo["id"], path, text, hashlib.sha256(raw).hexdigest()
                    )
    return links


def historical(live, workspace, revision):
    db = sqlite3.connect(":memory:")
    try:
        live.backup(db)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        repos = [
            dict(r) for r in db.execute("SELECT * FROM repositories ORDER BY role,id")
        ]
        root = next(r for r in repos if r["role"] == "root")
        links = checkout_into(db, root, revision)
        db.execute(
            "UPDATE repositories SET revision=?,binding='commit' WHERE id='root'",
            (revision or "",),
        )
        pins, manifest_error = load_pins(db)
        for repo in repos:
            if repo["id"] == "root":
                continue
            binding, ref, _ = source_binding(
                workspace.root, Path(repo["path"]), links, pins, manifest_error
            )
            if binding == "pinned":
                checkout_into(db, repo, ref)
            else:
                db.execute("DELETE FROM files WHERE repo=?", (repo["id"],))
            db.execute(
                "UPDATE repositories SET revision=?,binding=? WHERE id=?",
                (ref, binding, repo["id"]),
            )
        db.commit()
        return db
    except BaseException:
        db.close()
        raise


def manifest(view):
    return {
        (r["repo"], r["path"]): dict(r)
        for r in view.db.execute("SELECT id,repo,path,digest FROM files")
    }


def changed_ranges(old, new):
    a, b = old.splitlines(), new.splitlines()
    if max(len(a), len(b)) > 20000:
        return ([(1, len(a))] if a else [], [(1, len(b))] if b else [], True)
    changes = [
        (i, j, k, l)
        for tag, i, j, k, l in difflib.SequenceMatcher(None, a, b).get_opcodes()
        if tag != "equal"
    ]
    return (
        [(i + 1, j) for i, j, _, _ in changes if i < j],
        [(k + 1, l) for _, _, k, l in changes if k < l],
        False,
    )


def touched(view, item, spans):
    if not item or not spans:
        return []
    all_rows = [
        dict(r)
        for r in view.db.execute(
            SYMBOL + " WHERE s.file_id=? ORDER BY s.line,s.end_line", (item["id"],)
        )
    ]
    matched = [
        r
        for r in all_rows
        if any(a <= r["end_line"] and b >= r["line"] for a, b in spans)
    ]
    if not matched:
        return [view.node(item["id"])]
    # A change confined to a method should not implicate every caller of its class.
    output = []
    for r in matched:
        children = [
            c
            for c in matched
            if r["line"] <= c["line"]
            and r["end_line"] >= c["end_line"]
            and (r["line"], r["end_line"]) != (c["line"], c["end_line"])
        ]
        if not children or any(
            not any(
                c["line"] <= max(a, r["line"])
                and c["end_line"] >= min(b, r["end_line"])
                for c in children
            )
            for a, b in spans
            if a <= r["end_line"] and b >= r["line"]
        ):
            output.append(r)
    return output


def is_test(node):
    return bool(
        re.search(r"(?:^|[/_.-])(?:tests?|specs?)(?:$|[/_.-])", node["path"], re.I)
        or node["name"].startswith("test_")
    )


def analyze(live, workspace, args):
    base_spec = args.get("base")
    head_spec = args.get("head")
    base = commit(workspace.root, base_spec or "HEAD", optional=not base_spec)
    head = commit(workspace.root, head_spec) if head_spec else None
    limit = bounded(args.get("limit"), 50, 200)
    depth = bounded(args.get("depth"), 2, 5)
    before = historical(live, workspace, base)
    after = None
    try:
        after = historical(live, workspace, head) if head_spec else None
        old, new = View(before), View(after if after is not None else live)
        prior, later = manifest(old), manifest(new)
        gaps = set()
        git_changed = None
        for rid in old.repos:
            if rid != "root" and (
                old.repos[rid]["binding"] != "pinned"
                or new.repos[rid]["binding"] != "pinned"
            ):
                gaps.add(
                    "Shared dependency has no comparable locked revisions: "
                    + Path(old.repos[rid]["path"]).name
                )
        changes = []
        edited, direct, indirect, possible, tests, edges = {}, {}, {}, {}, {}, {}
        truncated = False
        paths = sorted(prior.keys() | later.keys(), key=lambda k: (k[0] != "root", k))
        for key in paths:
            rid, path = key
            if rid != "root" and (
                old.repos[rid]["binding"] != "pinned"
                or new.repos[rid]["binding"] != "pinned"
            ):
                continue
            o, n = prior.get(key), later.get(key)
            if o and n and o["digest"] == n["digest"]:
                continue
            if len(changes) >= limit:
                truncated = True
                break

            def body(view, item):
                return (
                    view.db.execute(
                        "SELECT body FROM files WHERE id=?", (item["id"],)
                    ).fetchone()[0]
                    if item
                    else ""
                )

            old_spans, new_spans, coarse = changed_ranges(body(old, o), body(new, n))
            if (
                rid == "root"
                and not head_spec
                and base
                and o
                and n
                and not old_spans
                and not new_spans
            ):
                # The working copy may be CRLF while Git stores LF. Ask Git before claiming an edit.
                if git_changed is None:
                    raw = git_bytes(
                        workspace.root,
                        "diff",
                        "--no-ext-diff",
                        "--no-textconv",
                        "--name-only",
                        "--no-renames",
                        "-z",
                        base,
                        "--",
                    )
                    git_changed = {
                        in_scope_path(entry) for entry in raw.split(b"\0") if entry
                    }
                if path not in git_changed:
                    continue
            status = "modified" if o and n else "added" if n else "deleted"
            changes.append(
                {
                    "repo": rid,
                    "path": path,
                    "status": status,
                    "old_lines": old_spans[:20],
                    "new_lines": new_spans[:20],
                }
            )
            if coarse:
                gaps.add("Large file: change localized to file only: " + path)
                continue
            for view, side, item, spans in (
                (old, "base", o, old_spans),
                (new, "head", n, new_spans),
            ):
                for node in touched(view, item, spans):
                    if len(edited) >= limit * 3 and node["id"] not in edited:
                        truncated = True
                        continue
                    record = view.decorate(node, "changed source range")
                    record["side"] = side
                    edited[node["id"]] = record
                    if is_test(record):
                        tests[node["id"]] = record
                    affected_nodes, clipped = propagate(
                        view, node, depth=depth, limit=limit, gaps=gaps
                    )
                    truncated |= clipped
                    for other, route in affected_nodes:
                        edge = route[-1]
                        uncertain = any(
                            hop["confidence"] in ("inferred", "heuristic")
                            for hop in route
                        )
                        collection = (
                            possible
                            if uncertain
                            else direct
                            if len(route) == 1
                            else indirect
                        )
                        if len(collection) >= limit and other["id"] not in collection:
                            truncated = True
                            continue
                        if other["id"] in collection and collection[other["id"]][
                            "depth"
                        ] < len(route):
                            continue
                        affected = view.decorate(
                            other,
                            f"{len(route)} hop(s) via {edge['kind']} at {edge['path']}:{edge['line']}",
                        )
                        affected.update(
                            side=side,
                            depth=len(route),
                            evidence=edge,
                            evidence_path=route,
                        )
                        collection[other["id"]] = affected
                        if is_test(affected):
                            tests[other["id"]] = affected
                        for hop in route:
                            edges[
                                (
                                    hop["source"],
                                    hop["target"],
                                    hop["kind"],
                                    hop["path"],
                                    hop["line"],
                                    side,
                                )
                            ] = dict(hop, side=side)
        gaps.update(old.diagnostics + new.diagnostics)
        return {
            "base": base or "EMPTY",
            "head": head or "WORKTREE",
            "changes": changes,
            "changed_symbols": list(edited.values()),
            "direct": list(direct.values()),
            "indirect": [item for key, item in indirect.items() if key not in direct],
            "possible": [
                item
                for key, item in possible.items()
                if key not in direct and key not in indirect
            ],
            "tests": list(tests.values())[:limit],
            "edges": list(edges.values()),
            "gaps": sorted(gaps)[:30],
            "truncated": truncated,
            "depth": depth,
            "external_consumers": "out_of_scope",
        }
    finally:
        before.close()
        if after is not None:
            after.close()
