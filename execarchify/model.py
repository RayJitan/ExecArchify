"""Shared, language-neutral contract for file extractors and the local index."""

from dataclasses import dataclass, field
from pathlib import Path
import sqlite3


@dataclass(frozen=True)
class Definition:
    name: str
    qualified: str
    kind: str
    line: int
    end_line: int
    confidence: str = "parsed"


@dataclass(frozen=True)
class Reference:
    source: str  # Qualified enclosing symbol, or empty for a file-level reference.
    target: str  # Spelling at the reference site, not a guessed definition.
    kind: str
    line: int
    confidence: str = "parsed"


@dataclass
class Facts:
    language: str
    definitions: list[Definition] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)


@dataclass(frozen=True)
class Workspace:
    root: Path
    gitdir: Path
    database: Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS repositories(
  id TEXT PRIMARY KEY, path TEXT NOT NULL, role TEXT NOT NULL,
  revision TEXT NOT NULL, binding TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files(
  id TEXT PRIMARY KEY, repo TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
  path TEXT NOT NULL, digest TEXT NOT NULL, language TEXT NOT NULL, body TEXT NOT NULL,
  UNIQUE(repo,path)
);
CREATE TABLE IF NOT EXISTS symbols(
  id TEXT PRIMARY KEY, file_id TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  name TEXT NOT NULL, qualified TEXT NOT NULL, kind TEXT NOT NULL,
  line INTEGER NOT NULL, end_line INTEGER NOT NULL, confidence TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS symbols_file ON symbols(file_id);
CREATE INDEX IF NOT EXISTS symbols_name ON symbols(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS symbols_qualified ON symbols(qualified);
CREATE TABLE IF NOT EXISTS refs(
  file_id TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE, source TEXT NOT NULL,
  target TEXT NOT NULL, kind TEXT NOT NULL, line INTEGER NOT NULL, confidence TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS refs_source ON refs(file_id,source);
CREATE INDEX IF NOT EXISTS refs_target ON refs(target);
CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(file_id UNINDEXED,path,body,tokenize='unicode61');
CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files BEGIN
  INSERT INTO files_fts(rowid,file_id,path,body) VALUES(new.rowid,new.id,new.path,new.body);
END;
CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON files BEGIN
  DELETE FROM files_fts WHERE rowid=old.rowid;
END;
CREATE TRIGGER IF NOT EXISTS files_au AFTER UPDATE OF path,body ON files BEGIN
  DELETE FROM files_fts WHERE rowid=old.rowid;
  INSERT INTO files_fts(rowid,file_id,path,body) VALUES(new.rowid,new.id,new.path,new.body);
END;
"""


def connect(workspace: Workspace) -> sqlite3.Connection:
    connection = sqlite3.connect(workspace.database, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection
