"""Read a single database snapshot and retain the origin of each answer."""

from collections import Counter, deque
from difflib import SequenceMatcher
from fnmatch import fnmatchcase
from pathlib import PurePosixPath, Path
import json
import re
from .imports import import_paths


SYMBOL = """SELECT s.id,s.name,s.qualified,s.kind,s.line,s.end_line,s.confidence,
 f.id file_id,f.repo,f.path,f.language FROM symbols s JOIN files f ON f.id=s.file_id"""
FILE = """SELECT f.id,f.path name,f.path qualified,'file' kind,1 line,1 end_line,
 'indexed' confidence,f.id file_id,f.repo,f.path,f.language FROM files f"""


def bounded(value, default, maximum):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("Expected a positive integer")
    return min(value, maximum)


def words(value):
    value = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    return re.findall(r"[^\W_]+", value.casefold(), re.UNICODE)


def snippets(body, line, padding=2):
    lines = body.splitlines()
    start = max(0, line - padding - 1)
    return start + 1, "\n".join(row[:400] for row in lines[start : line + padding])


def relation_lines(text):
    """Find actual declaration lines in already validated JSON, including multiline arrays."""
    decoder = json.JSONDecoder()
    blanks = re.compile(r"\s*")

    def skip(pos):
        return blanks.match(text, pos).end()

    rows = []
    pos = skip(skip(0) + 1)
    while pos < len(text) and text[pos] != "}":
        key, pos = decoder.raw_decode(text, pos)
        pos = skip(skip(pos) + 1)
        if key == "relations" and text[pos] == "[":
            rows = []
            pos = skip(pos + 1)
            while text[pos] != "]":
                rows.append(text.count("\n", 0, pos) + 1)
                _, pos = decoder.raw_decode(text, pos)
                pos = skip(pos)
                if text[pos] != ",":
                    break
                pos = skip(pos + 1)
            pos += 1
        else:
            _, pos = decoder.raw_decode(text, pos)
        pos = skip(pos)
        if text[pos] != ",":
            break
        pos = skip(pos + 1)
    return rows


class View:
    def __init__(self, connection):
        self.db = connection
        self.repos = {
            r["id"]: dict(r)
            for r in connection.execute("SELECT * FROM repositories ORDER BY role,id")
        }
        self.diagnostics = []
        self.config = {}
        self._nodes = {}
        self._resolved = {}
        self._declared = None
        self._config_file = connection.execute(
            "SELECT repo,path,body,digest FROM files "
            "WHERE repo='root' AND path='.execarchify.json'"
        ).fetchone()
        if self._config_file:
            try:
                config = json.loads(self._config_file["body"])
                if not isinstance(config, dict):
                    raise ValueError("Expected an object")
                self.config = config
            except (ValueError, RecursionError) as error:
                self.diagnostics.append(f".execarchify.json: {error}")
        self.terms = {}
        terms = self.config.get("terms", {})
        if isinstance(terms, dict):
            for key, aliases in terms.items():
                if isinstance(aliases, str):
                    aliases = [aliases]
                if isinstance(aliases, list):
                    self.terms[key] = [
                        a for a in aliases if isinstance(a, str) and a.strip()
                    ]
        layers = self.config.get("layers", [])
        self.layers = (
            [
                g
                for g in layers
                if isinstance(g, dict)
                and isinstance(g.get("name"), str)
                and isinstance(g.get("paths"), list)
            ]
            if isinstance(layers, list)
            else []
        )

    def group(self, repo, path):
        if repo != "root":
            return Path(self.repos[repo]["path"]).name
        for rule in self.layers:
            if any(fnmatchcase(path, p) for p in rule["paths"] if isinstance(p, str)):
                return rule["name"]
        return path.split("/", 1)[0] if "/" in path else "."

    def decorate(self, row, reason=None, line=None):
        item = dict(row)
        if line is not None:
            item["line"] = item["end_line"] = line
        f = self.db.execute(
            "SELECT body,digest FROM files WHERE id=?", (item["file_id"],)
        ).fetchone()
        item["snippet_line"], item["snippet"] = (
            snippets(f["body"], item["line"]) if f else (item["line"], "")
        )
        repo = self.repos[item["repo"]]
        item.update(
            group=self.group(item["repo"], item["path"]),
            revision=repo["revision"],
            binding=repo["binding"],
            digest=f["digest"] if f else None,
        )
        if reason:
            item["reason"] = reason
        return item

    def edge_evidence(self, file, line, padding=0):
        first, text = snippets(file["body"], line, padding)
        repo = self.repos[file["repo"]]
        return {
            "repo": file["repo"],
            "path": file["path"],
            "line": line,
            "snippet": text,
            "snippet_line": first,
            "revision": repo["revision"],
            "binding": repo["binding"],
            "digest": file["digest"],
        }

    def node(self, key):
        if key not in self._nodes:
            row = self.db.execute(SYMBOL + " WHERE s.id=?", (key,)).fetchone()
            if row is None:
                row = self.db.execute(FILE + " WHERE f.id=?", (key,)).fetchone()
            self._nodes[key] = dict(row) if row else None
        return self._nodes[key]

    def endpoint(self, value):
        if isinstance(value, dict):
            path, name = value.get("path"), value.get("symbol")
            repo = value.get("repo", "root")
            matches = [
                r["id"]
                for r in self.repos.values()
                if r["id"] == repo or Path(r["path"]).name == repo
            ]
            if len(matches) != 1 or not isinstance(path, str):
                return None
            rows = (
                self.db.execute(
                    SYMBOL + " WHERE f.repo=? AND f.path=? AND s.qualified=? LIMIT 2",
                    (matches[0], path, name),
                ).fetchall()
                if name
                else self.db.execute(
                    FILE + " WHERE f.repo=? AND f.path=?", (matches[0], path)
                ).fetchall()
            )
        elif isinstance(value, str):
            found = self.node(value)
            if found:
                return found
            if "#" in value:
                path, name = value.rsplit("#", 1)
                return self.endpoint({"path": path, "symbol": name})
            rows = self.db.execute(
                SYMBOL + " WHERE s.qualified=? OR s.name=? LIMIT 2", (value, value)
            ).fetchall()
            if not rows:
                return self.endpoint({"path": value})
        else:
            return None
        return dict(rows[0]) if len(rows) == 1 else None

    def context(self, args):
        if args.get("id"):
            row = self.endpoint(args["id"])
            if row is None:
                raise ValueError("No unique indexed item in this workspace")
            return {"item": self.decorate(row)}
        grouped = {}
        for row in self.db.execute("""SELECT f.repo,f.path,count(s.id) n FROM files f
                                      LEFT JOIN symbols s ON s.file_id=f.id GROUP BY f.id"""):
            key = self.group(row["repo"], row["path"])
            g = grouped.setdefault(key, {"name": key, "files": 0, "symbols": 0})
            g["files"] += 1
            g["symbols"] += row["n"]
        stats = {
            key: self.db.execute("SELECT count(*) FROM " + table).fetchone()[0]
            for key, table in (
                ("files", "files"),
                ("symbols", "symbols"),
                ("references", "refs"),
            )
        }
        omitted = self.db.execute(
            "SELECT value FROM meta WHERE key='skipped'"
        ).fetchone()
        stats["skipped"] = int(omitted[0]) if omitted else 0
        langs = dict(
            self.db.execute(
                "SELECT language,count(*) FROM files GROUP BY language ORDER BY count(*) DESC"
            )
        )
        projection = (
            self.group_edges(scan_limit=2000)
            if args.get("architecture") is True
            else {}
        )
        return {
            **projection,
            "repositories": list(self.repos.values()),
            "groups": sorted(grouped.values(), key=lambda g: g["name"]),
            "stats": stats,
            "languages": langs,
            "terms": list(self.terms),
            "gaps": self.diagnostics.copy(),
        }

    def resolve(self, args):
        query = args.get("query", "")
        if not isinstance(query, str) or len(query) > 2048:
            raise ValueError("query must be text of at most 2048 characters")
        query = query.strip()
        limit = bounded(args.get("limit"), 12, 100)
        group, scope = args.get("group"), args.get("repo")

        def visible(row):
            return (not scope or row["repo"] == scope) and (
                not group or self.group(row["repo"], row["path"]) == group
            )

        if not query:
            output = []
            for sql in (
                SYMBOL + " ORDER BY f.repo,f.path,s.line",
                FILE + " ORDER BY f.repo,f.path",
            ):
                for row in self.db.execute(sql):
                    if visible(row):
                        output.append(self.decorate(row))
                        if len(output) == limit:
                            return {"results": output}
            return {"results": output}

        folded = query.casefold()
        phrases = {folded: "query"}
        for term, aliases in self.terms.items():
            labels = [term, *aliases]
            if any(label.casefold() in folded for label in labels if label):
                for label in labels:
                    phrases.setdefault(label.casefold(), "term " + term)
        for token in words(query):
            if len(token) >= 2:
                phrases.setdefault(token, "token")
        phrases = dict(list(phrases.items())[:20])
        ranked = {}

        def offer(row, score, reason, match_line=None):
            if (
                row["repo"] == "root"
                and row["path"] == ".execarchify.json"
                and query not in (row["id"], row["path"])
            ):
                # A glossary key describes the search. Its declaration should not hide the code it names.
                score -= 50
            if visible(row) and (
                row["id"] not in ranked or ranked[row["id"]][0] < score
            ):
                ranked[row["id"]] = (score, dict(row), reason, match_line)

        exact = self.node(query)
        if exact:
            offer(exact, 150, "exact ID")
        for phrase, why in phrases.items():
            for row in self.db.execute(
                SYMBOL + " WHERE s.name=? COLLATE NOCASE OR s.qualified=? LIMIT 250",
                (phrase, phrase),
            ):
                offer(
                    row,
                    115 if why == "query" else 108 if why.startswith("term ") else 86,
                    why + " · exact symbol",
                )
            for row in self.db.execute(
                SYMBOL
                + " WHERE instr(lower(s.name),?) OR instr(lower(s.qualified),?) LIMIT 180",
                (phrase, phrase),
            ):
                exact_name = row["name"].casefold() == phrase
                score = (
                    108
                    if exact_name
                    else 78 + min(12, 12 * len(phrase) / max(1, len(row["name"])))
                )
                offer(row, score, why + " · symbol")
            for row in self.db.execute(
                FILE + " WHERE instr(lower(f.path),?) LIMIT 80", (phrase,)
            ):
                offer(
                    row,
                    100 if row["path"].casefold() == phrase else 70,
                    why + " · path",
                )
        tokens = list(dict.fromkeys(t for q in phrases for t in words(q) if t))[:16]
        full_text = []
        if tokens:
            expr = " OR ".join(
                '"' + token.replace('"', '""') + '"*' for token in tokens
            )
            full_text = self.db.execute(
                """SELECT file_id FROM files_fts WHERE files_fts MATCH ?
                                           ORDER BY bm25(files_fts) LIMIT 80""",
                (expr,),
            ).fetchall()
        if not full_text or any(re.search(r"[\u3400-\u9fff]", p) for p in phrases):
            for phrase in list(phrases)[:4]:
                full_text.extend(
                    self.db.execute(
                        "SELECT id FROM files WHERE instr(lower(body),?) LIMIT 40",
                        (phrase,),
                    ).fetchall()
                )
        for rank, (key,) in enumerate(full_text):
            row = self.node(key)
            if not row or not visible(row) or key in ranked and ranked[key][0] >= 65:
                continue
            body = self.db.execute(
                "SELECT body FROM files WHERE id=?", (key,)
            ).fetchone()[0]
            anchors = [body.casefold().find(p) for p in phrases]
            offset = min((n for n in anchors if n >= 0), default=0)
            offer(
                row,
                64 - min(rank, 40) / 5,
                "source text",
                body.count("\n", 0, offset) + 1,
            )
        if len(ranked) < limit and 3 <= len(folded) <= 64:
            for row in self.db.execute(
                SYMBOL + " WHERE length(s.name) BETWEEN ? AND ? LIMIT 8000",
                (max(1, len(folded) - 3), len(folded) + 3),
            ):
                if not visible(row):
                    continue
                ratio = SequenceMatcher(None, folded, row["name"].casefold()).ratio()
                if ratio >= 0.72:
                    offer(row, 55 * ratio, "similar spelling")
        ordered = sorted(
            ranked.values(),
            key=lambda x: (-x[0], x[1]["repo"] != "root", x[1]["path"], x[1]["line"]),
        )
        return {
            "results": [
                self.decorate(row, why, line) for _, row, why, line in ordered[:limit]
            ]
        }

    def compatible(self, left, right):
        return left == right or all(
            self.repos[k]["binding"] in ("worktree", "pinned", "commit")
            for k in (left, right)
        )

    def owner(self, ref):
        if ref["source"]:
            rows = self.db.execute(
                SYMBOL
                + " WHERE s.file_id=? AND s.qualified=? ORDER BY abs(s.line-?) LIMIT 1",
                (ref["file_id"], ref["source"], ref["line"]),
            ).fetchone()
            if rows:
                return dict(rows)
        return self.node(ref["file_id"])

    def target(self, ref):
        key = (ref["file_id"], ref["source"], ref["target"], ref["kind"])
        if key in self._resolved:
            return self._resolved[key]
        target, reason = self._target(ref)
        self._resolved[key] = (target, reason)
        return target, reason

    def _target(self, ref):
        if ref["kind"] == "dynamic_call":
            return None, "dynamic call target"
        origin = self.node(ref["file_id"])
        if not origin:
            return None, "source missing"
        if ref["kind"] == "import":
            paths = import_paths(origin["language"], origin["path"], ref["target"])
            if paths:
                marks = ",".join("?" for _ in paths)
                matches = [
                    dict(r)
                    for r in self.db.execute(
                        FILE + f" WHERE f.path IN ({marks})", paths
                    )
                    if self.compatible(origin["repo"], r["repo"])
                ]
                local_files = [r for r in matches if r["repo"] == origin["repo"]]
                matches = local_files or matches
                if len(matches) == 1:
                    return matches[0], "matched reference"
                if len(matches) > 1:
                    return None, "ambiguous module path"
                if ref["target"].startswith(("./", "../")):
                    return None, "relative module not indexed"
        raw = ref["target"].replace("::", ".").replace("->", ".")
        final = raw.rsplit(".", 1)[-1]
        if not final:
            return None, "unresolved target"
        rows = [
            dict(r)
            for r in self.db.execute(SYMBOL + " WHERE s.name=? LIMIT 101", (final,))
        ]
        if len(rows) > 100:
            return None, "many same-name definitions"
        candidates = [r for r in rows if self.compatible(origin["repo"], r["repo"])]
        if not candidates:
            return (
                None,
                "unbound dependency version"
                if rows
                else "definition outside index or dynamic",
            )
        local = [r for r in candidates if r["file_id"] == ref["file_id"]]
        if raw.startswith(("self.", "cls.", "this.")):
            enclosing = ref["source"].rsplit(".", 1)[0]
            matched = [r for r in local if r["qualified"] == enclosing + "." + final]
        elif "." in raw:
            module = raw.rsplit(".", 1)[0]
            matched = [
                r
                for r in candidates
                if r["qualified"] == raw
                or self.module_matches(r["path"], module, origin["path"])
            ]
        else:
            imports = [
                r[0]
                for r in self.db.execute(
                    "SELECT target FROM refs WHERE file_id=? AND kind='import'",
                    (ref["file_id"],),
                )
                if r[0].endswith("." + final)
            ]
            matched = [
                r
                for r in candidates
                if any(
                    self.module_matches(r["path"], i.rsplit(".", 1)[0], origin["path"])
                    for i in imports
                )
            ]
            if not matched:
                enclosing = ref["source"].split(".")
                scopes = [
                    ".".join([*enclosing[:n], final])
                    for n in range(len(enclosing), -1, -1)
                ]
                for qual in scopes:
                    matched = [r for r in local if r["qualified"] == qual]
                    if matched:
                        break
            if not matched:
                same_repo = [r for r in candidates if r["repo"] == origin["repo"]]
                matched = same_repo or candidates
                if len(matched) == 1:
                    return matched[0], "unique-name inference"
        if len(matched) == 1:
            return matched[0], "matched reference"
        return None, "ambiguous target" if len(
            matched
        ) > 1 else "unresolved receiver or module"

    @staticmethod
    def module_matches(path, module, source_path):
        p = str(PurePosixPath(path).with_suffix("")).replace("/", ".")
        if p.endswith(".__init__"):
            p = p[:-9]
        if module.startswith("."):
            dots = len(module) - len(module.lstrip("."))
            parent = list(PurePosixPath(source_path).parent.parts)
            if dots > len(parent) + 1:
                return False
            parts = parent[: len(parent) - dots + 1]
            tail = module.lstrip(".")
            if tail:
                parts.append(tail)
            return p == ".".join(parts)
        return bool(module) and (p == module or p.endswith("." + module))

    def declared(self):
        if self._declared is not None:
            return self._declared
        edges = []
        specs = self.config.get("relations", [])
        if not isinstance(specs, list):
            self.diagnostics.append("relations must be a list in .execarchify.json")
            specs = []
        positions = relation_lines(self._config_file["body"]) if specs else []
        for index, spec in enumerate(specs):
            if not isinstance(spec, dict):
                self.diagnostics.append(f"relations[{index}] is not an object")
                continue
            left, right = self.endpoint(spec.get("from")), self.endpoint(spec.get("to"))
            if (
                not left
                or not right
                or not self.compatible(left["repo"], right["repo"])
            ):
                self.diagnostics.append(
                    f"relations[{index}] has an unresolved endpoint or dependency revision"
                )
                continue
            kind = spec.get("kind", "relates")
            if not isinstance(kind, str) or not kind.strip():
                self.diagnostics.append(f"relations[{index}] has an invalid kind")
                continue
            flow = spec.get("impact", "reverse")
            if not isinstance(flow, str) or flow not in {
                "forward",
                "reverse",
                "both",
                "none",
            }:
                self.diagnostics.append(
                    f"relations[{index}] has an invalid impact direction"
                )
                continue
            edges.append(
                {
                    "source": left["id"],
                    "target": right["id"],
                    "kind": kind,
                    "impact_direction": flow,
                    "confidence": "declared",
                    "pointer": f"/relations/{index}",
                    **self.edge_evidence(
                        self._config_file, positions[index], padding=2
                    ),
                }
            )
        self._declared = edges
        return edges

    def group_edges(self, scan_limit=None):
        """Collapse evidenced cross-group occurrences without turning containment into data flow."""
        grouped = {}

        def add(left, right, kind, confidence, make_sample):
            a = self.group(left["repo"], left["path"])
            b = self.group(right["repo"], right["path"])
            if a == b:
                return
            key = a, b, kind, confidence
            if key not in grouped:
                grouped[key] = {
                    "source_group": a,
                    "target_group": b,
                    "kind": kind,
                    "confidence": confidence,
                    "count": 0,
                    "sample": make_sample(),
                }
            grouped[key]["count"] += 1

        for edge in self.declared():
            add(
                self.node(edge["source"]),
                self.node(edge["target"]),
                edge["kind"],
                "declared",
                lambda edge=edge: dict(edge),
            )
        statement = "SELECT * FROM refs WHERE kind!='dynamic_call' ORDER BY rowid"
        refs = (
            self.db.execute(statement + " LIMIT ?", (scan_limit + 1,))
            if scan_limit is not None
            else self.db.execute(statement)
        )
        seen = 0
        truncated = False
        for ref in refs:
            if scan_limit is not None and seen >= scan_limit:
                truncated = True
                break
            seen += 1
            right, why = self.target(ref)
            if (
                not right
                or why != "matched reference"
                or right["file_id"] == ref["file_id"]
            ):
                continue
            left = self.node(ref["file_id"])

            def sample(ref=ref, right=right):
                owner = self.owner(ref)
                file = self.db.execute(
                    "SELECT repo,path,body,digest FROM files WHERE id=?",
                    (ref["file_id"],),
                ).fetchone()
                return {
                    "source": owner["id"],
                    "target": right["id"],
                    "kind": ref["kind"],
                    "confidence": ref["confidence"],
                    **self.edge_evidence(file, ref["line"]),
                }

            add(left, right, ref["kind"], ref["confidence"], sample)
        return {
            "group_edges": sorted(
                grouped.values(),
                key=lambda x: (-x["count"], x["source_group"], x["target_group"]),
            ),
            "group_edges_truncated": truncated,
            "reference_scan_count": seen,
        }

    def neighbors(self, node, direction, gaps):
        for edge in self.declared():
            if (direction != "in" and edge["source"] == node["id"]) or (
                direction != "out" and edge["target"] == node["id"]
            ):
                yield edge
        rows = []
        if direction != "in":
            rows.extend(
                (r, False)
                for r in self.db.execute(
                    "SELECT * FROM refs WHERE file_id=? AND source=? ORDER BY line LIMIT 401",
                    (
                        node["file_id"],
                        "" if node["kind"] == "file" else node["qualified"],
                    ),
                )
            )
        if direction != "out" and node["kind"] != "file":
            name = node["name"]
            rows.extend(
                (r, True)
                for r in self.db.execute(
                    """SELECT * FROM refs WHERE target=? OR substr(target,-length(?)-1)='.' || ?
                   OR substr(target,-length(?)-2) IN ('::' || ?, '->' || ?) ORDER BY file_id,line LIMIT 401""",
                    (name, name, name, name, name, name),
                )
            )
        elif direction != "out":
            path = PurePosixPath(node["path"])
            stem = path.parent.name if path.stem in ("__init__", "index") else path.stem
            if stem:
                rows.extend(
                    (r, True)
                    for r in self.db.execute(
                        "SELECT * FROM refs WHERE kind='import' AND instr(target,?)>0 ORDER BY file_id,line LIMIT 401",
                        (stem,),
                    )
                )
        counts = Counter()
        for ref, reverse in rows:
            counts[reverse] += 1
            if counts[reverse] > 400:
                gaps.add("Reference budget reached at " + node["path"])
                continue
            target, reason = self.target(ref)
            if not target:
                if not reverse or reason == "ambiguous target":
                    gaps.add(f"{ref['target']} · {reason}")
                continue
            if reverse and target["id"] != node["id"]:
                continue
            owner = self.owner(ref)
            if owner is None:
                continue
            confidence = (
                "inferred" if reason == "unique-name inference" else ref["confidence"]
            )
            file = self.db.execute(
                "SELECT repo,path,body,digest FROM files WHERE id=?", (ref["file_id"],)
            ).fetchone()
            yield {
                "source": owner["id"],
                "target": target["id"],
                "kind": ref["kind"],
                "confidence": confidence,
                **self.edge_evidence(file, ref["line"]),
            }

    def trace(self, args):
        node = self.endpoint(args.get("id"))
        if not node:
            raise ValueError(
                "Select one indexed symbol/file ID or an unambiguous path#symbol"
            )
        depth = bounded(args.get("depth"), 1, 5)
        limit = bounded(args.get("limit"), 35, 150)
        direction = {
            "upstream": "in",
            "callers": "in",
            "incoming": "in",
            "downstream": "out",
            "callees": "out",
            "outgoing": "out",
        }.get(args.get("direction"), args.get("direction", "both"))
        if direction not in ("in", "out", "both"):
            raise ValueError("direction must be in, out or both")
        seen = {node["id"]: node}
        edges = {}
        queue = deque([(node, 0)])
        gaps = set()
        truncated = False
        while queue:
            current, n = queue.popleft()
            if n >= depth:
                continue
            for edge in self.neighbors(current, direction, gaps):
                other = (
                    edge["target"]
                    if edge["source"] == current["id"]
                    else edge["source"]
                )
                if other not in seen:
                    if len(seen) >= limit:
                        truncated = True
                        continue
                    item = self.node(other)
                    if not item:
                        continue
                    seen[other] = item
                    queue.append((item, n + 1))
                key = (
                    edge["source"],
                    edge["target"],
                    edge["kind"],
                    edge["repo"],
                    edge["path"],
                    edge["line"],
                )
                if len(edges) >= limit * 4 and key not in edges:
                    truncated = True
                    continue
                edges[key] = edge
        return {
            "nodes": [self.decorate(n) for n in seen.values()],
            "edges": list(edges.values()),
            "gaps": sorted(gaps.union(self.diagnostics))[:30],
            "truncated": truncated or any("budget" in s for s in gaps),
        }
