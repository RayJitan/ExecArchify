import os
import struct
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path, PureWindowsPath

from execarchify.index import discover
from execarchify.model import Workspace
from execarchify.notify import _needs_scan, native_events


def record(name, final=True):
    encoded = name.encode("utf-16-le")
    padded = (12 + len(encoded) + 3) & ~3
    return (
        struct.pack("<III", 0 if final else padded, 3, len(encoded))
        + encoded
        + bytes(padded - 12 - len(encoded))
    )


class NotificationTests(unittest.TestCase):
    def test_overflow_malformed_unicode_chained_records_and_git_metadata(self):
        root = PureWindowsPath("C:/source")
        common = PureWindowsPath("C:/other/.git")
        gitdir = common / "worktrees" / "source"
        ws = Workspace(root, gitdir, gitdir / "execarchify" / "index.sqlite3")
        for raw in (b"", b"broken", struct.pack("<III", 4, 3, 200)):
            self.assertTrue(_needs_scan(raw, root, ws, common))
        self.assertTrue(_needs_scan(record("src/信号.py"), root, ws, common))
        self.assertTrue(_needs_scan(record("vendor/.git"), root, ws, common))
        self.assertFalse(_needs_scan(record("vendor/.git/logs/HEAD"), root, ws, common))
        self.assertFalse(
            _needs_scan(record("execarchify/index.sqlite3-wal"), gitdir, ws, common)
        )
        self.assertFalse(_needs_scan(record("objects/ab/cd"), common, ws, common))
        self.assertTrue(
            _needs_scan(
                record("execarchify/observer.json", final=False) + record("index"),
                gitdir,
                ws,
                common,
            )
        )
        self.assertTrue(_needs_scan(record("refs/heads/main"), common, ws, common))
        self.assertFalse(
            _needs_scan(record("worktrees/unselected/HEAD"), common, ws, common)
        )

    @unittest.skipUnless(os.name == "nt", "Windows notifications")
    def test_live_source_and_external_gitdir_and_idle_stop(self):
        with tempfile.TemporaryDirectory() as temp:
            project, linked = Path(temp) / "main", Path(temp) / "linked"
            subprocess.run(["git", "init", "-q", str(project)], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(project),
                    "-c",
                    "commit.gpgsign=false",
                    "-c",
                    "user.name=Notify",
                    "-c",
                    "user.email=notify@example.invalid",
                    "commit",
                    "-q",
                    "--allow-empty",
                    "-m",
                    "initial",
                ],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(project),
                    "worktree",
                    "add",
                    "-q",
                    "--detach",
                    str(linked),
                ],
                check=True,
            )
            ws = discover(linked)
            ws.database.parent.mkdir(parents=True)
            wake = threading.Event()
            observer = native_events(ws, wake)
            self.assertIsNotNone(observer)
            try:
                (ws.database.parent / "observer.json").write_text(
                    "{}", encoding="utf-8"
                )
                self.assertFalse(wake.wait(0.2))
                (ws.gitdir / "config.worktree").write_text("[core]\n", encoding="utf-8")
                self.assertTrue(wake.wait(2), "external worktree metadata change")
                # Ensure the next assertion observes the intended write.
                observer.stop()
                observer.join(2)
                self.assertTrue(all(not r.is_alive() for r in observer.readers))
                wake.clear()
                observer = native_events(ws, wake)
                self.assertIsNotNone(observer)
                (linked / "source.py").write_text("result=3\n", encoding="utf-8")
                self.assertTrue(wake.wait(2), "working source change")
            finally:
                if observer:
                    observer.stop()
                    observer.stop()
                    observer.join(2)
            self.assertTrue(all(not r.is_alive() for r in observer.readers))
