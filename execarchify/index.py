"""Transactional, worktree-local source indexes and explicitly selected Git snapshots.

Regular files up to 1 MiB with UTF-8 or BOM-marked UTF-16/32 text are eligible. Binary
control bytes, non-UTF-8 paths, symlinks, nested repositories and .git
are outside the source corpus. Tracked files keep their membership
when ignored. Untracked files follow Git's normal exclusion rules.
"""

from __future__ import annotations

import codecs
import hashlib
import json
import os
import re
import sqlite3
import stat
import time
from collections import Counter
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .extract import extract
from .gitio import (
    GitError,
    SourceDirectories,
    blob_reader,
    change_time,
    commit_at,
    discover,
    exact_git_root,
    is_link,
    tree_entries,
    worktree_entries,
)
from .model import SCHEMA, Workspace, connect

__all__ = [
    "FACTS_VERSION",
    "MAX_FILE_BYTES",
    "configure",
    "decode_source",
    "discover",
    "file_id",
    "load_pins",
    "put_text",
    "source_binding",
    "synchronize",
]

MAX_FILE_BYTES = 1024 * 1024
# Bump when text decoding, symbol identities, or extraction semantics change.
# Schema migrations use PRAGMA user_version independently.
FACTS_VERSION = "facts-v2"
_CONTROL_TEXT = re.compile(r"[\x00-\x08\x0e-\x1f\x7f-\x9f]")
_PIN = re.compile(r"[a-fA-F0-9]{4,64}\Z")
_DELTA_FILES = 256
_DELTA_FACTS_PER_KIND = 128
_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS file_cache(
  repo TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
  path TEXT NOT NULL, stamp TEXT NOT NULL, skipped TEXT NOT NULL,
  PRIMARY KEY(repo,path)
);
PRAGMA user_version=1;
"""


def decode_source(raw: bytes) -> str | None:
    if len(raw) > MAX_FILE_BYTES:
        return None
    if raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        encoding = "utf-32"
    elif raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = "utf-16"
    else:
        encoding = "utf-8-sig"
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        return None
    return None if _CONTROL_TEXT.search(text) else text


def _meta(db, key, default=""):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def _set_meta(db, key, value):
    db.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET "
        "value=excluded.value WHERE value<>excluded.value",
        (key, value),
    )


@contextmanager
def _writer(workspace):
    workspace.database.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(workspace)) as db:
        db.execute("PRAGMA busy_timeout=120000")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            # Concurrent creators may race while switching the initial journal.
            deadline = time.monotonic() + 120
            while True:
                try:
                    db.execute("PRAGMA journal_mode=WAL").fetchone()
                    break
                except sqlite3.OperationalError as error:
                    if (
                        getattr(error, "sqlite_errorcode", 0)
                        not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                        or time.monotonic() >= deadline
                    ):
                        raise
                    time.sleep(0.05)
            db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + _CACHE_SCHEMA)
        elif version == 1:
            db.execute("BEGIN IMMEDIATE")
        else:
            raise ValueError(f"Unsupported ExecArchify index format {version}")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise


def _path_key(path):
    return os.path.normcase(os.path.abspath(path))


def configure(ws: Workspace, shared_repos: list[str] | None = None) -> None:
    """Set the desired sources for the next atomic sync. None keeps the prior selection."""
    with _writer(ws) as db:
        if shared_repos is None:
            return
        selected = {}
        aliases = {}
        for item in shared_repos:
            if (
                not isinstance(item, (str, Path))
                or not str(item).strip()
                or "\0" in str(item)
            ):
                raise ValueError("Expected a local shared repository path")
            requested = ws.root / Path(item).expanduser()
            path = requested.resolve()
            if path != ws.root:
                selected[_path_key(path)] = str(path)
                if _path_key(requested) != _path_key(path):
                    aliases[_path_key(requested)] = _path_key(path)
        _set_meta(
            db, "shared_repos", json.dumps([selected[k] for k in sorted(selected)])
        )
        _set_meta(db, "configured_aliases", json.dumps(aliases, sort_keys=True))
        _set_meta(db, "configuration_root", _path_key(ws.root))


def _identifier(prefix, *parts):
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:32]
    return prefix + "_" + digest


def file_id(conn, repo_id: str, path: str) -> str:
    root = conn.execute("SELECT path FROM repositories WHERE id='root'").fetchone()[0]
    return _identifier("f", _path_key(root), repo_id, path)


def put_text(conn, repo_id: str, path: str, text: str, digest: str) -> None:
    """Replace one file's facts in the caller's transaction (also used for Git history)."""
    facts = extract(path, text)
    fid = file_id(conn, repo_id, path)
    conn.execute("DELETE FROM files WHERE repo=? AND path=?", (repo_id, path))
    conn.execute(
        "INSERT INTO files(id,repo,path,digest,language,body) VALUES(?,?,?,?,?,?)",
        (fid, repo_id, path, digest, facts.language, text),
    )
    occurrences = Counter()
    definitions = []
    for item in facts.definitions:
        identity = (item.qualified, item.kind)
        ordinal = occurrences[identity]
        occurrences[identity] += 1
        sid = _identifier("s", fid, *identity, str(ordinal))
        definitions.append(
            (
                sid,
                fid,
                item.name,
                item.qualified,
                item.kind,
                item.line,
                item.end_line,
                item.confidence,
            )
        )
    conn.executemany(
        "INSERT INTO symbols(id,file_id,name,qualified,kind,line,end_line,confidence) "
        "VALUES(?,?,?,?,?,?,?,?)",
        definitions,
    )
    conn.executemany(
        "INSERT INTO refs(file_id,source,target,kind,line,confidence) VALUES(?,?,?,?,?,?)",
        (
            (fid, r.source, r.target, r.kind, r.line, r.confidence)
            for r in facts.references
        ),
    )


@dataclass
class _Report:
    added: int = 0
    updated: int = 0
    deleted: int = 0
    read_files: int = 0
    rebuilt: int = 0
    delta: list = field(default_factory=list)
    truncated: bool = False

    def remember(self, db, repo, path, status, old_id=None):
        if len(self.delta) >= _DELTA_FILES:
            self.truncated = True
            return
        record = {
            "repo": repo,
            "path": path,
            "status": status,
            "old_symbols": [],
            "old_refs": [],
        }
        if old_id:
            for key, table, fields in (
                (
                    "old_symbols",
                    "symbols",
                    "id,name,qualified,kind,line,end_line,confidence",
                ),
                ("old_refs", "refs", "source,target,kind,line,confidence"),
            ):
                rows = db.execute(
                    f"SELECT {fields} FROM {table} WHERE file_id=? ORDER BY line,rowid "
                    "LIMIT ?",
                    (old_id, _DELTA_FACTS_PER_KIND + 1),
                ).fetchall()
                if len(rows) > _DELTA_FACTS_PER_KIND:
                    self.truncated = record["truncated"] = True
                record[key] = [dict(row) for row in rows[:_DELTA_FACTS_PER_KIND]]
        self.delta.append(record)

    def merge(self, other):
        self.added += other.added
        self.updated += other.updated
        self.deleted += other.deleted
        self.read_files += other.read_files
        self.rebuilt += other.rebuilt
        room = _DELTA_FILES - len(self.delta)
        self.truncated |= other.truncated or len(other.delta) > room
        self.delta.extend(other.delta[:room])


class _Files:
    """Reconcile one source by path without loading unchanged source text or facts."""

    def __init__(self, db, repo, rebuild):
        self.db = db
        self.repo = repo
        self.rebuild = rebuild
        self.files = {
            row["path"]: (row["id"], row["digest"])
            for row in db.execute(
                "SELECT path,id,digest FROM files WHERE repo=?", (repo,)
            )
        }
        self.cache = {
            row["path"]: (row["stamp"], row["skipped"])
            for row in db.execute(
                "SELECT path,stamp,skipped FROM file_cache WHERE repo=?", (repo,)
            )
        }
        self.seen = set()
        self.report = _Report()

    def unchanged(self, path, stamp):
        prior = self.cache.get(path)
        if (
            not self.rebuild
            and prior
            and prior[0] == stamp
            and (prior[1] or path in self.files)
        ):
            self.seen.add(path)
            return True
        return False

    def mark(self, path, stamp, skipped=""):
        self.seen.add(path)
        if self.cache.get(path) != (stamp, skipped):
            self.db.execute(
                "INSERT INTO file_cache(repo,path,stamp,skipped) VALUES(?,?,?,?) "
                "ON CONFLICT(repo,path) DO UPDATE SET stamp=excluded.stamp,"
                "skipped=excluded.skipped",
                (self.repo, path, stamp, skipped),
            )

    def remove(self, path, remember):
        if path in self.files:
            fid, _ = self.files.pop(path)
            if remember:
                self.report.remember(self.db, self.repo, path, "deleted", fid)
            self.db.execute("DELETE FROM files WHERE id=?", (fid,))
            self.report.deleted += 1

    def skip(self, path, stamp, reason):
        self.mark(path, stamp, reason)
        self.remove(path, remember=False)

    def apply(self, path, stamp, raw):
        self.report.read_files += 1
        prior = self.files.get(path)
        digest = hashlib.sha256(raw).hexdigest()
        same_bytes = prior and prior[1] == digest
        if same_bytes and not self.rebuild:
            self.mark(path, stamp)
            return
        body = decode_source(raw)
        if body is None:
            self.skip(path, stamp, "binary or unsupported encoding")
            return
        if same_bytes:
            self.report.rebuilt += 1
        elif prior:
            self.report.remember(self.db, self.repo, path, "updated", prior[0])
            self.report.updated += 1
        else:
            self.report.remember(self.db, self.repo, path, "added")
            self.report.added += 1
        put_text(self.db, self.repo, path, body, digest)
        self.mark(path, stamp)

    def finish(self, remember_removed):
        for path in sorted(self.files.keys() - self.seen):
            self.remove(path, remember_removed(path))
        self.db.executemany(
            "DELETE FROM file_cache WHERE repo=? AND path=?",
            ((self.repo, path) for path in self.cache.keys() - self.seen),
        )
        return self.report


class _ChangedDuringScan(RuntimeError):
    pass


def _stat_tuple(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_worktree_file(path, before):
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    if path.resolve() != path:
        raise _ChangedDuringScan("Source path changed during indexing")
    with os.fdopen(os.open(path, flags), "rb") as stream:
        current = os.fstat(stream.fileno())
        if not stat.S_ISREG(current.st_mode) or _stat_tuple(current) != _stat_tuple(
            before
        ):
            raise _ChangedDuringScan("Source changed before it could be read")
        raw = stream.read(MAX_FILE_BYTES + 1)
        if _stat_tuple(os.fstat(stream.fileno())) != _stat_tuple(before):
            raise _ChangedDuringScan("Source changed while it was being read")
    return raw


def _sync_worktree(db, ws, entries, rebuild):
    files = _Files(db, "root", rebuild)
    dirs = SourceDirectories(ws.root)
    for name, index_oid in sorted(entries.items()):
        path = ws.root / name
        if not dirs.contains(path.parent):
            continue
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or is_link(info):
                continue
            changed_at = change_time(path, info)
            stamp = ":".join(map(str, (*_stat_tuple(info), changed_at, index_oid)))
            if changed_at is not None and files.unchanged(name, stamp):
                continue
            if info.st_size > MAX_FILE_BYTES:
                files.skip(name, stamp, "over 1 MiB")
            else:
                files.apply(name, stamp, _read_worktree_file(path, info))
        except (FileNotFoundError, NotADirectoryError):
            continue
        except PermissionError:
            # Permissions can be restored without changing file timestamps.
            files.skip(name, "", "unreadable")

    # A newly ignored, symlinked or nested source must also leave the delta.
    def actually_deleted(name):
        path = ws.root / name
        ancestor = path.parent
        while ancestor != ws.root and not os.path.lexists(ancestor):
            ancestor = ancestor.parent
        return dirs.contains(ancestor) and not os.path.lexists(path)

    return files.finish(actually_deleted)


def load_pins(db) -> tuple[dict, str]:
    row = db.execute(
        "SELECT body FROM files WHERE repo='root' AND path='.execarchify.json'"
    ).fetchone()
    if not row:
        return {}, ""
    try:
        config = json.loads(row[0])
    except (ValueError, RecursionError):
        return {}, "Could not parse .execarchify.json"
    if not isinstance(config, dict) or not isinstance(config.get("pins", {}), dict):
        return {}, "Expected a pins object in .execarchify.json"
    pins = config.get("pins", {})
    aliases = json.loads(_meta(db, "shared_aliases", "{}"))
    if not aliases:
        return pins, ""
    # Bind aliases exactly as explicitly selected, without resolving arbitrary paths
    # named by a repository-controlled manifest.
    root = Path(
        db.execute("SELECT path FROM repositories WHERE id='root'").fetchone()[0]
    )
    normalized = {}
    for key, value in pins.items():
        if "\0" in key:
            continue
        name = _path_key(root / key)
        name = aliases.get(name, name)
        if name in normalized:
            old = normalized[name]
            if (
                isinstance(old, str)
                and isinstance(value, str)
                and _PIN.fullmatch(old)
                and _PIN.fullmatch(value)
                and (
                    old.lower().startswith(value.lower())
                    or value.lower().startswith(old.lower())
                )
            ):
                value = max(old, value, key=len)
            else:
                value = None
        normalized[name] = value
    return normalized, ""


def source_binding(
    root: Path, path: Path, links: dict, pins: dict, manifest_error: str = ""
) -> tuple[str, str, str]:
    """Return binding, actual commit (or missing requested revision), and coverage gap."""
    if manifest_error:
        return "missing", "", manifest_error
    subpath = path.relative_to(root).as_posix() if path.is_relative_to(root) else None
    if subpath and any(p.casefold() == ".git" for p in subpath.split("/")):
        return "missing", "", "Selected source is inside an excluded directory"
    gitlinks = {
        value
        for rel, value in links.items()
        if subpath and os.path.normcase(rel) == os.path.normcase(subpath)
    }
    if "" in gitlinks or len(gitlinks) > 1:
        return "missing", "", "Submodule gitlink has conflicting index stages"
    gitlink = next(iter(gitlinks), "")
    values = [
        value
        for key, value in pins.items()
        if isinstance(key, str)
        and "\0" not in key
        and _path_key(root / key) == _path_key(path)
    ]
    if any(not isinstance(value, str) or not _PIN.fullmatch(value) for value in values):
        return "missing", "", "Expected a commit object ID in .execarchify.json"
    expected = gitlink or (max(values, key=len).lower() if values else "")
    if any(not expected.startswith(value.lower()) for value in values):
        return "missing", expected, "Conflicting dependency revisions"
    try:
        if not exact_git_root(path):
            return "missing", expected, "Selected local Git repository is unavailable"
        revision = commit_at(path, expected or "HEAD")
    except (GitError, OSError) as error:
        return "missing", expected, str(error)
    if not revision or (expected and not revision.startswith(expected)):
        return (
            "missing",
            expected,
            "Requested commit is unavailable in the selected repository",
        )
    if expected:
        return "pinned", revision, ""
    return (
        "reference",
        revision,
        "No dependency revision is pinned by the root repository",
    )


def _sync_objects(db, repo_id, path, revision, rebuild):
    files = _Files(db, repo_id, rebuild)
    with blob_reader(path) as read:
        for name, oid, size in tree_entries(path, revision):
            if files.unchanged(name, oid):
                continue
            if size > MAX_FILE_BYTES:
                files.skip(name, oid, "over 1 MiB")
            else:
                files.apply(name, oid, read(oid, size))
    return files.finish(lambda name: True)


def _repository(db, repo):
    db.execute(
        "INSERT INTO repositories(id,path,role,revision,binding) VALUES(?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET path=excluded.path,role=excluded.role,"
        "revision=excluded.revision,binding=excluded.binding",
        repo,
    )


def _clear_source(db, repo_id):
    count = db.execute("DELETE FROM files WHERE repo=?", (repo_id,)).rowcount
    db.execute("DELETE FROM file_cache WHERE repo=?", (repo_id,))
    return count


def _index_stamp(ws):
    try:
        return _stat_tuple((ws.gitdir / "index").stat())
    except FileNotFoundError:
        return None


def _synchronize_once(ws):
    with _writer(ws) as db:
        previous = {
            r["id"]: tuple(r)
            for r in db.execute(
                "SELECT id,path,role,revision,binding FROM repositories"
            )
        }
        stored_generation = _meta(db, "generation", None)
        generation = int(stored_generation or 0)
        rebuild = _meta(db, "facts_version") != FACTS_VERSION
        root_revision = commit_at(ws.root)
        index_stamp = _index_stamp(ws)
        entries, links = worktree_entries(ws.root)
        root = ("root", str(ws.root), "root", root_revision, "worktree")
        selection = json.loads(_meta(db, "shared_repos", "[]"))
        published_aliases = _meta(db, "shared_aliases", "{}")
        aliases = _meta(db, "configured_aliases", published_aliases)
        if _meta(db, "configuration_root") != _path_key(ws.root):
            selection = []
            aliases = "{}"
        if aliases != published_aliases:
            _set_meta(db, "shared_aliases", aliases)
        report = _Report()
        if "root" in previous and _path_key(previous["root"][1]) != _path_key(ws.root):
            # A moved/copied worktree must not inherit file IDs from the old location.
            report.deleted = db.execute("SELECT count(*) FROM files").fetchone()[0]
            db.execute("DELETE FROM repositories")
            previous = {}
        current = {"root": root}
        if previous.get("root") != root:
            _repository(db, root)
        report.merge(_sync_worktree(db, ws, entries, rebuild))
        gaps = []
        if selection:
            pins, manifest_error = load_pins(db)
            if (
                os.path.lexists(ws.root / ".execarchify.json")
                and not db.execute(
                    "SELECT 1 FROM files WHERE repo='root' AND path='.execarchify.json'"
                ).fetchone()
            ):
                manifest_error = "Pin manifest must be an indexed, readable text file"
        for selected in selection:
            path = Path(selected)
            rid = _identifier("shared", _path_key(path))
            revision = ""
            try:
                binding, revision, reason = source_binding(
                    ws.root, path, links, pins, manifest_error
                )
                source = (rid, selected, "shared", revision, binding)
                if binding != "missing" and (rebuild or previous.get(rid) != source):
                    db.execute("SAVEPOINT shared_source")
                    try:
                        _repository(db, source)
                        changes = _sync_objects(db, rid, path, revision, rebuild)
                    except BaseException:
                        db.execute("ROLLBACK TO shared_source")
                        raise
                    finally:
                        db.execute("RELEASE shared_source")
                    if binding == "reference":
                        changes.delta.clear()
                        changes.truncated = False
                    report.merge(changes)
            except (GitError, OSError) as error:
                binding, reason = "missing", str(error)
                source = (rid, selected, "shared", revision, binding)
            if binding == "missing" and (rebuild or previous.get(rid) != source):
                _repository(db, source)
                report.deleted += _clear_source(db, rid)
            if reason:
                gaps.append(f"{selected}: {reason}")
            current[rid] = source
        for rid in sorted(previous.keys() - current.keys()):
            report.deleted += _clear_source(db, rid)
            db.execute("DELETE FROM repositories WHERE id=?", (rid,))
        if index_stamp != _index_stamp(ws) or root_revision != commit_at(ws.root):
            raise _ChangedDuringScan(
                "Root Git revision or index changed while indexing"
            )
        skipped = db.execute(
            "SELECT count(*) FROM file_cache WHERE skipped<>''"
        ).fetchone()[0]
        gap_json = json.dumps(gaps, ensure_ascii=False)
        if (
            rebuild
            or stored_generation is None
            or previous != current
            or aliases != published_aliases
            or report.added
            or report.updated
            or report.deleted
            or gap_json != _meta(db, "gaps", "[]")
            or skipped != int(_meta(db, "skipped", "0"))
        ):
            generation += 1
            _set_meta(db, "generation", str(generation))
            _set_meta(db, "facts_version", FACTS_VERSION)
            _set_meta(db, "gaps", gap_json)
            _set_meta(db, "skipped", str(skipped))
            # A previous extractor's definitions are outside the new facts format.
            compatible_delta = not (rebuild and previous)
            _set_meta(
                db,
                "last_delta",
                json.dumps(
                    report.delta if compatible_delta else [], ensure_ascii=False
                ),
            )
            _set_meta(
                db,
                "delta_truncated",
                "1" if compatible_delta and report.truncated else "0",
            )
        total = db.execute("SELECT count(*) FROM files").fetchone()[0]
        return {
            "added": report.added,
            "updated": report.updated,
            "deleted": report.deleted,
            "read_files": report.read_files,
            "rebuilt": report.rebuilt,
            "files": total,
            "skipped": skipped,
            "generation": generation,
            "gaps": gaps,
        }


def synchronize(ws: Workspace) -> dict:
    """Publish facts, scope, commits, diagnostics, and generation in one transaction."""
    for attempt in range(3):
        try:
            return _synchronize_once(ws)
        except _ChangedDuringScan:
            if attempt == 2:
                raise
